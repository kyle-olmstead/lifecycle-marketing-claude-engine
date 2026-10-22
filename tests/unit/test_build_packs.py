"""ESP build packs: every flow on every platform, native triggers for absence flows, capability checks against the account,
artifacts valid against config/esp_artifact_schemas.json, and lcm build --create through a mocked API (no network)."""
import json

import httpx
import jsonschema
import pytest

from adapters.destinations.build import build, write
from adapters.destinations.build import cli as build_cli
from adapters.destinations.build import validate as V
from adapters.destinations.build.context import capabilities
from engine.flows.library import FLOWS
from engine.models import Brief
from engine.pipeline import orchestrator
from tests.conftest import ROOT

PLATFORMS = ("klaviyo", "hubspot", "braze", "customerio", "iterable", "sfmc")
UNIT = {"braze": "seconds", "klaviyo": "minutes", "hubspot": "minutes", "iterable": "minutes", "sfmc": "minutes", "customerio": "hours"}
EM_DASH, EN_DASH = chr(0x2014), chr(0x2013)
_PLANS = {}


def _plan(platform, flow, channels=("email", "sms", "push"), **update):
    key = (platform, flow, tuple(channels), tuple(sorted(update.items())))
    if key not in _PLANS:
        fd = FLOWS[flow]
        bm = "ecommerce" if fd.models == ("ecommerce",) else "saas"
        b = Brief(product_name="Northfield" if bm == "ecommerce" else "Ridgeview", flow_type=flow, platform=platform, business_model=bm,
                  postal_address="1 Example Way, Springfield, OR 97477", channels=list(channels), **update)
        _PLANS[key] = orchestrator.run(b, copy_mode="offline")
    return _PLANS[key]


def test_capabilities_file_keeps_a_source_and_confidence_on_every_fact():
    caps = capabilities()
    assert set(caps["platforms"]) == set(PLATFORMS)
    for p, facts in caps["platforms"].items():
        for name, f in facts.items():
            if name == "label":
                continue
            assert f["source"] and f["confidence"] in ("VERIFIED", "LIKELY", "UNVERIFIED"), (p, name)
    for c in caps["checks"]:
        assert c["source"] and c["confidence"] in ("VERIFIED", "LIKELY", "UNVERIFIED") and c["kind"] in ("requirement", "limit", "advisory", "info"), c["id"]
    hub = {c["id"]: c for c in caps["checks"]}["hubspot.custom_event_enrollment"]
    assert hub["requires"]["tier_in"] == ["professional", "enterprise"] and hub["confidence"] == "VERIFIED"   # no longer Enterprise only (2026)


def test_artifact_schemas_are_valid_and_name_their_sources():
    cfg = json.loads((ROOT / "config/esp_artifact_schemas.json").read_text())
    jsonschema.Draft202012Validator.check_schema(cfg)
    for name in ("klaviyo_flow_create", "klaviyo_segment_create", "klaviyo_template_create", "hubspot_flow_create", "sfmc_interaction_create",
                 "braze_canvas_duplicate", "build_sheet"):
        d = cfg["$defs"][name]
        assert d["x-endpoint"]["docs"] and "x-unverified" in d, name


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("flow", sorted(FLOWS))
def test_every_flow_builds_on_every_platform(platform, flow):
    plan = _plan(platform, flow)
    pack = build(plan)
    assert pack.status != "BLOCKED", pack.blocked_reasons
    assert all(a.valid is not False for a in pack.artifacts), [(a.name, a.errors[:3]) for a in pack.artifacts]
    waits = [s.wait for s in pack.steps if s.wait and s.wait.seconds]
    assert waits and all(w.unit == UNIT[platform] or (platform == "customerio" and w.unit == "minutes" and w.seconds % 3600) for w in waits)
    assert all(w.value * {"seconds": 1, "minutes": 60, "hours": 3600}[w.unit] == w.seconds for w in waits)
    if plan.brief.holdout_pct > 0:
        assert pack.steps[0].split and pack.steps[0].split.kind == "holdout"
    for s in pack.steps:
        if s.split and s.split.kind == "consent":
            ids = [x.id for x in pack.steps]
            assert s.split.yes in ids and s.split.no in ids and s.split.no.endswith("_email_fallback")
    sends = [s for s in pack.steps if s.kind == "send" and not s.manual]
    assert sends and all(s.content and s.content.chosen for s in sends)
    assert pack.trigger.exit_criteria and pack.settings.status_on_create
    kinds = {a.kind for a in pack.artifacts}
    if platform in ("klaviyo", "hubspot", "sfmc"):
        assert "create_request" in kinds
    else:
        assert "build_sheet" in kinds and "create_request" not in kinds
    text = json.dumps(pack.model_dump())
    assert EM_DASH not in text and EN_DASH not in text


@pytest.mark.parametrize("platform,mode", [("klaviyo", "native_segment"), ("hubspot", "native_segment"), ("customerio", "native_segment"),
                                           ("braze", "scheduled_segment"), ("iterable", "scheduled_segment"), ("sfmc", "scheduled_segment")])
def test_winback_triggers_natively_and_keeps_the_warehouse_job_as_the_alternative(platform, mode):
    plan = _plan(platform, "winback", channels=("email",))
    pack = build(plan)
    assert pack.mode == mode and pack.trigger.segment and pack.trigger.segment.conditions
    assert "warehouse" in pack.trigger.warehouse_alternative and "--warehouse-job" in pack.trigger.warehouse_alternative
    if platform != "sfmc":
        assert pack.trigger.holdout.startswith("split on the person property lcm_holdout_")
        assert any(a.kind == "holdout_sync_sql" for a in pack.artifacts)
    job = build(plan, native=False)
    assert job.mode == "warehouse_job" and job.trigger.native_alternative and job.trigger.segment is None
    assert job.trigger.event == {"hubspot": f"pe<hub id>_{plan.cohort.trigger_event}"}.get(platform, plan.cohort.trigger_event) or platform == "sfmc"


def test_klaviyo_winback_creates_the_segment_then_a_segment_triggered_draft_flow():
    pack = build(_plan("klaviyo", "winback", channels=("email", "sms")))
    seg = next(a for a in pack.artifacts if a.name == "klaviyo_segment")
    conds = [c for g in seg.body["data"]["attributes"]["definition"]["condition_groups"] for c in g["conditions"]]
    assert {c["timeframe_filter"]["operator"] for c in conds} == {"alltime", "in-the-last"} and all(c["type"] == "profile-metric" for c in conds)
    flow = next(a for a in pack.artifacts if a.name == "klaviyo_flow")
    d = flow.body["data"]["attributes"]["definition"]
    assert d["triggers"] == [{"type": "segment", "id": "<<klaviyo_segment:ridgeview_winback_14d>>"}]
    assert d["reentry_criteria"] == {"duration": 0, "unit": "alltime"}           # Re-enter whenever they re-qualify: once per lapse
    acts = {a["temporary_id"]: a for a in d["actions"]}
    assert d["entry_action_id"] == "holdout_split" and acts["holdout_split"]["type"] == "conditional-split"
    assert acts["holdout_split"]["links"]["next_if_true"] is None             # the holdout exits
    assert all(a["data"].get("status", "draft") == "draft" for a in d["actions"])
    assert {a["data"]["unit"] for a in d["actions"] if a["type"] == "time-delay"} <= {"days", "hours", "minutes"}
    sms = acts["t4_sms"]["data"]["message"]
    assert sms["sms_quiet_hours_enabled"] is True and sms["additional_filters"]["condition_groups"][0]["conditions"][0]["consent"]["channel"] == "sms"
    fb = acts["t4_email_fallback"]["data"]["message"]["additional_filters"]["condition_groups"][0]["conditions"][0]["consent"]
    assert fb["can_receive_marketing"] is False                                   # the email fallback goes to everyone without SMS consent
    assert acts["sunset_update"]["data"]["profile_operations"][0]["property_key"] == "lcm_sunset"
    assert [a.order for a in pack.artifacts if a.kind == "create_request"] == sorted(a.order for a in pack.artifacts if a.kind == "create_request")


def test_klaviyo_cart_flow_splits_the_holdout_on_the_trigger_event():
    pack = build(_plan("klaviyo", "cart_abandon"))
    d = next(a for a in pack.artifacts if a.name == "klaviyo_flow").body["data"]["attributes"]["definition"]
    assert d["triggers"][0] == {"type": "metric", "id": "<<klaviyo_metric:Started Checkout>>"}     # the copy reads Shopify's line items
    assert build(_plan("klaviyo", "cart_abandon"), account={"options": {"shopify": False}}).trigger.event == "checkout_started"
    split = next(a for a in d["actions"] if a["temporary_id"] == "holdout_split")
    assert split["type"] == "trigger-split" and split["data"]["trigger_filter"]["condition_groups"][0]["conditions"][0]["field"] == "in_holdout"
    since = d["profile_filter"]["condition_groups"][0]["conditions"][0]
    assert since["timeframe_filter"] == {"type": "date", "operator": "flow-start"} and since["measurement_filter"]["value"] == 0
    assert not V.errors(next(a for a in pack.artifacts if a.name == "klaviyo_flow").body, "klaviyo_flow_create")


def test_replenishment_is_native_with_the_supply_delay_first():
    pack = build(_plan("customerio", "replenishment", channels=("email",), replenish_after_days=30))
    assert pack.mode == "native_event"
    first = next(s for s in pack.steps if s.kind == "send")
    assert first.wait.seconds == (30 - 5) * 86400 and first.wait.unit == "hours"


@pytest.mark.parametrize("tier,status", [("starter", "BLOCKED"), ("professional", None), (None, None)])
def test_hubspot_custom_event_enrollment_needs_professional(tier, status):
    pack = build(_plan("hubspot", "cart_abandon", channels=("email",)), account={"platform": "hubspot", "tier": tier})
    c = next(c for c in pack.checks if c.id == "hubspot.custom_event_enrollment")
    assert c.status == {"starter": "fail", "professional": "pass", None: "warn"}[tier]
    if status:
        assert pack.status == status and any("custom_event_enrollment" in r for r in pack.blocked_reasons)
    freq = next(c for c in pack.checks if c.id == "hubspot.frequency_safeguard")
    assert freq.status in ("warn",) and not freq.hard                              # the cap is Enterprise only, and soft


@pytest.mark.parametrize("ctx_on,expect", [(True, "pass"), (False, "fail"), (None, "warn")])
def test_braze_product_block_needs_canvas_context(ctx_on, expect):
    acc = {"platform": "braze", "options": {} if ctx_on is None else {"canvas_context": ctx_on}}
    pack = build(_plan("braze", "cart_abandon", channels=("email",)), account=acc)
    assert next(c for c in pack.checks if c.id == "braze.canvas_context").status == expect
    assert (pack.status == "BLOCKED") == (expect == "fail")


def test_customerio_legacy_liquid_blocks_and_message_limits_are_opt_in():
    pack = build(_plan("customerio", "welcome", channels=("email",)), account={"platform": "customerio", "options": {"liquid": "legacy"}})
    assert pack.status == "BLOCKED"
    assert next(c for c in pack.checks if c.id == "customerio.message_limits_opt_in").status == "warn"


def test_an_event_missing_from_the_esp_blocks_the_native_trigger_and_names_the_job():
    pack = build(_plan("klaviyo", "winback", channels=("email",)), account={"options": {"events_in_esp": ["activation_reached"]}})
    assert pack.status == "BLOCKED" and any("feature_used" in r and "--warehouse-job" in r for r in pack.blocked_reasons)
    ok = build(_plan("klaviyo", "winback", channels=("email",)), account={"options": {"events_in_esp": ["activation_reached", "feature_used"]}})
    assert next(c for c in ok.checks if c.id == "all.events_in_esp").status == "pass"


def test_copy_in_another_dialect_blocks_the_pack():
    pack = build(_plan("klaviyo", "cart_abandon", channels=("email",)), platform="braze")
    assert pack.status == "BLOCKED" and any("all.content_dialect" in r for r in pack.blocked_reasons)


def test_unverified_fields_are_listed_on_the_artifact_and_in_the_pack():
    sf = build(_plan("sfmc", "cart_abandon", channels=("email",)))
    art = next(a for a in sf.artifacts if a.name == "sfmc_journey")
    assert any("MULTICRITERIADECISION" in u and "UNVERIFIED" in u for u in art.unverified_fields)
    assert any(f.confidence == "UNVERIFIED" for f in sf.unverified)
    hs = build(_plan("hubspot", "cart_abandon", channels=("email",)))
    art = next(a for a in hs.artifacts if a.name == "hubspot_workflow")
    assert art.body["isEnabled"] is False and art.url.endswith("/automation/v4/flows")
    assert any("LIST_BRANCH" in u for u in art.unverified_fields)


def test_braze_duplicate_request_is_optional_and_unverified():
    plan = _plan("braze", "welcome", channels=("email",))
    assert not any(a.kind == "duplicate_request" for a in build(plan).artifacts)
    pack = build(plan, template_canvas="tmpl-123")
    dup = next(a for a in pack.artifacts if a.kind == "duplicate_request")
    assert dup.body["canvas_id"] == "tmpl-123" and dup.url.endswith("/canvas/duplicate") and dup.valid
    assert any(f.confidence == "UNVERIFIED" for f in pack.unverified)


def test_write_lays_out_the_pack(tmp_path):
    pack = build(_plan("iterable", "browse_abandon", channels=("email", "push")))
    files = write(pack, tmp_path / "build")
    assert (tmp_path / "build/BUILD.md").is_file() and (tmp_path / "build/artifacts/build_sheet.json").is_file()
    md = (tmp_path / "build/BUILD.md").read_text()
    assert "## Click by click" in md and "Yes/No Split" in md and "## Relied on, not verified" in md
    sheet = json.loads((tmp_path / "build/artifacts/build_sheet.json").read_text())
    assert not V.errors(sheet, "build_sheet") and sheet["steps"][0]["action"] == "Yes/No Split"
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert EM_DASH not in text and EN_DASH not in text, f


# ----------------------------------------------------------------------------------------------
# the CLI and --create, through httpx.MockTransport (no network)
# ----------------------------------------------------------------------------------------------

def _folder(tmp_path, platform, flow, channels=("email",)):
    plan = _plan(platform, flow, channels=channels)
    out = orchestrator.write_outputs(plan, tmp_path / "out")
    return out


def _no_network():
    def handler(request):
        raise AssertionError(f"no request may be sent: {request.method} {request.url}")
    return httpx.MockTransport(handler)


def test_cli_writes_the_pack_and_a_dry_run_creates_nothing(tmp_path, capsys):
    folder = _folder(tmp_path, "klaviyo", "cart_abandon")
    assert build_cli._cli(["--run", str(folder), "--json"], env={}, transport=_no_network()) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["platform"] == "klaviyo" and (folder / "build/BUILD.md").is_file() and (folder / "build/artifacts/klaviyo_flow.json").is_file()
    rc = build_cli._cli(["--run", str(folder), "--create", "--json"], env={"KLAVIYO_PRIVATE_KEY": "k"}, transport=_no_network())
    created = json.loads(capsys.readouterr().out)["create"]
    assert rc == 0 and created["mode"].startswith("dry run") and created["created"] == [] and len(created["calls"]) == 4
    assert all("${KLAVIYO_PRIVATE_KEY}" in c["headers"]["Authorization"] for c in created["calls"])


def test_create_approve_makes_templates_then_the_draft_flow_and_never_twice(tmp_path, capsys):
    folder = _folder(tmp_path, "klaviyo", "cart_abandon")
    posts = []

    def handler(request):
        assert request.headers["Authorization"] == "Klaviyo-API-Key secret" and request.headers["revision"] == "2026-10-15"
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "M_CS", "attributes": {"name": "Started Checkout"}},     # Shopify's metrics
                                                      {"id": "M_OC", "attributes": {"name": "Placed Order"}}], "links": {"next": None}})
        body = json.loads(request.content)
        posts.append((str(request.url), body))
        if request.url.path == "/api/templates":
            return httpx.Response(201, json={"data": {"id": f"T{len(posts)}", "type": "template"}})
        return httpx.Response(201, json={"data": {"id": "FLOW1", "type": "flow"}})

    args = ["--run", str(folder), "--create", "--approve", "--id", "from_email=hello@northfield.example", "--id", "from_label=Northfield", "--json"]
    rc = build_cli._cli(args, env={"KLAVIYO_PRIVATE_KEY": "secret"}, transport=httpx.MockTransport(handler))
    res = json.loads(capsys.readouterr().out)["create"]
    assert rc == 0 and res["refused"] is None, res
    assert [u.rsplit("/", 1)[-1] for u, _ in posts] == ["templates", "templates", "templates", "flows"]
    flow = posts[-1][1]["data"]["attributes"]["definition"]
    assert flow["triggers"][0]["id"] == "M_CS" and "<<" not in json.dumps(flow)
    emails = [a["data"]["message"]["template_id"] for a in flow["actions"] if a["type"] == "send-email"]
    assert emails == ["T1", "T2", "T3"] and res["created"][-1] == {"artifact": "klaviyo_flow", "id": "FLOW1", "state": "Draft: the flow and every action in draft"}
    receipts = (folder / "build/create_receipts.jsonl").read_text().splitlines()
    assert len(receipts) == 4 and all(json.loads(r)["status"] == "accepted" for r in receipts)
    posts.clear()
    assert build_cli._cli(args, env={"KLAVIYO_PRIVATE_KEY": "secret"}, transport=httpx.MockTransport(handler)) == 0
    again = json.loads(capsys.readouterr().out)["create"]
    assert posts == [] and len(again["skipped"]) == 4                              # a rerun never creates the same object twice


def test_create_never_retries_a_5xx(tmp_path, capsys):
    folder = _folder(tmp_path, "hubspot", "welcome")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(502, text="bad gateway")
    ids = []
    pack = build(folder)
    for ph in pack.inputs_required:
        ids += ["--id", f"{ph[2:-2]}=123"]
    rc = build_cli._cli(["--run", str(folder), "--create", "--approve", "--allow-unverified", "--json"] + ids,
                        env={"HUBSPOT_PRIVATE_APP_TOKEN": "t"}, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    res = json.loads(capsys.readouterr().out)["create"]
    assert rc == 2 and len(calls) == 1 and "not retried" in res["refused"]
    body = json.loads(calls[0].content)
    assert body["isEnabled"] is False and calls[0].url.path == "/automation/v4/flows"


def test_create_refuses_unresolved_inputs_and_unverified_shapes(tmp_path, capsys):
    hs = _folder(tmp_path, "hubspot", "welcome")
    rc = build_cli._cli(["--run", str(hs), "--create", "--approve", "--json"], env={"HUBSPOT_PRIVATE_APP_TOKEN": "t"}, transport=_no_network())
    assert rc == 2 and "--allow-unverified" in json.loads(capsys.readouterr().out)["create"]["refused"]     # the list branch type
    rc = build_cli._cli(["--run", str(hs), "--create", "--approve", "--allow-unverified", "--json"], env={"HUBSPOT_PRIVATE_APP_TOKEN": "t"},
                        transport=_no_network())
    assert rc == 2 and "unresolved" in json.loads(capsys.readouterr().out)["create"]["refused"]
    sf = _folder(tmp_path, "sfmc", "welcome")
    rc = build_cli._cli(["--run", str(sf), "--create", "--approve", "--json"], env={"SFMC_ACCESS_TOKEN": "t", "SFMC_SUBDOMAIN": "x"},
                        transport=_no_network())
    assert rc == 2 and "--allow-unverified" in json.loads(capsys.readouterr().out)["create"]["refused"]
    cio = _folder(tmp_path, "customerio", "welcome")
    rc = build_cli._cli(["--run", str(cio), "--create", "--approve", "--json"], env={}, transport=_no_network())
    assert "no create API" in json.loads(capsys.readouterr().out)["create"]["refused"]
    kl = _folder(tmp_path, "klaviyo", "welcome")
    rc = build_cli._cli(["--run", str(kl), "--create", "--approve", "--json"], env={}, transport=_no_network())
    assert rc == 2 and "KLAVIYO_PRIVATE_KEY" in json.loads(capsys.readouterr().out)["create"]["refused"]


def test_the_workspace_esp_block_is_the_default_account(tmp_path, monkeypatch, capsys):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "lmm.yaml").write_text("platform: hubspot\nesp:\n  tier: Starter\n")
    monkeypatch.setenv("LCM_WORKSPACE", str(ws))
    folder = _folder(tmp_path, "hubspot", "cart_abandon")
    assert build_cli._cli(["--run", str(folder), "--json"], env={}, transport=_no_network()) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "BLOCKED" and any("Account tier is starter" in b for b in out["blocked"])
    assert build_cli._cli(["--run", str(folder), "--tier", "enterprise", "--json"], env={}, transport=_no_network()) == 0
