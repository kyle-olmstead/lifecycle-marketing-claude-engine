"""End to end: brief to plan, on every platform and channel mix, plus the simulator's ability to catch broken flows."""
import copy
import json
from types import SimpleNamespace

import jsonschema
import pytest

from tests.conftest import ROOT
from engine.agents import copywriter
from engine.pipeline import orchestrator
from engine.brief import load_brief, parse_text
from engine.models import Brief
from engine.models import EventMatch
from engine.policy import simulator as campaign_simulator
from engine.render import template_lint

FLOW_SCHEMA = json.loads((ROOT / "schemas/flow_spec.schema.json").read_text())
EXAMPLE = ROOT / "examples" / "brands" / "ridgeview_saas" / "briefs" / "winback_14d.yaml"
EM_DASH = chr(0x2014)


@pytest.fixture(scope="module")
def plan():
    # the demo brief lives in the demo workspace, so it is read with that workspace's lmm.yaml, as lcm reads it there
    from engine import workspace
    defaults = workspace.brief_defaults(workspace.Workspace(ROOT / "examples/brands/ridgeview_saas"))
    return orchestrator.run(load_brief(EXAMPLE, defaults=defaults), str(ROOT / "examples/brands/ridgeview_saas/data"), copy_mode="offline")


def _all_checks_pass_except_known(p, allowed=("copy:", "holdout read", "compliance", "sending gate", "audience after the gate")):
    bad = [c for c in p.qa.checks if c.status == "fail" or (c.status == "warn" and not c.name.startswith(allowed))]
    assert not bad, [(c.name, c.detail) for c in bad]


def test_worked_example_is_ready_with_assumptions(plan):
    assert plan.qa.status == "READY WITH ASSUMPTIONS"
    _all_checks_pass_except_known(plan)
    assert not list(jsonschema.Draft202012Validator(FLOW_SCHEMA).iter_errors(plan.flow.flow_spec))
    assert plan.flow.flow_spec["steps"][0]["id"] == "holdout_split"
    assert plan.flow.flow_spec["holdout"]["percent"] == 15 and plan.flow.flow_spec["holdout"]["persistent"] is True


def test_worked_example_matches_the_committed_output(plan):
    committed = ROOT / "examples" / "brands" / "ridgeview_saas" / "runs" / "winback_14d"
    assert json.loads((committed / "flow_spec.json").read_text()) == plan.flow.flow_spec
    assert json.loads((committed / "simulation.json").read_text()) == plan.flow.simulation


def test_every_rendered_template_lints_and_carries_compliance(plan):
    n = 0
    for tc in plan.copy_plan:
        for v in tc.variants:
            for name, text in v.rendered.items():
                assert EM_DASH not in text
                if name.endswith(".html"):
                    r = template_lint.lint(text, "customerio", email=True)
                    assert r["ok"], (name, r["errors"])
                    assert "{% unsubscribe_url %}" in text and "100 Example Way" in text
                    n += 1
    assert n >= 8


def test_outputs_are_written_and_free_of_em_dashes(plan, tmp_path):
    out = orchestrator.write_outputs(plan, tmp_path)
    for f in out.rglob("*"):
        if f.is_file():
            assert EM_DASH not in f.read_text(encoding="utf-8"), f
    assert (out / "plan.md").read_text().startswith("# Win-back: Ridgeview")
    assert {p.name for p in (out / "cohort").iterdir()} >= {"entry_postgres.sql", "entry_snowflake.sql", "entry_bigquery.sql", "history_bigquery.sql"}


@pytest.mark.parametrize("platform", ["klaviyo", "hubspot", "braze", "customerio", "sfmc", "iterable"])
def test_every_platform_renders_valid_templates_and_payloads(platform):
    b = load_brief(EXAMPLE).model_copy(update={"platform": platform})
    p = orchestrator.run(b, copy_mode="offline")
    assert p.qa.status != "BLOCKED", [(c.name, c.detail) for c in p.qa.checks if c.status == "fail"]
    assert p.flow.flow_spec["template_dialect"] == b.template_dialect
    for tc in p.copy_plan:
        for v in tc.variants:
            assert v.ok, (platform, tc.step_id, v.errors)
            html = [t for k, t in v.rendered.items() if k.endswith(".html")]
            for h in html:
                assert template_lint.lint(h, b.template_dialect, email=True)["ok"]
    if platform != "sfmc":
        from adapters.destinations.event_payloads import validate_request   # the simulator's endpoints plus Iterable
        name, errs = validate_request(p.cohort.trigger_payload_example)
        assert not errs, (name, errs)


def test_multichannel_flow_has_consent_fallbacks_and_passes_simulation():
    b = load_brief(EXAMPLE).model_copy(update={"channels": ["email", "sms", "push", "in_app"], "touches": 6})
    p = orchestrator.run(b, copy_mode="offline")
    steps = {s["id"]: s for s in p.flow.flow_spec["steps"]}
    sms = [s for s in steps.values() if s["channel"] == "sms"]
    push = [s for s in steps.values() if s["channel"] == "push"]
    assert len(sms) == 1 and len(push) == 1
    assert all(f"{s['id'].split('_')[0]}_email_fallback" in steps for s in sms + push)
    sim = campaign_simulator.simulate(p.flow.flow_spec, p.flow.simulation)
    assert sim["status"] == "pass", [c for c in sim["checks"] if c["status"] == "fail"]
    sms_copy = [c for c in p.copy_plan if c.channel == "sms"][0]
    for v in sms_copy.variants:
        text = json.loads(next(iter(v.rendered.values())))["text"]
        assert "Reply STOP to opt out." in text and text.startswith("Ridgeview:")


def test_activation_flow():
    b = load_brief(EXAMPLE).model_copy(update={
        "flow_type": "activation", "touches": 4,
        "qualifying_event": EventMatch(event="onboarding_step_completed", where={"step_index": 3}, label="completed onboarding")})
    p = orchestrator.run(b, str(ROOT / "examples/brands/ridgeview_saas/data"), copy_mode="offline")
    spec = p.flow.flow_spec
    assert p.qa.status != "BLOCKED"
    assert spec["trigger"]["type"] == "event" and p.flow.touches[0].role == "first_step"
    assert "step_index = 3" in spec["trigger"]["definition"]
    assert any(f["condition"] == "trigger filter: step_index = 3" for f in spec["entry_filters"])
    assert "onboarding_step_completed trigger event" in spec["steps"][0]["branch"]["condition"]
    assert "md5(user_id" in p.flow.build_steps[0], "the build steps say how to put in_holdout on the live event"
    assert "re-entry off" in p.cohort.platform_pattern and p.cohort.trigger_payload_example == {}


def test_winback_build_step_one_runs_the_outbox(plan):
    step = plan.flow.build_steps[0]
    assert "queue_postgres.sql" in step and "pending_postgres.sql" in step and "mark_postgres.sql" in step


def test_red_gate_blocks_and_amber_limits_to_engaged():
    red = orchestrator.run(load_brief(EXAMPLE).model_copy(update={"sending_gate": "RED"}), copy_mode="offline")
    assert red.qa.status == "BLOCKED"
    amber = orchestrator.run(load_brief(EXAMPLE).model_copy(update={"sending_gate": "AMBER"}), copy_mode="offline")
    assert amber.flow.flow_spec["gate_required"] == "AMBER"
    assert any("90 days" in f["condition"] for f in amber.flow.flow_spec["entry_filters"])


SPEC_SENTENCE = ("Build a 5-touch automated win-back flow for users who completed onboarding but haven't "
                 "triggered a core action in 14 days")


def test_the_spec_sentence_is_blocked_until_a_postal_address_is_given():
    """Customer.io has no address token, so a sentence-only brief has no address: CAN-SPAM makes that a stop, not a warning."""
    p = orchestrator.run(parse_text(SPEC_SENTENCE, "Acme"), copy_mode="offline")
    assert p.qa.status == "BLOCKED"
    fails = [f for f in p.compliance.findings if f.status == "fail"]
    assert [f.rule for f in fails] == ["CAN-SPAM postal address"] and "postal_address" in fails[0].detail


def test_the_spec_sentence_plans_end_to_end_without_data():
    p = orchestrator.run(parse_text(SPEC_SENTENCE, "Acme", {"postal_address": "1 Main St, Springfield, IL 62701"}), copy_mode="offline")
    assert p.qa.status == "READY WITH ASSUMPTIONS", [(c.name, c.detail) for c in p.qa.checks if c.status == "fail"]
    assert p.experiment.read == "not readable" and p.experiment.sensitivity
    md = orchestrator.render_plan(p)
    assert "## Confirm before you build" in md and "core_conversion_event" in md


def test_exit_and_metric_respect_the_core_event_filter(plan):
    spec = plan.flow.flow_spec
    assert "feature = reports" in spec["exit"]["goal"] and "feature = reports" in spec["ongoing_filters"][0]["condition"]
    assert "feature = reports" in plan.experiment.primary_metric and "feature = reports" in plan.experiment.decision_rule


@pytest.mark.parametrize("touches", range(1, 9))
def test_every_touch_count_plans_cleanly(touches):
    b = load_brief(EXAMPLE).model_copy(update={"touches": touches, "channels": ["email", "sms", "push", "in_app"]})
    p = orchestrator.run(b, copy_mode="offline")
    assert p.qa.status != "BLOCKED", [(c.name, c.detail) for c in p.qa.checks if c.status == "fail"]


def test_outcome_window_covers_the_whole_flow():
    b = load_brief(EXAMPLE).model_copy(update={"touches": 8, "frequency_cap_per_week": 1})
    p = orchestrator.run(b, copy_mode="offline")
    last = max(t.day for t in p.flow.touches)
    assert p.experiment.outcome_window_days >= last + 7
    assert any(a.field == "outcome_window_days" for a in p.brief.assumptions)


def test_custom_send_windows_reach_the_simulator():
    # the deprecated quiet_hours input is read once as the permitted email window outside it
    b = Brief.model_validate({**load_brief(EXAMPLE).model_dump(), "send_hour_local": 7, "quiet_hours": "22:00 to 06:00"})
    assert any(a.field == "quiet_hours" and "06:00 to 22:00" in a.why for a in b.assumptions)
    p = orchestrator.run(b, copy_mode="offline")
    assert p.flow.simulation["send_windows"]["email"] == [6.0, 22.0] and p.qa.status != "BLOCKED"


def test_frequency_cap_spreads_touches():
    b = load_brief(EXAMPLE).model_copy(update={"frequency_cap_per_week": 1, "touches": 4})
    p = orchestrator.run(b, copy_mode="offline")
    days = [t.day for t in p.flow.touches]
    assert all(b - a >= 7 for a, b in zip(days, days[1:])), days
    assert p.qa.status != "BLOCKED"


# ----------------------------------------------------------------------------------------------
# copywriter with a stub Claude client
# ----------------------------------------------------------------------------------------------

GOOD = {"subject": "Your reports are waiting", "preheader": "Two minutes to pick up again", "headline": "Pick up where you left off",
        "paragraphs": ["Hi {first_name}, your data sources in {product_name} are still connected.", "Open a template and you are done."],
        "cta_label": "Open a template", "framework": "BAB", "hypothesis": "Naming reports raises clicks; falsified if B is equal or higher."}
IN_APP = {"headline": "New: report templates", "body": "Start from a template built for operations teams.", "cta_label": "Try a template",
          "framework": "AIDA", "hypothesis": "Templates raise starts; falsified if the control is equal."}


class StubClient:
    def __init__(self, email_drafts):
        self.calls = []
        self.email_drafts = email_drafts
        self.messages = self

    def parse(self, **kw):
        self.calls.append(kw)
        m = kw["output_format"]
        if m.__name__ == "EmailVariants":
            v = self.email_drafts(len([c for c in self.calls if c["output_format"].__name__ == "EmailVariants"]))
        else:
            v = [IN_APP, IN_APP]
        return SimpleNamespace(stop_reason="end_turn", parsed_output=m(variants=v))


def test_claude_mode_repairs_failing_drafts_once():
    client = StubClient(lambda n: [dict(GOOD, subject="RE: your reports") if n == 1 else GOOD, GOOD])
    p = orchestrator.run(load_brief(EXAMPLE), copy_mode="claude", client=client)
    assert all(c.mode == "claude" and len(c.variants) == 2 for c in p.copy_plan)
    repair = [c for c in client.calls if "previous draft failed" in c["messages"][0]["content"]]
    assert len(repair) == 1 and "RE: or FW:" in repair[0]["messages"][0]["content"]
    assert client.calls[0]["model"] == copywriter.DEFAULT_MODEL


def test_claude_mode_blocks_when_copy_cannot_pass():
    bad = dict(GOOD, subject="Last chance: 20% off")
    p = orchestrator.run(load_brief(EXAMPLE), copy_mode="claude", client=StubClient(lambda n: [bad, bad]))
    assert p.qa.status == "BLOCKED"
    assert any(c.name.startswith("copy:") and c.status == "fail" for c in p.qa.checks)


def test_claude_mode_falls_back_offline_on_api_errors():
    class Broken:
        messages = SimpleNamespace(parse=lambda **kw: (_ for _ in ()).throw(TimeoutError("timed out")))
    p = orchestrator.run(load_brief(EXAMPLE), copy_mode="claude", client=Broken())
    assert all(c.mode == "offline" and "TimeoutError" in c.note for c in p.copy_plan)


def test_handoff_and_drafts_round_trip(tmp_path):
    b = load_brief(EXAMPLE)
    p = orchestrator.run(b, copy_mode="handoff")
    assert all(c.mode == "handoff" for c in p.copy_plan)
    md = copywriter.handoff_markdown(b, p.copy_plan)
    skeleton = json.loads(md.split("```json\n")[1].split("```")[0])
    drafts = {k: [IN_APP if "in_app" in k else GOOD for _ in v] for k, v in skeleton.items()}
    p2 = orchestrator.run(b, drafts=drafts)
    assert all(c.mode == "supplied" and all(v.ok for v in c.variants) for c in p2.copy_plan)


# ----------------------------------------------------------------------------------------------
# the simulator catches broken flows
# ----------------------------------------------------------------------------------------------

def _fails(spec, rules):
    return {c["name"] for c in campaign_simulator.simulate(spec, rules)["checks"] if c["status"] == "fail"}


def test_simulator_catches_injected_defects(plan):
    spec, rules = plan.flow.flow_spec, plan.flow.simulation
    assert not _fails(spec, rules)

    s = copy.deepcopy(spec)
    s["steps"] = [x for x in s["steps"] if x["id"] != "holdout_split"]
    assert "holdout is held out before the first message" in _fails(s, rules)

    s = copy.deepcopy(spec)
    for x in s["steps"]:
        if x["wait"]["type"] == "fixed":
            x["wait"]["duration"] = "P1D"
    assert "nominal schedule respects the weekly frequency cap" in _fails(s, rules)

    s = copy.deepcopy(spec)
    s["steps"][0]["branch"]["no"] = "t9_email"
    assert "every branch target is a step or an exit" in _fails(s, rules)

    r = copy.deepcopy(rules)
    r["goal_event"] = "never_happens"
    assert "nothing is sent after the core action" in _fails(spec, r)

    r = copy.deepcopy(rules)
    r["steps"]["holdout_split"] = {"branch": {"holdout": False}}
    assert "holdout members receive nothing" in _fails(spec, r)


def test_simulator_catches_a_message_without_consent():
    b = load_brief(EXAMPLE).model_copy(update={"channels": ["email", "sms"], "touches": 6})
    p = orchestrator.run(b, copy_mode="offline")
    r = copy.deepcopy(p.flow.simulation)
    sms_step = next(s["id"] for s in p.flow.flow_spec["steps"] if s["channel"] == "sms")
    r["steps"][sms_step] = {"condition": None}            # drop the consent check
    r["steps"][f"{sms_step.split('_')[0]}_email_fallback"] = {"condition": None}
    spec = copy.deepcopy(p.flow.flow_spec)
    sent = campaign_simulator.walk(spec, r, {"consents": ["email"], "events": []})["sends"]
    assert any(x["step"] == sms_step for x in sent)
    assert "every send matches a consent" in _fails(spec, r)

    s = copy.deepcopy(spec)
    s["ongoing_filters"] = [f for f in s["ongoing_filters"] if "email" not in f["condition"]]
    assert "no message without consent" in _fails(s, p.flow.simulation)


@pytest.mark.parametrize("path", sorted((ROOT / "examples/flows").glob("*.json")))
def test_hand_written_examples_pass_structure(path):
    r = campaign_simulator.simulate(json.loads(path.read_text()))
    assert r["status"] == "pass", [c for c in r["checks"] if c["status"] == "fail"]
