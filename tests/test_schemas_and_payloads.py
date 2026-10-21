import json
import xml.etree.ElementTree as ET

import jsonschema
import pytest

from tests.conftest import ROOT, fenced
from adapters.destinations.payloads import HUBSPOT_MAX_INPUTS, KLAVIYO_MAX_PROFILES, hubspot_batches, klaviyo_jobs

EVENTS = json.loads((ROOT / "config/tracking_plan.json").read_text())
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
    for md in (ROOT / "prompts").rglob("*.md"):
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


@pytest.mark.parametrize("path", ["templates/context/context_loader.xml", "examples/brands/ridgeview_saas/context.xml"])
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


# ---- regressions from the v2.2 audit (docs/AUDIT.md) ----

def test_leading_zeros_and_long_ids_stay_strings():
    from adapters.destinations.payloads import _coerce
    assert _coerce("02134") == "02134" and _coerce("1234567890123456") == "1234567890123456"
    assert _coerce("0") == 0 and _coerce("42") == 42 and _coerce("0.5") == 0.5 and _coerce("TRUE") is True


def test_hubspot_values_are_the_source_strings():
    bodies, _ = hubspot_batches([{"email": " A@Example.com ", "zip": "02134", "vip": True, "score": "7.50"}], "email",
                                [("zip", "zip"), ("vip", "vip"), ("score", "score")])
    inp = bodies[0]["inputs"][0]
    assert inp["id"] == "a@example.com" and inp["properties"] == {"zip": "02134", "vip": "true", "score": "7.50"}


def test_read_rows_handles_bom_and_newlines_inside_quotes(tmp_path):
    from adapters.destinations.payloads import read_rows
    p = tmp_path / "rows.csv"
    p.write_bytes('﻿email,note\na@example.com,"line one\nline two"\n'.encode("utf-8"))
    rows = read_rows(p)
    assert rows == [{"email": "a@example.com", "note": "line one\nline two"}]


def test_written_request_files_are_the_size_that_was_checked(tmp_path):
    from adapters.destinations import payloads
    rows = tmp_path / "rows.csv"
    rows.write_text("email,seg\n" + "".join(f"u{i}@example.com,s{i % 3}\n" for i in range(50)))
    assert payloads._cli(["klaviyo", str(rows), "--properties", "seg", "--out", str(tmp_path / "k")]) == 0
    text = (tmp_path / "k" / "klaviyo_batch_0001.json").read_text()
    assert "\n" not in text and len(text.encode()) == payloads._bytes(json.loads(text))


@pytest.mark.parametrize("platform,schema", [("klaviyo", "klaviyo_profile_bulk_import"), ("hubspot", "hubspot_contacts_batch_upsert"),
                                             ("braze", "braze_users_track"), ("customerio", "customerio_pipelines_batch")])
def test_attribute_sync_requests_match_the_endpoint_schemas(platform, schema):
    from adapters.destinations.payloads import attribute_sync
    cfg = json.loads((ROOT / "config/esp_payload_schemas.json").read_text())
    sub = {"$schema": cfg["$schema"], "$defs": cfg["$defs"], "$ref": f"#/$defs/{schema}"}
    rows = [{"user_id": f"u{i}", "email": f"u{i}@example.com", "lcm_engagement_tier": "unengaged", "lcm_sunset": True} for i in range(160)]
    reqs, report = attribute_sync(platform, rows)
    assert reqs and report["rows"] == 160
    for r in reqs:
        assert "${" in json.dumps(r["headers"]) and r["url"].startswith("https://")
        jsonschema.validate(r["body"], sub)
    if platform == "braze":
        assert [len(r["body"]["attributes"]) for r in reqs] == [75, 75, 10]
        assert all(o["_update_existing_only"] for r in reqs for o in r["body"]["attributes"])
    if platform == "hubspot":
        assert reqs[0]["body"]["inputs"][0]["properties"]["lcm_sunset"] == "true"
