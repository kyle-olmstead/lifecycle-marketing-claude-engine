"""Contracts: every JSON Schema and its Pydantic twin accept the same good documents and reject the same bad ones.

The schemas are what other tools (an ESP builder, a reviewer, CI in another repo) validate against; the Pydantic models
are what the engine runs. A field one accepts and the other rejects is a plan that validates and then fails, or the
reverse, so each pair is checked on the same cases."""
import copy
import json

import pytest
from pydantic import ValidationError

from engine import schemas
from engine.policy.contracts import ConsentRecord, FrequencyPolicy, OfferPolicy, SendingGateRecord, TriggerDefinition
from tests.conftest import ROOT

GATE = {"verdict": "GREEN", "assessed_at": "2026-10-20T12:00:00Z", "auth_label": "LIVE PULL", "domain": "northfield.example",
        "source": "lcm deliverability check northfield.example", "signals": {"complaint_rate": 0.0004}, "critical_findings": []}
TRIGGER = {"flow_id": "cart_abandon", "type": "event", "event": {"event": "checkout_started"}, "abandon_after": "PT1H",
           "exclude_if": [{"event": "order_completed"}], "re_entry": {"mode": "cooldown", "cooldown": "P7D"},
           "dedupe_key": "user_id + checkout_id", "source": "esp_native"}
FREQ = {"caps": [{"channels": ["email", "sms", "push"], "max": 3, "window": "P7D"}, {"channels": ["sms"], "max": 1, "window": "P1D"}],
        "send_windows": [{"channel": "email", "allowed_from": "08:00", "allowed_until": "21:00"},
                         {"channel": "sms", "allowed_from": "09:00", "allowed_until": "20:00"}],
        "flow_priority": ["dunning", "cart_abandon", "winback"], "collision_rule": "higher_priority_preempts"}
OFFER = {"offers": [{"offer_id": "second10", "kind": "percent_off", "value": 10, "eligible": "second order", "expires_after": "P3D"}],
         "max_offers_per_person": {"count": 1, "window": "P30D"}, "min_days_between_offers": 30, "never_in_first_touch": True,
         "exclude_if": ["discount_share_over_50pct_last_3_orders"]}
CONSENT = {"user_id": "u_0001", "channel": "sms", "status": "granted", "purpose": "marketing", "source": "checkout_checkbox",
           "occurred_at": "2026-09-01T10:00:00Z"}


def _with(doc, path, value):
    """A copy of doc with path (a list of keys/indexes) set to value, or deleted when value is DELETE."""
    out = copy.deepcopy(doc)
    cur = out
    for k in path[:-1]:
        cur = cur[k]
    if value is DELETE:
        del cur[path[-1]]
    else:
        cur[path[-1]] = value
    return out


DELETE = object()
PAIRS = {
    "sending_gate.schema.json": (SendingGateRecord, GATE, [
        (["verdict"], "BLUE"), (["auth_label"], "GUESSED"), (["assessed_at"], DELETE), (["unknown_field"], 1),
        (["verdict"], DELETE)]),
    "trigger.schema.json": (TriggerDefinition, TRIGGER, [
        (["type"], "whenever"), (["flow_id"], "Cart Abandon"), (["dedupe_key"], DELETE), (["source"], "carrier_pigeon"),
        (["surprise"], True)]),
    "frequency_policy.schema.json": (FrequencyPolicy, FREQ, [
        (["caps"], []), (["caps", 0, "max"], -1), (["collision_rule"], "whoever_is_first"),
        (["send_windows", 0, "channel"], "fax"), (["caps", 0, "window"], "a week"), (["send_windows", 1, "allowed_from"], "9am")]),
    "offer_policy.schema.json": (OfferPolicy, OFFER, [
        (["offers", 0, "kind"], "bogo_free_car"), (["offers", 0, "value"], -5), (["min_days_between_offers"], -1),
        (["exclude_if"], ["everyone_i_dislike"]), (["offers", 0, "expires_after"], "soon")]),
    "consent_record.schema.json": (ConsentRecord, CONSENT, [
        (["channel"], "carrier_pigeon"), (["status"], "maybe"), (["user_id"], DELETE), (["occurred_at"], DELETE),
        (["purpose"], "spam")]),
}


def _schema_ok(name, doc):
    return not schemas.errors(name, doc)


def _model_ok(model, doc):
    try:
        model.model_validate(json.loads(json.dumps(doc)))
        return True
    except (ValidationError, ValueError):
        return False


@pytest.mark.parametrize("name", sorted(PAIRS))
def test_schema_and_model_accept_the_good_document(name):
    model, good, _ = PAIRS[name]
    assert _schema_ok(name, good), schemas.errors(name, good)
    assert _model_ok(model, good)


@pytest.mark.parametrize("name,case", [(n, c) for n in sorted(PAIRS) for c in range(len(PAIRS[n][2]))])
def test_schema_and_model_reject_the_same_bad_documents(name, case):
    model, good, bad = PAIRS[name]
    path, value = bad[case]
    doc = _with(good, path, value)
    assert not _schema_ok(name, doc), f"schema accepted {path}={value!r}"
    assert not _model_ok(model, doc), f"model accepted {path}={value!r}"


def test_a_green_gate_without_observed_evidence_is_refused_by_the_model():
    # the schema cannot express "GREEN needs LIVE PULL" across fields as plainly; the model refuses it, and effective_gate reads it AMBER
    assert not _model_ok(SendingGateRecord, {**GATE, "auth_label": "SUPPLIED"})


def _inline(node, root):
    """node with every local $ref replaced by its target, and descriptions dropped, so two layouts compare equal."""
    if isinstance(node, dict):
        if "$ref" in node and node["$ref"].startswith("#/"):
            target = root
            for part in node["$ref"][2:].split("/"):
                target = target[part]
            return _inline(target, root)
        return {k: _inline(v, root) for k, v in node.items() if k not in ("$schema", "$id", "$defs", "title", "description")}
    if isinstance(node, list):
        return [_inline(x, root) for x in node]
    return node


def test_trigger_schema_matches_the_flow_spec_copy():
    standalone = json.loads((ROOT / "schemas/trigger.schema.json").read_text())
    spec = json.loads((ROOT / "schemas/flow_spec.schema.json").read_text())
    assert _inline(standalone, standalone) == _inline(spec["$defs"]["trigger"], spec)


def test_every_planned_flow_writes_a_trigger_both_contracts_accept():
    from engine.agents import campaign_architect, cohort_analyst
    from engine.brief import parse_text
    from engine.flows.library import FLOWS
    sentences = {"welcome": "a welcome series", "activation": "an activation flow for users who signed up but have not created a report",
                 "winback": "a win-back for customers who have not ordered in 60 days", "cart_abandon": "an abandoned cart flow",
                 "browse_abandon": "a browse abandonment flow", "post_purchase": "a post-purchase flow",
                 "replenishment": "a replenishment flow every 30 days", "dunning": "a failed payment flow",
                 "trial_convert": "a trial conversion flow for a 14 day trial", "sunset": "a sunset flow for subscribers inactive 180 days"}
    assert set(sentences) == set(FLOWS)
    for key, text in sentences.items():
        b = parse_text(text, "Acme", defaults={})
        assert b.flow_type == key, (key, b.flow_type)
        plan = campaign_architect.design(b, cohort_analyst.define(b, None))
        trig = plan.flow_spec["trigger"]["typed"]
        assert _schema_ok("trigger.schema.json", trig), (key, schemas.errors("trigger.schema.json", trig))
        assert _model_ok(TriggerDefinition, trig), key


def test_the_workspace_template_holds_valid_policies():
    import yaml
    cfg = yaml.safe_load((ROOT / "templates/workspace/lmm.yaml").read_text())
    assert _schema_ok("workspace.schema.json", cfg), schemas.errors("workspace.schema.json", cfg)
    assert _schema_ok("frequency_policy.schema.json", cfg["frequency"]) and _model_ok(FrequencyPolicy, cfg["frequency"])
    assert _schema_ok("offer_policy.schema.json", cfg["offer_policy"]) and _model_ok(OfferPolicy, cfg["offer_policy"])


def test_each_cap_phrase_keeps_its_own_channel():
    from engine.policy.contracts import parse_cap_text
    caps = parse_cap_text("no more than 2 texts a day and 6 emails a month")
    assert [(c.channels, c.max, c.window) for c in caps] == [(["sms"], 2, "P1D"), (["email"], 6, "P30D")]
    assert parse_cap_text("three messages a week")[0].channels == ["email", "sms", "push"]
