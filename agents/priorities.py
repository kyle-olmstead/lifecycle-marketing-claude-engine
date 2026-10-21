"""Today's priorities: the builds most worth doing next, ranked from your own data, each with its why and its size.

Ask "what are my top 3 priority builds today and why?" (lcm, the plugin, or the MCP server), or read the top of the
05:00 daily check, which runs this every morning. Nothing here is a guess dressed as a forecast:

  * Every candidate comes from something measured in the data: people stalling at an onboarding step, people
    lapsing with no flow catching them, a calendar send reaching most of its audience late, a sending domain or a list
    in trouble.
  * Its size is people affected a month (measured) x the share who get there on their own (measured) x a lift range
    (assumed: 10 to 25 percent relative by default, the size of effect the engine plans tests to detect) x the value of
    one conversion (yours, or estimated from orders). The assumptions are printed next to every number.
  * Confidence says how much data stands behind the size; effort says how big the build is.
  * Problems that cap everything else (a sending domain that is RED, a list that is RED on engagement, data that has
    stopped arriving) are listed first as fixes, because no new build pays while they stand.

Ranking: fixes first, then builds by mid-range monthly value (or conversions, when no value is known) x a confidence
weight (high 1.0, medium 0.7, low 0.4) / an effort weight (S 1.0, M 1.5, L 2.0). Every build comes with a brief written
to <out>/priorities/<key>.yaml, so one command turns the idea into a plan.

    python3 -m agents.priorities --data exports/                 # top 3
    python3 -m agents.priorities --data exports/ --top 5 --value-per-conversion 120
    lcm "what are my top 3 priority builds today and why?" --data exports/
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.models import Brief, EventMatch  # noqa: E402

LIFT_LOW, LIFT_HIGH = 0.10, 0.25
CONFIDENCE_WEIGHT = {"high": 1.0, "medium": 0.7, "low": 0.4}
EFFORT_WEIGHT = {"S": 1.0, "M": 1.5, "L": 2.0}


class Opportunity(BaseModel):
    key: str
    kind: Literal["fix", "build"]
    title: str
    why: List[str] = Field(default_factory=list, description="the evidence, each line with its source label")
    people_per_month: Optional[float] = None
    baseline_rate: Optional[float] = None
    lift_low: Optional[float] = None
    lift_high: Optional[float] = None
    conversions_low: Optional[float] = None
    conversions_high: Optional[float] = None
    value_per_conversion: Optional[float] = None
    value_label: Optional[str] = None
    revenue_low: Optional[float] = None
    revenue_high: Optional[float] = None
    confidence: Literal["high", "medium", "low"] = "medium"
    confidence_why: str = ""
    effort: Literal["S", "M", "L"] = "M"
    effort_why: str = ""
    measurable: Optional[str] = None
    score: float = 0.0
    next_step: str = ""
    brief_path: Optional[str] = None
    confirm: List[str] = Field(default_factory=list)
    rank: Optional[int] = None


# ----------------------------------------------------------------------------------------------
# inputs measured from the data
# ----------------------------------------------------------------------------------------------

def value_per_conversion(con, as_of: date) -> Optional[float]:
    """Monthly net revenue per paying customer over the 90 days to as_of: a stand-in for one converted person's value."""
    row = con.execute(
        f"SELECT SUM(CAST(net_amount AS DOUBLE)), COUNT(DISTINCT user_id) FROM orders "
        f"WHERE status = 'completed' AND CAST(order_at AS DATE) > DATE '{(as_of - timedelta(days=90)).isoformat()}' "
        f"AND CAST(order_at AS DATE) <= DATE '{as_of.isoformat()}'").fetchone()
    total, payers = row if row else (None, 0)
    if not total or not payers:
        return None
    return round(float(total) / payers / 3, 2)


def _variant(base: Brief, flow_type: str) -> Optional[Brief]:
    """The same product and core action, as a win-back or an activation flow, so the Analyst can size it."""
    data = base.model_dump()
    data["flow_type"] = flow_type
    data["assumptions"] = []
    for k in ("monthly_entrants", "baseline_conversion_rate", "outcome_window_days"):
        data[k] = None
    core = base.core_conversion_event
    if flow_type == "activation":
        data.update(qualifying_event=EventMatch(event="signed_up", label="signed up").model_dump(), touches=3,
                    churn_threshold_days=max(7, min(base.churn_threshold_days, 14)))
        if core.event == "signed_up":
            return None
    else:
        q = base.qualifying_event if base.flow_type == "winback" else EventMatch(event="activation_reached", label="reached activation")
        if (q.event, q.where) == (core.event, core.where):
            q = EventMatch(event="signed_up", label="signed up")
        data.update(qualifying_event=q.model_dump(), touches=5)
    try:
        return Brief.model_validate(data)
    except ValueError:
        return None


def _confidence(people: Optional[float], basis: str, label: str) -> (str, str):
    import re
    m = re.search(r"(\d[\d,]*) of (\d[\d,]*)", basis or "")
    matured = int(m.group(2).replace(",", "")) if m else 0
    if people is None or not matured:
        return "low", "the data could not size this audience"
    level = "high" if people >= 100 and matured >= 200 else ("medium" if people >= 20 and matured >= 50 else "low")
    if label == "DEMO DATA" and level == "high":
        level = "medium"
    return level, f"{people:,.0f} people a month and {matured:,} past cases behind the baseline, {label}"


def _size(o: Opportunity, people: Optional[float], baseline: Optional[float], value: Optional[float], value_label: str,
          lift_low: float, lift_high: float) -> None:
    o.people_per_month, o.baseline_rate, o.lift_low, o.lift_high = people, baseline, lift_low, lift_high
    if people is None or baseline is None:
        return
    o.conversions_low = round(people * baseline * lift_low, 1)
    o.conversions_high = round(people * baseline * lift_high, 1)
    if value:
        o.value_per_conversion, o.value_label = value, value_label
        o.revenue_low = round(o.conversions_low * value, 0)
        o.revenue_high = round(o.conversions_high * value, 0)


def _measurable(people: Optional[float], baseline: Optional[float], lift: float) -> Optional[str]:
    if not people or not baseline:
        return None
    from tools import stats
    f = stats.feasibility(entries_per_month=people, holdout_share=0.15, p_control=baseline, mde_rel=lift)
    months = f.get("months_to_read")
    return (f"a 15% holdout could prove a {lift:.0%} lift in about {months:.0f} months" if f["expected_read"].startswith("causal")
            else f"too few people to prove a {lift:.0%} lift inside a year: results will be directional")


# ----------------------------------------------------------------------------------------------
# candidates
# ----------------------------------------------------------------------------------------------

def _build_candidates(briefs: List[Brief], live: List[Brief], data: str, as_of: Optional[str], value: Optional[float],
                      value_label: str, lift_low: float, lift_high: float, out: Optional[Path]) -> List[Opportunity]:
    from agents import cohort_analyst
    from agents.brief import to_yaml
    found: List[Opportunity] = []
    seen_core = set()
    for base in briefs:
        core = base.core_conversion_event
        ck = (core.event, tuple(sorted(core.where.items())))
        if ck in seen_core:
            continue
        seen_core.add(ck)
        live_types = {b.flow_type for b in live if (b.core_conversion_event.event, tuple(sorted(b.core_conversion_event.where.items()))) == ck}
        did = core.label or core.describe()
        for flow_type in ("activation", "winback"):
            if flow_type in live_types:
                continue
            b = _variant(base, flow_type)
            if b is None:
                continue
            try:
                cohort = cohort_analyst.define(b, data, as_of)
            except (ValueError, FileNotFoundError):
                continue
            me, br = cohort.monthly_entrants, cohort.baseline_conversion_rate
            people, baseline = me.value, br.value
            if not people:
                continue
            if flow_type == "activation":
                title = f"Activation flow: get new signups to {core.do}" + (f", aimed at {cohort.leak.split(' (')[0]}" if cohort.leak else "")
                why = [f"{people:,.0f} people sign up each month ({me.label}); only {baseline:.1%} {did} within "
                       f"{b.churn_threshold_days} days on their own ({br.label})" if baseline is not None else
                       f"{people:,.0f} people sign up each month ({me.label})"]
                if cohort.leak:
                    why.append(f"the largest drop in onboarding: {cohort.leak} ({me.label})")
                effort, effort_why = "M", "3 touches, email and in-app"
            else:
                title = f"Win-back: bring back people {b.churn_threshold_days} days after they last {did}"
                why = [f"{people:,.0f} people a month reach {b.churn_threshold_days} days since they last {did} ({me.label}); "
                       f"{baseline:.1%} come back within 30 days with nothing sent ({br.label})" if baseline is not None else
                       f"{people:,.0f} people a month lapse ({me.label})"]
                effort, effort_why = "M", "5 touches, email and in-app, ending with re-permission"
                why += _blast_evidence(b, data)
            why.append("no live flow in briefs/active/ catches them")
            o = Opportunity(key=f"{b.slug}", kind="build", title=title, why=why, effort=effort, effort_why=effort_why)
            _size(o, people, baseline, value, value_label, lift_low, lift_high)
            o.confidence, o.confidence_why = _confidence(people, br.basis, br.label)
            o.measurable = _measurable(people, baseline, lift_high)
            o.confirm = [f"the core action is right: {core.detail()}",
                         f"the lift range ({lift_low:.0%} to {lift_high:.0%} relative) is an assumption, not a measurement"]
            if out is not None:
                (out / "priorities").mkdir(parents=True, exist_ok=True)
                path = out / "priorities" / f"{o.key}.yaml"
                path.write_text(to_yaml(b), encoding="utf-8")
                o.brief_path = str(path)
                o.next_step = f'lcm {path} --data {data}'
            found.append(o)
    return found


def _repeat_purchase(con, as_of: date, data_label: str, lift_low: float, lift_high: float, window: int = 60,
                     supplied: Optional[float] = None) -> Optional[Opportunity]:
    """First-time buyers who never place a second order: a post-purchase flow, sized from the orders table."""
    a = f"DATE '{as_of.isoformat()}'"
    row = con.execute(f"""
        WITH o AS (SELECT user_id, CAST(order_at AS DATE) AS d, CAST(net_amount AS DOUBLE) AS v FROM orders WHERE status = 'completed'
                   AND CAST(order_at AS DATE) <= {a}),
             f AS (SELECT user_id, MIN(d) AS first_d FROM o GROUP BY user_id),
             recent AS (SELECT COUNT(*) AS n FROM f WHERE first_d > {a} - INTERVAL 90 DAY),
             matured AS (SELECT f.user_id, MAX(CASE WHEN o.d > f.first_d AND o.d <= f.first_d + INTERVAL {window} DAY THEN 1 ELSE 0 END) AS again
                         FROM f JOIN o USING (user_id)
                         WHERE f.first_d <= {a} - INTERVAL {window} DAY AND f.first_d > {a} - INTERVAL 365 DAY GROUP BY f.user_id),
             aov AS (SELECT AVG(v) AS aov FROM o WHERE d > {a} - INTERVAL 90 DAY)
        SELECT (SELECT n FROM recent), (SELECT COUNT(*) FROM matured), (SELECT SUM(again) FROM matured), (SELECT aov FROM aov)""").fetchone()
    recent, matured, again, aov = row
    if not matured or matured < 20 or not recent:
        return None
    people = recent / 3
    baseline = again / matured
    o = Opportunity(key="post_purchase_second_order", kind="build", title="Post-purchase flow: turn first orders into second orders",
                    why=[f"{people:,.0f} new customers a month place a first order ({data_label})",
                         f"{baseline:.1%} of first-time buyers order again within {window} days with nothing sent ({again} of {matured}, {data_label})"],
                    effort="S", effort_why="3 emails after the first order: thank-you and setup, a use case, a second-order prompt")
    if supplied is not None:
        _size(o, people, baseline, supplied, "SUPPLIED", lift_low, lift_high)
    else:
        _size(o, people, baseline, round(aov, 2) if aov else None, "ESTIMATE: average order value, last 90 days", lift_low, lift_high)
    o.confidence, o.confidence_why = _confidence(people, f"{again} of {matured}", data_label)
    o.measurable = _measurable(people, baseline, lift_high)
    o.confirm = ["a second order is the goal (not a subscription renewal or an upgrade)",
                 f"the lift range ({lift_low:.0%} to {lift_high:.0%} relative) is an assumption, not a measurement"]
    o.next_step = 'ask Claude: "plan a 3-touch post-purchase flow that turns first orders into second orders" (lifecycle-flows)'
    return o


def _blast_evidence(b: Brief, data: str) -> List[str]:
    """What the calendar send reaches, against this trigger: an observed difference, not a test result."""
    from agents import cohort_analyst
    try:
        r = cohort_analyst.blast_vs_trigger(b, data)
    except (ValueError, FileNotFoundError):
        return []
    blasts = [x for x in r.get("blasts", []) if x.get("recipients")]
    if not blasts:
        return []
    late = sum(x["bands"]["late"]["recipients"] for x in blasts if "late" in x["bands"]) / sum(x["recipients"] for x in blasts)
    def rate(band: str) -> Optional[float]:
        n = sum(x["bands"][band]["recipients"] for x in blasts if band in x["bands"])
        k = sum(x["bands"][band]["acted_within_window"] for x in blasts if band in x["bands"])
        return k / n if n >= 10 else None
    on, lt = rate("on time"), rate("late")
    line = f"a monthly send to everyone qualified reaches {late:.0%} of them late, more than a week past {b.churn_threshold_days} days"
    if on is not None and lt is not None:
        line += f"; people reached on time acted {on:.0%} of the time, people reached late {lt:.0%}"
    from agents import cohort_analyst
    return [f"{line} (observed, not a test; {cohort_analyst.data_label(data)})"]


def _fix_candidates(audits: List[Dict[str, Any]], engagement: Optional[Dict[str, Any]],
                    deliverability: Optional[Dict[str, Dict[str, Any]]], data: str = "<exports>") -> (List[Opportunity], List[str]):
    fixes: List[Opportunity] = []
    also: List[str] = []
    for domain, r in (deliverability or {}).items():
        verdict = r["gate"]["verdict"]
        crit = [f for f in r["findings"] if f["severity"] == "critical"]
        if verdict == "RED":
            fixes.append(Opportunity(key=f"fix_domain_{domain}", kind="fix", title=f"Fix sending for {domain} before building anything",
                                     why=["sending gate RED: " + "; ".join(r["gate"]["reasons"][:3])] + [f"critical: {f['title']}" for f in crit[:3]],
                                     confidence="high", confidence_why="read from live DNS and supplied signals", effort="S",
                                     effort_why="DNS records and list fixes, written out by the check",
                                     next_step=f'lcm "check deliverability for {domain}"'))
        elif verdict == "AMBER":
            also.append(f"{domain} is AMBER on the sending gate: hold volume flat and fix what the check lists (lcm \"check deliverability for {domain}\")")
    if engagement:
        band, share = engagement.get("gate_band"), engagement.get("unengaged_share")
        if band == "RED":
            fixes.append(Opportunity(key="fix_unengaged", kind="fix", title="Sunset the unengaged before scaling any campaign",
                                     why=[f"{share:.0%} of the people you can email got 5 or more emails in six months and clicked none "
                                          f"({engagement.get('sunset_cohort', 0):,} people); over 50% is RED on the sending gate"],
                                     confidence="high", confidence_why="counted from message events", effort="S",
                                     effort_why="sync the sunset flag and exclude it from marketing sends",
                                     next_step=f'lcm "build the engagement tiers and the sunset list" --data {data} --platform <your ESP>'))
        elif band == "AMBER":
            also.append(f"{share:.0%} of your list is unengaged (AMBER): plan a re-permission send and a sunset within a month")
    for a in audits:
        for x in a.get("anomalies", []):
            if x["metric"] == "data_freshness":
                fixes.append(Opportunity(key=f"fix_data_{a['flow']}", kind="fix", title="Get the data flowing again",
                                         why=[x.get("reason", "the newest event is too old")], confidence="high",
                                         confidence_why="the newest event date in the data", effort="S",
                                         effort_why="usually a stalled warehouse sync or tracking deploy",
                                         next_step="check the sync from your CDP or event source into the warehouse"))
            else:
                also.append(f"{a['flow']}: {x['metric']} is unusual today ({x.get('reason') or 'outside its 28-day range'})")
    return fixes, also


# ----------------------------------------------------------------------------------------------
# the ranking
# ----------------------------------------------------------------------------------------------

def rank(found: List[Opportunity], top: int) -> List[Opportunity]:
    for o in found:
        if o.kind == "fix":
            o.score = float("inf")
            continue
        mid = ((o.revenue_low or 0) + (o.revenue_high or 0)) / 2 if o.revenue_high is not None else \
              ((o.conversions_low or 0) + (o.conversions_high or 0)) / 2
        o.score = round(mid * CONFIDENCE_WEIGHT[o.confidence] / EFFORT_WEIGHT[o.effort], 2)
    ordered = sorted(found, key=lambda o: (o.kind != "fix", -o.score if o.score != float("inf") else 0, o.key))
    for i, o in enumerate(ordered, 1):
        o.rank = i
    return ordered[:top] if top else ordered


def run(data: str, briefs: Optional[List[Brief]] = None, live: Optional[List[Brief]] = None, as_of: Optional[str] = None,
        top: int = 3, value: Optional[float] = None, lift_low: float = LIFT_LOW, lift_high: float = LIFT_HIGH,
        audits: Optional[List[Dict[str, Any]]] = None, engagement: Optional[Dict[str, Any]] = None,
        deliverability: Optional[Dict[str, Dict[str, Any]]] = None, out: Optional[Path] = None) -> Dict[str, Any]:
    """briefs: what defines your core actions (the active briefs, or the example); live: the flows already running."""
    from agents import cohort_analyst
    from agents.brief import load_brief
    if briefs is None:
        active = sorted((ROOT / "briefs" / "active").glob("*.yaml"))
        live = [load_brief(p) for p in active] if live is None else live
        briefs = live or [load_brief(ROOT / "briefs" / "examples" / "winback_14d.yaml")]
    live = live or []
    notes: List[str] = []
    if not live:
        notes.append("no live flows are listed in briefs/active/, so every candidate is a new build")
    if not 0 < lift_low <= lift_high:
        raise ValueError("the lift range must satisfy 0 < low <= high")
    con = cohort_analyst.connect(data)
    as_of_date = date.fromisoformat(as_of) if as_of else cohort_analyst.latest_complete_day(con)[0]
    if value is not None:
        value_label = "SUPPLIED"
    else:
        value = value_per_conversion(con, as_of_date)
        value_label = "ESTIMATE: monthly revenue per paying customer, last 90 days; counts one month of value per person"
        if value is None:
            notes.append("no completed orders in the last 90 days: sizes are in conversions; give --value-per-conversion for dollars")
    if value is not None and value <= 0:
        raise ValueError("the value of a conversion must be positive")
    builds = _build_candidates(briefs, live, data, as_of_date.isoformat(), value, value_label, lift_low, lift_high, out)
    repeat = _repeat_purchase(con, as_of_date, cohort_analyst.data_label(data), lift_low, lift_high,
                              supplied=value if value_label == "SUPPLIED" else None)
    if repeat is not None:
        builds.append(repeat)
    if audits is None and live:
        audits = []
        for b in live:
            try:
                audits.append(cohort_analyst.audit(b, data))
            except (ValueError, FileNotFoundError) as e:
                notes.append(f"could not audit {b.slug}: {e}")
    if engagement is None:
        try:
            from agents import engagement as eng
            if con.execute("SELECT COUNT(*) FROM message_events").fetchone()[0]:
                engagement = eng.run(data)["summary"]
        except Exception as e:  # noqa: BLE001 - the engagement read is a bonus; a failure must not hide the builds
            notes.append(f"engagement tiers not read: {type(e).__name__}")
    fixes, also = _fix_candidates(audits or [], engagement, deliverability, data)
    ranked = rank(fixes + builds, 0)
    return {"as_of": as_of_date.isoformat(), "data_label": cohort_analyst.data_label(data), "top": ranked[:top],
            "others": ranked[top:], "also_today": also, "notes": notes,
            "method": {"lift_range": [lift_low, lift_high], "value_per_conversion": value, "value_label": value_label if value else None,
                       "confidence_weights": CONFIDENCE_WEIGHT, "effort_weights": EFFORT_WEIGHT}}


def _usd(v: Optional[float]) -> str:
    return "unknown" if v is None else f"${v:,.0f}"


def render(r: Dict[str, Any], top: Optional[int] = None) -> str:
    items = r["top"]
    L = [f"## Top {len(items)} priorit{'y' if len(items) == 1 else 'ies'} today ({r['as_of']})", "",
         "*Fixes that cap everything else come first; builds follow, ranked by expected monthly value x confidence / effort.*", ""]
    if not items:
        L += ["Nothing to build or fix stood out in the data today.", ""]
    for o in items:
        o = o if isinstance(o, Opportunity) else Opportunity(**o)
        L.append(f"### {o.rank}. {o.title}" + ("  (fix first)" if o.kind == "fix" else ""))
        L.append("")
        L += [f"- {w}" for w in o.why]
        if o.kind == "build":
            if o.revenue_high is not None:
                size = (f"about {_usd(o.revenue_low)} to {_usd(o.revenue_high)} a month ({_usd(o.revenue_low * 12)} to "
                        f"{_usd(o.revenue_high * 12)} a year)")
            elif o.conversions_high is not None:
                size = f"about {o.conversions_low:,.1f} to {o.conversions_high:,.1f} more conversions a month"
            else:
                size = "not sized: the data could not measure the audience"
            L.append(f"- **Size:** {size}. "
                     + (f"That is {o.people_per_month:,.0f} people a month x {o.baseline_rate:.1%} baseline x a "
                        f"{o.lift_low:.0%} to {o.lift_high:.0%} lift (assumed)"
                        + (f" x {_usd(o.value_per_conversion)} per conversion ({o.value_label})" if o.value_per_conversion else "") + "."
                        if o.conversions_high is not None else ""))
        L.append(f"- **Confidence:** {o.confidence} ({o.confidence_why}). **Effort:** {o.effort} ({o.effort_why}).")
        if o.measurable:
            L.append(f"- **Can it be proven?** {o.measurable}.")
        if o.next_step:
            L.append(f"- **Next step:** `{o.next_step}`")
        if o.confirm:
            L.append("- **Confirm:** " + "; ".join(o.confirm) + ".")
        L.append("")
    if r.get("also_today"):
        L += ["**Also today:**", ""] + [f"- {x}" for x in r["also_today"]] + [""]
    others = r.get("others") or []
    if others:
        L += ["**Next in line:** " + "; ".join((o.title if isinstance(o, Opportunity) else o["title"]) for o in others[:5]) + ".", ""]
    if r.get("notes"):
        L += ["*" + " ".join(n[:1].upper() + n[1:] + "." for n in r["notes"]) + "*", ""]
    L.append("*Sizes are estimates, not forecasts: the people and baselines are measured, the lift is assumed. A holdout on "
             "each build is what turns the estimate into a result.*")
    return "\n".join(L) + "\n"


def to_json(r: Dict[str, Any]) -> Dict[str, Any]:
    dump = lambda xs: [x.model_dump() if isinstance(x, Opportunity) else x for x in xs]  # noqa: E731
    out = {**r, "top": dump(r["top"]), "others": dump(r["others"])}
    for xs in (out["top"], out["others"]):
        for x in xs:
            if x["score"] == float("inf"):
                x["score"] = None
    return out


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Today's priorities: the builds most worth doing next, ranked from your data")
    ap.add_argument("--data", required=True, help="folder or URL prefix with users, orders, track_events (and message_events)")
    ap.add_argument("--brief", action="append", default=[], help="a brief defining a core action (default: briefs/active/, else the example)")
    ap.add_argument("--as-of")
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--value-per-conversion", type=float, help="what one more converted person is worth to you, in dollars")
    ap.add_argument("--lift-low", type=float, default=LIFT_LOW)
    ap.add_argument("--lift-high", type=float, default=LIFT_HIGH)
    ap.add_argument("--out", default="out")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    from agents.brief import load_brief
    try:
        briefs = [load_brief(Path(p)) for p in a.brief] or None
        r = run(a.data, briefs, None if briefs is None else [], a.as_of, a.top, a.value_per_conversion, a.lift_low, a.lift_high,
                out=Path(a.out))
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    folder = Path(a.out) / "priorities"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "priorities.json").write_text(json.dumps(to_json(r), indent=2, default=str), encoding="utf-8")
    (folder / "priorities.md").write_text(render(r), encoding="utf-8")
    print(json.dumps(to_json(r), indent=2, default=str) if a.json else render(r))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
