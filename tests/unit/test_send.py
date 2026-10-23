"""lcm send: a dry run by default; with --approve, batched to each platform's limit, idempotent, retried on 429 and 5xx with
backoff (honoring Retry-After), never retried on another 4xx, and receipted so a rerun sends nothing twice. No network:
every request goes to an httpx.MockTransport."""
import csv
import email.utils
import json

import httpx
import pytest

from adapters.destinations import send
from adapters.destinations.send import Policy, ReceiptLog, SafeClient, plan_requests, retry_after_seconds


def _rows(n, start=0):
    return [{"user_id": f"u_{i:04d}", "email": f"u_{i:04d}@example.test", "anchor_date": "2026-10-01", "days_inactive": "14",
             "holdout_bucket": str(i % 100), "in_holdout": "false"} for i in range(start, start + n)]


def _write_rows(tmp_path, rows, name="pending.csv"):
    p = tmp_path / name
    with p.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return p


def _args(tmp_path, rows_path, platform, *extra):
    return ["--rows", str(rows_path), "--platform", platform, "--event", "core_action_lapsed_14d", "--flow", "acme_winback_14d",
            "--as-of", "2026-10-15", "--receipts", str(tmp_path / "send" / "receipts.jsonl"), *extra]


KEYS = {"KLAVIYO_PRIVATE_KEY": "k", "HUBSPOT_PRIVATE_APP_TOKEN": "h", "BRAZE_REST_API_KEY": "b", "CUSTOMERIO_PIPELINES_API_KEY_BASE64": "c",
        "ITERABLE_API_KEY": "i"}


class Recorder:
    """A MockTransport handler that answers from a script of (status, headers) and records every request."""

    def __init__(self, script=None, default=202):
        self.script = list(script or [])
        self.default = default
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        status, headers = self.script.pop(0) if self.script else (self.default, {})
        return httpx.Response(status, headers=headers, json={"ok": status < 300})

    @property
    def transport(self):
        return httpx.MockTransport(self)


def test_dry_run_prints_the_first_request_and_sends_nothing(tmp_path, capsys):
    rec = Recorder()
    rc = send._cli(_args(tmp_path, _write_rows(tmp_path, _rows(3)), "klaviyo"), env=KEYS, transport=rec.transport)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and rec.requests == [] and out["mode"].startswith("dry run") and out["items_to_send"] == 3
    assert out["first_request"]["headers"]["Authorization"] == "Klaviyo-API-Key ${KLAVIYO_PRIVATE_KEY}"     # placeholders, never the key
    assert out["first_request"]["body"]["data"]["attributes"]["unique_id"] == "acme_winback_14d:u_0000:2026-10-01"


def test_approve_without_the_key_refuses(tmp_path, capsys):
    rec = Recorder()
    rc = send._cli(_args(tmp_path, _write_rows(tmp_path, _rows(2)), "hubspot", "--approve"), env={}, transport=rec.transport)
    assert rc == 2 and rec.requests == [] and "HUBSPOT_PRIVATE_APP_TOKEN" in capsys.readouterr().out


def test_429_with_retry_after_waits_that_long_then_succeeds(tmp_path, capsys):
    rec = Recorder([(429, {"Retry-After": "7"}), (202, {})])
    slept = []
    rc = send._cli(_args(tmp_path, _write_rows(tmp_path, _rows(1)), "klaviyo", "--approve"), env=KEYS, transport=rec.transport, sleep=slept.append)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and slept == [7.0] and len(rec.requests) == 2 and out["accepted_items"] == 1
    assert rec.requests[0].headers["Authorization"] == "Klaviyo-API-Key k"            # the key is filled only at send time
    log = [json.loads(x) for x in (tmp_path / "send/receipts.jsonl").read_text().splitlines()]
    assert log[0]["status"] == "accepted" and log[0]["attempts"] == 2 and log[0]["waits"] == [7.0]


def test_5xx_is_retried_with_backoff_and_jitter_then_succeeds():
    rec = Recorder([(503, {}), (500, {}), (200, {})])
    slept = []
    client = SafeClient({"BRAZE_REST_API_KEY": "b"}, transport=rec.transport, sleep=slept.append, rand=lambda: 0.5,
                        policy=Policy(base_seconds=1.0, cap_seconds=60.0))
    out = client.request({"method": "POST", "url": "https://rest.iad-01.braze.com/users/track", "headers": {"Authorization": "Bearer ${BRAZE_REST_API_KEY}"},
                          "body": {"events": []}})
    assert out.ok and out.attempts == 3 and slept == [0.75, 1.5]      # step 1 then 2 seconds, half fixed and half jittered (rand 0.5)


def test_a_400_is_never_retried_and_the_summary_names_it(tmp_path, capsys):
    rec = Recorder([(400, {})], default=400)
    slept = []
    rc = send._cli(_args(tmp_path, _write_rows(tmp_path, _rows(1)), "iterable", "--approve"), env=KEYS, transport=rec.transport, sleep=slept.append)
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and len(rec.requests) == 1 and slept == []
    assert out["failed_requests"] == 1 and out["failures"][0]["http"] == 400 and out["failures"][0]["items"] == ["acme_winback_14d:u_0000:2026-10-01"]
    assert rec.requests[0].url.host == "api.iterable.com" and rec.requests[0].headers["Api-Key"] == "i"


def test_retries_stop_at_the_attempt_limit():
    rec = Recorder(default=503)
    client = SafeClient({"ITERABLE_API_KEY": "i"}, transport=rec.transport, sleep=lambda s: None, policy=Policy(max_attempts=3))
    out = client.request({"method": "POST", "url": "https://api.iterable.com/api/events/track", "headers": {"Api-Key": "${ITERABLE_API_KEY}"}, "body": {}})
    assert not out.ok and out.attempts == 3 and len(rec.requests) == 3
    assert not SafeClient({}, transport=rec.transport).missing({"url": "https://x.example/", "headers": {"A": "plain"}})


def test_a_create_call_retries_429_but_not_5xx():
    rec = Recorder([(500, {})])
    client = SafeClient({"K": "v"}, transport=rec.transport, sleep=lambda s: None)
    out = client.request({"method": "POST", "url": "https://a.klaviyo.com/api/flows", "headers": {"Authorization": "${K}"}, "body": {}}, retry_5xx=False)
    assert not out.ok and out.attempts == 1


def test_a_rerun_sends_nothing_twice(tmp_path, capsys):
    rows = _write_rows(tmp_path, _rows(5))
    rec = Recorder([(202, {}), (202, {}), (500, {}), (500, {}), (500, {}), (500, {}), (500, {}), (500, {})])
    rc = send._cli(_args(tmp_path, rows, "klaviyo", "--approve"), env=KEYS, transport=rec.transport, sleep=lambda s: None)
    first = json.loads(capsys.readouterr().out)
    assert rc == 1 and first["accepted_items"] == 4 and first["failed_requests"] == 1        # one row's request failed six times
    rec2 = Recorder()
    assert send._cli(_args(tmp_path, rows, "klaviyo", "--approve"), env=KEYS, transport=rec2.transport, sleep=lambda s: None) == 0
    second = json.loads(capsys.readouterr().out)
    assert len(rec2.requests) == 1 and second["already_accepted"] == 4                        # only the failed row goes again
    rec3 = Recorder()
    assert send._cli(_args(tmp_path, rows, "klaviyo", "--approve"), env=KEYS, transport=rec3.transport) == 0
    assert rec3.requests == [] and "nothing to send" in capsys.readouterr().out
    marked = list(csv.DictReader((tmp_path / "send/accepted_rows.csv").open()))
    assert len(marked) == 5 and marked[0] == {"user_id": "u_0000", "anchor_date": "2026-10-01"}


def test_braze_has_no_dedupe_so_the_receipt_log_is_its_sent_log(tmp_path, capsys):
    rows = _write_rows(tmp_path, _rows(3))
    rec = Recorder(default=201)
    send._cli(_args(tmp_path, rows, "braze", "--approve"), env=KEYS, transport=rec.transport)
    capsys.readouterr()
    later = [a if a != "2026-10-15" else "2026-10-16" for a in _args(tmp_path, rows, "braze", "--approve")]   # the next day's run
    rec2 = Recorder(default=201)
    send._cli(later, env=KEYS, transport=rec2.transport)
    assert len(rec.requests) == 1 and rec2.requests == []          # same flow, user and lapse: not sent again, though the time differs


@pytest.mark.parametrize("platform,n,sizes", [("hubspot", 1001, [500, 500, 1]), ("braze", 151, [75, 75, 1])])
def test_batches_split_at_each_platforms_limit(tmp_path, capsys, platform, n, sizes):
    rec = Recorder(default=204 if platform == "hubspot" else 201)
    rc = send._cli(_args(tmp_path, _write_rows(tmp_path, _rows(n)), platform, "--approve", "--hub-id", "12345"), env=KEYS, transport=rec.transport)
    assert rc == 0
    bodies = [json.loads(r.content) for r in rec.requests]
    assert [len(b["inputs"] if platform == "hubspot" else b["events"]) for b in bodies] == sizes
    if platform == "hubspot":
        assert bodies[0]["inputs"][0]["uuid"] == "acme_winback_14d:u_0000:2026-10-01" and rec.requests[0].url.path.endswith("/send/batch")


def test_an_oversized_request_file_is_split_not_sent_as_is(tmp_path):
    big = {"method": "POST", "url": "https://rest.iad-01.braze.com/users/track", "headers": {"Authorization": "Bearer ${BRAZE_REST_API_KEY}"},
           "body": {"attributes": [{"external_id": f"u{i}", "lcm_sunset": True} for i in range(80)],
                    "events": [{"external_id": f"u{i}", "name": "x", "time": "2026-10-15T05:00:00Z"} for i in range(80)]}}
    planned, skipped = plan_requests([big])
    sizes = [len(p.request["body"].get("attributes", [])) + len(p.request["body"].get("events", [])) for p in planned]
    assert sizes == [75, 75, 10] and skipped == 0
    again, skipped = plan_requests([big], accepted={k for p in planned[:2] for k in p.keys})
    assert skipped == 150 and len(again) == 1


def test_request_files_and_profile_updates_go_through_the_same_path(tmp_path, capsys):
    from adapters.destinations import event_payloads
    folder = tmp_path / "reqs"
    folder.mkdir()
    events = event_payloads.rows_to_events(_rows(2), "core_action_lapsed_14d", "acme_winback_14d", "2026-10-15")
    for i, r in enumerate(event_payloads.batches("customerio", events), 1):
        (folder / f"customerio_request_{i:04d}.json").write_text(json.dumps(r))
    rec = Recorder(default=200)
    assert send._cli(["--requests", str(folder), "--approve"], env=KEYS, transport=rec.transport) == 0
    assert json.loads(capsys.readouterr().out)["platform"] == "customerio" and len(rec.requests) == 2
    assert json.loads(rec.requests[0].content)["messageId"] == "acme_winback_14d:u_0000:2026-10-01"
    prof = _write_rows(tmp_path, [{"user_id": "u_1", "email": "u_1@example.test", "lcm_holdout_acme_winback_14d": "true"}], "holdout.csv")
    rec = Recorder(default=202)
    assert send._cli(["--profile-updates", str(prof), "--platform", "klaviyo", "--approve"], env=KEYS, transport=rec.transport) == 0
    body = json.loads(rec.requests[0].content)
    assert body["data"]["attributes"]["profiles"]["data"][0]["attributes"]["properties"] == {"lcm_holdout_acme_winback_14d": True}


def test_retry_after_reads_seconds_dates_and_braze_headers():
    assert retry_after_seconds({"Retry-After": "5"}) == 5.0
    when = email.utils.formatdate(1_800_000_030, usegmt=True)
    assert retry_after_seconds({"Retry-After": when}, now=1_800_000_000) == pytest.approx(30.0, abs=1)
    assert retry_after_seconds({"X-RateLimit-Retry-After": "2"}) == 2.0
    assert retry_after_seconds({"X-RateLimit-Reset": "1800000012"}, now=1_800_000_000) == 12.0
    assert retry_after_seconds({}) is None


def test_receipt_log_reads_only_accepted_items(tmp_path):
    log = ReceiptLog(tmp_path / "r.jsonl")
    log.append({"status": "accepted", "items": ["a", "b"]})
    log.append({"status": "failed", "items": ["c"]})
    assert log.accepted() == {"a", "b"}


class Partial(Recorder):
    """Answers the first request with a body that rejects some items inside a 2xx."""

    def __init__(self, status, body):
        super().__init__(default=200)
        self.first = (status, body)

    def __call__(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            return httpx.Response(self.first[0], json=self.first[1])
        return httpx.Response(200, json={"message": "success"})


def test_items_a_batch_rejects_inside_a_2xx_are_resent_and_the_rest_are_not(tmp_path, capsys):
    rows = _write_rows(tmp_path, _rows(3))
    rec = Partial(201, {"message": "success", "errors": [{"type": "invalid", "input_array": "events", "index": 1}]})
    rc = send._cli(_args(tmp_path, rows, "braze", "--approve"), env=KEYS, transport=rec.transport)
    capsys.readouterr()
    assert rc == 1
    log = ReceiptLog(tmp_path / "send" / "receipts.jsonl")
    sent = json.loads(rec.requests[0].content)
    assert len(log.accepted()) == len(sent.get("events", [])) + len(sent.get("attributes", [])) - 1
    rec2 = Recorder(default=201)
    send._cli(_args(tmp_path, rows, "braze", "--approve"), env=KEYS, transport=rec2.transport)
    again = json.loads(rec2.requests[0].content)
    assert len(rec2.requests) == 1 and len(again.get("events", [])) + len(again.get("attributes", [])) == 1


def test_a_hubspot_207_without_item_detail_accepts_nothing(tmp_path, capsys):
    rows = _write_rows(tmp_path, _rows(2))
    rec = Partial(207, {"status": "COMPLETE", "results": [], "numErrors": 1, "errors": [{"status": "error", "message": "bad property"}]})
    rc = send._cli(_args(tmp_path, rows, "hubspot", "--approve"), env=KEYS, transport=rec.transport)
    capsys.readouterr()
    assert rc == 1 and ReceiptLog(tmp_path / "send" / "receipts.jsonl").accepted() == set()
