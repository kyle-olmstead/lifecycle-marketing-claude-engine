"""The retention curve: in which week after signup do people stop doing the core action, and does it differ by segment?

For everyone who signed up long enough ago to be observed, the share who did the core action in each week since
signup (week 0 is the first 7 days). The curve shows two things a lifecycle program is built around:

  the cliff     the week with the largest drop from the week before: the moment a flow has to land before
  the floor     the first week after which the curve moves less than 2 points a week: the share who stay

People are counted only in weeks they have fully lived through, so a young cohort never reads as churn. With --by,
the same curve for each value of a users column (plan, country, lifecycle_stage), for segments with at least 30
people observed through week 4. Which behaviors predict the cliff is a modeling question this does not answer; it
shows where to look.

    python3 -m agents.retention --data exports/
    python3 -m agents.retention --data exports/ --by plan --weeks 16
    lcm "when do customers stop using the product, by plan?" --data exports/
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BY_COLUMNS = ("plan", "country", "lifecycle_stage")
MIN_SEGMENT = 30
FLAT = 0.02


def _core_filter(brief) -> str:
    core = brief.core_conversion_event
    cond = [f"e.event_name = '{core.event}'"]
    for k, v in core.where.items():
        text = ("true" if v else "false") if isinstance(v, bool) else str(v).replace("'", "''")
        cond.append(f"CAST(json_extract_string(CAST(e.properties AS JSON), '$.{k}') AS VARCHAR) = '{text}'")
    return " AND ".join(cond)


def curve(data: str, brief=None, weeks: int = 12, by: Optional[str] = None, as_of: Optional[str] = None) -> Dict[str, Any]:
    from agents import cohort_analyst
    from agents.brief import load_brief
    if by is not None and by not in BY_COLUMNS:
        raise ValueError(f"--by must be one of {', '.join(BY_COLUMNS)}")
    if not 2 <= weeks <= 52:
        raise ValueError("weeks must be between 2 and 52")
    brief = brief or load_brief(ROOT / "briefs" / "examples" / "winback_14d.yaml")
    con = cohort_analyst.connect(data)
    end = date.fromisoformat(as_of) if as_of else cohort_analyst.latest_complete_day(con)[0]
    seg = f"COALESCE(CAST(u.{by} AS VARCHAR), '(none)')" if by else "'all'"
    rows = cohort_analyst._rows(con, f"""
        WITH u AS (SELECT user_id, CAST(created_at AS DATE) AS signup, {seg} AS segment FROM users u
                   WHERE CAST(created_at AS DATE) <= DATE '{end}'),
             wk AS (SELECT range AS k FROM range(0, {weeks + 1})),
             eligible AS (SELECT u.user_id, u.segment, u.signup, wk.k FROM u CROSS JOIN wk
                          WHERE u.signup + INTERVAL (7 * (wk.k + 1)) DAY <= DATE '{end}' + INTERVAL 1 DAY),
             acts AS (SELECT DISTINCT e.user_id, CAST(e.event_at AS DATE) AS d FROM track_events e WHERE {_core_filter(brief)})
        SELECT el.segment, el.k, COUNT(*) AS observed,
               SUM(CASE WHEN EXISTS (SELECT 1 FROM acts a WHERE a.user_id = el.user_id
                                     AND a.d >= el.signup + INTERVAL (7 * el.k) DAY AND a.d < el.signup + INTERVAL (7 * (el.k + 1)) DAY)
                        THEN 1 ELSE 0 END) AS active
        FROM eligible el GROUP BY el.segment, el.k ORDER BY el.segment, el.k""")
    segments: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        n = int(r["observed"])
        segments.setdefault(str(r["segment"]), []).append({"week": int(r["k"]), "observed": n, "active": int(r["active"]),
                                                            "rate": round(int(r["active"]) / n, 4) if n else None})
    out = {}
    for name, pts in segments.items():
        at4 = next((p["observed"] for p in pts if p["week"] == 4), 0)
        if by and at4 < MIN_SEGMENT:
            continue
        out[name] = {"points": pts, **_shape(pts)}
    if not out:
        raise ValueError("not enough people observed for a curve" + (f" in any {by} segment (need {MIN_SEGMENT} through week 4)" if by else ""))
    return {"as_of": end.isoformat(), "data_label": cohort_analyst.data_label(data), "core_action": brief.core_conversion_event.describe(),
            "by": by, "weeks": weeks, "segments": out}


def _shape(pts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The cliff is the largest week-over-week drop that is bigger than chance (about two standard errors); small or noisy
    curves get no cliff rather than a made-up one."""
    import math
    usable = [p for p in pts if p["rate"] is not None and p["observed"] >= 10]
    drops = []
    for i in range(1, len(usable)):
        a, b = usable[i - 1], usable[i]
        se = math.sqrt(a["rate"] * (1 - a["rate"]) / a["observed"] + b["rate"] * (1 - b["rate"]) / b["observed"])
        d = a["rate"] - b["rate"]
        if d > 0 and se > 0 and d / se >= 2:
            drops.append((d, b["week"]))
    cliff = max(drops, default=None)
    floor_week = None
    for i in range(1, len(usable)):
        rest = [abs(usable[j]["rate"] - usable[j - 1]["rate"]) for j in range(i, len(usable))]
        if rest and all(x < FLAT for x in rest):
            floor_week = usable[i - 1]["week"]
            break
    return {"cliff_week": cliff[1] if cliff and cliff[0] > 0 else None, "cliff_drop": round(cliff[0], 4) if cliff and cliff[0] > 0 else None,
            "floor_week": floor_week, "floor_rate": next((p["rate"] for p in usable if p["week"] == floor_week), None)}


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
            bits.append(f"the curve levels off from week {s['floor_week']} at about {s['floor_rate']:.0%}")
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
    L += ["", "*Week k counts people whose week k is complete. What predicts the drop (the behavior the week before) is a "
              "modeling question; compare the people who stayed past the cliff with those who did not before building on it.*"]
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
    from agents.brief import load_brief
    try:
        r = curve(a.data, load_brief(Path(a.brief)) if a.brief else None, a.weeks, a.by, a.as_of)
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(r, indent=2, default=str) if a.json else render(r))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
