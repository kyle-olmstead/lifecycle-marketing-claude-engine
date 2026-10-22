import json

from tests.conftest import ROOT
from engine.deliverability import analyze as D


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
    assert D.sending_gate(base, auth_observed=True)["verdict"] == "GREEN"
    assert D.sending_gate(base)["verdict"] == "AMBER"          # "verified" by assertion only
    assert D.sending_gate({**base, "auth_state": "unknown"})["verdict"] == "RED"
    assert D.sending_gate({"hard_bounce_rate": 0.002, "auth_state": "verified"})["verdict"] == "AMBER"
    assert D.sending_gate({**base, "domain_reputation_bounces_90d": 1})["verdict"] == "AMBER"
    assert D.sending_gate({**base, "domain_reputation_bounces_90d": 3, "domain_reputation_bounces_trend": "rising"})["verdict"] == "RED"
    assert D.sending_gate({**base, "complaint_rate": 0.004})["verdict"] == "RED"


# ---- regressions from the v2.2 audit (docs/AUDIT.md) ----

def test_microsoft_authentication_results_without_authserv_id_are_read():
    raw = ("From: News <news@example.com>\nTo: a@outlook.com\nSubject: hi\n"
           "Authentication-Results: spf=pass (sender IP is 192.0.2.1) smtp.mailfrom=example.com; dkim=pass (signature was verified) "
           "header.d=example.com;dmarc=pass action=none header.from=example.com;compauth=pass reason=100\n\nbody\n")
    h = D.parse_headers(raw)
    ar = h["authentication_results"][0]
    assert ar["authserv_id"] is None
    assert {r["method"]: r["result"] for r in ar["results"]}["spf"] == "pass"
    al = D.evaluate_alignment(h, D.parse_dmarc("v=DMARC1; p=reject; rua=mailto:d@example.com"))
    assert al["dmarc_reported"] == "pass"


def test_auth_state_prefers_the_receivers_dmarc_verdict():
    dm = D.parse_dmarc("v=DMARC1; p=reject; rua=mailto:d@example.com")
    al = {"dmarc_reported": "pass", "dmarc_computed": "fail"}
    assert D._auth_state(dm, al, "LIVE PULL 2026-10-20", True, True) == "verified"
    assert D._auth_state(dm, {"dmarc_reported": None, "dmarc_computed": "fail"}, "LIVE PULL 2026-10-20", True, True) == "failing"


def test_subdomain_sender_is_judged_by_sp_not_p():
    org = D.parse_dmarc("v=DMARC1; p=reject; sp=none; rua=mailto:d@example.com")
    eff = D.for_subdomain(org)
    assert eff.policy == "none" and not eff.enforcing and org.enforcing
    step = D.next_dmarc_step(org, "example.com", aligned_pass_observed=True, subdomain=True)
    assert step["value"] == "v=DMARC1; p=reject; sp=quarantine; rua=mailto:d@example.com"
    # without sp the parent's p applies, so a subdomain sender at p=reject has nothing to step
    assert D.next_dmarc_step(D.parse_dmarc("v=DMARC1; p=reject; rua=mailto:d@example.com"), "example.com", True, subdomain=True) is None
    doc = {"from_domain": "news.example.com", "observed_at": "2026-10-20",
           "dns": {"example.com": {"dmarc": "v=DMARC1; p=reject; sp=none; rua=mailto:d@example.com", "bimi": "v=BIMI1; l=https://example.com/l.svg; a=https://example.com/v.pem"}}}
    r = D.analyze(doc)
    assert r["checked"]["dmarc"]["effective_policy"] == "none"
    assert any("sp=none" in f["detail"] and f["severity"] == "high" for f in r["findings"])
    assert not r["checked"]["bimi"]["eligible"]


def test_one_click_unsubscribe_needs_a_passing_covering_signature():
    base = ("From: News <news@example.com>\nList-Unsubscribe: <https://example.com/u/1>\n"
            "List-Unsubscribe-Post: List-Unsubscribe=One-Click\n"
            "DKIM-Signature: v=1; a=rsa-sha256; d=example.com; s=s1; h=from:list-unsubscribe:list-unsubscribe-post; bh=x; b=y\n")
    failed = D.parse_headers("Authentication-Results: mx.google.com; dkim=fail header.d=example.com\n" + base + "\nbody\n")
    assert D.one_click_unsubscribe(failed)["rfc8058_compliant"] is False
    passed = D.parse_headers("Authentication-Results: mx.google.com; dkim=pass header.d=example.com\n" + base + "\nbody\n")
    assert D.one_click_unsubscribe(passed)["rfc8058_compliant"] is True
    unknown = D.parse_headers(base + "\nbody\n")
    assert D.one_click_unsubscribe(unknown)["rfc8058_compliant"] is None


def test_spf_include_loop_is_a_permerror_and_modifiers_are_ignored():
    r = D.parse_spf("v=spf1 include:a.example.net -all", {"a.example.net": "v=spf1 include:b.example.net -all",
                                                          "b.example.net": "v=spf1 include:a.example.net -all"})
    assert any(sev == "critical" and "loops" in msg for sev, msg in r.issues)
    clean = D.parse_spf("v=spf1 ip4:192.0.2.0/24 ra=postmaster rp=10 -all")
    assert not [m for sev, m in clean.issues if sev in ("critical", "high")]
    assert any(sev == "high" for sev, _ in D.parse_spf("v=spf1 ipv4:192.0.2.1 -all").issues)


def test_spf_redirect_is_ignored_when_all_is_present():
    r = D.parse_spf("v=spf1 ip4:192.0.2.0/24 redirect=_spf.example.net -all")
    assert r.lookups_direct == 0 and any("ignored" in m for _, m in r.issues)


def test_dmarc_version_tag_is_case_sensitive():
    assert D.parse_dmarc("v=DMARC1; p=none; rua=mailto:d@example.com").valid
    assert not D.parse_dmarc("v=dmarc1; p=none; rua=mailto:d@example.com").valid


def test_mta_sts_mode_none_skips_mx_coverage():
    r = D.parse_mta_sts("v=STSv1; id=1", "version: STSv1\nmode: none\nmx: *.mail.example.com\nmax_age: 86400\n", ["mx.other.example.net"])
    assert not [m for _, m in r["issues"] if "mx.other.example.net" in m]


def test_one_click_unsubscribe_reads_the_verdict_of_the_covering_signature():
    sigs = ("DKIM-Signature: v=1; a=rsa-sha256; d=example.com; s=s1; h=from:list-unsubscribe:list-unsubscribe-post; bh=x; b=AAAA1111\n"
            "DKIM-Signature: v=1; a=rsa-sha256; d=example.com; s=s2; h=from:subject; bh=y; b=BBBB2222\n")
    base = "From: News <news@example.com>\nList-Unsubscribe: <https://example.com/u/1>\nList-Unsubscribe-Post: List-Unsubscribe=One-Click\n"
    covering_fails = ("Authentication-Results: mx.google.com; dkim=fail header.d=example.com header.s=s1; "
                      "dkim=pass header.d=example.com header.s=s2\n")
    covering_passes = ("Authentication-Results: mx.google.com; dkim=pass header.d=example.com header.s=s1; "
                       "dkim=fail header.d=example.com header.s=s2\n")
    assert D.one_click_unsubscribe(D.parse_headers(covering_fails + base + sigs + "\nbody\n"))["rfc8058_compliant"] is False
    assert D.one_click_unsubscribe(D.parse_headers(covering_passes + base + sigs + "\nbody\n"))["rfc8058_compliant"] is True
    no_selector = "Authentication-Results: mx.google.com; dkim=fail header.d=example.com; dkim=pass header.d=example.com\n"
    assert D.one_click_unsubscribe(D.parse_headers(no_selector + base + sigs + "\nbody\n"))["rfc8058_compliant"] is None


def test_subdomain_testing_phase_ends_by_dropping_t():
    rec = D.parse_dmarc("v=DMARC1; p=reject; sp=quarantine; t=y; rua=mailto:d@example.com")
    step = D.next_dmarc_step(rec, "example.com", aligned_pass_observed=True, subdomain=True)
    assert step["value"] == "v=DMARC1; p=reject; sp=quarantine; rua=mailto:d@example.com"


# ---- regressions from the v2.3 product audit: false greens ----

def test_a_supplied_verified_claim_never_raises_the_gate():
    # no DNS and no headers: "verified" is a claim, so authentication reads AMBER, and so does the gate
    r = D.analyze({"signals": {"complaint_rate": 0.0004, "hard_bounce_rate": 0.003, "unengaged_share": 0.1, "auth_state": "verified"}})
    assert r["gate"]["verdict"] == "AMBER"
    row = next(s for s in r["gate"]["signals"] if s["signal"] == "authentication_state")
    assert row["status"] == "AMBER" and "supplied" in str(row["value"])
    # a claim can still lower an observed state: failing beats an observed pass
    doc = json.loads((ROOT / "examples/deliverability/healthy.json").read_text())
    doc["signals"] = {**(doc.get("signals") or {}), "auth_state": "failing"}
    assert D.analyze(doc)["gate"]["verdict"] == "RED"


def test_spf_plus_all_keeps_an_otherwise_healthy_sender_off_green():
    doc = json.loads((ROOT / "examples/deliverability/healthy.json").read_text())
    for host, rec in doc["dns"].items():
        if rec.get("txt"):
            rec["txt"] = ["v=spf1 include:_spf.example-esp.net +all" if t.lower().startswith("v=spf1") else t for t in rec["txt"]]
    r = D.analyze(doc)
    assert any(f["severity"] == "critical" and f["cause"] == "identity" for f in r["findings"])
    assert r["gate"]["verdict"] == "AMBER"
    assert any("critical identity finding" in x for x in r["gate"]["reasons"])


def test_dmarc_never_tightens_for_mail_seen_failing_or_without_an_observed_pass():
    q = D.parse_dmarc("v=DMARC1; p=quarantine; rua=mailto:d@example.com")
    assert D.next_dmarc_step(q, "example.com", aligned_pass_observed=True)["value"].startswith("v=DMARC1; p=reject")
    assert D.next_dmarc_step(q, "example.com", aligned_pass_observed=True, aligned_fail_observed=True) is None
    assert D.next_dmarc_step(q, "example.com", aligned_pass_observed=False) is None
    # no reports: the only step is to add them, at the same policy
    held = D.next_dmarc_step(D.parse_dmarc("v=DMARC1; p=quarantine"), "example.com", aligned_pass_observed=True)
    assert held["value"].startswith("v=DMARC1; p=quarantine;") and "rua=" in held["value"]
    # testing phases are not dropped on failing mail either
    assert D.next_dmarc_step(D.parse_dmarc("v=DMARC1; p=quarantine; t=y; rua=mailto:d@example.com"), "example.com", True,
                             aligned_fail_observed=True) is None
