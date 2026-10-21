"""Tracking health: are the events your flows depend on still arriving, complete, and on the plan?

Flows break quietly when tracking changes: an event is renamed in a release, a property goes null, a new event ships
outside the plan. Nothing errors; the trigger just stops firing. This check compares the last 7 days with the 4 weeks
before, for every event in the tracking plan (modules/02_behavioral_triggers_and_flows/schemas/custom_event_definitions.json):

  stopped        seen before, none in the last 7 days
  dropped        at least half below its usual weekly volume (only judged when the usual volume is 20 or more a week)
  not tracked    in the plan, never seen in the data (fine if you do not use it; a problem if a flow does)
  ok             arriving at its usual volume

It also reports required properties that are missing on more than 5 percent of recent events, events arriving that are
not in the plan, the flows in briefs/active/ that depend on anything stopped or dropped, and, when the webhook
receiver runs, how many events it quarantined and why.

    python3 -m agents.tracking --data exports/
    python3 -m agents.tracking --data exports/ --events-dir out/events
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

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PLAN_PATH = ROOT / "modules" / "02_behavioral_triggers_and_flows" / "schemas" / "custom_event_definitions.json"
MIN_WEEKLY = 20
MISSING_PROPERTY_SHARE = 0.05


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
            try:
                reason = json.loads(line).get("reason", "unknown")
            except ValueError:
                continue
            total += 1
            reasons[reason.split(":")[0][:80]] += 1
    accepted = sum(len((Path(events_dir) / "track_events" / f"{(today - timedelta(days=k)).isoformat()}.ndjson").read_text(encoding="utf-8").splitlines())
                   for k in range(days) if (Path(events_dir) / "track_events" / f"{(today - timedelta(days=k)).isoformat()}.ndjson").exists())
    return {"quarantined": total, "accepted": accepted, "share": round(total / (total + accepted), 4) if total + accepted else None,
            "top_reasons": reasons.most_common(5)}


def health(data: str, as_of: Optional[str] = None, briefs: Optional[List[Any]] = None, events_dir: Optional[Path] = None) -> Dict[str, Any]:
    from agents import cohort_analyst
    con = cohort_analyst.connect(data)
    end = date.fromisoformat(as_of) if as_of else cohort_analyst.latest_complete_day(con)[0]
    recent_from, base_from = end - timedelta(days=6), end - timedelta(days=34)
    rows = cohort_analyst._rows(con, f"""
        SELECT event_name,
               SUM(CASE WHEN CAST(event_at AS DATE) BETWEEN DATE '{recent_from}' AND DATE '{end}' THEN 1 ELSE 0 END) AS last_7d,
               SUM(CASE WHEN CAST(event_at AS DATE) BETWEEN DATE '{base_from}' AND DATE '{recent_from - timedelta(days=1)}' THEN 1 ELSE 0 END) AS prior_28d,
               SUM(CASE WHEN CAST(event_at AS DATE) <= DATE '{end}' THEN 1 ELSE 0 END) AS ever
        FROM track_events GROUP BY event_name""")
    seen = {r["event_name"]: r for r in rows}
    p = plan()
    events = []
    for name, required in p.items():
        r = seen.get(name) or {"last_7d": 0, "prior_28d": 0, "ever": 0}
        last, usual = int(r["last_7d"] or 0), (int(r["prior_28d"] or 0)) / 4
        if not r["ever"]:
            status = "not tracked"
        elif last == 0:
            status = "stopped"
        elif usual >= MIN_WEEKLY and last < usual * 0.5:
            status = "dropped"
        else:
            status = "ok"
        missing = {}
        if last and required:
            fills = con.execute(
                "SELECT " + ", ".join(f"AVG(CASE WHEN json_extract(CAST(properties AS JSON), '$.{prop}') IS NULL "
                                      f"OR CAST(json_extract(CAST(properties AS JSON), '$.{prop}') AS VARCHAR) = 'null' THEN 1.0 ELSE 0 END)"
                                      for prop in required) +
                f" FROM track_events WHERE event_name = ? AND CAST(event_at AS DATE) BETWEEN DATE '{recent_from}' AND DATE '{end}'",
                [name]).fetchone()
            missing = {prop: round(float(v), 4) for prop, v in zip(required, fills) if v is not None and float(v) > MISSING_PROPERTY_SHARE}
        events.append({"event": name, "status": status, "last_7d": last, "usual_week": round(usual, 1), "missing_required": missing})
    off_plan = sorted(({"event": n, "last_7d": int(r["last_7d"] or 0)} for n, r in seen.items() if n not in p and r["last_7d"]),
                      key=lambda x: -x["last_7d"])
    broken = {e["event"] for e in events if e["status"] in ("stopped", "dropped") or e["missing_required"]}
    affected = []
    for b in briefs or []:
        uses = {b.qualifying_event.event, b.core_conversion_event.event}
        hit = sorted(uses & broken)
        if hit:
            affected.append({"flow": b.slug, "events": hit})
    not_tracked_used = sorted({e for b in (briefs or []) for e in (b.qualifying_event.event, b.core_conversion_event.event)}
                              & {e["event"] for e in events if e["status"] == "not tracked"})
    q = quarantine(events_dir, today=end) if events_dir and Path(events_dir).exists() else None
    problems = [e for e in events if e["status"] in ("stopped", "dropped") or e["missing_required"]]
    status = "problem" if problems or affected or not_tracked_used or (q and (q["share"] or 0) > 0.02) else "ok"
    return {"as_of": end.isoformat(), "data_label": cohort_analyst.data_label(data), "status": status, "events": events,
            "off_plan": off_plan, "flows_affected": affected, "flows_needing_untracked_events": not_tracked_used, "quarantine": q}


def render(r: Dict[str, Any]) -> str:
    L = [f"## Tracking health, week to {r['as_of']} ({r['data_label']})", ""]
    bad = [e for e in r["events"] if e["status"] in ("stopped", "dropped") or e["missing_required"]]
    if not bad and not r["flows_affected"] and not r["flows_needing_untracked_events"]:
        L.append("Every tracked event is arriving at its usual volume with its required properties.")
    for e in bad:
        if e["status"] == "stopped":
            usual = f"usually {e['usual_week']:,.0f} a week" if e["usual_week"] else "and none in the four weeks before either"
            L.append(f"- **{e['event']} stopped**: none in the last 7 days ({usual}). Check the last release or the CDP mapping.")
        elif e["status"] == "dropped":
            L.append(f"- **{e['event']} dropped**: {e['last_7d']:,} in the last 7 days against {e['usual_week']:,.0f} a week before.")
        for prop, share in e["missing_required"].items():
            L.append(f"- **{e['event']}.{prop} missing** on {share:.0%} of recent events: any flow filtering on it is losing people.")
    for f in r["flows_affected"]:
        L.append(f"- **Flow at risk: {f['flow']}** depends on {', '.join(f['events'])}.")
    if r["flows_needing_untracked_events"]:
        L.append(f"- **Not tracked but used by a live flow:** {', '.join(r['flows_needing_untracked_events'])}.")
    L += ["", "| Event | Status | Last 7 days | Usual week |", "|---|---|---:|---:|"]
    L += [f"| {e['event']} | {e['status']} | {e['last_7d']:,} | {e['usual_week']:,.0f} |" for e in r["events"]]
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
    ap = argparse.ArgumentParser(description="Tracking health: are the events your flows depend on still arriving?")
    ap.add_argument("--data", required=True)
    ap.add_argument("--as-of")
    ap.add_argument("--events-dir", help="the webhook receiver's folder (LCM_EVENTS_DIR), for quarantine counts")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    from agents.brief import load_brief
    briefs = [load_brief(p) for p in sorted((ROOT / "briefs" / "active").glob("*.yaml"))]
    try:
        r = health(a.data, a.as_of, briefs, Path(a.events_dir) if a.events_dir else None)
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(r, indent=2, default=str) if a.json else render(r))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
