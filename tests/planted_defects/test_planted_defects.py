"""Planted defects: one broken artifact per checker, and each checker must catch its own.

A checker that never fails is indistinguishable from one that is not running. Each case here hands a checker the
specific mistake it exists to catch (the kind that ships when it is missing) and requires the failure, by name."""
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from engine import schemas
from tests.conftest import ROOT


def test_a_typed_green_gate_runs_as_amber():
    from engine.policy.contracts import effective_gate
    assert effective_gate("GREEN")[0] == "AMBER"
    stale = {"verdict": "GREEN", "assessed_at": (datetime.now(timezone.utc) - timedelta(days=45)).isoformat(), "auth_label": "LIVE PULL"}
    assert effective_gate(stale)[0] == "AMBER"


def test_a_dangling_branch_fails_the_flow_spec_schema():
    spec = json.loads((ROOT / "examples/flows/activation_customerio.json").read_text())
    assert not schemas.errors("flow_spec.schema.json", spec)
    bad = copy.deepcopy(spec)
    step = next(s for s in bad["steps"] if s.get("branch"))
    step["branch"]["yes"] = "no_such_step"
    from engine.policy.simulator import simulate
    r = simulate(bad)
    failed = [c for c in r.get("checks", []) if c.get("status") == "fail"]
    assert r.get("status") == "fail" or failed, r
    assert any("no_such_step" in json.dumps(c) for c in failed) or "no_such_step" in json.dumps(r)


def test_an_email_without_an_unsubscribe_link_fails_lint():
    from engine.render.template_lint import lint
    html = ('<html><head><meta name="color-scheme" content="light dark"></head><body><div>preheader</div><p>'
            + "Plenty of words. " * 30 + "</p></body></html>")
    r = lint(html, "klaviyo", email=True)
    assert not r["ok"] and any("unsubscribe" in e for e in r["errors"])


def test_an_sms_without_opt_out_fails_the_guardrail():
    from engine.policy.guardrails import check_sms
    errors, _, _ = check_sms("Acme: your cart is saved. https://ex.co/a1B2c3d", "Acme", first_message=False)
    assert any("opt-out" in e for e in errors)


def test_a_discount_the_policy_does_not_allow_fails_the_copy_check():
    from engine.policy.guardrails import check_text
    errors, _ = check_text("Take 20% off today only", "no discounts or offers", "email")
    assert any("offer" in e for e in errors)


def test_sql_that_does_not_parse_fails_the_sql_check():
    from engine.sql.check import parse
    ok, _ = parse("SELECT user_id, FROM WHERE users", "postgres")
    assert not ok


def test_a_send_hour_outside_the_sms_window_is_refused_when_the_brief_is_read():
    # day-level flows send at send_hour_local; cart and browse flows are timed from the event and delayed into the window
    from engine.brief import parse_text
    with pytest.raises(ValueError, match="outside the permitted"):
        parse_text("a 5-touch win-back with email and sms for users inactive 14 days", "Acme", defaults={}, overrides={"send_hour_local": 7})


def test_a_low_contrast_brand_kit_is_rejected():
    import yaml
    from engine.render.brand import BrandKitError, parse_brand
    kit = yaml.safe_load((ROOT / "templates/brand/brand.yaml").read_text())
    kit["colors"]["text"] = "#EEEEEE"            # light gray text on the white card
    with pytest.raises(BrandKitError):
        parse_brand(kit)


def test_a_future_consent_timestamp_fails_check_data(tmp_path):
    from engine.data.check import check_folder
    (tmp_path / "users.csv").write_text("user_id,email,created_at,email_consent_at\nu1,u1@example.test,2026-01-01T00:00:00Z,2099-01-01T00:00:00Z\n")
    (tmp_path / "track_events.csv").write_text("event_id,user_id,event_name,event_at\ne0000001,u1,signed_up,2026-01-01T00:00:00Z\n")
    r = check_folder(tmp_path)
    assert not r["ok"] and any(f["column"] == "users.email_consent_at" or "email_consent_at" in str(f) for f in r["findings"])


def test_two_flows_that_break_a_cap_together_are_flagged():
    from engine.brief import load_brief
    from engine.policy import program
    from engine import workspace
    root = ROOT / "examples/brands/northfield_dtc"
    d = workspace.brief_defaults(workspace.Workspace(root))
    briefs = [load_brief(p, defaults=d) for p in sorted((root / "briefs/active").glob("*.yaml"))]
    r = program.simulate(briefs, program.FrequencyPolicy.model_validate(d["frequency"]))
    cross = next(c for c in r["checks"] if c["name"].startswith("caps hold across flows"))
    assert cross["status"] == "warn" and "5 in P7D" in cross["detail"]


def test_a_template_reading_an_unplanned_field_is_named():
    from engine.render.fields import unknown
    assert unknown("{{ event.discount_code | default: '' }}", "customerio", "checkout_started") == ["discount_code"]


def test_a_flow_the_engine_does_not_build_is_refused_by_name():
    from engine.brief import parse_text
    with pytest.raises(ValueError, match="not supported yet"):
        parse_text("a referral program for happy customers", "Acme", defaults={})


def test_a_duplicate_flow_priority_fails_the_frequency_contract():
    from pydantic import ValidationError
    from engine.policy.contracts import FrequencyPolicy
    bad = {"caps": [{"channels": ["email"], "max": 3, "window": "P7D"}], "flow_priority": ["winback", "winback"]}
    with pytest.raises(ValidationError):
        FrequencyPolicy.model_validate(bad)
    assert schemas.errors("frequency_policy.schema.json", bad)


def test_a_readout_on_a_tiny_holdout_is_not_reported_as_a_lift():
    from engine.measurement.stats import readout
    r = readout(4, 30, 1, 5)
    assert r["read"] == "not readable" and "lift_rel" not in r
