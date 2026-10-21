"""v2.2 operations: engagement tiers, calendar blast vs trigger, live DNS collection, input contracts, the lcm router, the daily job."""
import json
from pathlib import Path

import duckdb
import pytest
import sqlglot

from tests.conftest import ROOT
from agents import cli, cohort_analyst, engagement
from agents.brief import load_brief
from scripts import daily, sheets_sync
from tools import contracts, deliverability, dns_collect, sql_check

FIXTURES = str(ROOT / "tests" / "fixtures")
EXAMPLE = ROOT / "briefs" / "examples" / "winback_14d.yaml"


# ----------------------------------------------------------------------------------------------
# engagement tiers
# ----------------------------------------------------------------------------------------------

def _tier_con():
    con = duckdb.connect()
    con.execute("CREATE TABLE users (user_id VARCHAR, email VARCHAR, email_consent_at TIMESTAMP)")
    con.execute("CREATE TABLE message_events (user_id VARCHAR, channel VARCHAR, event_name VARCHAR, event_at TIMESTAMP)")
    con.executemany("INSERT INTO users VALUES (?, ?, ?)", [(f"u{i}", f"u{i}@example.com", "2026-01-01") for i in range(1, 6)]
                    + [("u6", "u6@example.com", None)])
    ev = []
    for i in range(6):   # everyone gets six deliveries over the last 180 days
        for u in ("u1", "u2", "u4", "u6"):
            ev.append((u, "email", "delivered", f"2026-0{9 - i // 3}-1{i}"))
    ev += [("u1", "email", "clicked", "2026-10-05"),                                          # engaged_30d
           ("u2", "email", "opened", "2026-10-05"), ("u2", "email", "opened", "2026-10-06"),   # opens only: unengaged
           ("u3", "email", "delivered", "2026-10-01"),                                         # too few deliveries: new
           ("u4", "email", "complained", "2026-09-01"), ("u4", "email", "clicked", "2026-10-06"),   # suppressed wins
           ("u6", "email", "clicked", "2026-10-06")]                                           # no consent: not listed
    con.executemany("INSERT INTO message_events VALUES (?, ?, ?, ?)", ev)
    return con


def test_tiers_count_clicks_not_opens_and_suppression_wins():
    rows = cohort_analyst._rows(_tier_con(), engagement.tiers_sql("duckdb", "2026-10-10"))
    tiers = {r["user_id"]: r["tier"] for r in rows}
    assert tiers == {"u1": "engaged_30d", "u2": "unengaged", "u3": "new", "u4": "suppressed", "u5": "new"}
    assert next(r for r in rows if r["user_id"] == "u4")["suppression_reason"] == "complained"


def test_fixture_tiers_feed_the_gate_and_the_counts_query_agrees():
    r = engagement.run(FIXTURES)
    s = r["summary"]
    assert s["gate_band"] == "RED" and 0.5 < s["unengaged_share"] < 0.7
    con = cohort_analyst.connect(FIXTURES)
    counts = {x["tier"]: int(x["people"]) for x in cohort_analyst._rows(con, engagement.counts_sql("duckdb", s["as_of"]))}
    assert engagement.summarize_counts(counts, s["as_of"]) == s
    assert set(s["warmup_tiers"]) == {"engaged_30d", "engaged_90d", "engaged_180d", "unengaged"}   # tools/warmup.py's tier names
    sync = engagement.sync_rows(r["rows"])
    assert all(x["lcm_engagement_tier"] != "suppressed" for x in sync) and any(x["lcm_sunset"] for x in sync)


@pytest.mark.parametrize("dialect", ["postgres", "snowflake", "bigquery"])
def test_tier_sql_parses_in_every_warehouse(dialect):
    for sql in (engagement.tiers_sql(dialect), engagement.counts_sql(dialect, "2026-10-10")):
        ok, msg = sql_check.parse(sql, dialect)
        assert ok, msg


# ----------------------------------------------------------------------------------------------
# calendar blast vs behavioral trigger
# ----------------------------------------------------------------------------------------------

def test_blast_vs_trigger_bands_every_recipient():
    r = cohort_analyst.blast_vs_trigger(load_brief(EXAMPLE), FIXTURES, ["2026-08-01"])
    b = r["blasts"][0]
    assert set(b["bands"]) == {"active", "at risk", "on time", "late"}
    assert sum(x["recipients"] for x in b["bands"].values()) == b["recipients"] > 0
    assert abs(sum(x["share"] for x in b["bands"].values()) - 1) < 0.01
    assert "late" in cohort_analyst.render_blast(r).lower()


# ----------------------------------------------------------------------------------------------
# live DNS collection (with a fake resolver; the real one is dnspython)
# ----------------------------------------------------------------------------------------------

ZONE = {
    ("example.org", "SOA"): ["ns1.example.org. hostmaster.example.org. 1 7200 3600 1209600 300"],
    ("example.org", "TXT"): ["v=spf1 include:_spf.esp.example.net -all"],
    ("example.org", "MX"): ["mx1.example.org"],
    ("_dmarc.example.org", "TXT"): ["v=DMARC1; p=reject; rua=mailto:d@example.org"],
    ("_mta-sts.example.org", "TXT"): ["v=STSv1; id=20261001"],
    ("_smtp._tls.example.org", "TXT"): ["v=TLSRPTv1; rua=mailto:tls@example.org"],
    ("_spf.esp.example.net", "TXT"): ["v=spf1 ip4:192.0.2.0/24 -all"],
    ("s1._domainkey.example.org", "TXT"): ["v=DKIM1; k=rsa; p=MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA"],
}


def fake_resolve(name, rdtype):
    return list(ZONE.get((name, rdtype), []))


def test_collect_builds_a_valid_input_and_follows_spf_includes():
    doc = dns_collect.collect("example.org", ["s1", "s2"], resolve=fake_resolve, today="2026-10-20",
                              fetch=lambda url: "version: STSv1\nmode: enforce\nmx: mx1.example.org\nmax_age: 604800\n")
    assert doc["spf_includes"] == {"_spf.esp.example.net": "v=spf1 ip4:192.0.2.0/24 -all"}
    assert list(doc["dkim_keys"]) == ["s1._domainkey.example.org"] and doc["mta_sts_policies"]["example.org"].startswith("version")
    doc.pop("_lookups")
    r = deliverability.analyze(contracts.validate_deliverability(doc))
    assert r["checked"]["dmarc"]["policy"] == "reject" and r["checked"]["mta_sts"]["mode"] == "enforce"


def test_collect_refuses_a_domain_that_does_not_resolve():
    with pytest.raises(LookupError, match="does not resolve"):
        dns_collect.collect("example.invalid", resolve=lambda n, t: [], today="2026-10-20")


def test_default_selectors_come_from_documented_providers():
    assert set(dns_collect.DEFAULT_SELECTORS) == {s for group in dns_collect.SELECTOR_SOURCES.values() for s in group}
    assert "google" in dns_collect.DEFAULT_SELECTORS and "selector1" in dns_collect.DEFAULT_SELECTORS


# ----------------------------------------------------------------------------------------------
# input contracts
# ----------------------------------------------------------------------------------------------

def test_contracts_name_the_field_that_is_wrong():
    with pytest.raises(contracts.InputError, match="signals.complaint_rate"):
        contracts.validate_deliverability({"signals": {"complaint_rate": 12}})
    with pytest.raises(contracts.InputError, match="dns.example.com.spf"):
        contracts.validate_deliverability({"dns": {"example.com": {"spf": "v=spf1 -all"}}})
    with pytest.raises(contracts.InputError, match="count converting entrants"):
        contracts.validate_readout({"flow": "f", "period": "q", "treatment": {"n": 10, "conversions": 11}, "holdout": {"n": 5, "conversions": 1}})
    assert contracts.validate_tiers({"engaged_30d": 5}) == {"engaged_30d": 5, "engaged_90d": 0, "engaged_180d": 0, "unengaged": 0}


@pytest.mark.parametrize("name", sorted(contracts.SCHEMAS))
def test_committed_schemas_match_the_models(name):
    committed = json.loads((ROOT / "schemas" / name).read_text())
    assert committed == contracts.json_schema(contracts.SCHEMAS[name]), "run python3 tools/contracts.py --write-schemas"


# ----------------------------------------------------------------------------------------------
# the lcm router
# ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,kind", [
    ("check deliverability for example.com", "deliverability"),
    ("why is our mail going to the spam folder", "deliverability"),
    ("build the engagement tiers and the sunset list", "engagement"),
    ("compare our monthly newsletter with the win-back trigger", "blast"),
    ("run a health check on the cohort", "audit"),
    ("read the holdout results in results/q3.json", "readout"),
    ("how many people do we need to detect a 20% lift on a 10% baseline", "size"),
    ("plan a 5-touch win-back for users inactive 14 days", "plan"),
])
def test_router_picks_the_job(text, kind):
    assert cli.classify(text) == kind


def test_router_fills_arguments_and_asks_for_what_is_missing():
    j = cli.route("check deliverability for news.example.com with selectors s1, s2", {})
    assert j.args["domain"] == "news.example.com" and j.args["selectors"] == "s1,s2" and not j.missing
    size = cli.route("how many people do we need to detect a 20% lift on a 10% baseline with a 25% holdout", {})
    assert (size.args["baseline"], size.args["mde_rel"], size.args["holdout"]) == (0.10, 0.20, 0.25)
    assert cli.route("plan a 5-touch win-back for users inactive 14 days", {}).missing == ["--product (the product name customers know)"]
    assert cli.route("read the holdout results", {}).missing
    assert cli.route("tell me a joke", {}).kind == "help"


def test_router_reads_yaml_jobs(tmp_path):
    job = tmp_path / "job.yaml"
    job.write_text("kind: size\nbaseline: 0.1\nmde_rel: 0.2\nholdout: 0.15\n")
    j = cli.from_yaml(job, {})
    assert j.kind == "size" and j.args["baseline"] == 0.1
    assert cli.from_yaml(EXAMPLE, {}).kind == "plan"
    assert cli.main([str(job)]) == 0


def test_dry_run_reports_missing_arguments_with_exit_2(capsys):
    assert cli.main(["--dry-run", "check deliverability"]) == 2
    assert "sending domain" in capsys.readouterr().out


# ----------------------------------------------------------------------------------------------
# the daily job
# ----------------------------------------------------------------------------------------------

def _fake_collect(domain):
    doc = dns_collect.collect(domain, ["s1"], resolve=lambda n, t: fake_resolve(n.replace(domain, "example.org"), t), today="2026-10-20",
                              fetch=lambda url: None)
    return doc


def test_daily_demo_run_logs_every_tab_and_keeps_going_past_a_failure(tmp_path):
    def collect(domain):
        if domain == "broken.example":
            raise LookupError("broken.example does not resolve")
        return _fake_collect(domain)

    st = daily.run("demo", briefs_dir=tmp_path / "none", domains=["example.org", "broken.example"], out=tmp_path / "out", dry_run=True,
                   collect=collect)
    assert [f["status"] for f in st["flows"]] == ["ok"] and "no briefs" in st["notes"][0]
    assert st["engagement"]["status"] == "ok" and st["engagement"]["gate_band"] == "RED"
    assert [d["status"] for d in st["deliverability"]] == ["ok", "error"] and len(st["errors"]) == 1
    sheets = tmp_path / "out" / "sheets"
    for tab in ("cohort_health", "benchmarks", "engagement", "deliverability"):
        assert (sheets / f"{tab}.csv").exists(), tab
    issues = json.loads((tmp_path / "out" / "issues.json").read_text())
    # the fixtures' unengaged share is RED, and it feeds the domain's sending gate, which turns RED with it
    assert [i["label"] for i in issues] == ["engagement", "deliverability"]
    assert st["deliverability"][0]["gate"] == "RED"
    assert "Errors" in (tmp_path / "out" / "summary.md").read_text()


def test_daily_bigquery_mode_runs_the_warehouse_sql(tmp_path):
    con = sql_check.connect(integer_division=False)   # BigQuery divides integers to a decimal

    def bq(sql):   # BigQuery SQL, run on the fixtures through a transpile, returned the way bq --format=json returns rows
        cur = con.execute(sqlglot.transpile(sql, read="bigquery", write="duckdb")[0])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, [None if v is None else str(v) for v in row])) for row in cur.fetchall()]

    st = daily.run("bigquery", briefs_dir=ROOT / "briefs" / "examples", out=tmp_path, bq=bq, sheet=False)
    assert not st["errors"] and st["flows"] and st["engagement"]["status"] == "ok"
    assert (tmp_path / "ridgeview_winback_14d" / "warehouse_results" / "daily.json").exists()


def test_daily_skips_engagement_without_message_events(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    for t in ("users", "orders", "track_events"):
        (data / f"{t}.csv").write_text((Path(FIXTURES) / f"{t}.csv").read_text())
    st = daily.run("url", data=str(data), briefs_dir=tmp_path / "none", out=tmp_path / "out", sheet=False)
    assert st["engagement"] == {"status": "skipped", "reason": "no message_events in the data"} and not st["errors"]


def test_bq_queries_never_start_with_a_flag():
    assert not daily.strip_comments("-- dialect: bigquery\n-- note\nSELECT 1 -- trailing\n").startswith("--")


def test_sheet_rows_for_the_new_tabs_match_their_headers():
    r = engagement.run(FIXTURES)["summary"]
    rows = sheets_sync.engagement_rows(r, logged_at="t")["Engagement"]
    assert set(rows[0]) == set(sheets_sync.TABS["Engagement"]["header"])
    d = deliverability.analyze(json.loads((ROOT / "examples/deliverability/healthy.json").read_text()))
    drow = sheets_sync.deliverability_rows(d, "2026-10-20", "t")["Deliverability"][0]
    assert set(drow) == set(sheets_sync.TABS["Deliverability"]["header"]) and drow["gate"] == "GREEN" and drow["auth_state"] == "verified"
    with pytest.raises(ValueError, match="no tab named"):
        sheets_sync.sync(extra={"Nope": [{}]}, dry_run=True)


# ----------------------------------------------------------------------------------------------
# regressions from the v2.2 review
# ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,kind", [
    ("plan a re-engagement flow for inactive subscribers", "plan"),
    ("plan a welcome series that suppresses people who bounce", "plan"),
    ("plan a win-back that protects our inbox placement", "plan"),
])
def test_asking_for_a_flow_is_always_a_plan(text, kind):
    assert cli.classify(text) == kind


def test_yaml_jobs_name_what_is_missing(tmp_path, capsys):
    job = tmp_path / "job.yaml"
    job.write_text("kind: engagement\n")
    assert cli.main([str(job)]) == 2 and "--data" in capsys.readouterr().out
    job.write_text("kind: size\nbaseline: 0.1\nmde_rel: 0.2\n")
    assert cli.from_yaml(job, {}).args["holdout"] == 0.15
    job.write_text("kind: nope\n")
    assert cli.main([str(job)]) == 2


def test_duplicate_dmarc_records_mean_no_dmarc():
    zone = dict(ZONE)
    zone[("_dmarc.example.org", "TXT")] = ["v=DMARC1; p=reject; rua=mailto:d@example.org", "v=DMARC1; p=none"]
    doc = dns_collect.collect("example.org", ["s1"], resolve=lambda n, t: list(zone.get((n, t), [])), today="2026-10-20", fetch=lambda u: None)
    doc.pop("_lookups")
    r = deliverability.analyze(contracts.validate_deliverability(doc))
    ids = {f["id"]: f["severity"] for f in r["findings"]}
    assert ids.get("dmarc.multiple") == "critical" and "dmarc.missing" not in ids and r["gate"]["verdict"] == "RED"


def test_customerio_batches_stay_under_the_documented_limit():
    from tools.payloads import _bytes, attribute_sync
    rows = [{"user_id": f"user_{i:06d}", "email": f"user_{i:06d}@example.com", "lcm_engagement_tier": "engaged_90d", "lcm_sunset": False}
            for i in range(20000)]
    reqs, _ = attribute_sync("customerio", rows)
    assert len(reqs) > 1 and all(_bytes(r["body"]) <= 450_000 for r in reqs)
