"""The program simulator: every live flow at once, against one frequency policy.

A flow can pass its own simulation and still break the program: a customer who places a first order, starts another
checkout the next day and comes due for a reorder the week after is in three flows, and three flows that each respect a
weekly cap of 3 can still send them 7 messages. v2 simulated one flow at a time and gave every flow priority 3.

This module compiles every brief in the workspace's briefs/active/ (plus a candidate brief, when planning a new flow),
walks synthetic people who enter several flows close together (every pair and triple at every spacing; larger groups
that could break a cap are searched for a timing that does), applies the policy's collision rule in flow_priority order, and checks every cap in the frequency
policy across all flows:

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


EXHAUSTIVE_UP_TO = 3     # groups this size or smaller are walked at every spacing
SEARCH_BUDGET = 4000     # walks spent searching larger groups for a cap break, so a big program still checks in seconds


def _timings(k: int) -> List[List[float]]:
    """Every way k flows can be entered on the OFFSETS grid, once each up to a shift in time (the first entry is at 0)."""
    return [list(ts) for ts in itertools.product(OFFSETS, repeat=k) if min(ts) == 0.0]


def scenarios(briefs: List[Brief], policy: FrequencyPolicy) -> List[List[Tuple[Brief, float]]]:
    """The walks checked exhaustively: every pair and triple of live flows at every spacing in OFFSETS (0, 24 and 72 hours
    apart, in every order). Larger groups are searched (_search), not enumerated."""
    out: List[List[Tuple[Brief, float]]] = []
    for k in range(2, min(EXHAUSTIVE_UP_TO, len(briefs)) + 1):
        for group in itertools.combinations(briefs, k):
            for ts in _timings(k):
                out.append(sorted(zip(group, ts), key=lambda x: (x[1], x[0].slug)))
    return out


def _window_max(times: List[float], hours: float) -> int:
    return max([sum(1 for x in times if a <= x < a + hours) for a in times] or [0])


def _bound(group, own: Dict[str, Dict[int, int]], policy: FrequencyPolicy) -> float:
    """An upper bound on how far over its cap any entry timing can push the group: each flow contributes at most its own
    densest window (a collision rule can drop or delay sends, never pack a flow's own sends closer). At or under 1, no
    timing can break a cap, so the group need not be walked."""
    return max((sum(own[b.slug][i] for b in group) / c.max for i, c in enumerate(policy.caps)), default=0.0)


def _score(walk, sends, policy: FrequencyPolicy) -> Tuple[float, int, List[Dict[str, Any]]]:
    managed = combine([(br, h, sends[br.slug]) for br, h in walk], policy)
    worst = []
    for c in policy.caps:
        ts = [s["t"] for s in managed if s["channel"] in c.channels]
        worst.append((_window_max(ts, c.hours) / c.max, _window_max(ts, c.hours)))
    top = max(worst, default=(0.0, 0))
    return top[0], sum(w for _, w in worst), managed


def _search(group, sends, policy: FrequencyPolicy, budget: int, rng) -> Tuple[Optional[List[Tuple[Brief, float]]], int]:
    """Look for entry timings on the OFFSETS grid that break a cap: start from the group all entering at once, a day apart
    and three days apart (in priority order and reverse), and two random timings, then move one flow at a time while the
    group's densest window grows. Returns (a breaking walk or None, walks spent)."""
    k = len(group)
    ranked = sorted(range(k), key=lambda i: _rank(policy, group[i].flow_type))
    starts = [[0.0] * k]
    for gap in (24.0, 72.0):
        for order in (ranked, ranked[::-1]):
            ts = [0.0] * k
            for pos, i in enumerate(order):
                ts[i] = min(OFFSETS[-1], pos * gap) if gap == 24.0 else OFFSETS[min(pos, len(OFFSETS) - 1)]
            starts.append(ts)
    starts += [[rng.choice(OFFSETS) for _ in range(k)] for _ in range(2)]
    spent = 0
    walk = lambda ts: sorted(zip(group, ts), key=lambda x: (x[1], x[0].slug))   # noqa: E731
    for ts in starts:
        if spent >= budget:
            break
        cur, spent = list(ts), spent + 1
        best = _score(walk(cur), sends, policy)[:2]
        if best[0] > 1:
            return walk(cur), spent
        improved = True
        while improved and spent < budget:
            improved = False
            for i in range(k):
                for v in OFFSETS:
                    if v == cur[i] or spent >= budget:
                        continue
                    cand = cur[:i] + [v] + cur[i + 1:]
                    s = _score(walk(cand), sends, policy)[:2]
                    spent += 1
                    if s[0] > 1:
                        return walk(cand), spent
                    if s > best:
                        cur, best, improved = cand, s, True
    return None, spent


def _label(s: Dict[str, Any]) -> str:
    return ", ".join(f"{f} {'at entry' if h == 0 else f'+{h:g}h'}" for f, h in zip(s["flows"], s["entry_hours"]))


def _walk_record(walk, sends, policy: FrequencyPolicy) -> Dict[str, Any]:
    entries = [(br, h, sends[br.slug]) for br, h in walk]
    raw = sorted([{"t": s + h, "channel": ch, "flow": br.slug} for br, s, ss in entries for h, ch in ss], key=lambda x: x["t"])
    managed = combine(entries, policy)
    return {"flows": [br.slug for br, _ in walk], "entry_hours": [h for _, h in walk],
            "without_rule": _cap_breaks(raw, policy), "with_rule": _cap_breaks(managed, policy), "same_day_flows": _same_day(managed),
            "sends_with_rule": len(managed), "sends_without_rule": len(raw)}


def simulate(briefs: List[Brief], workspace_policy: Optional[FrequencyPolicy] = None) -> Dict[str, Any]:
    """Every group of live flows one person could be in: pairs and triples at every spacing, larger groups searched for a
    timing that breaks a cap (skipping groups that cannot, and stopping at SEARCH_BUDGET walks). A cap broken even with
    the collision rule is a warning: the program sends more than its policy allows."""
    import random
    policy = policy_for(briefs, workspace_policy)
    sends = {b.slug: _sends(b) for b in briefs}
    own = {b.slug: {i: _window_max([h for h, ch in sends[b.slug] if ch in c.channels], c.hours) for i, c in enumerate(policy.caps)}
           for b in briefs}
    checks: List[Dict[str, str]] = []
    for b in briefs:
        mine = [{"t": h, "channel": ch} for h, ch in sends[b.slug]]
        breaks = _cap_breaks(mine, policy)
        checks.append({"name": f"{b.slug} alone respects the program caps", "status": "fail" if breaks else "pass",
                       "detail": "; ".join(breaks) if breaks else f"{len(mine)} send(s)"})
    walks = scenarios(briefs, policy)
    out_scenarios = [_walk_record(w, sends, policy) for w in walks]
    unmanaged = sum(bool(s["without_rule"]) for s in out_scenarios)
    # larger groups: only those whose flows could exceed a cap together, most at risk first, within the budget
    larger = [g for k in range(EXHAUSTIVE_UP_TO + 1, len(briefs) + 1) for g in itertools.combinations(briefs, k)
              if _bound(g, own, policy) > 1]
    larger.sort(key=lambda g: (-_bound(g, own, policy), len(g)))
    rng = random.Random(0)
    spent, searched, found = 0, 0, []
    for g in larger:
        if spent >= SEARCH_BUDGET:
            break
        hit, used = _search(g, sends, policy, SEARCH_BUDGET - spent, rng)
        spent += used
        searched += 1
        if hit is not None:
            found.append(hit)
            out_scenarios.append(_walk_record(hit, sends, policy))
    failing = sorted([s for s in out_scenarios if s["with_rule"]], key=lambda s: (len(s["flows"]), s["entry_hours"]))
    holds = []
    checked = walks + found
    for rule in ("higher_priority_preempts", "queue_lower_priority", "drop_lower_priority"):
        alt = policy.model_copy(update={"collision_rule": rule})
        if not any(_cap_breaks(combine([(br, h, sends[br.slug]) for br, h in w], alt), alt) for w in checked):
            holds.append(rule)
    from engine.agents.campaign_architect import VOCAB
    esp_caps = sorted({VOCAB[b.platform]["freq"] for b in briefs})
    fix = (f"switch collision_rule to {holds[0].replace('_', ' ')}, which holds every cap in these scenarios, or " if holds else "") + \
          f"turn on the platform's own limit ({'; '.join(esp_caps)}) so the cap is enforced at send time"
    coverage = ""
    if larger:
        coverage = (f"; {searched} larger group(s) that could break a cap searched ({spent} walks)" if searched == len(larger) else
                    f"; {searched} of {len(larger)} larger groups that could break a cap searched before the {SEARCH_BUDGET}-walk limit, "
                    f"the most at risk first")
    checks.append({"name": f"caps hold across flows with the collision rule ({policy.collision_rule.replace('_', ' ')})",
                   "status": "warn" if failing else "pass",
                   "detail": (("; ".join(f"{_label(s)}: {s['with_rule'][0]}" for s in failing[:3])
                               + (f" ({len(failing)} scenarios in all)" if len(failing) > 3 else "") + f". To hold the cap: {fix}")
                              if failing else
                              f"{len(walks)} pair and triple scenario(s); {unmanaged} would break a cap without the rule" + coverage)})
    clashes = [s for s in out_scenarios if s["same_day_flows"] and len(s["flows"]) == 2]
    checks.append({"name": "no two flows send to the same person on the same day", "status": "warn" if clashes else "pass",
                   "detail": ("; ".join(f"{' and '.join(s['same_day_flows'][0])} ({_label(s)})" for s in clashes[:4])
                              if clashes else "none")})
    status = "fail" if any(c["status"] == "fail" for c in checks) else ("warn" if any(c["status"] == "warn" for c in checks) else "pass")
    return {"status": status, "policy": policy.model_dump(mode="json"), "flows": [b.slug for b in briefs], "checks": checks,
            "rules_that_hold": holds, "scenarios": out_scenarios, "larger_groups": {"could_break": len(larger), "searched": searched,
                                                                                    "walks": spent}}


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
    mark = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}
    lines = [f"Program simulation ({len(r['flows'])} flows): {r['status'].upper()}", ""]
    lines += [f"  {mark[c['status']]:4}  {c['name']}: {c['detail']}" for c in r["checks"]]
    return "\n".join(lines) + "\n"


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="lcm simulate --program", description="Simulate every live flow at once against one frequency "
                                 "policy. Exit 0 when every check passes, 1 when a cap or a same-day clash needs attention, 2 on error.")
    ap.add_argument("--briefs", help="folder of live briefs (default: briefs/active/ in the workspace)")
    ap.add_argument("--add", action="append", default=[], help="a candidate brief to include (repeatable)")
    ap.add_argument("--json", action="store_true", help="print the checks and every scenario as JSON")
    a = ap.parse_args(argv)
    from engine.brief import load_brief
    briefs = active_briefs(Path(a.briefs) if a.briefs else None) + [load_brief(Path(p)) for p in a.add]
    if len(briefs) < 1:
        print("No live flows to check: copy the briefs of flows that are live into briefs/active/ (or pass --add).")
        return 0
    r = simulate(briefs, workspace_policy())
    print(json.dumps(r, indent=2) if a.json else render(r))
    return 0 if r["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(_cli())
