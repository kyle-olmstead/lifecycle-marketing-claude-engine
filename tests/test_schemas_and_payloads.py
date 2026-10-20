import json
import xml.etree.ElementTree as ET

import jsonschema
import pytest

from tests.conftest import ROOT, fenced
from tools.payloads import HUBSPOT_MAX_INPUTS, KLAVIYO_MAX_PROFILES, hubspot_batches, klaviyo_jobs

EVENTS = json.loads((ROOT / "modules/02_behavioral_triggers_and_flows/schemas/custom_event_definitions.json").read_text())
FLOW = json.loads((ROOT / "schemas/flow_spec.schema.json").read_text())


def test_event_schema_accepts_valid_and_rejects_invalid_examples():
    v = jsonschema.Draft202012Validator(EVENTS, format_checker=jsonschema.FormatChecker())
    for e in json.loads((ROOT / "examples/events/valid_events.json").read_text()):
        assert not list(v.iter_errors(e)), e["event"]
    for case in json.loads((ROOT / "examples/events/invalid_events.json").read_text()):
        assert list(v.iter_errors(case["event"])), case["why_invalid"]


def test_every_event_in_the_plan_names_what_it_unlocks():
    plan = {p["event"]: p for p in EVENTS["x-tracking-plan"]}
    assert set(plan) == set(EVENTS["properties"]["event"]["enum"])
    assert all(p["unlocks"] for p in plan.values())


def _flow_specs():
    specs = [json.loads(p.read_text()) for p in (ROOT / "examples/flows").glob("*.json")]
    for md in (ROOT / "modules").rglob("*.md"):
        specs += [json.loads(b) for b in fenced(md, "json", "flow_spec")]
    return specs


def test_every_flow_spec_in_the_repo_validates():
    specs = _flow_specs()
    assert len(specs) >= 4
    v = jsonschema.Draft202012Validator(FLOW)
    for s in specs:
        assert not list(v.iter_errors(s)), s["flow"]


def test_flow_schema_enforces_consent_and_persistent_holdout():
    spec = json.loads((ROOT / "examples/flows/activation_customerio.json").read_text())
    v = jsonschema.Draft202012Validator(FLOW)
    bad = json.loads(json.dumps(spec))
    bad["steps"][0]["requires"] = []
    assert list(v.iter_errors(bad))
    bad = json.loads(json.dumps(spec))
    bad["holdout"]["persistent"] = False
    assert list(v.iter_errors(bad))
    bad = json.loads(json.dumps(spec))
    bad["entry_filters"][0]["evaluated"] = "every_step"
    assert list(v.iter_errors(bad))


@pytest.mark.parametrize("path", ["core/context_loader.xml", "examples/context/ridgeview_context.xml"])
def test_context_files_parse_and_carry_required_fields(path):
    tree = ET.parse(ROOT / path)
    required = {e.tag for e in tree.iter() if e.get("required") == "true"}
    assert {"brand_name", "legal_name", "postal_address", "business_model", "transaction_object", "customer_definition", "messaging_platform"} <= required


def test_klaviyo_jobs_split_at_limit_and_drop_nulls():
    rows = [{"email": f"u{i}@example.com", "seg": "champions" if i % 2 else ""} for i in range(KLAVIYO_MAX_PROFILES + 5)]
    bodies, report = klaviyo_jobs(rows, "email", [("seg", "lmm_rfm_segment")])
    assert len(bodies) == 2 and report["profiles"] == KLAVIYO_MAX_PROFILES + 5
    assert report["dropped_null_values"] == (KLAVIYO_MAX_PROFILES + 5) // 2 + 1 - ((KLAVIYO_MAX_PROFILES + 5) % 2 == 0)
    assert bodies[0]["data"]["type"] == "profile-bulk-import-job"


def test_hubspot_batches_and_property_names():
    rows = [{"email": f"u{i}@example.com", "seg": "loyal"} for i in range(250)]
    bodies, _report = hubspot_batches(rows, "email", [("seg", "lmm_rfm_segment")])
    assert [len(b["inputs"]) for b in bodies] == [HUBSPOT_MAX_INPUTS, HUBSPOT_MAX_INPUTS, 50]
    assert bodies[0]["inputs"][0] == {"idProperty": "email", "id": "u0@example.com", "properties": {"lmm_rfm_segment": "loyal"}}
    with pytest.raises(ValueError):
        hubspot_batches(rows, "email", [("seg", "RFM Segment")])
