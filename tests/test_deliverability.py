import json

from tests.conftest import ROOT
from tools import deliverability as D


def test_spf_counts_lookups_through_supplied_includes():
    r = D.parse_spf("v=spf1 include:a.example.net include:b.example.net mx -all",
                    {"a.example.net": "v=spf1 include:c.example.net a -all", "b.example.net": "v=spf1 ip4:192.0.2.0/24 -all",
                     "c.example.net": "v=spf1 a mx -all"})
    assert r.valid and r.lookups_direct == 3 and r.lookups_total == 7 and r.all_qualifier == "-"


def test_spf_over_ten_lookups_is_critical_and_plus_all_is_critical():
    many = "v=spf1 " + " ".join(f"include:s{i}.example.net" for i in range(11)) + " -all"
    assert any(sev == "critical" and "limit is 10" in msg for sev, msg in D.parse_spf(many).issues)
    assert any(sev == "critical" and "+all" in msg for sev, msg in D.parse_spf("v=spf1 +all").issues)


def test_spf_unresolved_include_makes_total_unknown():
    r = D.parse_spf("v=spf1 include:_spf.unknown.example ~all")
    assert r.lookups_total is None and r.unresolved == ["_spf.unknown.example"]


def test_dmarc_flags_legacy_pct_and_conflict_with_t():
    r = D.parse_dmarc("v=DMARC1; p=quarantine; pct=50; t=y; rua=mailto:d@example.com")
    msgs = " ".join(m for _, m in r.issues)
    assert "RFC 9989" in msgs and "both present" in msgs and not r.enforcing


def test_dmarc_enforcement_and_missing_rua():
    assert D.parse_dmarc("v=DMARC1; p=reject; rua=mailto:d@example.com").enforcing
    r = D.parse_dmarc("v=DMARC1; p=none")
    assert not r.enforcing and any(sev == "high" and "rua" in msg for sev, msg in r.issues)
    assert not D.parse_dmarc("v=DMARC2; p=none").valid


def test_dmarc_next_step_path():
    with_rua = D.parse_dmarc("v=DMARC1; p=none; rua=mailto:d@example.com")
    step = D.next_dmarc_step(with_rua, "example.com", aligned_pass_observed=True)
    assert step["host"] == "_dmarc.example.com" and step["value"] == "v=DMARC1; p=quarantine; t=y; rua=mailto:d@example.com"
    # reports exist but nothing aligned has been seen: the record is already right, so there is no step
    assert D.next_dmarc_step(with_rua, "example.com") is None
    q = D.next_dmarc_step(D.parse_dmarc("v=DMARC1; p=quarantine; t=y; rua=mailto:d@example.com"), "example.com", True)
    assert q["value"] == "v=DMARC1; p=quarantine; rua=mailto:d@example.com"
    r = D.next_dmarc_step(D.parse_dmarc("v=DMARC1; p=reject; pct=50; rua=mailto:d@example.com"), "example.com", True)
    assert r["value"] == "v=DMARC1; p=reject; rua=mailto:d@example.com"
    assert D.next_dmarc_step(D.parse_dmarc("v=DMARC1; p=reject; rua=mailto:d@example.com"), "example.com", True) is None


def test_dmarc_never_jumps_to_enforcement_without_reports_or_alignment():
    # the incident's record: no rua, and its mail fails alignment. Quarantine here would bin its own sends.
    step = D.next_dmarc_step(D.parse_dmarc("v=DMARC1; p=none; pct=100"), "example.com", aligned_pass_observed=False)
    assert step["value"] == "v=DMARC1; p=none; rua=mailto:dmarc-reports@example.com"
    assert "Stay at p=none" in step["note"]
    # reports but no observed aligned pass, and an aligned pass but no reports: both stay at none
    assert D.next_dmarc_step(D.parse_dmarc("v=DMARC1; p=none"), "example.com", True)["value"].startswith("v=DMARC1; p=none;")


def test_dkim_key_sizes_from_real_keys(incident):
    k1024 = D.parse_dkim_key("k1", incident["dkim_keys"]["k1._domainkey.esp-mail.example.net"])
    k2048 = D.parse_dkim_key("nf1", incident["dkim_keys"]["nf1._domainkey.example.com"])
    assert k1024.bits == 1024 and any(sev == "medium" for sev, _ in k1024.issues)
    assert k2048.bits == 2048 and not k2048.issues
    assert D.parse_dkim_key("x", "v=DKIM1; k=rsa; p=").revoked


def test_mta_sts_flags_uncovered_mx_in_enforce_mode():
    r = D.parse_mta_sts("v=STSv1; id=1", "version: STSv1\nmode: enforce\nmx: *.mail.example.com\nmax_age: 604800\n",
                        ["mx1.mail.example.com", "mx.other.example.net"])
    assert [sev for sev, msg in r["issues"] if "mx.other.example.net" in msg] == ["critical"]


def test_alignment_on_incident_headers():
    h = D.parse_headers((ROOT / "examples/deliverability/headers_gmail.txt").read_text())
    al = D.evaluate_alignment(h, D.parse_dmarc("v=DMARC1; p=none"))
    assert al["from_domain"] == "example.com"
    assert al["spf"]["result"] == "pass" and al["spf"]["aligned"] is False
    assert al["dkim"][0]["domain"] == "esp-mail.example.net" and al["dkim"][0]["aligned"] is False
    assert al["dmarc_computed"] == "fail" == al["dmarc_reported"]
    unsub = D.one_click_unsubscribe(h)
    assert unsub["mailto_uri"] and not unsub["rfc8058_compliant"]


def test_relaxed_and_strict_alignment():
    assert D.aligned("bounces.example.co.uk", "news.example.co.uk", "r")
    assert not D.aligned("bounces.example.co.uk", "news.example.co.uk", "s")
    assert not D.aligned("example.net", "example.com", "r")


def test_incident_is_red_with_identity_as_primary_cause(incident):
    incident = json.loads(json.dumps(incident))
    incident["headers"]["path"] = str(ROOT / "examples/deliverability/headers_gmail.txt")
    r = D.analyze(incident)
    assert r["gate"]["verdict"] == "RED" and r["primary_cause_class"] == "identity"
    ids = {f["id"] for f in r["findings"]}
    assert {"align.dkim", "align.spf", "unsub.rfc8058", "signal.enterprise_vs_consumer_engagement"} <= ids
    statuses = {q["requirement"]: q["status"] for q in r["bulk_sender"]["requirements"]}
    assert statuses["DMARC passes through an aligned SPF or DKIM domain"] == "FAIL"
    assert r["bulk_sender"]["bulk_sender_gmail"] is True
    fixes = [f["fix"] for f in r["findings"] if f.get("fix")]
    dmarc_fixes = [f for f in fixes if f["host"] == "_dmarc.example.com"]
    assert len(dmarc_fixes) == 1 and dmarc_fixes[0]["value"] == "v=DMARC1; p=none; rua=mailto:dmarc-reports@example.com"
    assert "not a diagnosis" in r["primary_cause_basis"]


def test_healthy_sender_is_green():
    doc = json.loads((ROOT / "examples/deliverability/healthy.json").read_text())
    r = D.analyze(doc)
    assert r["gate"]["verdict"] == "GREEN" and not [f for f in r["findings"] if f["severity"] in ("critical", "high")]


def test_gate_never_green_on_unverified_or_unknown():
    base = {"complaint_rate": 0.0005, "hard_bounce_rate": 0.002, "auth_state": "verified"}
    assert D.sending_gate(base)["verdict"] == "GREEN"
    assert D.sending_gate({**base, "auth_state": "unknown"})["verdict"] == "RED"
    assert D.sending_gate({"hard_bounce_rate": 0.002, "auth_state": "verified"})["verdict"] == "AMBER"
    assert D.sending_gate({**base, "domain_reputation_bounces_90d": 1})["verdict"] == "AMBER"
    assert D.sending_gate({**base, "domain_reputation_bounces_90d": 3, "domain_reputation_bounces_trend": "rising"})["verdict"] == "RED"
    assert D.sending_gate({**base, "complaint_rate": 0.004})["verdict"] == "RED"
