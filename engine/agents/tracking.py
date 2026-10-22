"""Tracking health: are the events your flows depend on still arriving, complete, and on the plan?

Flows break quietly when tracking changes: an event is renamed in a release, a property goes null, a new event ships
outside the plan. Nothing errors; the trigger just stops firing. This check compares the 7 days to yesterday (UTC) with
the 4 weeks before, for every event in the tracking plan
(config/tracking_plan.json):

  stopped           none for 7 days or more, and a silence that long would happen by chance less than 1 time in 100
                    at its usual rate (the 28 days up to the last time it arrived)
  dropped           at least half below its usual weekly volume (only judged when the usual volume is 20 or more a week)
  too few to judge  none in the last 7 days, but it arrives so rarely that a silence that long is normal
  not tracked       in the plan, never seen in the data (fine if you do not use it; a problem if a flow does)
  ok                arriving at its usual volume

The window ends yesterday, not on the newest event, so a feed that has stopped altogether reads as stopped (and as
stale, when the newest event is more than 2 days old) instead of quietly showing the last good week. The demo data is
read to its own last day. order_completed is counted from the orders table, where the engine reads it everywhere else.

It also reports required properties that are missing on more than 5 percent of recent events, events arriving that are
not in the plan, the flows in briefs/active/ whose exact trigger or core action (with its property filters) has
stopped or dropped, and, when the webhook receiver runs, how many events it quarantined and why.

    python3 -m engine.agents.tracking --data exports/
    python3 -m engine.agents.tracking --data exports/ --events-dir out/events
    lcm "are our events firing correctly?" --data exports/
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PLAN_PATH = ROOT / "config" / "tracking_plan.json"
MIN_WEEKLY = 20        # a drop is judged only above this usual week
SILENCE_EXPECTED = 4.6  # a silence is "stopped" when the usual rate would have produced 4.6 or more events in it (P(0) < 1%)
MISSING_PROPERTY_SHARE = 0.05
STALE_AFTER_DAYS = 2


def plan() -> Dict[str, List[str]]:
    """Event name to its required properties, from the tracking plan."""
    doc = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    return {e: list((doc["$defs"].get(e) or {}).get("required", [])) for e in doc["properties"]["event"]["enum"]}


def quarantine(events_dir: Path, days: int = 7, today: Optional[date] = None) -> Dict[str, Any]:
    """What the webhook receiver held back in the last `days` days, by reason."""
    today = today or datetime.now(timezone.utc).date()
    reasons: Counter = Counter()
    total = 0
    for k in range(days):
        path = Path(events_dir) / "quarantine" / f"{(today - timedelta(days=k)).isoformat()}.ndjson"
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                obj = None
            reason = obj.get("reason") if isinstance(obj, dict) else None
            reason = str(reason) if reason else "no reason recorded"
            total += 1
            reasons[reason.split(":")[0][:80]] += 1
    accepted = sum(len((Path(events_dir) / "track_events" / f"{(today - timedelta(days=k)).isoformat()}.ndjson").read_text(encoding="utf-8").splitlines())
                   for k in range(days) if (Path(events_dir) / "track_events" / f"{(today - timedelta(days=k)).isoformat()}.ndjson").exists())
    return {"quarantined": total, "accepted": accepted, "share": round(total / (total + accepted), 4) if total + accepted else None,
            "top_reasons": reasons.most_common(5)}


def _status(last: int, usual: float, ever: int, silent_days: int = 7, before_silence: int = 0) -> str:
    """before_silence: how many arrived in the 28 days up to the last one, the usual rate a silence is judged against."""
    if not ever:
        return "not tracked"
    if last == 0:
        return "stopped" if before_silence / 28 * silent_days >= SILENCE_EXPECTED else "too few to judge"
    if usual >= MIN_WEEKLY and last < usual * 0.5:
        return "dropped"
    return "ok"


def _match_counts(con, m, recent_from: date, end: date, base_from: date) -> Dict[str, int]:
    """Last 7 days, the 28 before, and ever, for one event with its property filters (order_completed from orders)."""
    from engine.agents import cohort_analyst
    from engine.sql.dialects import dialect
    if m.event == "order_completed":
        src = "SELECT CAST(order_at AS DATE) AS d FROM orders WHERE status = 'completed'"
    else:
        src = f"SELECT CAST(event_at AS DATE) AS d FROM track_events t WHERE {dialect('duckdb').event_filter('t.', m.event, m.where)}"
    rows = cohort_analyst._rows(con, _COUNTS.format(src=f"SELECT 'x' AS event_name, d FROM ({src}) m", end=end,
                                                         recent_from=recent_from, base_from=base_from,
                                                         base_to=recent_from - timedelta(days=1)))
    return _counted(rows[0] if rows else {}, end)


_COUNTS = """
    WITH s AS (SELECT event_name, d FROM ({src}) x WHERE d <= DATE '{end}'),
         ls AS (SELECT event_name, MAX(d) AS last_seen FROM s GROUP BY event_name)
    SELECT s.event_name, ls.last_seen,
           SUM(CASE WHEN s.d >= DATE '{recent_from}' THEN 1 ELSE 0 END) AS last_7d,
           SUM(CASE WHEN s.d BETWEEN DATE '{base_from}' AND DATE '{base_to}' THEN 1 ELSE 0 END) AS prior_28d,
           COUNT(*) AS ever,
           SUM(CASE WHEN s.d > ls.last_seen - INTERVAL 28 DAY THEN 1 ELSE 0 END) AS before_silence
    FROM s JOIN ls USING (event_name) GROUP BY s.event_name, ls.last_seen"""


def _counted(r: Dict[str, Any], end: date) -> Dict[str, Any]:
    last_seen = r.get("last_seen")
    return {"last_7d": int(r.get("last_7d") or 0), "prior_28d": int(r.get("prior_28d") or 0), "ever": int(r.get("ever") or 0),
            "silent_days": (end - last_seen).days if last_seen else 0, "before_silence": int(r.get("before_silence") or 0)}


def _judge(c: Dict[str, Any]) -> str:
    return _status(c["last_7d"], c["prior_28d"] / 4, c["ever"], c["silent_days"], c["before_silence"])


def health(data: str, as_of: Optional[str] = None, briefs: Optional[List[Any]] = None, events_dir: Optional[Path] = None,
           today: Optional[date] = None) -> Dict[str, Any]:
    from engine.agents import cohort_analyst
    from engine.models import EventMatch
    con = cohort_analyst.connect(data)
    label = cohort_analyst.data_label(data)
    today = today or datetime.now(timezone.utc).date()
    newest = con.execute("SELECT CAST(MAX(event_at) AS DATE) FROM track_events").fetchone()[0]
    if as_of:
        end = date.fromisoformat(as_of)
    elif label == "DEMO DATA" and newest is not None:
        end = cohort_analyst.latest_complete_day(con)[0]      # the demo data does not move; read it to its own last day
    else:
        end = today - timedelta(days=1)
    lag = (end - newest).days if newest is not None else None
    stale = {"newest_event": newest.isoformat() if newest else None, "days_behind": lag} \
        if newest is None or lag > STALE_AFTER_DAYS else None
    recent_from, base_from = end - timedelta(days=6), end - timedelta(days=34)
    rows = cohort_analyst._rows(con, _COUNTS.format(src="SELECT event_name, CAST(event_at AS DATE) AS d FROM track_events", end=end,
                                                     recent_from=recent_from, base_from=base_from, base_to=recent_from - timedelta(days=1)))
    seen = {r["event_name"]: _counted(r, end) for r in rows}
    orders = _match_counts(con, EventMatch(event="order_completed"), recent_from, end, base_from)
    p = plan()
    events = []
    for name, required in p.items():
        r = seen.get(name) or _counted({}, end)
        source = "events"
        if name == "order_completed" and orders["ever"]:
            r, source = orders, "orders"
        last, usual = r["last_7d"], r["prior_28d"] / 4
        status = _judge(r)
        missing = {}
        if last and required and source == "events":
            fills = con.execute(
                "SELECT " + ", ".join(f"AVG(CASE WHEN json_extract(CAST(properties AS JSON), '$.{prop}') IS NULL "
                                      f"OR CAST(json_extract(CAST(properties AS JSON), '$.{prop}') AS VARCHAR) = 'null' THEN 1.0 ELSE 0 END)"
                                      for prop in required) +
                f" FROM track_events WHERE event_name = ? AND CAST(event_at AS DATE) BETWEEN DATE '{recent_from}' AND DATE '{end}'",
                [name]).fetchone()
            missing = {prop: round(float(v), 4) for prop, v in zip(required, fills) if v is not None and float(v) > MISSING_PROPERTY_SHARE}
        events.append({"event": name, "status": status, "last_7d": last, "usual_week": round(usual, 1), "missing_required": missing,
                       "source": source, "silent_days": r["silent_days"] if not last else 0,
                       "week_before_silence": round(r["before_silence"] / 4, 1) if not last else None})
    off_plan = sorted(({"event": n, "last_7d": int(r["last_7d"] or 0)} for n, r in seen.items() if n not in p and r["last_7d"]),
                      key=lambda x: -x["last_7d"])
    missing_props = {e["event"] for e in events if e["missing_required"]}
    affected, not_tracked_used = [], set()
    for b in briefs or []:
        hit = []
        for m in _unique((b.qualifying_event, b.core_conversion_event)):
            c = _match_counts(con, m, recent_from, end, base_from)
            st = _judge(c)
            if st == "not tracked":
                not_tracked_used.add(m.detail())
            elif st in ("stopped", "dropped"):
                hit.append({"event": m.detail(), "status": st, "last_7d": c["last_7d"], "usual_week": round(c["prior_28d"] / 4, 1),
                            "silent_days": c["silent_days"] if not c["last_7d"] else 0,
                            "week_before_silence": round(c["before_silence"] / 4, 1) if not c["last_7d"] else None})
            elif m.event in missing_props:
                hit.append({"event": m.detail(), "status": "missing properties", "last_7d": c["last_7d"], "usual_week": round(c["prior_28d"] / 4, 1)})
        if hit:
            affected.append({"flow": b.slug, "events": [h["event"] for h in hit], "detail": hit})
    q = quarantine(events_dir, today=date.fromisoformat(as_of) if as_of else today) if events_dir and Path(events_dir).exists() else None
    problems = [e for e in events if e["status"] in ("stopped", "dropped") or e["missing_required"]]
    status = "problem" if stale or problems or affected or not_tracked_used or (q and (q["share"] or 0) > 0.02) else "ok"
    return {"as_of": end.isoformat(), "data_label": label, "status": status, "stale": stale, "events": events,
            "off_plan": off_plan, "flows_affected": affected, "flows_needing_untracked_events": sorted(not_tracked_used), "quarantine": q}


def _unique(matches) -> List[Any]:
    out, keys = [], set()
    for m in matches:
        k = (m.event, tuple(sorted((str(a), str(b)) for a, b in m.where.items())))
        if k not in keys:
            keys.add(k)
            out.append(m)
    return out


def render(r: Dict[str, Any]) -> str:
    L = [f"## Tracking health, week to {r['as_of']} ({r['data_label']})", ""]
    bad = [e for e in r["events"] if e["status"] in ("stopped", "dropped") or e["missing_required"]]
    if r.get("stale"):
        st = r["stale"]
        L.append(f"- **No new events since {st['newest_event']}**" + (f" ({st['days_behind']} days before {r['as_of']})" if st["days_behind"] is not None else "")
                 + ": the feed into the warehouse or the export looks stalled. Every flow that triggers on events is "
                   "sending nothing until it is fixed.")
    if not bad and not r["flows_affected"] and not r["flows_needing_untracked_events"] and not r.get("stale"):
        L.append("Every tracked event is arriving at its usual volume with its required properties.")
    for e in bad:
        if e["status"] == "stopped" and r.get("stale"):
            continue                                   # the stale feed explains it; the table lists them
        if e["status"] == "stopped":
            L.append(f"- **{e['event']} stopped**: none for {e['silent_days']} days (about {e['week_before_silence']:,.0f} a week before that). "
                     "Check the last release or the CDP mapping.")
        elif e["status"] == "dropped":
            L.append(f"- **{e['event']} dropped**: {e['last_7d']:,} in the last 7 days against {e['usual_week']:,.0f} a week before.")
        for prop, share in e["missing_required"].items():
            L.append(f"- **{e['event']}.{prop} missing** on {share:.0%} of recent events: any flow filtering on it is losing people.")
    for f in r["flows_affected"]:
        parts = [(f"{d['event']} (stopped: none for {d['silent_days']} days, about {d['week_before_silence']:,.0f} a week before that)"
                  if d["status"] == "stopped" else
                  f"{d['event']} ({d['status']}: {d['last_7d']:,} in the last 7 days, usually {d['usual_week']:,.0f} a week)")
                 for d in f.get("detail", [])] or f["events"]
        L.append(f"- **Flow at risk: {f['flow']}** depends on " + "; ".join(parts) + ".")
    if r["flows_needing_untracked_events"]:
        L.append(f"- **Not tracked but used by a live flow:** {', '.join(r['flows_needing_untracked_events'])}.")
    L += ["", "| Event | Status | Last 7 days | Usual week |", "|---|---|---:|---:|"]
    L += [f"| {e['event']}" + (" (orders)" if e.get("source") == "orders" else "") + f" | {e['status']} | {e['last_7d']:,} | {(e['week_before_silence'] if e['status'] == 'stopped' else e['usual_week']):,.0f} |"
          for e in r["events"]]
    if r["off_plan"]:
        L += ["", "Arriving but not in the tracking plan: " + ", ".join(f"{x['event']} ({x['last_7d']:,})" for x in r["off_plan"][:8])
              + ". Add the ones a flow should use to the plan; ignore the rest."]
    q = r.get("quarantine")
    if q:
        L += ["", f"Webhook receiver, last 7 days: {q['quarantined']:,} quarantined of {q['quarantined'] + q['accepted']:,}"
              + (f" ({q['share']:.1%})" if q["share"] is not None else "") + "."]
        L += [f"- {n:,}: {reason}" for reason, n in q["top_reasons"]]
    return "\n".join(L) + "\n"


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="lcm tracking", description="Tracking health: are the events your flows depend on still arriving?")
    ap.add_argument("--data", required=True, help="folder of exports with track_events and orders")
    ap.add_argument("--as-of", help="last day of the 7-day window, YYYY-MM-DD (default: yesterday, UTC)")
    ap.add_argument("--events-dir", help="the webhook receiver's folder (LCM_EVENTS_DIR), for quarantine counts")
    ap.add_argument("--json", action="store_true", help="print the report as JSON instead of text")
    a = ap.parse_args(argv)
    from engine.brief import load_brief
    from engine import workspace
    briefs = [load_brief(p) for p in sorted(workspace.find().active_briefs.glob("*.yaml"))]
    try:
        r = health(a.data, a.as_of, briefs, Path(a.events_dir) if a.events_dir else None)
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(r, indent=2, default=str) if a.json else render(r))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
