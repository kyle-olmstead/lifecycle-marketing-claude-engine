"""
Warming ladder for a new or recovering sending IP or domain (modules/01 warmup_schedule_generator).

The rules are the deliverability-engineer method, enforced in code so a schedule cannot be produced
that breaks them:

  * The gate decides first. RED: no ladder, remediation only. AMBER: hold volume flat, engaged
    tiers only. GREEN: a ladder.
  * Warm on engagement, never on volume alone. Tiers are mailed most-engaged first; the unengaged
    tier is never used to warm (it gets a separate re-permission path).
  * Volume roughly doubles every 2 days while it fits inside the most-engaged tier and every 3
    days once the ladder reaches older tiers, because risk rises as engagement falls.
  * Every step ends at a checkpoint. Advance only if all abort criteria are clear; otherwise hold,
    or drop back one step.
  * Capacity is honest: a tier of N people mailed at most F times a week supports N x F / 7 sends a
    day. If the target exceeds what the engaged audience supports, the schedule says so instead of
    padding the ladder with people who will not engage.

    python3 tools/warmup.py --target 60000 --tiers '{"engaged_30d": 40000, "engaged_90d": 70000, "engaged_180d": 90000, "unengaged": 400000}' --gate GREEN --markdown
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional

TIER_ORDER = ("engaged_30d", "engaged_90d", "engaged_180d")
ABORT_CRITERIA = [
    "complaint rate above 0.1% on any day",
    "hard bounce rate above 2% on any day",
    "Gmail Postmaster spam rate above 0.1%, or any reputation-class (DOMAIN_REPUTATION) or FILTERED bounces at a provider",
    "deferrals (4xx) climbing at one provider for two consecutive days",
]
ON_ABORT = "Halt the ladder, drop back one step, hold until two clean days, then resume at that step."


def _split(volume: int, mix: Dict[str, float]) -> Dict[str, int]:
    if not mix:
        return {}
    total = sum(mix.values())
    out = {k: int(volume * v / total) for k, v in mix.items()}
    first = max(mix, key=mix.get)
    out[first] += volume - sum(out.values())
    return out


def plan(target: int, tiers: Dict[str, int], gate: str, days: int = 30, max_per_week: float = 3.0,
         start: Optional[int] = None, current: Optional[int] = None, new_infrastructure: bool = True,
         provider_mix: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    gate = gate.upper()
    if gate not in ("GREEN", "AMBER", "RED"):
        raise ValueError("gate must be GREEN, AMBER or RED")
    cap = {t: int(tiers.get(t, 0) * max_per_week / 7) for t in TIER_ORDER}
    cumulative, run = {}, 0
    for t in TIER_ORDER:
        run += cap[t]
        cumulative[t] = run
    engaged_capacity = run
    base: Dict[str, Any] = {
        "gate": gate, "target_daily_volume": target, "max_sends_per_person_per_week": max_per_week,
        "tier_daily_capacity": cap, "engaged_daily_capacity": engaged_capacity,
        "excluded": {"unengaged": tiers.get("unengaged", 0),
                     "reason": "never used to warm; route to a single re-permission send from a separate subdomain if volume warrants (expect 1 to 5% opt-in)"},
        "abort_criteria": ABORT_CRITERIA, "on_abort": ON_ABORT,
    }
    if gate == "RED":
        return {**base, "schedule": [], "summary": "Gate is RED. No warming ladder is permitted. Stop bulk sending; remediation and re-permission only."}
    if gate == "AMBER":
        flat = min(current if current is not None else cap["engaged_30d"], cumulative["engaged_90d"])
        rows = [{"day": d, "volume": flat, "step": 0, "tiers": _tiers_for(flat, cumulative), "checkpoint": d % 7 == 0,
                 "providers": _split(flat, provider_mix or {})} for d in range(1, days + 1)]
        return {**base, "schedule": rows,
                "summary": (f"Gate is AMBER. Hold flat at {flat:,} a day to engaged tiers only while remediating. "
                            "Re-run the gate weekly; a ladder starts only after it reads GREEN.")}

    goal = min(target, engaged_capacity)
    if start is None:
        start = min(cap["engaged_30d"], 500) if new_infrastructure else cap["engaged_30d"]
    start = max(1, min(start, goal))
    rows: List[Dict[str, Any]] = []
    volume, step, day, step_day = start, 1, 1, 0
    while day <= days:
        length = 2 if volume <= cap["engaged_30d"] else 3
        step_day += 1
        at_goal = volume >= goal
        rows.append({"day": day, "volume": volume, "step": step, "tiers": _tiers_for(volume, cumulative),
                     "checkpoint": (step_day == length) and not at_goal, "providers": _split(volume, provider_mix or {})})
        if step_day == length and not at_goal:
            volume, step, step_day = min(goal, volume * 2), step + 1, 0
        day += 1
    reached = next((r["day"] for r in rows if r["volume"] >= goal), None)
    gap = target - goal
    summary = (f"Start at {start:,} a day to the 30-day engaged tier and roughly double at each checkpoint while every abort "
               f"criterion is clear. " + (f"The ladder reaches {goal:,} a day on day {reached} if no checkpoint holds. " if reached else
                                          f"At this pace the ladder ends day {days} at {rows[-1]['volume']:,} a day, short of {goal:,}; extend the schedule rather than steepen it. "))
    if gap > 0:
        summary += (f"The target of {target:,} exceeds what the engaged audience supports at {max_per_week:g} sends per person a week "
                    f"({engaged_capacity:,} a day). The remaining {gap:,} a day is not warmable: it is unengaged volume, and adding it is the "
                    "most common way a new sending domain is damaged permanently.")
    return {**base, "goal_daily_volume": goal, "unwarmable_gap": max(0, gap), "start_volume": start, "schedule": rows, "summary": summary}


def _tiers_for(volume: int, cumulative: Dict[str, int]) -> List[str]:
    used = []
    for t in TIER_ORDER:
        used.append(t)
        if volume <= cumulative[t]:
            break
    return used


def render_markdown(p: Dict[str, Any]) -> str:
    L = [f"## Warming plan (gate: {p['gate']})", "", p["summary"], ""]
    if p["schedule"]:
        prov = list(p["schedule"][0]["providers"].keys())
        L += ["| Day | Volume | Step | Tiers mailed | Checkpoint |" + "".join(f" {k} |" for k in prov),
              "|---|---|---|---|---|" + "---|" * len(prov)]
        for r in p["schedule"]:
            L.append(f"| {r['day']} | {r['volume']:,} | {r['step']} | {', '.join(r['tiers'])} | {'advance only if clear' if r['checkpoint'] else ''} |"
                     + "".join(f" {r['providers'][k]:,} |" for k in prov))
        L += [""]
    L += ["**Abort criteria (stated before the first send):**", ""] + [f"- {c}" for c in p["abort_criteria"]] + ["", f"On abort: {p['on_abort']}", "",
          f"Excluded from warming: {p['excluded']['unengaged']:,} unengaged contacts ({p['excluded']['reason']})."]
    return "\n".join(L) + "\n"


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Warming ladder generator")
    ap.add_argument("--target", type=int, required=True, help="target daily volume")
    ap.add_argument("--tiers", required=True, help='JSON: {"engaged_30d": n, "engaged_90d": n, "engaged_180d": n, "unengaged": n} (each tier exclusive of the ones before it)')
    ap.add_argument("--gate", required=True, choices=["GREEN", "AMBER", "RED", "green", "amber", "red"])
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--max-per-week", type=float, default=3.0)
    ap.add_argument("--start", type=int)
    ap.add_argument("--current", type=int, help="current daily volume (AMBER holds it flat)")
    ap.add_argument("--existing-reputation", action="store_true", help="infrastructure already has sending history (start at engaged-30d capacity)")
    ap.add_argument("--provider-mix", default="", help="e.g. gmail=0.55,microsoft=0.2,yahoo=0.1,other=0.15")
    ap.add_argument("--markdown", action="store_true")
    a = ap.parse_args(argv)
    mix = {k: float(v) for k, v in (kv.split("=") for kv in a.provider_mix.split(",") if kv)} if a.provider_mix else {}
    p = plan(a.target, json.loads(a.tiers), a.gate, a.days, a.max_per_week, a.start, a.current, not a.existing_reputation, mix)
    print(render_markdown(p) if a.markdown else json.dumps(p, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
