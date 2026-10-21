"""The eight pipeline stages. Each takes the Run, reads what earlier stages produced, and returns its outputs.

Stages log the decisions they make with run.decisions, so the plan can say why it looks the way it does.
"""
from __future__ import annotations

from typing import Any, Dict, List

from engine.agents import campaign_architect, cohort_analyst, copywriter, experimentation_agent, strategist
from engine.policy import compliance as compliance_agent, guardrails
from engine.models import Assumption, Brief, Check, Decision, QAReport
from engine.policy import simulator as campaign_simulator

READ_STATUS = {"causal": "pass", "causal, slow": "warn", "directional only": "warn", "not readable": "warn"}


def intake(run) -> Dict[str, Any]:
    """Fix the outcome window and the sending gate before any agent uses them."""
    brief: Brief = run.state.brief
    if not brief.outcome_window_days:
        days = campaign_architect.schedule_days(brief)
        w = campaign_architect.outcome_window(brief)
        a = Assumption(field="outcome_window_days", value=w,
                       why=f"not stated; set to {w} days so the whole flow (last touch on day {max(days)}) plus a week is inside it")
        brief = brief.model_copy(update={"outcome_window_days": w, "assumptions": brief.assumptions + [a]})
    if not brief.postal_address:
        from engine.agents.copywriter import brand_for
        kit_address = getattr(getattr(brand_for(brief), "footer", None), "postal_address", None)
        if kit_address:
            brief = brief.model_copy(update={"postal_address": kit_address,
                                             "assumptions": [a for a in brief.assumptions if a.field != "postal_address"]})
            run.decisions.append(Decision(stage="intake", decision="postal address from the brand kit", why=kit_address))
    pol = guardrails.gate_policy(brief.sending_gate, brief.flow_type)
    run.decisions.append(Decision(stage="intake", decision=f"sending gate {pol['effective_gate']}", why=pol["note"]))
    return {"brief": brief, "gate_policy": pol}


def analyst(run) -> Dict[str, Any]:
    brief: Brief = run.out["brief"]
    cohort = cohort_analyst.define(brief, run.state.data, run.state.as_of)
    notes = {"monthly_entrants": cohort.monthly_entrants, "baseline_conversion_rate": cohort.baseline_conversion_rate}
    out = []
    for a in brief.assumptions:
        est = notes.get(a.field)
        if est is not None and est.value is not None and est.label != "SUPPLIED":
            shown = f"{est.value:,.0f} a month" if a.field == "monthly_entrants" else f"{est.value:.1%}"
            out.append(a.model_copy(update={"value": est.value, "why": f"estimated from the data ({est.label}): {shown}, {est.basis}"}))
        else:
            out.append(a)
    brief = brief.model_copy(update={"assumptions": out})
    blocked = [d.name for d in cohort.data_dependencies if d.status == "blocked"]
    run.decisions.append(Decision(stage="analyst", decision="entry rule", why=cohort.plain_english))
    if blocked:
        run.decisions.append(Decision(stage="analyst", decision="blocked data", why=", ".join(blocked)))
    return {"cohort": cohort, "brief": brief}


def strategist_stage(run) -> Dict[str, Any]:
    s = strategist.decide(run.out["brief"], run.out["cohort"])
    run.decisions.extend(s.decisions)
    return {"strategy": s}


def architect(run) -> Dict[str, Any]:
    flow = campaign_architect.design(run.out["brief"], run.out["cohort"], run.out["strategy"])
    return {"flow": flow}


def copywriter_stage(run) -> Dict[str, Any]:
    from engine.pipeline.core import copy_key
    brief = run.out["brief"]
    reuse = run.previous_copy()
    key = lambda t: copy_key(t, brief, run.state.copy_mode)  # noqa: E731
    plan = copywriter.write(brief, run.out["flow"], run.state.copy_mode, run.state.drafts, run.client, reuse=reuse, reuse_key=key)
    touches = {t.step_id: t for t in run.out["flow"].touches}
    reused = sum(1 for tc in plan if tc.note and "reused from run" in tc.note)
    # An approval names words, not a slot: it stands only while the approved words do.
    kept: Dict[str, str] = {}
    dropped: Dict[str, str] = {}
    if run.prior.get("copy_plan") is not None:
        # this run wrote copy before (a resume that reruns the copywriter): keep an approval if its variant's words are unchanged
        def words(plan_: List[Any], step: str, variant: str) -> Any:
            tc = next((c for c in plan_ if c.step_id == step), None)
            v = next((x for x in (tc.variants if tc else []) if x.variant_id == variant), None)
            return None if v is None else (v.fields, v.framework, v.hypothesis)
        for step, variant in run.state.approvals.items():
            same = words(run.prior["copy_plan"], step, variant) is not None and \
                words(run.prior["copy_plan"], step, variant) == words(plan, step, variant)
            (kept if same else dropped)[step] = variant
    elif run.parent is not None:
        # a revision: an approval follows its words to whichever touch reused them
        carried = {}
        for tc in plan:
            if tc.note and "reused from run" in tc.note:
                carried[reuse[key(touches[tc.step_id])][0].step_id] = tc.step_id
        for step, variant in run.parent.state.approvals.items():
            if step in carried:
                kept[carried[step]] = variant
            else:
                dropped[step] = variant
    else:
        kept = dict(run.state.approvals)
    for step, variant in dropped.items():
        run.decisions.append(Decision(stage="copywriter", decision=f"approval of {step} variant {variant} dropped",
                                      why="its words were rewritten in this run (the touch's job, channel or audience changed, or the "
                                          "copy was written again); approve the new copy if it should ship"))
    run.state.approvals = kept
    if reused:
        run.decisions.append(Decision(stage="copywriter", decision=f"copy carried over for {reused} touch(es)",
                                      why="their job and channel did not change, so the existing (and any approved) copy stands"))
    return {"copy_plan": plan}


def experiment(run) -> Dict[str, Any]:
    exp = experimentation_agent.design(run.out["brief"], run.out["cohort"])
    flow = run.out["flow"].model_copy(deep=True)
    flow.flow_spec = experimentation_agent.attach(flow.flow_spec, exp)
    run.decisions.append(Decision(stage="experiment", decision=f"{exp.holdout_pct:g}% persistent holdout, read: {exp.read}", why=exp.read_after))
    return {"experiment": exp, "flow": flow}


def qa(run) -> Dict[str, Any]:
    return {"qa": qa_report(run.out["brief"], run.out["gate_policy"], run.out["cohort"], run.out["flow"], run.out["copy_plan"],
                            run.out["experiment"], run.state.data is not None)}


def compliance(run) -> Dict[str, Any]:
    report = compliance_agent.review(run.out["brief"], run.out["cohort"], run.out["flow"], run.out["copy_plan"])
    q: QAReport = run.out["qa"]
    checks = [c for c in q.checks if not c.name.startswith("compliance:")]
    for f in report.findings:
        if f.status != "pass":
            checks.append(Check(name=f"compliance: {f.rule}", status=f.status, detail=f"{f.jurisdiction}: {f.detail}"))
    checks.append(Check(name="compliance", status=report.status,
                        detail=f"{len(report.findings)} rule(s) checked for {', '.join(report.jurisdictions) or 'no jurisdiction'}"))
    run.decisions.append(Decision(stage="compliance", decision=f"compliance {report.status}",
                                  why=f"jurisdictions in the audience: {', '.join(report.jurisdictions) or 'unknown'}"))
    return {"compliance": report, "qa": QAReport.from_checks(checks, len(run.out["brief"].assumptions))}


def qa_report(brief: Brief, pol: Dict[str, Any], cohort, flow, copy_plan, exp, data_given: bool) -> QAReport:
    checks: List[Check] = []
    checks.append(Check(name="sending gate", status="fail" if not pol["sending_allowed"] else ("pass" if pol["effective_gate"] == "GREEN" else "warn"),
                        detail=pol["note"]))
    aud = getattr(cohort, "audience_after_gate", None)
    if aud and aud.get("entrants"):
        if aud["kept"] == 0:
            checks.append(Check(name="audience after the gate", status="fail",
                                detail=f"the gate's entry filter ({aud['rule']}) keeps none of today's {aud['entrants']} would-be entrants: "
                                       f"the flow would enroll nobody. Lift the gate (lcm deliverability) or change who the flow is for"))
        else:
            checks.append(Check(name="audience after the gate", status="warn" if aud["share"] < 0.2 else "pass",
                                detail=f"{aud['kept']} of today's {aud['entrants']} would-be entrants pass the gate's filter ({aud['rule']})"))
    core_names = {brief.qualifying_event.event, brief.core_conversion_event.event}
    for d in cohort.data_dependencies:
        base = d.name.split(" ")[0]
        if d.status == "blocked":
            critical = base in core_names or d.name in ("email", "email_consent")
            checks.append(Check(name=f"data: {d.name}", status="fail" if critical else "warn", detail=d.note or "blocked"))
    if not data_given:
        checks.append(Check(name="data dependencies", status="warn",
                            detail="not verified against data: run with --data (a folder of users, orders and track_events exports) to check events, fill rates and volume"))
    elif not any(c.name.startswith("data:") for c in checks):
        checks.append(Check(name="data dependencies", status="pass", detail="every event and attribute the flow needs is present"))
    bad_sql = {k: v for k, v in cohort.sql_checks.items() if not (v == "parses" or v.startswith("ran"))}
    checks.append(Check(name="cohort SQL", status="fail" if bad_sql else "pass",
                        detail="; ".join(f"{k}: {v}" for k, v in bad_sql.items()) if bad_sql else
                        "every generated query parses in postgres, snowflake and bigquery" + ("; entry query ran on the data" if data_given else "")))
    sim = campaign_simulator.simulate(flow.flow_spec, flow.simulation)
    for c in sim["checks"]:
        if c["name"] == "journey length":
            checks.append(Check(name="simulation: journey length", status="pass", detail=c["detail"]))
            continue
        checks.append(Check(name=f"simulation: {c['name']}", status=c["status"], detail=c["detail"]))
    if cohort.trigger_payload_example:
        name, errs = campaign_simulator.validate_request(cohort.trigger_payload_example)
        checks.append(Check(name=f"trigger payload ({name})", status="fail" if errs else "pass",
                            detail="; ".join(errs) if errs else "valid against config/esp_payload_schemas.json"))
    for c in copy_plan:
        if c.mode == "handoff":
            checks.append(Check(name=f"copy: {c.step_id}", status="warn", detail="awaiting drafts (copy_briefs.md)"))
            continue
        passing = [v for v in c.variants if v.ok]
        if not passing:
            errs = "; ".join(e for v in c.variants for e in v.errors[:3]) or (c.note or "no variants")
            checks.append(Check(name=f"copy: {c.step_id}", status="fail", detail=errs))
            continue
        warns = sorted({w for v in passing for w in v.warnings})
        detail = f"{len(passing)} variant(s) pass lint and guardrails ({c.mode})" + (f"; {c.note}" if c.note else "")
        checks.append(Check(name=f"copy: {c.step_id}", status="warn" if warns else "pass", detail=detail + ("; " + "; ".join(warns[:4]) if warns else "")))
    checks.append(Check(name="holdout read", status=READ_STATUS[exp.read], detail=f"{exp.read}: {exp.read_after}"))
    return QAReport.from_checks(checks, len(brief.assumptions))


FUNCS = {"intake": intake, "analyst": analyst, "strategist": strategist_stage, "architect": architect, "copywriter": copywriter_stage,
         "experiment": experiment, "qa": qa, "compliance": compliance}
