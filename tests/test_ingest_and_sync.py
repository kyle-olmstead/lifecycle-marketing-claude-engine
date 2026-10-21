"""Event ingest (Segment and GA4), ESP trigger payloads, the Google Sheets sync, the MCP server and the configs."""
import asyncio
import hashlib
import hmac
import json

import jsonschema
import pytest
import yaml

from tests.conftest import ROOT
from scripts import campaign_simulator, sheets_sync, webhook_parser
from tools import event_payloads

TS = "2026-10-19T10:00:00Z"


def _seg(event="feature_used", mid="msg_0000000001", props=None, **kw):
    return {"type": "track", "event": event, "messageId": mid, "userId": "u_0001", "timestamp": TS,
            "properties": {"feature": "reports"} if props is None else props, **kw}


# ----------------------------------------------------------------------------------------------
# ingest
# ----------------------------------------------------------------------------------------------

def test_segment_events_normalize_to_the_canonical_row():
    ev, users, rej = webhook_parser.from_segment({"batch": [_seg(), _seg("Report Shared", "msg_0000000002", {"x": 1}),
                                                            {"type": "identify", "userId": "u_0001", "messageId": "id_0000000001",
                                                             "timestamp": TS, "traits": {"email": "A@Example.com"}}]})
    assert not rej
    assert ev[0] == {"event_id": "msg_0000000001", "user_id": "u_0001", "anonymous_id": None, "event_name": "feature_used",
                     "properties": {"feature": "reports"}, "source": "segment", "event_at": "2026-10-19 10:00:00", "in_tracking_plan": True}
    assert ev[1]["event_name"] == "report_shared" and ev[1]["in_tracking_plan"] is False
    assert users[0]["email"] == "a@example.com"


@pytest.mark.parametrize("bad,reason", [
    (_seg(props={"feature": "Reports"}), "tracking plan"),
    (_seg(mid=None), "idempotency"),
    ({**_seg(), "userId": None}, "user_id"),
    ({**_seg(), "timestamp": "2099-01-01T00:00:00Z"}, "future"),
])
def test_bad_segment_events_are_quarantined_with_a_reason(bad, reason):
    ev, _u, rej = webhook_parser.from_segment(bad)
    assert not ev and reason in rej[0]["reason"]


def test_money_with_cents_is_valid_in_the_tracking_plan():
    """Regression: v2.0 used multipleOf 0.01, which rejects 19.99 under binary floating point."""
    ev, _u, rej = webhook_parser.from_segment(_seg("Order Completed", props={"order_id": "o_1", "net_amount": 19.99, "currency": "USD"}))
    assert ev and not rej


def test_ga4_measurement_protocol_maps_to_the_plan():
    doc = {"client_id": "123.456", "user_id": "u_9", "timestamp_micros": 1760900000000000, "events": [
        {"name": "purchase", "params": {"transaction_id": "T1000001", "value": 19.99, "currency": "usd", "items": [{"item_id": "x"}]}},
        {"name": "view_item", "params": {"currency": "USD", "items": [{"item_id": "sku_1", "item_category": "boots", "price": 89.5}]}},
        {"name": "begin_checkout", "params": {"currency": "USD", "value": 89.5, "items": [{"item_id": "sku_1", "price": 89.5}]}},
        {"name": "sign_up", "params": {"method": "Google"}}, {"name": "session_start", "params": {}}]}
    ev, _u, rej = webhook_parser.from_ga4(doc)
    assert not rej, rej
    assert [e["event_name"] for e in ev] == ["order_completed", "product_viewed", "checkout_started", "signed_up", "session_started"]
    assert ev[0]["event_id"] == "purchase:T1000001" and ev[0]["properties"]["net_amount"] == 19.99
    again, _u, _r = webhook_parser.from_ga4(doc)
    assert [e["event_id"] for e in again] == [e["event_id"] for e in ev], "event ids must be stable for deduplication"


def test_segment_signature_forms():
    secret = "s3cret"
    body = json.dumps([_seg(), _seg(mid="msg_0000000002")], indent=2).encode()
    sign = lambda b: hmac.new(secret.encode(), b, hashlib.sha1).hexdigest()  # noqa: E731
    compact = json.dumps(json.loads(body), separators=(",", ":")).encode()
    first = json.dumps(json.loads(body)[0], separators=(",", ":")).encode()
    for b in (body, compact):
        assert webhook_parser.verify_segment_signature(body, sign(b), secret)
    assert not webhook_parser.verify_segment_signature(body, sign(first), secret), "a signature on one event must not cover a batch"
    assert not webhook_parser.verify_segment_signature(body, sign(b"other"), secret)
    assert not webhook_parser.verify_segment_signature(body, None, secret)


def test_http_app(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("SEGMENT_SHARED_SECRET", "s3cret")
    monkeypatch.setenv("LCM_GA4_TOKEN", "tok")
    client = TestClient(webhook_parser.create_app(tmp_path))
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    raw = json.dumps({**_seg(), "timestamp": now}).encode()
    sig = hmac.new(b"s3cret", raw, hashlib.sha1).hexdigest()
    assert client.post("/webhooks/segment", content=raw, headers={"X-Signature": "0" * 40}).status_code == 401
    r = client.post("/webhooks/segment", content=raw, headers={"X-Signature": sig})
    assert r.status_code == 200 and r.json()["accepted"] == 1
    assert client.post("/webhooks/segment", content=raw, headers={"X-Signature": sig}).json()["duplicates"] == 1
    bad = json.dumps({**_seg(props={"feature": "Reports"}, mid="msg_0000000009"), "timestamp": now}).encode()
    assert client.post("/webhooks/segment", content=bad, headers={"X-Signature": hmac.new(b"s3cret", bad, hashlib.sha1).hexdigest()}).json()["quarantined"] == 1
    ga = {"client_id": "1.2", "events": [{"name": "sign_up", "params": {"method": "email"}}]}
    assert client.post("/webhooks/ga4", json=ga).status_code == 401
    assert client.post("/webhooks/ga4", json=ga, headers={"X-LCM-Token": "tok"}).json()["accepted"] == 1
    lines = [json.loads(x) for f in (tmp_path / "track_events").glob("*.ndjson") for x in f.read_text().splitlines()]
    assert len(lines) == 2 and (tmp_path / "quarantine").exists()


def test_forged_batch_and_replays_are_refused(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from fastapi.testclient import TestClient
    monkeypatch.setenv("SEGMENT_SHARED_SECRET", "s3cret")
    sign = lambda b: hmac.new(b"s3cret", b, hashlib.sha1).hexdigest()  # noqa: E731
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    good = {**_seg(), "timestamp": now}
    forged = json.dumps({"batch": [good, {**_seg("Order Completed", "msg_9999999999", {"order_id": "o", "net_amount": 1, "currency": "USD"}),
                                          "timestamp": now}]}).encode()
    client = TestClient(webhook_parser.create_app(tmp_path))
    assert client.post("/webhooks/segment", content=forged, headers={"X-Signature": sign(json.dumps(good, separators=(",", ":")).encode())}).status_code == 401
    old = json.dumps({**_seg(mid="msg_0000000077"), "timestamp": (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()}).encode()
    r = client.post("/webhooks/segment", content=old, headers={"X-Signature": sign(old)}).json()
    assert r["accepted"] == 0 and r["quarantined"] == 1
    fresh = json.dumps(good).encode()
    assert client.post("/webhooks/segment", content=fresh, headers={"X-Signature": sign(fresh)}).json()["accepted"] == 1
    restarted = TestClient(webhook_parser.create_app(tmp_path))      # dedupe survives a restart
    assert restarted.post("/webhooks/segment", content=fresh, headers={"X-Signature": sign(fresh)}).json()["duplicates"] == 1


def test_identify_replays_are_refused(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from fastapi.testclient import TestClient
    monkeypatch.setenv("SEGMENT_SHARED_SECRET", "s3cret")
    sign = lambda b: hmac.new(b"s3cret", b, hashlib.sha1).hexdigest()  # noqa: E731
    client = TestClient(webhook_parser.create_app(tmp_path))
    old = json.dumps({"type": "identify", "userId": "u_1", "messageId": "id_00000001", "timestamp": "2020-01-01T00:00:00Z",
                      "traits": {"email": "old@example.com"}}).encode()
    assert client.post("/webhooks/segment", content=old, headers={"X-Signature": sign(old)}).json()["users"] == 0
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    fresh = json.dumps({"type": "identify", "userId": "u_1", "messageId": "id_00000002", "timestamp": now,
                        "traits": {"email": "new@example.com"}}).encode()
    assert client.post("/webhooks/segment", content=fresh, headers={"X-Signature": sign(fresh)}).json()["users"] == 1
    again = TestClient(webhook_parser.create_app(tmp_path)).post("/webhooks/segment", content=fresh, headers={"X-Signature": sign(fresh)}).json()
    assert again["users"] == 0 and again["duplicates"] == 1


def test_unsigned_traffic_is_refused_by_default(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.delenv("SEGMENT_SHARED_SECRET", raising=False)
    monkeypatch.delenv("LCM_ALLOW_UNSIGNED", raising=False)
    assert TestClient(webhook_parser.create_app(tmp_path)).post("/webhooks/segment", json=_seg()).status_code == 503


def test_ingested_csv_feeds_the_cohort_analyst(tmp_path):
    from agents import cohort_analyst
    import shutil
    for t in ("users", "orders"):
        shutil.copy(ROOT / "tests/fixtures" / f"{t}.csv", tmp_path / f"{t}.csv")
    ev, _u, _r = webhook_parser.from_segment([_seg(mid=f"msg_{i:010d}") for i in range(3)])
    webhook_parser.to_csv(ev, tmp_path)
    con = cohort_analyst.connect(str(tmp_path))
    assert con.execute("SELECT COUNT(*) FROM track_events WHERE event_name = 'feature_used'").fetchone()[0] == 3


# ----------------------------------------------------------------------------------------------
# ESP trigger payloads
# ----------------------------------------------------------------------------------------------

ROW = {"user_id": "u_1", "email": "U_1@Example.com", "holdout_bucket": "7", "in_holdout": "true", "days_inactive": "14"}


@pytest.mark.parametrize("platform", ["klaviyo", "hubspot", "braze", "customerio"])
def test_payloads_validate_against_the_config_schema(platform):
    events = event_payloads.rows_to_events([ROW], "core_action_lapsed_14d", "acme_winback_14d", "2026-10-19")
    for req in event_payloads.batches(platform, events, hub_id="12345678"):
        name, errs = campaign_simulator.validate_request(req)
        assert name and not errs, (platform, errs)
        assert "${" in json.dumps(req["headers"]), "secrets are placeholders, never values"
    assert events[0]["properties"]["in_holdout"] is True


def test_payload_batching_limits():
    rows = [dict(ROW, user_id=f"u_{i}", email=f"u{i}@example.com") for i in range(1201)]
    ev = event_payloads.rows_to_events(rows, "lapsed", "f", "2026-10-19")
    assert [len(r["body"]["inputs"]) for r in event_payloads.batches("hubspot", ev, hub_id="1")] == [500, 500, 201]
    assert [len(r["body"]["events"]) for r in event_payloads.batches("braze", ev)][:2] == [75, 75]
    assert len(event_payloads.batches("klaviyo", ev)) == 1201
    with pytest.raises(ValueError):
        event_payloads.build("braze", {"user_id": "u", "event": "x", "properties": {"time": 1}})
    with pytest.raises(ValueError):
        event_payloads.build("hubspot", {"user_id": "u", "event": "x"}, hub_id="abc")


def test_payload_schema_rejects_malformed_bodies():
    req = event_payloads.build("klaviyo", {"user_id": "u", "email": "u@example.com", "event": "x", "event_id": "e1"})
    req["body"]["data"]["attributes"]["profile"]["data"]["attributes"] = {}
    assert campaign_simulator.validate_request(req)[1]


# ----------------------------------------------------------------------------------------------
# Google Sheets sync, against an in-memory worksheet
# ----------------------------------------------------------------------------------------------

class FakeWS:
    def __init__(self, title, cols=10):
        self.title, self.rows, self.col_count = title, [], cols

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def add_cols(self, n):
        self.col_count += n

    def update(self, values, range_name, value_input_option=None):
        assert range_name == "A1"
        if self.rows:
            self.rows[0] = list(values[0])
        else:
            self.rows.append(list(values[0]))

    def batch_update(self, data, value_input_option=None):
        for d in data:
            row = int(d["range"].split(":")[0][1:])
            self.rows[row - 1] = [str(x) for x in d["values"][0]]

    def append_rows(self, values, value_input_option=None):
        self.rows += [[str(x) for x in v] for v in values]


class FakeSheet:
    def __init__(self):
        self.tabs = {}

    def worksheet(self, title):
        from gspread.exceptions import WorksheetNotFound
        if title not in self.tabs:
            raise WorksheetNotFound(title)
        return self.tabs[title]

    def add_worksheet(self, title, rows, cols):
        self.tabs[title] = FakeWS(title, cols)
        return self.tabs[title]


@pytest.fixture(scope="module")
def audit_doc():
    from agents import cohort_analyst
    from agents.brief import load_brief
    return json.loads(json.dumps(cohort_analyst.audit(load_brief(ROOT / "briefs/examples/winback_14d.yaml"), str(ROOT / "tests/fixtures")), default=str))


def test_sync_is_idempotent(audit_doc):
    sh = FakeSheet()
    first = sheets_sync.sync(audits=[audit_doc], spreadsheet=sh)
    second = sheets_sync.sync(audits=[audit_doc], spreadsheet=sh)
    assert first["tabs"]["Benchmarks"] == {"updated": 0, "appended": 5}
    assert second["tabs"]["Benchmarks"] == {"updated": 5, "appended": 0}
    assert len(sh.tabs["Cohort Health"].rows) == 2 and sh.tabs["Cohort Health"].rows[0] == sheets_sync.TABS["Cohort Health"]["header"]


def test_sync_extends_an_older_header_and_refuses_a_foreign_one():
    ws = FakeWS("Anomalies", cols=3)
    ws.rows = [["as_of", "flow", "metric"], ["2026-10-01", "f", "signups"]]
    sheets_sync.upsert(ws, sheets_sync.TABS["Anomalies"]["header"], ["as_of", "flow", "metric"],
                       [{"as_of": "2026-10-01", "flow": "f", "metric": "signups", "value": 3}])
    assert ws.rows[0] == sheets_sync.TABS["Anomalies"]["header"] and len(ws.rows) == 2 and ws.col_count >= 6
    ws2 = FakeWS("Anomalies")
    ws2.rows = [["name", "email"]]
    with pytest.raises(ValueError, match="different header"):
        sheets_sync.upsert(ws2, sheets_sync.TABS["Anomalies"]["header"], ["as_of"], [])


def test_duplicate_keys_in_one_call_keep_the_last_row():
    ws = FakeWS("Anomalies")
    rows = [{"as_of": "2026-10-01", "flow": "f", "metric": "signups", "value": v} for v in (1, 2)]
    assert sheets_sync.upsert(ws, sheets_sync.TABS["Anomalies"]["header"], ["as_of", "flow", "metric"], rows) == {"updated": 0, "appended": 1}
    assert sheets_sync.upsert(ws, sheets_sync.TABS["Anomalies"]["header"], ["as_of", "flow", "metric"], rows) == {"updated": 1, "appended": 0}
    assert len(ws.rows) == 2 and ws.rows[1][3] == "2"


def test_retry_on_quota_errors():
    from gspread.exceptions import APIError

    class Resp:
        status_code = 429
        text = "quota"

        def json(self):
            return {"error": {"code": 429, "message": "Quota exceeded", "status": "RESOURCE_EXHAUSTED"}}

    calls, sleeps = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise APIError(Resp())
        return "ok"
    assert sheets_sync.with_retry(flaky, sleep=sleeps.append) == "ok" and len(sleeps) == 2


def test_dry_run_without_credentials(audit_doc, tmp_path, monkeypatch):
    monkeypatch.delenv("LCM_SHEET_ID", raising=False)
    r = sheets_sync.sync(audits=[audit_doc], out=tmp_path)
    assert r["mode"] == "dry run" and (tmp_path / "benchmarks.csv").read_text().count("\n") == 6


# ----------------------------------------------------------------------------------------------
# MCP server and configuration
# ----------------------------------------------------------------------------------------------

def test_mcp_server_tools(tmp_path, monkeypatch):
    from scripts import mcp_server
    monkeypatch.setattr(mcp_server, "OUT", tmp_path)
    names = {t.name for t in asyncio.run(mcp_server.server.list_tools())}
    assert names == {"draft_brief", "plan_campaign", "revise_plan", "approve_variant", "audit_cohort", "engagement_tiers", "check_domain",
                     "simulate_flow", "priorities"}
    r = asyncio.run(mcp_server.server.call_tool("plan_campaign", {"brief_path": str(ROOT / "briefs/examples/winback_14d.yaml"),
                                                                  "data_dir": str(ROOT / "tests/fixtures"), "copy_mode": "offline"}))
    text = r.content[0].text
    assert "Status: READY WITH ASSUMPTIONS" in text and (tmp_path / "ridgeview_winback_14d" / "flow_spec.json").exists()
    y = asyncio.run(mcp_server.server.call_tool("draft_brief", {"text": "5-touch win-back after 14 days inactive", "product_name": "Acme"}))
    assert "churn_threshold_days: 14" in y.content[0].text
    ok = asyncio.run(mcp_server.server.call_tool("approve_variant", {"flow": "ridgeview_winback_14d", "step_id": "t1_email", "variant_id": "B"}))
    assert ok.content[0].text.startswith("Approved")
    rv = asyncio.run(mcp_server.server.call_tool("revise_plan", {"flow": "ridgeview_winback_14d", "changes": {"holdout_pct": 20}}))
    assert "a revision of" in rv.content[0].text and "(approved)" in rv.content[0].text
    tiers = asyncio.run(mcp_server.server.call_tool("engagement_tiers", {"data_dir": str(ROOT / "tests/fixtures")}))
    assert "Unengaged share" in tiers.content[0].text
    top = asyncio.run(mcp_server.server.call_tool("priorities", {"data_dir": str(ROOT / "tests/fixtures")}))
    assert "Today's priorities" in top.content[0].text and "### 3. " in top.content[0].text


def test_mcp_config():
    cfg = json.loads((ROOT / "config/mcp_server_config.json").read_text())
    assert cfg["mcpServers"]["lcm-teammate"]["args"][0].endswith("scripts/mcp_server.py")
    assert cfg["mcpServers"]["klaviyo"]["env"]["READ_ONLY"] == "true"
    assert all(c["url"].startswith("https://") and c["docs"].startswith("https://") for c in cfg["remoteConnectors"])


def test_payload_config_is_a_valid_schema_with_sources():
    cfg = json.loads((ROOT / "config/esp_payload_schemas.json").read_text())
    jsonschema.Draft202012Validator.check_schema(cfg)
    assert all(d["x-endpoint"]["docs"] and d["x-endpoint"]["checked"] == "2026-10-20" for d in cfg["$defs"].values())


def test_daily_workflow():
    wf = yaml.safe_load((ROOT / ".github/workflows/daily_cohort_audit.yml").read_text())
    assert wf[True]["schedule"] == [{"cron": "0 5 * * *"}]  # PyYAML reads the 'on' key as True
    runs = " ".join(s.get("run", "") for s in wf["jobs"]["audit"]["steps"])
    assert "scripts/daily.py" in runs
    uses = [s["uses"] for s in wf["jobs"]["audit"]["steps"] if "uses" in s]
    assert "actions/checkout@v7" in uses and "actions/upload-artifact@v7" in uses   # Node 24 majors
    assert (ROOT / "briefs/examples/winback_14d.yaml").exists()


def test_parquet_exports_load_and_orders_are_optional(tmp_path):
    """A folder (or URL prefix) of .parquet files loads, parquet is preferred over csv, and a missing orders table is empty."""
    import duckdb
    from agents import cohort_analyst
    con = duckdb.connect()
    for t in ("users", "track_events"):
        con.execute(f"COPY (SELECT * FROM read_csv('{ROOT / 'tests/fixtures' / (t + '.csv')}', header=true, all_varchar=true)) "
                    f"TO '{tmp_path / (t + '.parquet')}' (FORMAT parquet)")
    (tmp_path / "users.csv").write_text("not,a,valid,users,file\n")   # parquet must win over a broken csv
    c = cohort_analyst.connect(str(tmp_path))
    assert c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 400
    assert c.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    (tmp_path / "track_events.parquet").unlink()
    with pytest.raises(FileNotFoundError, match="track_events"):
        cohort_analyst.connect(str(tmp_path))


def test_brief_command_creates_its_folder(tmp_path):
    from agents import orchestrator
    out = tmp_path / "lmm-engine" / "briefs" / "acme.yaml"
    assert orchestrator._cli(["brief", "--text", "5-touch win-back after 14 days inactive", "--product", "Acme", "--out", str(out)]) == 0
    assert "churn_threshold_days: 14" in out.read_text()


# ----------------------------------------------------------------------------------------------
# real-time trigger forwarding (v2.2, opt-in)
# ----------------------------------------------------------------------------------------------

def _activation(platform="klaviyo"):
    from agents.brief import parse_text
    return parse_text("3-touch activation flow for people who signed up but have not created a report within 7 days", "Acme",
                      {"platform": platform})


def test_forwarder_adds_the_warehouse_holdout_and_sends_only_with_credentials(tmp_path):
    import httpx
    from agents.sql_dialects import holdout_bucket
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(202, json={})

    ev = {"event_id": "e1", "user_id": "u_0042", "event_name": "signed_up", "event_at": TS, "properties": {"method": "email"}}
    other = {**ev, "event_id": "e2", "event_name": "feature_used"}
    fw = webhook_parser.Forwarder([_activation()], tmp_path, env={"KLAVIYO_PRIVATE_KEY": "pk_test"}, transport=httpx.MockTransport(handler))
    assert fw.forward([ev, other]) == {"forwarded": 1, "not_sent": 0, "failed": 0}
    body = json.loads(seen[0].content)
    props = body["data"]["attributes"]["properties"]
    bucket = holdout_bucket("u_0042", "acme_activation")
    assert props["holdout_bucket"] == bucket and props["in_holdout"] == (bucket < 15) and props["flow"] == "acme_activation"
    assert seen[0].headers["Authorization"] == "Klaviyo-API-Key pk_test"
    # no credential: nothing is sent, and the request is kept to replay, with the placeholder rather than any secret
    fw2 = webhook_parser.Forwarder([_activation()], tmp_path, env={}, transport=httpx.MockTransport(handler))
    assert fw2.forward([ev]) == {"forwarded": 0, "not_sent": 1, "failed": 0} and len(seen) == 1
    kept = [json.loads(x) for x in next((tmp_path / "forward_failed").glob("*.ndjson")).read_text().splitlines()]
    assert "KLAVIYO_PRIVATE_KEY" in kept[0]["reason"] and "${KLAVIYO_PRIVATE_KEY}" in json.dumps(kept[0]["request"])


def test_forwarder_records_esp_errors_and_skips_winback_briefs(tmp_path):
    import httpx
    from agents.brief import load_brief
    winback = load_brief(ROOT / "briefs/examples/winback_14d.yaml")
    fw = webhook_parser.Forwarder([winback, _activation("braze")], tmp_path, env={"BRAZE_REST_API_KEY": "k"},
                                  transport=httpx.MockTransport(lambda r: httpx.Response(400, text="bad")))
    assert [b.slug for b in fw.briefs] == ["acme_activation"]
    ev = {"event_id": "e1", "user_id": "u_1", "event_name": "signed_up", "event_at": TS, "properties": {}}
    assert fw.forward([ev]) == {"forwarded": 0, "not_sent": 0, "failed": 1}


def test_forwarding_is_off_unless_asked(tmp_path):
    assert webhook_parser.Forwarder.from_env(tmp_path, env={}) is None


def test_http_app_forwards_after_responding(tmp_path, monkeypatch):
    import httpx
    from datetime import datetime, timezone
    from fastapi.testclient import TestClient
    sent = []
    fw = webhook_parser.Forwarder([_activation()], tmp_path, env={"KLAVIYO_PRIVATE_KEY": "pk"},
                                  transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(202)))
    monkeypatch.setattr(webhook_parser.Forwarder, "from_env", classmethod(lambda cls, root, env=None, transport=None: fw))
    monkeypatch.setenv("SEGMENT_SHARED_SECRET", "s3cret")
    client = TestClient(webhook_parser.create_app(tmp_path))
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    raw = json.dumps({**_seg("Signed Up", props={"method": "email", "plan": "free"}), "timestamp": now}).encode()
    r = client.post("/webhooks/segment", content=raw, headers={"X-Signature": hmac.new(b"s3cret", raw, hashlib.sha1).hexdigest()})
    assert r.json()["accepted"] == 1 and r.json()["forwarding"] == 1 and len(sent) == 1
    assert client.get("/healthz").json()["forwarding"] == ["acme_activation (klaviyo): signed_up"]


def test_one_briefs_bad_setting_does_not_stop_the_others(tmp_path):
    import httpx
    sent = []
    fw = webhook_parser.Forwarder([_activation("hubspot"), _activation("klaviyo")], tmp_path,
                                  env={"HUBSPOT_PRIVATE_APP_TOKEN": "t", "HUBSPOT_HUB_ID": "not-digits", "KLAVIYO_PRIVATE_KEY": "pk"},
                                  transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(202)))
    ev = {"event_id": "e1", "user_id": "u_1", "event_name": "signed_up", "event_at": TS, "properties": {"email": "a@example.com"}}
    assert fw.forward([ev]) == {"forwarded": 1, "not_sent": 1, "failed": 0} and len(sent) == 1
    kept = next((tmp_path / "forward_failed").glob("*.ndjson")).read_text()
    assert "could not build the request" in kept
