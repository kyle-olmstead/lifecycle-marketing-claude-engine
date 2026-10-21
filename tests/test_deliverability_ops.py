"""Deliverability operations: provider mapping, bounce classes, complaint ingestion, the daily monitor and its scenario data."""
import csv
import filecmp
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import ROOT
from engine.deliverability import bounces as B
from engine.deliverability import complaints as C
from engine.deliverability import monitor as M
from engine.deliverability import providers as P
from engine.deliverability import warmup as W

SCENARIO = ROOT / "examples" / "deliverability" / "monitor"
CORPUS = ROOT / "examples" / "deliverability" / "bounces" / "corpus.json"
REPORTS = ROOT / "examples" / "deliverability" / "complaints"
COLS = ["event_id", "user_id", "channel", "event_name", "event_at", "recipient_domain", "smtp_status", "diagnostic"]


def _folder(tmp_path, rows, name="data"):
    d = tmp_path / name
    d.mkdir()
    (d / "users.csv").write_text("user_id,email,created_at\n")
    (d / "track_events.csv").write_text("event_id,user_id,anonymous_id,event_name,event_at,properties\n")
    with (d / "message_events.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLS)
        for i, r in enumerate(rows):
            w.writerow([f"e{i:06d}", r.get("user", f"u{i}"), "email", r["event"], r["at"], r.get("domain", ""), r.get("status", ""), r.get("diag", "")])
    return str(d)


def _day(day, domain, delivered=0, bounced=(), deferred=(), complained=0, unsubscribed=0):
    at = f"{day} 10:00:00"
    rows = [{"event": "delivered", "at": at, "domain": domain} for _ in range(delivered)]
    rows += [{"event": "bounced", "at": at, "domain": domain, "status": s, "diag": t} for s, t in bounced]
    rows += [{"event": "deferred", "at": at, "domain": domain, "status": s, "diag": t} for s, t in deferred]
    rows += [{"event": "complained", "at": at, "domain": domain} for _ in range(complained)]
    rows += [{"event": "unsubscribed", "at": at, "domain": domain} for _ in range(unsubscribed)]
    return rows


def _row(report, day, provider):
    return next(r for r in report["rows"] if r["day"] == day and r["provider"] == provider)


def _result(row, rule):
    return next(r for r in row["results"] if r["rule"] == rule)


AUTH = ("5.7.26", "550 5.7.26 This email has been blocked because the sender is unauthenticated.")
HARD = ("5.1.1", "550 5.1.1 The email account that you tried to reach does not exist.")
REP_BLOCK = ("5.7.1", "550 5.7.1 This message is likely suspicious due to the very low reputation of the sending domain.")
MS_DEFER = ("4.7.650", "451 4.7.650 The mail server [192.0.2.10] has been temporarily rate limited due to IP reputation.")


# ----------------------------------------------------------------------------------------------
# providers
# ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("domain,provider,basis", [
    ("gmail.com", "google", "consumer list"), ("GoogleMail.com.", "google", "consumer list"),
    ("someone@Outlook.com", "microsoft", "consumer list"), ("msn.com", "microsoft", "consumer list"),
    ("hotmail.co.uk", "microsoft", "name pattern"), ("live.fr", "microsoft", "name pattern"),
    ("yahoo.com", "yahoo", "consumer list"), ("aol.com", "yahoo", "consumer list"), ("yahoo.de", "yahoo", "name pattern"),
    ("icloud.com", "apple", "consumer list"), ("mac.com", "apple", "consumer list"),
    ("yahoo.co.jp", "other", "excluded"), ("acme.example", "other", "unknown"), ("", "other", "unknown"),
])
def test_consumer_domains_and_patterns(domain, provider, basis):
    m = P.classify(domain)
    assert (m.provider, m.basis) == (provider, basis)
    if basis == "name pattern":
        assert m.verified is False and "MX" in m.evidence


@pytest.mark.parametrize("mx,provider", [
    (["smtp.google.com"], "google_workspace"), (["ASPMX.L.GOOGLE.COM."], "google_workspace"), (["alt1.aspmx.l.google.com"], "google_workspace"),
    (["acme-example.mail.protection.outlook.com"], "microsoft_365"), (["acme-example.1j2b-v1.mx.microsoft"], "microsoft_365"),
    (["mx1-us1.ppe-hosted.com"], "proofpoint"), (["mx0a-00176a02.pphosted.com"], "proofpoint"), (["mx1.hc1234-56.iphmx.com"], "cisco"),
    (["eu-smtp-inbound-1.mimecast.com"], "mimecast"), (["d9307303a.ess.barracudanetworks.com"], "barracuda"),
    (["mx.unknown-host.example"], "other"), (["aspmx.evil.example"], "other"),
])
def test_business_domains_map_from_mx(mx, provider):
    assert P.classify("acme.example", mx=mx).provider == provider


def test_mx_lookups_happen_only_when_allowed():
    def boom(domain):
        raise AssertionError("looked up without permission")

    assert P.classify("acme.example", mx_lookup=boom).provider == "other"
    assert P.classify("acme.example", lookup=True, mx_lookup=lambda d: ["acme-example.mail.protection.outlook.com"]).provider == "microsoft_365"

    def fail(domain):
        raise TimeoutError("dns down")

    m = P.classify("acme.example", lookup=True, mx_lookup=fail)
    assert m.provider == "other" and "lookup failed" in m.evidence
    r = P.Resolver({"acme.example": ["smtp.google.com"]})
    assert r("x@ACME.example").provider == "google_workspace" and [m.domain for m in r.seen()] == ["acme.example"]


# ----------------------------------------------------------------------------------------------
# bounces
# ----------------------------------------------------------------------------------------------

_CASES = json.loads(CORPUS.read_text())["cases"]


@pytest.mark.parametrize("case", _CASES, ids=[c["id"] for c in _CASES])
def test_bounce_corpus(case):
    r = B.classify(case.get("smtp_status"), case.get("diagnostic"), case.get("provider"), case.get("event_name"))
    assert r.cls == case["expect"], (r.rule, r.note)
    if case.get("kind"):
        assert r.kind == case["kind"]
    assert r.action == B.ACTIONS[r.cls][0]


def test_every_class_carries_its_action_and_unknown_never_suppresses():
    assert {c: B.ACTIONS[c][0] for c in B.CLASSES} == {"hard": "suppress", "soft": "retry", "block": "fix_sender", "auth": "fix_sender",
                                                       "rate": "retry", "unknown": "retry"}
    assert B.classify(None, None, event_name="bounced").cls == "unknown"
    assert {c["expect"] for c in _CASES} == set(B.CLASSES)


def test_codes_are_read_from_the_status_or_text_and_never_from_ip_addresses():
    assert B.parse_codes("5.1.1", None) == (None, "5.1.1")
    assert B.parse_codes(None, "smtp; 550-5.7.26 Unauthenticated") == ("550", "5.7.26")
    assert B.parse_codes(None, "421 Connection from 10.5.7.1 refused")[1] is None
    assert B.parse_codes(None, "451 4.7.23 192.0.2.10 no PTR") == ("451", "4.7.23")
    r = B.classify("4.7.28", "421 4.7.28 Gmail has detected an unusual rate of email", "google")
    assert r.temporary is True and r.cls == "rate"
    assert B.classify("5.7.26", AUTH[1], "google").temporary is False


def test_provider_codes_apply_only_to_that_provider_and_blocks_carry_a_kind():
    # Gmail reuses 5.7.30 for DKIM; the registry's X.7.30 is REQUIRETLS. Both are auth, but the rule that matched differs.
    assert B.classify(None, "550 5.7.30 This message was blocked because it didn't pass DKIM authentication.", "google").rule == "gmail.auth_block"
    assert B.classify(None, "550 5.7.30 REQUIRETLS support required", None).rule == "rfc.requiretls"
    # a Yahoo-only rule does not fire for a Gmail recipient
    assert B.classify(None, "554 Message not delivered", "google").cls == "unknown"
    assert B.classify(None, "554 Message not delivered", "yahoo").cls == "hard"
    rep = B.classify(*REP_BLOCK, "google")
    pol = B.classify(None, "421 4.7.0 IP not in whitelist for RCPT domain, closing connection.", "google_workspace")
    assert rep.reputation_block and not pol.reputation_block and pol.cls == "block"
    assert B.classify(None, "552 5.3.4 Message size exceeds fixed limit").kind == "technical"


def test_every_rule_names_a_source_and_unverified_rules_are_marked():
    assert all(r.source for r in B.RULES)
    unverified = {r.id for r in B.RULES if not r.verified}
    assert {"microsoft.s3150", "yahoo.unauthenticated", "yahoo.bl_blocklist"} <= unverified
    assert B.classify(None, "550 5.7.1 Unfortunately, messages from [192.0.2.10] weren't sent ... (S3150).", "microsoft").verified is False


# ----------------------------------------------------------------------------------------------
# complaints
# ----------------------------------------------------------------------------------------------

def test_yahoo_cfl_report_parses_to_rfc_5965_fields():
    rep = C.parse_arf((REPORTS / "yahoo_cfl.eml").read_bytes())
    assert (rep.format, rep.feedback_type, rep.version, rep.reporter) == ("arf", "abuse", "1", "yahoo")
    assert rep.arrival_at == "2026-10-05 09:41:00" and rep.source_ip == "192.0.2.10" and rep.reported_domain == ["example.com"]
    assert rep.original_mail_from == "bounce@em.example.com" and rep.original_rcpt_to == [] and not rep.issues
    assert rep.original_headers["Message-ID"].startswith("<m_20261005")


def test_complainers_resolve_by_header_and_microsoft_reports_have_no_recipient():
    jmrp = C.parse_arf((REPORTS / "microsoft_jmrp.eml").read_text())
    assert jmrp.reporter == "microsoft" and jmrp.original_rcpt_to == []
    uid, how, notes = C.resolve(jmrp, C.ResolveConfig(order=("rcpt",)))
    assert uid is None and "omit" in notes[0]
    uid, how, _ = C.resolve(jmrp, C.ResolveConfig())
    assert (uid, how) == ("u_0019", "header")


def test_ingest_writes_message_events_rows_the_loader_accepts(tmp_path):
    users = {"u_0042@example.test": "u_0042", "u_0007@example.test": "u_0007"}
    cfg = C.ResolveConfig(users_by_email=users, emails_by_user={v: k for k, v in users.items()})
    files = sorted(REPORTS.glob("*.eml"))
    r = C.ingest([(f.name, f.read_bytes()) for f in files] + [("dup", files[-1].read_bytes())], cfg)
    assert len(r["events"]) == 3 and not r["unresolved"] and not r["errors"]   # the duplicate report is one event
    by_user = {e["user_id"]: e for e in r["events"]}
    assert by_user["u_0042"]["_resolved_by"] == "rcpt" and by_user["u_0007"]["campaign"] == "warmup_newsletter"
    assert all(e["event_name"] == "complained" and e["event_id"].startswith("cmp_") for e in r["events"])
    assert by_user["u_0007"]["recipient_domain"] == "example.test"
    data = tmp_path / "data"
    data.mkdir()
    (data / "users.csv").write_text("user_id,email,created_at\n")
    (data / "track_events.csv").write_text("event_id,event_name,event_at\n")
    C.write_events(r["events"], data / "message_events.csv")
    from engine.data.load import connect
    con, _ = connect(str(data))
    assert con.execute("SELECT COUNT(*) FROM message_events WHERE event_name = 'complained'").fetchone()[0] == 3


def _arf(ftype="abuse", extra_fields="", original="X-LCM-User-Id: u_1\nList-Unsubscribe: <https://example.com/u/tok_9>\n", top="multipart/report; report-type=feedback-report"):
    return (f"From: fbl@isp.example\nDate: Tue, 06 Oct 2026 10:00:00 +0000\nMIME-Version: 1.0\nContent-Type: {top}; boundary=\"b\"\n\n"
            f"--b\nContent-Type: text/plain\n\nreport\n\n--b\nContent-Type: message/feedback-report\n\nFeedback-Type: {ftype}\n"
            f"User-Agent: test/1\nVersion: 1\n{extra_fields}\n--b\nContent-Type: text/rfc822-headers\n\nFrom: news@example.com\n"
            f"Message-ID: <m1@example.com>\n{original}\n--b--\n")


def test_not_spam_and_auth_failure_reports_are_skipped_and_garbage_is_an_error():
    r = C.ingest([_arf("not-spam"), _arf("auth-failure"), "From: a@example.com\n\nhello\n", _arf("abuse")])
    assert [s["feedback_type"] for s in r["skipped"]] == ["not-spam", "auth-failure"]
    assert len(r["errors"]) == 1 and "not a feedback report" in r["errors"][0]["error"] and len(r["events"]) == 1


def test_malformed_reports_are_flagged_not_trusted():
    both = C.parse_arf(_arf(extra_fields="Arrival-Date: Tue, 06 Oct 2026 09:00:00 +0000\nReceived-Date: Tue, 06 Oct 2026 08:00:00 +0000\n"))
    assert both.arrival_at == "2026-10-06 09:00:00" and any("malformed" in i for i in both.issues)
    alias = C.parse_arf(_arf(extra_fields="Received-Date: Tue, 06 Oct 2026 08:00:00 -0500\n"))
    assert alias.arrival_at == "2026-10-06 13:00:00"
    assert any("multipart/report" in i for i in C.parse_arf(_arf(top="multipart/mixed")).issues)


def test_unsubscribe_token_and_verp_strategies():
    rep = C.parse_arf(_arf(original="List-Unsubscribe: <https://example.com/u/tok_9>\n",
                           extra_fields="Original-Mail-From: <bounce+u_77@bounces.example.com>\n"))
    cfg = C.ResolveConfig(order=("header", "unsubscribe", "verp"), unsubscribe_pattern=r"/u/(?P<token>[\w]+)", token_map={"tok_9": "u_9"})
    assert C.resolve(rep, cfg)[:2] == ("u_9", "unsubscribe")
    cfg = C.ResolveConfig(order=("verp",), verp_pattern=r"bounce\+(?P<user>[^@]+)@")
    assert C.resolve(rep, cfg)[:2] == ("u_77", "verp")
    with pytest.raises(ValueError, match="named group"):
        C.ResolveConfig(verp_pattern=r"bounce\+([^@]+)@")
    with pytest.raises(ValueError, match="unknown resolution strategy"):
        C.ResolveConfig(order=("guess",))


# ----------------------------------------------------------------------------------------------
# warm-up rules
# ----------------------------------------------------------------------------------------------

def test_every_abort_criterion_is_machine_readable():
    assert {r["criterion"] for r in W.ABORT_RULES} == set(range(len(W.ABORT_CRITERIA)))
    assert all(r["source"] and r["verdict"] in W.VERDICTS and r["kind"] in ("rate_above", "count_at_least", "rising") for r in W.ABORT_RULES)
    p = W.plan(4000, {"engaged_30d": 20000}, "GREEN")
    assert p["abort_rules"] == W.ABORT_RULES and p["abort_criteria"][:4] == W.ABORT_CRITERIA[:4]
    assert "authentication failures" in W.render_markdown(p)


# ----------------------------------------------------------------------------------------------
# the monitor
# ----------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def scenario():
    return M.monitor(str(SCENARIO), mx=P.load_mx_map(str(SCENARIO / "mx.json")))


def test_scenario_verdicts_by_day(scenario):
    assert [(d["day"], d["verdict"]) for d in scenario["days"]] == [
        ("2026-10-01", "CONTINUE"), ("2026-10-02", "CONTINUE"), ("2026-10-03", "STOP"),
        ("2026-10-04", "HOLD"), ("2026-10-05", "DROP BACK"), ("2026-10-06", "DROP BACK")]
    assert scenario["days"][2]["providers"]["google"] == "STOP" and scenario["days"][2]["gate"] == "RED"
    d5, d6 = scenario["days"][4]["providers"], scenario["days"][5]["providers"]
    assert d5["yahoo"] == "DROP BACK" and d5["microsoft"] == "DROP BACK" and d5["google"] == "HOLD"
    assert d6 == {**d6, "google": "CONTINUE", "yahoo": "HOLD", "microsoft": "DROP BACK"}
    assert scenario["today"]["verdict"] == "DROP BACK" and scenario["today"]["gate"] == "AMBER"


def test_scenario_rates_show_their_denominators_and_small_samples_are_not_judged(scenario):
    y5 = _result(_row(scenario, "2026-10-05", "yahoo"), "complaint_rate")
    assert y5["status"] == "triggered" and (y5["numerator"], y5["denominator"]) == (3, 1047) and y5["verdict"] == "DROP BACK"
    y4 = _result(_row(scenario, "2026-10-04", "yahoo"), "complaint_rate")
    assert y4["status"] == "too few sends to judge" and y4["min_sample"] == 1000
    g = _result(_row(scenario, "2026-10-05", "google"), "complaint_rate")
    assert g["status"] == "not evaluable" and "Postmaster" in g["evidence"]
    apple = _result(_row(scenario, "2026-10-06", "apple"), "hard_bounce_rate")
    assert apple["status"] == "too few sends to judge"   # 2 of 175 is 1.1%, but 175 is under the minimum
    ms = _result(_row(scenario, "2026-10-05", "microsoft"), "deferrals_climbing")
    assert ms["status"] == "triggered" and "2026-10-03" in ms["evidence"] and "attempts" in ms["evidence"]
    rates = _row(scenario, "2026-10-06", "ALL")["rates"]
    assert rates["hard_bounce_rate"]["per"] == "sent" and rates["deferral_rate"]["per"] == "attempts"
    assert _row(scenario, "2026-10-06", "google_workspace")["counts"]["other_blocks"] == 1


def test_scenario_triage_names_what_to_check_and_where(scenario):
    tri = {t["provider"]: t for t in scenario["triage"]}
    assert set(tri) >= {"microsoft", "yahoo", "google_workspace"}
    ms = tri["microsoft"]
    assert any("4.7.650" in s for s in ms["what_we_saw"]) and any("SNDS" in c for c in ms["what_to_check"])
    assert {w["url"] for w in ms["where"]} >= {"https://aka.ms/snds", "https://substrate.office.com/ip-domain-management-snds/snds/jmrp"}
    assert any(w["url"].startswith("https://senders.yahooinc.com/complaint-feedback-loop") for w in tri["yahoo"]["where"])
    assert all(w["url"].startswith("https://") for t in scenario["triage"] for w in t["where"])
    md = M.render_markdown(scenario)
    assert "## Deliverability monitor, 2026-10-06: DROP BACK" in md and "of 770 attempts" in md


def test_warmup_plan_maps_each_day_to_tomorrows_volume(scenario):
    plan = W.plan(4000, {"engaged_30d": 20000, "engaged_90d": 10000, "engaged_180d": 5000}, "GREEN", start=1000, days=10)
    r = M.monitor(str(SCENARIO), plan, "2026-10-01")
    w = {d["day"]: d for d in r["warmup"]["days"]}
    assert w["2026-10-03"]["next_volume"] == 0 and w["2026-10-04"]["next_volume"] == w["2026-10-04"]["planned"]
    assert w["2026-10-05"]["step"] == 3 and w["2026-10-05"]["next_volume"] == 2000 and "step 2" in w["2026-10-05"]["next"]
    assert w["2026-10-01"]["next_volume"] == plan["schedule"][1]["volume"]


def test_one_complaint_in_a_few_hundred_is_not_an_alarm(tmp_path):
    data = _folder(tmp_path, _day("2026-10-01", "yahoo.com", delivered=300, complained=1))
    r = M.monitor(data)
    assert r["today"]["verdict"] == "CONTINUE"
    assert _result(_row(r, "2026-10-01", "yahoo"), "complaint_rate")["status"] == "too few sends to judge"


def test_auth_failures_stop_even_on_a_small_sample_and_hard_bounce_bands(tmp_path):
    r = M.monitor(_folder(tmp_path, _day("2026-10-01", "gmail.com", delivered=20, bounced=[AUTH]), "a"))
    assert _row(r, "2026-10-01", "google")["verdict"] == "STOP" and r["today"]["gate"] == "RED"
    r = M.monitor(_folder(tmp_path, _day("2026-10-01", "gmail.com", delivered=300, bounced=[HARD] * 7), "b"))
    assert _row(r, "2026-10-01", "ALL")["verdict"] == "STOP"          # 7 of 307 is 2.3%: the gate's RED band
    r = M.monitor(_folder(tmp_path, _day("2026-10-01", "gmail.com", delivered=300, bounced=[HARD] * 4), "c"))
    assert _row(r, "2026-10-01", "ALL")["verdict"] == "HOLD"          # 4 of 304 is 1.3%: the AMBER band holds
    r = M.monitor(_folder(tmp_path, _day("2026-10-01", "gmail.com", delivered=50), "d"))
    assert r["today"]["verdict"] == "HOLD" and any("too few sends" in x for x in r["today"]["reasons"])


def test_bounces_without_detail_count_as_hard_and_say_so(tmp_path):
    r = M.monitor(_folder(tmp_path, _day("2026-10-01", "gmail.com", delivered=300, bounced=[("", "")] * 7)))
    row = _row(r, "2026-10-01", "ALL")
    assert row["counts"]["hard_bounces"] == 7 and row["counts"]["hard_bounces_without_detail"] == 7 and row["verdict"] == "STOP"
    assert any("counted as hard" in n for n in r["notes"])


def test_reputation_blocks_drop_back_and_rising_blocks_stop(tmp_path):
    rows = _day("2026-10-01", "gmail.com", delivered=500, bounced=[REP_BLOCK]) + _day("2026-10-02", "gmail.com", delivered=500, bounced=[REP_BLOCK] * 3)
    r = M.monitor(_folder(tmp_path, rows))
    assert [d["providers"]["google"] for d in r["days"]] == ["DROP BACK", "STOP"]
    assert "rising" in _result(_row(r, "2026-10-02", "google"), "reputation_blocks")["evidence"]


def test_a_one_day_rise_in_deferrals_is_watched_not_acted_on(tmp_path):
    rows = (_day("2026-10-01", "outlook.com", delivered=300, deferred=[MS_DEFER] * 10)
            + _day("2026-10-02", "outlook.com", delivered=300, deferred=[MS_DEFER] * 2)
            + _day("2026-10-03", "outlook.com", delivered=300, deferred=[MS_DEFER] * 20))
    r = M.monitor(_folder(tmp_path, rows))
    res = _result(_row(r, "2026-10-03", "microsoft"), "deferrals_climbing")
    assert res["status"] == "clear" and "watch tomorrow" in res["evidence"]
    assert r["today"]["verdict"] == "CONTINUE" and r["triage"][0]["provider"] == "microsoft"


def test_the_plans_own_rules_and_postmaster_data_are_honoured(tmp_path):
    data = _folder(tmp_path, _day("2026-10-01", "yahoo.com", delivered=2000, complained=2) + _day("2026-10-01", "gmail.com", delivered=300))
    assert M.monitor(data)["today"]["verdict"] == "CONTINUE"          # 2 of 2,000 is exactly 0.1%: not above it
    strict = [dict(r, above=0.0005) if r["id"] == "complaint_rate" else r for r in W.ABORT_RULES]
    assert M.monitor(data, {"abort_rules": strict, "schedule": []})["today"]["verdict"] == "DROP BACK"
    r = M.monitor(data, postmaster={"2026-10-01": 0.004})
    assert _row(r, "2026-10-01", "google")["verdict"] == "STOP"
    with pytest.raises(ValueError, match="unknown kind"):
        M.monitor(data, {"abort_rules": [{"id": "x", "kind": "vibes", "scope": ["day"]}]})


def test_missing_recipient_domain_is_filled_from_users(tmp_path):
    data = Path(_folder(tmp_path, [{"event": "delivered", "at": "2026-10-01 10:00:00", "user": "u1"}]))
    (data / "users.csv").write_text("user_id,email,created_at\nu1,someone@yahoo.com,2026-01-01\n")
    r = M.monitor(str(data))
    assert {p["provider"] for p in r["providers"]} == {"yahoo"}


def test_scenario_data_is_deterministic(tmp_path):
    shutil.copy(SCENARIO / "make_data.py", tmp_path / "make_data.py")
    subprocess.run([sys.executable, str(tmp_path / "make_data.py")], check=True, capture_output=True)
    for name in ("message_events.csv", "users.csv", "track_events.csv"):
        assert filecmp.cmp(tmp_path / name, SCENARIO / name, shallow=False), name


def test_cli_entry_points_run(tmp_path):
    for args in (["engine/deliverability/monitor.py", "--data", str(SCENARIO), "--mx", str(SCENARIO / "mx.json"), "--markdown", "--out", str(tmp_path / "m")],
                 ["engine/deliverability/bounces.py", "--corpus", str(CORPUS)],
                 ["engine/deliverability/bounces.py", "550 5.1.1 no such user", "--provider", "gmail.com"],
                 ["engine/deliverability/providers.py", "gmail.com", "acme.example", "--mx", "acme.example=smtp.google.com"],
                 ["engine/deliverability/complaints.py", str(REPORTS), "--out", str(tmp_path / "c.csv")]):
        r = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True)
        assert r.returncode == 0, (args, r.stderr[-500:])
    assert (tmp_path / "m" / "monitor.json").exists() and "DROP BACK" in (tmp_path / "m" / "monitor.md").read_text()
    r = subprocess.run([sys.executable, "engine/deliverability/monitor.py", "--data", str(tmp_path / "nope")], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 2 and "error" in r.stderr
