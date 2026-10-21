"""The program simulator: every live flow at once, against one frequency policy.

A flow can pass its own simulation and still break the program: a customer who places a first order, starts another
checkout the next day and comes due for a reorder the week after is in three flows, and three flows that each respect a
weekly cap of 3 can still send them 7 messages. v2 simulated one flow at a time and gave every flow priority 3.

This module compiles every brief in the workspace's briefs/active/ (plus a candidate brief, when planning a new flow),
walks synthetic people who enter several flows close together, applies the policy's collision rule in flow_priority order,
and checks every cap in the frequency policy across all flows:

  higher_priority_preempts  a lower-priority flow's sends are skipped while a higher-priority flow is running for the person
  queue_lower_priority      they wait until the higher-priority flow has finished
  drop_lower_priority       the lower-priority flow does not start at all while a higher-priority one is running

    lcm simulate --program                       # the active briefs in this workspace
    python3 engine/policy/program.py --briefs examples/brands/ridgeview_saas/briefs/active --json
"""
from __future__ import annotations

if __package__ in (None, ""):  # run as a file (python3 engine/policy/program.py): make the repository importable
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse
import itertools
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from engine.models import Brief
from engine.policy.contracts import FrequencyPolicy

OFFSETS = (0.0, 24.0, 72.0)   # how far apart two entries are in the scenarios, in hours
CAPPED = ("email", "sms", "push")


def _sends(brief: Brief) -> List[Tuple[float, str]]:
    """The flow's sends for a person who never converts and has every consent: (hours from entry, channel)."""
    from engine.agents import campaign_architect, cohort_analyst
    from engine.policy.simulator import _paths
    cohort = cohort_analyst.define(brief, None)
    plan = campaign_architect.design(brief, cohort)
    routes = _paths(plan.flow_spec["steps"], worst_case=False)
    longest = max(routes, key=len, default=[])
    return [(h, ch) for h, ch in longest if ch in CAPPED]


def policy_for(briefs: List[Brief], workspace_policy: Optional[FrequencyPolicy] = None) -> FrequencyPolicy:
    """The program's policy: the workspace's, else the tightest caps among the briefs, with the first brief's windows and priority."""
    if workspace_policy is not None:
        return workspace_policy
    caps = []
    for b in briefs:
        caps += b.freq.caps
    tight: Dict[Tuple[Tuple[str, ...], str], Any] = {}
    for c in caps:
        key = (tuple(sorted(c.channels)), c.window)
        if key not in tight or c.max < tight[key].max:
            tight[key] = c
    base = briefs[0].freq
    return FrequencyPolicy(caps=list(tight.values()), send_windows=base.send_windows, flow_priority=base.flow_priority,
                           collision_rule=base.collision_rule)


def _rank(policy: FrequencyPolicy, flow_type: str) -> int:
    return policy.flow_priority.index(flow_type) if flow_type in policy.flow_priority else len(policy.flow_priority)


def combine(entries: List[Tuple[Brief, float, List[Tuple[float, str]]]], policy: FrequencyPolicy) -> List[Dict[str, Any]]:
    """Merge the sends of several flows one person entered, applying the collision rule. Each entry: (brief, entry hour, sends)."""
    order = sorted(entries, key=lambda e: _rank(policy, e[0].flow_type))
    active: List[Tuple[float, float]] = []      # (start, end) of higher-priority flows already placed
    out: List[Dict[str, Any]] = []
    for b, start, sends in order:
        if not sends:
            continue
        end = start + sends[-1][0]
        if policy.collision_rule == "drop_lower_priority" and any(s <= start <= e for s, e in active):
            continue
        shift = 0.0
        if policy.collision_rule == "queue_lower_priority":
            overlapping = [e for s, e in active if s <= start <= e]
            shift = (max(overlapping) - start + 1.0) if overlapping else 0.0
        for h, ch in sends:
            t = start + shift + h
            if policy.collision_rule == "higher_priority_preempts" and any(s <= t <= e for s, e in active):
                continue
            out.append({"t": t, "channel": ch, "flow": b.slug, "flow_type": b.flow_type})
        active.append((start + shift, end + shift))
    return sorted(out, key=lambda x: x["t"])


def _cap_breaks(sends: List[Dict[str, Any]], policy: FrequencyPolicy) -> List[str]:
    breaks = []
    for c in policy.caps:
        ts = [s["t"] for s in sends if s["channel"] in c.channels]
        worst = max([sum(1 for x in ts if a <= x < a + c.hours) for a in ts] or [0])
        if worst > c.max:
            breaks.append(f"{worst} in {c.window} against {c.describe()}")
    return breaks


def simulate(briefs: List[Brief], workspace_policy: Optional[FrequencyPolicy] = None) -> Dict[str, Any]:
    """Every pair of live flows, entered 0, 24 and 72 hours apart in both orders, with and without the collision rule."""
    policy = policy_for(briefs, workspace_policy)
    sends = {b.slug: _sends(b) for b in briefs}
    checks: List[Dict[str, str]] = []
    scenarios: List[Dict[str, Any]] = []
    for b in briefs:
        own = [{"t": h, "channel": ch} for h, ch in sends[b.slug]]
        breaks = _cap_breaks(own, policy)
        checks.append({"name": f"{b.slug} alone respects the program caps", "status": "fail" if breaks else "pass",
                       "detail": "; ".join(breaks) if breaks else f"{len(own)} send(s)"})
    unmanaged = 0
    for a, b in itertools.combinations(briefs, 2):
        for first, second in ((a, b), (b, a)):
            for off in OFFSETS:
                entries = [(first, 0.0, sends[first.slug]), (second, off, sends[second.slug])]
                raw = sorted([{"t": s + h, "channel": ch, "flow": br.slug} for br, s, ss in entries for h, ch in ss], key=lambda x: x["t"])
                raw_breaks = _cap_breaks(raw, policy)
                managed = combine(entries, policy)
                managed_breaks = _cap_breaks(managed, policy)
                same_day = _same_day(managed)
                unmanaged += bool(raw_breaks)
                scenarios.append({"flows": [first.slug, second.slug], "second_enters_after_hours": off,
                                  "without_rule": raw_breaks, "with_rule": managed_breaks, "same_day_flows": same_day,
                                  "sends_with_rule": len(managed), "sends_without_rule": len(raw)})
    failing = [s for s in scenarios if s["with_rule"]]
    holds = []
    for rule in ("higher_priority_preempts", "queue_lower_priority", "drop_lower_priority"):
        alt = policy.model_copy(update={"collision_rule": rule})
        if not any(_cap_breaks(combine([(x, 0.0, sends[x.slug]), (y, off, sends[y.slug])], alt), alt)
                   for x, y in itertools.permutations(briefs, 2) for off in OFFSETS):
            holds.append(rule)
    from engine.agents.campaign_architect import VOCAB
    esp_caps = sorted({VOCAB[b.platform]["freq"] for b in briefs})
    fix = (f"switch collision_rule to {holds[0].replace('_', ' ')}, which holds every cap in these scenarios, or " if holds else "") + \
          f"turn on the platform's own limit ({'; '.join(esp_caps)}) so the cap is enforced at send time"
    checks.append({"name": f"caps hold across flows with the collision rule ({policy.collision_rule.replace('_', ' ')})",
                   "status": "warn" if failing else "pass",
                   "detail": (("; ".join(f"{' then '.join(s['flows'])} +{s['second_enters_after_hours']:g}h: {s['with_rule'][0]}" for s in failing[:3])
                               + f". To hold the cap: {fix}") if failing else
                              f"{len(scenarios)} scenario(s); {unmanaged} would break a cap without the rule")})
    clashes = [s for s in scenarios if s["same_day_flows"]]
    checks.append({"name": "no two flows send to the same person on the same day", "status": "warn" if clashes else "pass",
                   "detail": ("; ".join(f"{' and '.join(s['same_day_flows'][0])} +{s['second_enters_after_hours']:g}h" for s in clashes[:4])
                              if clashes else "none")})
    status = "fail" if any(c["status"] == "fail" for c in checks) else ("warn" if any(c["status"] == "warn" for c in checks) else "pass")
    return {"status": status, "policy": policy.model_dump(mode="json"), "flows": [b.slug for b in briefs], "checks": checks,
            "rules_that_hold": holds, "scenarios": scenarios}


def _same_day(sends: List[Dict[str, Any]]) -> List[Tuple[str, str]]:
    by_day: Dict[int, set] = {}
    for s in sends:
        by_day.setdefault(int(s["t"] // 24), set()).add(s["flow"])
    return [tuple(sorted(v)) for v in by_day.values() if len(v) > 1]


def active_briefs(folder: Optional[Path] = None) -> List[Brief]:
    from engine import workspace
    from engine.brief import load_brief
    folder = folder or workspace.find().active_briefs
    return [load_brief(p) for p in sorted(Path(folder).glob("*.y*ml")) if p.suffix in (".yaml", ".yml")]


def workspace_policy() -> Optional[FrequencyPolicy]:
    from engine import workspace
    cfg = workspace.find().config().get("frequency")
    return FrequencyPolicy.model_validate(cfg) if cfg else None


def render(r: Dict[str, Any]) -> str:
    mark = {"pass": "PASS", "warn": "NOTE", "fail": "FAIL"}
    lines = [f"Program simulation ({len(r['flows'])} flows): {r['status'].upper()}", ""]
    lines += [f"  {mark[c['status']]:4}  {c['name']}: {c['detail']}" for c in r["checks"]]
    return "\n".join(lines) + "\n"


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Simulate every live flow at once against one frequency policy")
    ap.add_argument("--briefs", help="folder of live briefs (default: briefs/active/ in the workspace)")
    ap.add_argument("--add", action="append", default=[], help="a candidate brief to include (repeatable)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    from engine.brief import load_brief
    briefs = active_briefs(Path(a.briefs) if a.briefs else None) + [load_brief(Path(p)) for p in a.add]
    if len(briefs) < 1:
        print("no live briefs: copy briefs into briefs/active/ (or pass --add)", file=sys.stderr)
        return 2
    r = simulate(briefs, workspace_policy())
    print(json.dumps(r, indent=2) if a.json else render(r))
    return 1 if r["status"] == "fail" else 0


if __name__ == "__main__":
    sys.exit(_cli())
