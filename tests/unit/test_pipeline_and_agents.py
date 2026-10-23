"""v2.2 agents: the stateful pipeline (persist, fail, resume, revise, approve), the Strategist and the Compliance agent."""
import json
from pathlib import Path

import pytest

from engine.models import Brief

from tests.conftest import ROOT
from engine.agents import cohort_analyst, strategist
from engine.policy import compliance
from engine.pipeline import orchestrator, stages
from engine.brief import load_brief, parse_text
from engine.pipeline.core import STAGES, PipelineError, Run, approve, revise

EXAMPLE = ROOT / "examples" / "brands" / "ridgeview_saas" / "briefs" / "winback_14d.yaml"
FIXTURES = str(ROOT / "tests" / "fixtures" / "ridgeview_small" / "data")   # the 400-person world these numbers are pinned to


@pytest.fixture()
def brief():
    return load_brief(EXAMPLE)


@pytest.fixture(scope="module")
def cohort():
    return cohort_analyst.define(load_brief(EXAMPLE), FIXTURES)


# ----------------------------------------------------------------------------------------------
# pipeline state
# ----------------------------------------------------------------------------------------------

def test_a_run_persists_every_stage_and_reloads_to_the_same_plan(tmp_path, brief):
    run = Run.new(brief, tmp_path, data=FIXTURES, copy_mode="offline")
    plan = run.execute()
    d = run.dir
    assert (d / "state.json").exists() and (d.parent / "latest").read_text().strip() == run.state.run_id
    assert sorted(p.stem for p in (d / "stages").glob("*.json")) == sorted(STAGES)
    again = Run.load(d).plan()
    assert again.model_dump(exclude={"generated_at"}) == plan.model_dump(exclude={"generated_at"})
    assert {"analyst", "strategist", "compliance"} <= {d.stage for d in plan.decisions}


def test_a_failed_stage_stops_the_run_and_resume_starts_there(tmp_path, brief):
    run = Run.new(brief, tmp_path, data=str(tmp_path / "missing"), copy_mode="offline", sleep=lambda s: None)
    with pytest.raises(PipelineError) as err:
        run.execute()
    assert err.value.stage == "analyst"
    st = Run.load(run.dir).state
    assert st.status == "failed" and st.failed_stage == "analyst" and st.stages["analyst"].attempts == 1   # a bad path is not retried
    assert st.stages["intake"].status == "ok" and all(st.stages[s].status == "skipped" for s in STAGES[2:])
    resumed = Run.load(run.dir, sleep=lambda s: None)
    resumed.state.data = FIXTURES
    plan = resumed.execute()
    assert resumed.state.status == "complete" and resumed.state.stages["intake"].status == "reused"
    assert plan.qa.status == "READY WITH ASSUMPTIONS"


def test_transient_errors_are_retried(tmp_path, brief, monkeypatch):
    calls = {"n": 0}
    real = stages.FUNCS["analyst"]

    def flaky(run):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("warehouse timed out")
        return real(run)

    monkeypatch.setitem(stages.FUNCS, "analyst", flaky)
    slept = []
    run = Run.new(brief, tmp_path, data=FIXTURES, copy_mode="offline", sleep=slept.append)
    run.execute()
    assert run.state.stages["analyst"].attempts == 2 and slept == [0.5]


def test_revise_keeps_unchanged_copy_and_approvals(tmp_path, brief):
    run = Run.new(brief, tmp_path, data=FIXTURES, copy_mode="offline")
    plan = run.execute()
    first = plan.copy_plan[0]
    approve(run.dir, first.step_id, first.variants[1].variant_id)
    new, p2 = revise(run.dir, {"holdout_pct": 20}, tmp_path)
    assert p2.parent_run_id == run.state.run_id and new.state.changes_from_parent == {"holdout_pct": 20}
    assert all("reused from run" in (c.note or "") for c in p2.copy_plan if c.variants)
    assert p2.approvals == {first.step_id: first.variants[1].variant_id}
    assert p2.flow.flow_spec["holdout"]["percent"] == 20
    md = orchestrator.render_plan(p2)
    assert "Revision of run" in md and "(approved)" in md


def test_revise_and_approve_refuse_what_does_not_exist(tmp_path, brief):
    run = Run.new(brief, tmp_path, data=FIXTURES, copy_mode="offline")
    run.execute()
    with pytest.raises(ValueError, match="not brief fields"):
        revise(run.dir, {"touchez": 3}, tmp_path)
    with pytest.raises(ValueError, match="no step"):
        approve(run.dir, "t9_email", "A")
    with pytest.raises(ValueError, match="no variant"):
        approve(run.dir, "t1_email", "Z")


def test_plan_cli_sets_brief_fields(tmp_path):
    rc = orchestrator._cli(["plan", "--text", "5-touch win-back after 14 days inactive", "--product", "Acme", "--copy-mode", "offline",
                            "--set", "postal_address=1 Main St, Springfield, IL 62701", "--out", str(tmp_path)])
    assert rc == 0
    plan = json.loads(next(tmp_path.glob("*/plan.json")).read_text())
    assert plan["brief"]["postal_address"] == "1 Main St, Springfield, IL 62701"


# ----------------------------------------------------------------------------------------------
# Strategist
# ----------------------------------------------------------------------------------------------

def _with_dep(cohort, name, **update):
    deps = [d.model_copy(update=update) if d.name == name else d for d in cohort.data_dependencies]
    return cohort.model_copy(update={"data_dependencies": deps})


def test_strategist_drops_sms_when_almost_nobody_consented(brief):
    b = brief.model_copy(update={"channels": ["email", "sms"]})
    cohort = cohort_analyst.define(b, FIXTURES)
    assert any(d.name == "sms_consent" for d in cohort.data_dependencies)
    s = strategist.decide(b, _with_dep(cohort, "sms_consent", status="ok", fill_rate=0.01))
    assert "sms" not in s.channels_used and any(d.decision == "drop sms" and "1.0%" in d.why for d in s.decisions)
    kept = strategist.decide(b, _with_dep(cohort, "sms_consent", status="ok", fill_rate=0.40))
    assert "sms" in kept.channels_used


def test_strategist_explains_every_touch_and_the_offer(brief, cohort):
    s = strategist.decide(brief, cohort)
    assert len(s.touches) == brief.touches and all(t.why for t in s.touches)
    assert s.touches[0].channel == "email" and s.touches[-1].channel == "email"
    assert any(d.decision == "offer stance" for d in s.decisions)
    assert any("re-permission" in d.decision for d in s.decisions)


def test_strategist_spreads_touches_to_the_weekly_cap(brief, cohort):
    s = strategist.decide(brief.model_copy(update={"frequency_cap_per_week": 1}), cohort)
    days = [t.day for t in s.touches]
    assert all(b - a >= 7 for a, b in zip(days, days[1:]))
    assert any(d.decision.startswith("touches at") and "no cap is broken" in d.why for d in s.decisions)


def test_activation_unblock_touch_aims_at_the_largest_leak():
    b = parse_text("3-touch activation flow for people who signed up but have not created a report within 7 days", "Acme")
    c = cohort_analyst.define(b, FIXTURES)
    s = strategist.decide(b, c)
    if c.leak:
        assert s.leak_step == c.leak and any(d.decision.startswith("aim the unblock touch") for d in s.decisions)


# ----------------------------------------------------------------------------------------------
# Compliance
# ----------------------------------------------------------------------------------------------

def test_jurisdictions_follow_the_audience(brief, cohort):
    c = cohort.model_copy(update={"countries": {"US": 0.6, "CA": 0.2, "DE": 0.15, "BR": 0.05}})
    juris, note = compliance.jurisdictions(brief, c)
    assert juris == ["CA", "DE", "EU", "OTHER", "US"] and note is None
    unknown, note = compliance.jurisdictions(brief, cohort.model_copy(update={"countries": {}}))
    assert unknown == ["US"] and "assumed United States" in note


def test_every_rule_has_a_check_and_a_source():
    rules = compliance.load_rules()["rules"]
    assert all(r["check"] in compliance.CHECKS and r["source"].startswith("https://") for r in rules)
    assert len({r["id"] for r in rules}) == len(rules)


@pytest.mark.parametrize("hour", [20, 8])
def test_an_sms_send_hour_outside_the_strictest_state_window_cannot_be_planned(brief, hour):
    # the window comes from config/compliance_rules.json (9:00 to 20:00, Connecticut): the brief itself refuses the hour
    with pytest.raises(ValueError, match="outside the permitted"):
        Brief.model_validate({**brief.model_dump(), "channels": ["email", "sms"], "send_hour_local": hour, "quiet_hours": "22:00-07:00"})


@pytest.mark.parametrize("hour,status", [(21, "fail"), (8, "fail"), (10, "warn"), (13, "pass")])
def test_sms_send_hour_is_held_to_the_strictest_state_window(brief, hour, status):
    # an independent second check: compliance reads the plan, even for a brief built without validation
    b = brief.model_copy(update={"channels": ["email", "sms"], "send_hour_local": hour})
    plan = orchestrator.run(b.model_copy(update={"send_hour_local": 13}), copy_mode="offline")
    states = [f for f in compliance.review(b, plan.cohort, plan.flow, plan.copy_plan).findings
              if f.rule == "state texting hours (strictest tracked)"]
    if not any(t.channel == "sms" for t in plan.flow.touches):
        pytest.skip("SMS not placed in this flow")
    assert states and states[0].status == status, states


def test_uncovered_countries_are_named(brief, cohort):
    c = cohort.model_copy(update={"countries": {"US": 0.9, "BR": 0.1}})
    plan = orchestrator.run(brief, copy_mode="offline")
    report = compliance.review(brief, c, plan.flow, plan.copy_plan)
    other = [f for f in report.findings if f.rule == "uncovered jurisdictions"]
    assert other and "BR" in other[0].detail and report.status in ("warn", "fail")


# ----------------------------------------------------------------------------------------------
# regressions from the v2.2 review
# ----------------------------------------------------------------------------------------------

def test_an_approval_is_dropped_when_a_resume_rewrites_its_words(tmp_path, brief):
    run = Run.new(brief, tmp_path, data=FIXTURES, copy_mode="offline")
    plan = run.execute()
    approve(run.dir, "t1_email", "B")
    approve(run.dir, "t2_email", "A")
    drafts = {tc.step_id: [{**v.fields, "framework": v.framework, "hypothesis": v.hypothesis} for v in tc.variants] for tc in plan.copy_plan}
    drafts["t1_email"][1]["subject"] = "A different subject line entirely"
    r = Run.load(run.dir)
    r.state.drafts = drafts
    p2 = r.execute()
    assert p2.approvals == {"t2_email": "A"}
    assert any(d.decision == "approval of t1_email variant B dropped" for d in p2.decisions)


def test_a_failed_revision_resumes_with_its_parent(tmp_path, brief, monkeypatch):
    run = Run.new(brief, tmp_path, data=FIXTURES, copy_mode="offline")
    run.execute()
    approve(run.dir, "t1_email", "B")

    def boom(r):
        raise ValueError("simulated strategist failure")

    real = stages.FUNCS["strategist"]
    monkeypatch.setitem(stages.FUNCS, "strategist", boom)
    with pytest.raises(PipelineError) as err:
        revise(run.dir, {"holdout_pct": 20}, tmp_path)
    monkeypatch.setitem(stages.FUNCS, "strategist", real)
    resumed = Run.load(err.value.run_dir)
    assert resumed.parent is not None and resumed.parent.state.run_id == run.state.run_id
    p2 = resumed.execute()
    assert p2.approvals == {"t1_email": "B"} and all("reused from run" in (c.note or "") for c in p2.copy_plan if c.variants)


def test_resume_on_new_data_does_not_keep_old_estimates(tmp_path, brief):
    run = Run.new(brief, tmp_path, data=FIXTURES, copy_mode="offline")
    run.execute()
    first = {a.field: a.value for a in run.plan().brief.assumptions if a.field in ("monthly_entrants", "baseline_conversion_rate")}
    assert first and all(v is not None for v in first.values())
    short = tmp_path / "short"
    short.mkdir()
    for t in ("users", "orders"):
        (short / f"{t}.csv").write_text((Path(FIXTURES) / f"{t}.csv").read_text())
    lines = (Path(FIXTURES) / "track_events.csv").read_text().splitlines()
    head = lines[0].split(",")
    at = head.index("event_at")
    recent = [line for line in lines[1:] if line.split(",")[at] >= "2026-09-28"]   # 20 days: too short to estimate from
    (short / "track_events.csv").write_text("\n".join([lines[0]] + recent) + "\n")
    r = Run.load(run.dir)
    r.state.data = str(short)
    p2 = r.execute()
    again = {a.field: a.value for a in p2.brief.assumptions if a.field in ("monthly_entrants", "baseline_conversion_rate")}
    assert all(v is None for v in again.values()), again


def test_revise_rewrites_copy_when_the_audience_changes(tmp_path, brief):
    run = Run.new(brief, tmp_path, data=FIXTURES, copy_mode="offline")
    run.execute()
    _, p2 = revise(run.dir, {"churn_threshold_days": 90}, tmp_path)
    assert not any("reused from run" in (c.note or "") for c in p2.copy_plan)


def test_run_commands_write_back_to_the_runs_own_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plans = tmp_path / "plans"
    assert orchestrator._cli(["plan", "--brief", str(EXAMPLE), "--copy-mode", "offline", "--out", str(plans)]) == 0
    flow = plans / "ridgeview_winback_14d"
    assert orchestrator._cli(["revise", "--run", str(flow), "--set", "holdout_pct=20"]) == 0
    assert orchestrator._cli(["approve", "--run", str(flow), "--step", "t1_email", "--variant", "B"]) == 0
    assert not (tmp_path / "out").exists()
    assert len(list((flow / "runs").iterdir())) == 3 and "(approved)" in (flow / "plan.md").read_text()


def test_plan_set_removes_the_assumption_it_answers(tmp_path):
    assert orchestrator._cli(["plan", "--brief", str(EXAMPLE), "--copy-mode", "offline", "--set", "monthly_entrants=500",
                              "--out", str(tmp_path)]) == 0
    plan = json.loads(next(tmp_path.glob("*/plan.json")).read_text())
    assert plan["brief"]["monthly_entrants"] == 500
    assert "monthly_entrants" not in {a["field"] for a in plan["brief"]["assumptions"]}


def test_remote_load_failures_that_can_pass_on_retry_are_transient():
    from engine.pipeline.core import _transient
    assert cohort_analyst.TRANSIENT_LOAD.search("HTTP GET error: 503 Service Unavailable")
    assert not cohort_analyst.TRANSIENT_LOAD.search("HTTP GET error: 404 Not Found")
    assert _transient(ConnectionError("x")) and not _transient(FileNotFoundError("x"))


def test_unknown_countries_fall_back_to_us_rules(brief, cohort):
    juris, note = compliance.jurisdictions(brief, cohort.model_copy(update={"countries": {"UNKNOWN": 1.0}}))
    assert juris == ["US"] and "assumed United States" in note
    juris, note = compliance.jurisdictions(brief, cohort.model_copy(update={"countries": {"CA": 0.7, "UNKNOWN": 0.3}}))
    assert juris == ["CA", "US"] and "30% of people have no country" in note
