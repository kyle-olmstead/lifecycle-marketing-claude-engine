"""Experimentation Agent: attaches a persistent holdout to the flow and says when it can be read.

The holdout is designed before the flow ships, never after: 15% of entrants by default, assigned
by a salted hash of the user id so the same person is in the same arm in the warehouse, the ESP and
the readout (engine/sql/dialects.py). Holdout members still enter the flow and exit at the first
step, so both arms are the same population at the moment they diverge.

Sizing comes from engine/measurement/stats.py, not from the model: sample size for the unequal split, months
of entries needed to fill the holdout, plus the outcome window. When entry volume or the baseline
is unknown, the agent does not guess. It marks the read "not readable" until they are supplied
and prints a sensitivity grid so the marketer can see what volume a read would need.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.models import Brief, CohortDefinition, ExperimentDesign  # noqa: E402
from engine.sql.dialects import dialect  # noqa: E402
from engine.measurement import stats  # noqa: E402

GRID_ENTRANTS = (250, 1000, 5000)
GRID_BASELINES = (0.05, 0.10, 0.20)


def design(brief: Brief, cohort: CohortDefinition) -> ExperimentDesign:
    pct = brief.holdout_pct
    share = pct / 100
    salt = brief.slug
    from engine.agents.campaign_architect import outcome_window
    w = outcome_window(brief)
    core = brief.core_conversion_event
    hash_field = "account_id" if brief.randomization_unit == "account" else "user_id"
    hash_expr = "COALESCE(account_id, user_id)" if hash_field == "account_id" else "user_id"
    assignment_sql = {d: f"{dialect(d).holdout_bucket(hash_expr, salt)} < {pct:g}" for d in ("postgres", "snowflake", "bigquery")}
    cl = cohort.cluster or {}
    deff, m_bar = 1.0, None
    deff_note = ""
    if hash_field == "account_id":
        if cl.get("design_effect"):
            deff, m_bar = float(cl["design_effect"]), cl.get("mean_size")
            deff_note = (f"; design effect {deff:.2f} from the data (ICC {cl.get('icc_for_design', cl.get('icc')):.3f} across "
                         f"{cl.get('clusters')} accounts, {cl.get('label', 'ESTIMATE')})")
        else:
            from engine.measurement.cluster import design_effect as _deff
            m_bar = 3.0
            deff = _deff(0.05, mean_cluster_size=m_bar)
            deff_note = (f"; design effect {deff:.2f} ASSUMED (ICC 0.05, 3 people per account) because the data could not measure it: "
                         f"run with --data to measure it")
    where = ("computed in the daily queue query and pushed on the trigger event as in_holdout" if brief.flow_type == "winback" else
             f"added as in_holdout to the live {brief.qualifying_event.event} event by the system that sends it")
    rule = (f"persistent, by {brief.randomization_unit}: bucket = first 7 hex digits of md5({hash_field} || ':{salt}') mod 100; "
            f"bucket < {pct:g} is the holdout, {where}; holdout members enter, then exit at holdout_split, so both arms are the "
            f"same population")
    primary = (f"{core.describe()} ({core.detail()}) within {w} days of entry, treatment vs holdout, both arms counted from their entry date")
    entries, base = cohort.monthly_entrants.value, cohort.baseline_conversion_rate.value
    e_label, b_label = cohort.monthly_entrants.label, cohort.baseline_conversion_rate.label
    feas = None
    mde_rows: List[Dict[str, Any]] = []
    grid: List[Dict[str, Any]] = []

    if pct == 0:
        why = brief.flow.holdout_reason_if_none or "no holdout by request: the flow can only be compared with a pre/post baseline"
        read, sizing, read_after = "directional only", why, \
            "a pre/post comparison after one full outcome window; directional only, no lift percentage"
    elif entries and base and base * (1 + brief.min_detectable_lift) >= 1:
        read = "not readable"
        sizing = (f"{base:.1%} of entrants ({b_label}) already {core.do} within {w} days with no flow, so a "
                  f"{brief.min_detectable_lift:.0%} relative lift would pass 100%: there is no room to measure. Narrow the core action "
                  f"or the entry rule so fewer people do it on their own, or shorten the outcome window")
        read_after = "never as defined: the baseline leaves no room for the lift"
    elif entries and base:
        feas = stats.feasibility(entries, share, base, mde_rel=brief.min_detectable_lift, outcome_window_days=w,
                                 design_effect=deff, mean_cluster_size=m_bar)
        read = feas["expected_read"]
        months = feas["months_to_read"]
        lift_abs = base * brief.min_detectable_lift
        sizing = (f"about {entries:,.0f} entrants a month ({e_label}), {base:.1%} return within {w} days with no flow ({b_label}), "
                  f"{brief.min_detectable_lift:.0%} relative lift ({lift_abs * 100:.1f} pts) worth acting on: "
                  f"{feas['required_holdout_n']:,} holdout entrants at {pct:g}%, "
                  + (f"{feas['enrollment_months']} months of entries plus the {w}-day outcome window, so the read lands about {months} months "
                     f"after launch" if months is not None else "which this volume never reaches")
                  + f" (engine/measurement/stats.py feasible --entries-per-month {entries:g} --holdout {share:g} --baseline {base:g} "
                    f"--mde-rel {brief.min_detectable_lift:g} --outcome-window-days {w})")
        if read in ("causal", "causal, slow"):
            read_after = f"{feas['required_holdout_n']:,} holdout entrants have each completed {w} days (about {months} months after launch)"
        elif read == "directional only":
            read_after = (f"not inside 12 months at this volume (it would take about {months:.0f} months): keep the holdout, report against a "
                          f"pre/post baseline, and label every result directional")
        else:
            read_after = "never at this volume: report counts qualitatively and do not compute a lift percentage"
        for horizon in (3, 6, 12):
            total = entries * horizon
            n_c, n_t = int(total * share), int(total - total * share)
            if n_c < 1 or n_t < 1:
                continue
            m = stats.minimum_detectable_effect(n_c, n_t, base)
            mde_rows.append({"months_of_entries": horizon, "holdout_n": n_c, "treatment_n": n_t,
                             "mde_abs_pts": None if m["mde_abs"] is None else round(m["mde_abs"] * 100, 2),
                             "mde_rel": m["mde_rel"]})
    else:
        read = "not readable"
        missing = [n for n, v in (("monthly entrants", entries), ("baseline return rate", base)) if not v]
        sizing = (f"not computed: {' and '.join(missing)} unknown. Supply them in the brief or run with --data; until then no lift is reported. "
                  f"The grid below shows how long a {pct:g}% holdout takes to read a {brief.min_detectable_lift:.0%} lift")
        read_after = "not computed until entry volume and the baseline are known"
        for e in GRID_ENTRANTS:
            for b in GRID_BASELINES:
                f = stats.feasibility(e, share, b, mde_rel=brief.min_detectable_lift, outcome_window_days=w)
                grid.append({"entrants_per_month": e, "baseline": b, "months_to_read": f["months_to_read"], "read": f["expected_read"]})

    guardrails = ["spam complaint rate per send below 0.1% (Gmail's target; 0.3% is its hard limit)",
                  "unsubscribe rate per send, against the 90-day median for your lifecycle sends; pause a touch running above twice it",
                  "hard bounce rate below 1%"]
    if "sms" in brief.channels:
        guardrails.append("SMS opt-out rate per send, against your SMS program's median")
    variants = [f"Copy variants rotate evenly inside the treatment arm. Read them on clicks or the core action, never opens, and only "
                f"directionally below about 1,000 sends per variant (size exactly with engine/measurement/stats.py size --baseline <click rate> --mde-rel 0.2)"]
    if deff_note and feas:
        sizing += deff_note
    stopping = {"design": "fixed_horizon", "looks": 1, "information_fractions": [1.0], "alpha": 0.05}
    peeking = "One planned read on the date above. Earlier looks check sample ratio and guardrails only; no early stop for a good-looking lift."
    decision = (f"Keep the flow when the 95% interval for (treatment minus holdout) rate of {core.detail()} lies above zero and the guardrails "
                f"hold. If it spans zero at the planned read, report it as inconclusive: keep the holdout running or change the flow. "
                f"Report attributed, incremental and the gap together (lcm readout).")
    if pct > 0 and brief.stopping_rule == "group_sequential":
        from engine.measurement import sequential
        k = brief.looks
        fractions = [round((i + 1) / k, 4) for i in range(k)]
        stopping = {"design": "group_sequential", "looks": k, "information_fractions": fractions, "alpha": 0.05, "spending": "obf"}
        shown = ", ".join(f"{b:.2f}" for b in sequential.boundaries(fractions, alpha=0.05, spending="obf")["boundaries"])
        peeking = (f"{k} planned looks at equal information, Lan-DeMets O'Brien-Fleming alpha spending (two-sided 0.05). Stop early only when "
                   f"|z| crosses that look's boundary ({shown}); otherwise continue to the final read. Record every look (lcm experiments record-look).")
        decision = (f"At each planned look compute z for (treatment minus holdout) and compare it with the boundary; stop for efficacy when it "
                    f"crosses and the guardrails hold. At the final look the boundary is the last value. Report attributed, incremental and the gap.")
        if feas and feas.get("required_holdout_n"):
            factor = sequential.inflation_factor(fractions, alpha=0.05, power=0.80, spending="obf")["inflation_factor"]
            sizing += (f"; group-sequential with {k} looks needs up to {factor:.3f} times that sample "
                       f"({int(feas['required_holdout_n'] * factor):,} holdout entrants at most), in exchange for stopping early on a clear result")
    elif pct > 0 and brief.stopping_rule == "always_valid":
        stopping = {"design": "always_valid", "looks": 1, "information_fractions": [1.0], "alpha": 0.05}
        peeking = ("Always-valid inference (mixture SPRT): the p-value and confidence sequence may be read at any time without inflating error. "
                   "Stop when the always-valid p falls below 0.05 and the guardrails hold (lcm sequential msprt).")
        decision = ("Keep the flow once the always-valid confidence sequence for (treatment minus holdout) lies above zero. It reads later than a "
                    "fixed-horizon test of the same size; that is the price of looking any time.")
    design = ExperimentDesign(
        holdout_pct=pct, unit=brief.randomization_unit, salt=salt, assignment_sql=assignment_sql, assignment_rule=rule,
        primary_metric=primary, outcome_window_days=w, read=read, sizing=sizing, read_after=read_after,
        feasibility=feas, mde_by_horizon=mde_rows, sensitivity=grid, decision_rule=decision,
        srm_rule=(f"At every look, test the split against {100 - pct:g}/{pct:g} (lcm readout --expected-holdout {share:g})"
                  + (", counting accounts, not people" if hash_field == "account_id" else "") +
                  ". A chi-square p below 0.001 means assignment or logging is broken; read nothing until it is fixed."),
        peeking_policy=peeking, guardrail_metrics=guardrails, variant_tests=variants, hash_field=hash_field,
        global_holdout_pct=brief.global_holdout_pct)
    if pct > 0:
        from engine.measurement import registry
        try:
            design.registry_entry = registry.entry_from_design(
                design.model_dump(), brief.slug, metric_event=core.event, stopping_rule=stopping, hash_field=hash_field,
                icc=(cl.get("icc_for_design") or cl.get("icc")) if hash_field == "account_id" else None,
                mean_cluster_size=m_bar if hash_field == "account_id" else None)
        except Exception as e:  # noqa: BLE001  (a registry problem must not lose the plan; it is reported instead)
            design.registry_entry = {"error": str(e)[:300]}
    return design


def attach(flow_spec: Dict[str, Any], d: ExperimentDesign) -> Dict[str, Any]:
    """Write the holdout and measurement blocks of the flow spec from the design."""
    if d.holdout_pct > 0:
        flow_spec["holdout"] = {"percent": d.holdout_pct, "persistent": True, "unit": d.unit, "assignment": d.assignment_rule,
                                "sizing": d.sizing, "read": d.read}
        compared = "holdout"
    else:
        flow_spec["holdout"] = {"percent": 0, "read": d.read, "reason_if_none": d.sizing}
        compared = "pre_post_baseline"
    flow_spec["measurement"] = {"primary_metric": d.primary_metric, "read_after": d.read_after, "compared_against": compared,
                                "guardrails": d.guardrail_metrics}
    return flow_spec
