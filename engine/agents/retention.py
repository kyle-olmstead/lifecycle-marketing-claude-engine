"""The retention curve: in which week after signup do people stop doing the core action, and does it differ by segment?

For everyone who signed up long enough ago to be observed, the share who did the core action in each week since
signup (week 0 is the first 7 days). The curve shows two things a lifecycle program is built around:

  the cliff     the week with the largest drop from the week before, among drops bigger than chance
  the floor     the week after which the curve stops moving: over at least three more weeks it changes less than
                half a point a week on average, under 4 points in all, and under 5 points in any one week

Both compare the same people: week k against week k+1 among everyone observed through week k+1 (a paired test), so a
change in who signed up when (a big recent cohort, a strong old one) never reads as churn. A drop counts as a cliff
only when it clears a bar corrected for the number of weeks tested (Bonferroni, about z 2.9 for 12 weeks) and does not
bounce back by more than half the next week. People are counted only in weeks they have fully lived through, and only
in weeks the export covers, so a young cohort or a short export never reads as churn.

With --by, the same curve for each value of a users column (plan, country, lifecycle_stage), for segments with at least
30 people observed through week 4 (or the last week asked for, when that is sooner). order_completed is read from the
orders table, as everywhere else in the engine. Which behaviors predict the cliff is a modeling question this does not
answer; it shows where to look.

    python3 -m engine.agents.retention --data exports/
    python3 -m engine.agents.retention --data exports/ --by plan --weeks 16
    lcm "when do customers stop using the product, by plan?" --data exports/
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date
from pathlib import Path
from statistics import NormalDist
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BY_COLUMNS = ("plan", "country", "lifecycle_stage")
MIN_SEGMENT = 30
FLAT_SLOPE = 0.005     # a floor moves less than half a point a week on average...
FLAT_TOTAL = 0.04      # ...under 4 points in all...
FLAT_STEP = 0.05       # ...and under 5 points in any one week
FLOOR_WEEKS = 3        # over at least this many more weeks
MIN_PAIR = 10          # people observed in both weeks before a step is judged
MIN_DISCORDANT = 10    # people who changed between the two weeks before the paired test is used


def curve(data: str, brief=None, weeks: int = 12, by: Optional[str] = None, as_of: Optional[str] = None) -> Dict[str, Any]:
    from engine.agents import cohort_analyst
    from engine.brief import load_brief
    from engine.sql.dialects import dialect
    if by is not None and by not in BY_COLUMNS:
        raise ValueError(f"--by must be one of {', '.join(BY_COLUMNS)}")
    if not 2 <= weeks <= 52:
        raise ValueError("weeks must be between 2 and 52")
    from engine.paths import DEMO_BRIEF
    brief = brief or load_brief(DEMO_BRIEF)
    core = brief.core_conversion_event
    con = cohort_analyst.connect(data)
    end = date.fromisoformat(as_of) if as_of else cohort_analyst.latest_complete_day(con)[0]
    seg = f"COALESCE(CAST(u.{by} AS VARCHAR), '(none)')" if by else "'all'"
    covered_from = "(SELECT CAST(MIN(order_at) AS DATE) FROM orders)" if core.event == "order_completed" else \
                   "(SELECT CAST(MIN(event_at) AS DATE) FROM track_events)"
    base = f"""
        WITH u AS (SELECT user_id, CAST(created_at AS DATE) AS signup, {seg} AS segment FROM users u
                   WHERE CAST(created_at AS DATE) <= DATE '{end}'),
             wk AS (SELECT range AS k FROM range(0, {weeks + 1})),
             eligible AS (SELECT u.user_id, u.segment, u.signup, wk.k FROM u CROSS JOIN wk
                          WHERE u.signup + INTERVAL (7 * (wk.k + 1)) DAY <= DATE '{end}' + INTERVAL 1 DAY
                            AND u.signup + INTERVAL (7 * wk.k) DAY >= {covered_from}),
             acts AS (SELECT DISTINCT s.user_id, CAST(s.event_at AS DATE) AS d FROM ({cohort_analyst._source(dialect('duckdb'), core, 'e')}) s),
             flags AS (SELECT el.user_id, el.segment, el.k,
                              CASE WHEN EXISTS (SELECT 1 FROM acts a WHERE a.user_id = el.user_id
                                                AND a.d >= el.signup + INTERVAL (7 * el.k) DAY
                                                AND a.d < el.signup + INTERVAL (7 * (el.k + 1)) DAY) THEN 1 ELSE 0 END AS a
                       FROM eligible el)"""
    rows = cohort_analyst._rows(con, base + """
        SELECT segment, k, COUNT(*) AS observed, SUM(a) AS active FROM flags GROUP BY segment, k ORDER BY segment, k""")
    pair_rows = cohort_analyst._rows(con, base + """
        SELECT x.segment, x.k, COUNT(*) AS n, SUM(x.a) AS a0, SUM(y.a) AS a1,
               SUM(CASE WHEN x.a = 1 AND y.a = 0 THEN 1 ELSE 0 END) AS lost,
               SUM(CASE WHEN x.a = 0 AND y.a = 1 THEN 1 ELSE 0 END) AS gained
        FROM flags x JOIN flags y ON y.user_id = x.user_id AND y.k = x.k + 1
        GROUP BY x.segment, x.k ORDER BY x.segment, x.k""")
    segments: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        n = int(r["observed"])
        segments.setdefault(str(r["segment"]), []).append({"week": int(r["k"]), "observed": n, "active": int(r["active"]),
                                                            "rate": round(int(r["active"]) / n, 4) if n else None})
    pairs: Dict[str, List[Dict[str, Any]]] = {}
    for r in pair_rows:
        pairs.setdefault(str(r["segment"]), []).append({"week": int(r["k"]) + 1, "n": int(r["n"]), "lost": int(r["lost"]),
                                                         "gained": int(r["gained"])})
    gate_week = min(4, weeks)
    out = {}
    for name, pts in segments.items():
        at = next((p["observed"] for p in pts if p["week"] == gate_week), 0)
        if by and at < MIN_SEGMENT:
            continue
        out[name] = {"points": pts, "steps": pairs.get(name, []), **_shape(pairs.get(name, []), pts)}
    if not out:
        raise ValueError("not enough people observed for a curve" +
                         (f" in any {by} segment (need {MIN_SEGMENT} through week {gate_week})" if by else ""))
    return {"as_of": end.isoformat(), "data_label": cohort_analyst.data_label(data), "core_action": core.describe(),
            "by": by, "weeks": weeks, "segments": out}


def _shape(steps: List[Dict[str, Any]], pts: Optional[List[Dict[str, Any]]] = None, alpha: float = 0.05) -> Dict[str, Any]:
    """steps: for each week k (1..), the people observed in both week k-1 and week k, how many stopped (lost) and how
    many started (gained). The cliff is the largest drop that clears a Bonferroni-corrected paired test and is not
    half undone the next week; small or noisy curves get no cliff rather than a made-up one."""
    usable = [s for s in steps if s["n"] >= MIN_PAIR]
    for s in usable:
        s["change"] = (s["gained"] - s["lost"]) / s["n"]
    tested = [s for s in usable if s["lost"] + s["gained"] >= MIN_DISCORDANT]
    z_bar = NormalDist().inv_cdf(1 - alpha / (2 * max(len(tested), 1)))
    by_week = {s["week"]: s for s in usable}
    cliffs = []
    for s in tested:
        z = (s["lost"] - s["gained"]) / math.sqrt(s["lost"] + s["gained"])
        drop = -s["change"]
        nxt = by_week.get(s["week"] + 1)
        if z >= z_bar and drop > 0 and not (nxt and nxt["change"] > drop / 2):
            cliffs.append((drop, s["week"]))
    cliff = max(cliffs, default=None)
    floor_week = None
    for i, s in enumerate(usable):
        rest = usable[i:]
        total = sum(x["change"] for x in rest)          # paired changes telescope, so noise does not pile up
        if (len(rest) >= FLOOR_WEEKS and abs(total) < FLAT_TOTAL and abs(total) / len(rest) < FLAT_SLOPE
                and all(abs(x["change"]) < FLAT_STEP for x in rest)):
            floor_week = s["week"] - 1
            break
    rate = next((p["rate"] for p in (pts or []) if p["week"] == floor_week), None) if floor_week is not None else None
    return {"cliff_week": cliff[1] if cliff else None, "cliff_drop": round(cliff[0], 4) if cliff else None,
            "floor_week": floor_week, "floor_rate": rate, "cliff_bar_z": round(z_bar, 2)}


def render(r: Dict[str, Any]) -> str:
    L = [f"## When people stop: weekly retention on {r['core_action']} ({r['data_label']}, to {r['as_of']})", ""]
    for name, s in r["segments"].items():
        head = f"**{name}**: " if r["by"] else ""
        bits = []
        if s["cliff_week"] is not None:
            bits.append(f"the biggest drop is into week {s['cliff_week']} ({s['cliff_drop'] * 100:.1f} points), so a flow has to land before day {7 * s['cliff_week']}")
        else:
            bits.append("no clear cliff: no week-to-week drop is bigger than chance at this sample size")
        if s["floor_week"] is not None:
            bits.append(f"the curve levels off from week {s['floor_week']}" + (f" at about {s['floor_rate']:.0%}" if s["floor_rate"] is not None else ""))
        else:
            bits.append(f"it has not levelled off by week {r['weeks']}")
        L.append(f"- {head}" + "; ".join(bits) + ".")
    L += ["", "| Week | " + " | ".join(r["segments"]) + " |", "|---:|" + "---:|" * len(r["segments"])]
    for k in range(r["weeks"] + 1):
        cells = []
        for s in r["segments"].values():
            p = next((p for p in s["points"] if p["week"] == k), None)
            cells.append("" if not p or p["rate"] is None else f"{p['rate']:.0%} (n={p['observed']:,})")
        L.append(f"| {k} | " + " | ".join(cells) + " |")
    L += ["", "*Week k counts people whose week k is complete. The cliff and the floor compare the same people from one week to "
              "the next, so a change in who signed up when does not read as churn. What predicts the drop (the behavior the week "
              "before) is a modeling question; compare the people who stayed past the cliff with those who did not before "
              "building on it.*"]
    return "\n".join(L) + "\n"


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Weekly retention on the core action, with the cliff and the floor")
    ap.add_argument("--data", required=True)
    ap.add_argument("--brief", help="the brief whose core action counts as retained (default: the example)")
    ap.add_argument("--by", choices=BY_COLUMNS)
    ap.add_argument("--weeks", type=int, default=12)
    ap.add_argument("--as-of")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    from engine.brief import load_brief
    try:
        r = curve(a.data, load_brief(Path(a.brief)) if a.brief else None, a.weeks, a.by, a.as_of)
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(r, indent=2, default=str) if a.json else render(r))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
