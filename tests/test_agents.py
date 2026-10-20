"""Teammate agents: briefs, the cohort SQL (semantics and dialect equivalence), experiment design, guardrails."""
import csv
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import sqlglot

from tests.conftest import ROOT
from agents import cohort_analyst, experimentation_agent, guardrails
from agents.brief import load_brief, parse_text, to_yaml
from agents.models import Brief, EventMatch, base_form
from agents.sql_dialects import dialect, holdout_bucket

FIX = ROOT / "tests" / "fixtures"
EXAMPLE = ROOT / "briefs" / "examples" / "winback_14d.yaml"
SPEC_SENTENCE = ("Build a 5-touch automated win-back flow for users who completed onboarding but haven't triggered a core "
                 "action in 14 days")


@pytest.fixture(scope="module")
def brief():
    return load_brief(EXAMPLE)


@pytest.fixture(scope="module")
def con():
    return cohort_analyst.connect(str(FIX))


# ----------------------------------------------------------------------------------------------
# briefs
# ----------------------------------------------------------------------------------------------

def test_the_spec_sentence_parses_and_records_every_assumption():
    b = parse_text(SPEC_SENTENCE, "Acme")
    assert (b.flow_type, b.touches, b.churn_threshold_days) == ("winback", 5, 14)
    assert b.qualifying_event == EventMatch(event="onboarding_step_completed", where={"step_index": 3}, label="completed onboarding")
    assumed = {a.field for a in b.assumptions}
    # the sentence never names the core action or the platform: both must be flagged, not silently filled
    assert {"core_conversion_event", "platform", "sending_gate", "monthly_entrants", "baseline_conversion_rate"} <= assumed
    assert b.holdout_pct == 15 and b.channels == ["email"]


@pytest.mark.parametrize("text,expect", [
    ("3-step welcome series for new signups who haven't connected data, in Klaviyo with SMS", {"touches": 3, "platform": "klaviyo", "sms": True}),
    ("win back customers who haven't purchased in 2 months via Braze push", {"churn_threshold_days": 60, "platform": "braze", "push": True}),
    ("Reactivate users inactive for three weeks, 20% holdout, HubSpot", {"churn_threshold_days": 21, "holdout_pct": 20.0, "platform": "hubspot"}),
])
def test_sentence_variants(text, expect):
    b = parse_text(text, "Acme")
    for k, v in expect.items():
        if k in ("sms", "push"):
            assert k in b.channels
        else:
            assert getattr(b, k) == v, (k, getattr(b, k))


def test_template_loads_and_round_trips(tmp_path):
    b = load_brief(ROOT / "briefs" / "brief_template.yaml")
    assert b.core_conversion_event.where == {"feature": "reports"}
    p = tmp_path / "b.yaml"
    p.write_text(to_yaml(b))
    again = load_brief(p)
    assert again.model_dump(exclude={"assumptions"}) == b.model_dump(exclude={"assumptions"})


def test_missing_required_fields_are_named(tmp_path):
    p = tmp_path / "b.yaml"
    p.write_text("product_name: Acme\n")
    with pytest.raises(ValueError, match="core_conversion_event, churn_threshold_days"):
        load_brief(p)


@pytest.mark.parametrize("bad", ["Report Created", "report-created", "1st_report"])
def test_event_names_must_be_snake_case(bad):
    with pytest.raises(ValueError):
        EventMatch(event=bad)


def test_email_is_always_a_channel():
    with pytest.raises(ValueError):
        Brief(product_name="A", channels=["sms"])


@pytest.mark.parametrize("label,expect", [("created a report", "create a report"), ("placed an order", "place an order"),
                                          ("logged in", "log in"), ("used the product", "use the product"), ("bought again", "buy again")])
def test_base_form(label, expect):
    assert base_form(label) == expect


# ----------------------------------------------------------------------------------------------
# cohort SQL: semantics against an independent computation, and the same answer in every dialect
# ----------------------------------------------------------------------------------------------

def _independent_entrants(as_of: date, n: int):
    """Who should enter on as_of, computed from the CSVs in plain Python (no SQL)."""
    qualified, last_core = {}, {}
    for r in csv.DictReader((FIX / "track_events.csv").open()):
        if not r["user_id"]:
            continue
        at = datetime.fromisoformat(r["event_at"])
        if at.date() > as_of:
            continue
        props = json.loads(r["properties"] or "{}")
        if r["event_name"] == "onboarding_step_completed" and str(props.get("step_index")) == "3":
            qualified[r["user_id"]] = min(qualified.get(r["user_id"], at), at)
        if r["event_name"] == "feature_used" and props.get("feature") == "reports":
            last_core[r["user_id"]] = max(last_core.get(r["user_id"], at), at)
    out = set()
    for u, q in qualified.items():
        anchor = max(last_core.get(u, q), q).date()
        if anchor == as_of - timedelta(days=n):
            out.add(u)
    return out


AS_OF_DATES = [date(2026, 10, 18) - timedelta(days=7 * k) for k in range(0, 40)]


def test_entry_query_matches_an_independent_computation(brief, con):
    total = 0
    for d in AS_OF_DATES:
        rows = cohort_analyst._rows(con, cohort_analyst.entry_sql(brief, "duckdb", d.isoformat()))
        assert {r["user_id"] for r in rows} == _independent_entrants(d, 14), d
        assert all(r["days_inactive"] == 14 for r in rows)
        total += len(rows)
    assert total >= 20, "the fixture dates must exercise the query"


def _to_duckdb(sql: str, d: str) -> str:
    """Transpile to DuckDB, first replacing the two constructs sqlglot does not translate faithfully:
    Snowflake's VARIANT path (x:key::string returns unquoted text in Snowflake) and the hex parse in the
    holdout bucket (checked separately, by value, below)."""
    import re
    if d == "snowflake":
        sql = re.sub(r"(\w+(?:\.\w+)?):(\w+)::string", r"JSON_EXTRACT_PATH_TEXT(\1, '\2')", sql)
        sql = re.sub(r"MOD\(TO_NUMBER\(SUBSTR\(MD5\((.+?) \|\| '(:[^']*)'\), 1, 7\), 'XXXXXXX'\), 100\)",
                     r"(CAST('0x' || SUBSTR(MD5(\1 || '\2'), 1, 7) AS INTEGER) % 100)", sql)
        return sqlglot.transpile(sql, read="snowflake", write="duckdb")[0]
    return sqlglot.transpile(sql, read=d, write="duckdb")[0]


def _norm(rows):
    out = []
    for r in rows:
        out.append(tuple(str(v)[:10] if hasattr(v, "year") else (int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
                         for k, v in r.items() if k not in ("qualified_at", "last_core_at")))
    return sorted(out, key=str)


@pytest.mark.parametrize("d", ["snowflake", "bigquery"])
def test_warehouse_dialects_return_the_same_rows(brief, con, d):
    for day in AS_OF_DATES[::4]:
        ref = _norm(cohort_analyst._rows(con, cohort_analyst.entry_sql(brief, "duckdb", day.isoformat())))
        got = _norm(cohort_analyst._rows(con, _to_duckdb(cohort_analyst.entry_sql(brief, d, day.isoformat()), d)))
        assert got == ref, (d, day)
    ref = _norm(cohort_analyst._rows(con, cohort_analyst.history_sql(brief, "duckdb", "2026-10-18")))
    assert _norm(cohort_analyst._rows(con, _to_duckdb(cohort_analyst.history_sql(brief, d, "2026-10-18"), d))) == ref
    for name, sql in cohort_analyst.audit_sql(brief, d, "2026-10-18").items():
        ref = _norm(cohort_analyst._rows(con, cohort_analyst.audit_sql(brief, "duckdb", "2026-10-18")[name]))
        assert _norm(cohort_analyst._rows(con, _to_duckdb(sql, d))) == ref, name


@pytest.mark.parametrize("d", ["postgres", "snowflake", "bigquery"])
def test_every_generated_query_parses(brief, d):
    sqls = [cohort_analyst.entry_sql(brief, d), cohort_analyst.history_sql(brief, d), *cohort_analyst.audit_sql(brief, d).values()]
    for sql in sqls:
        ok, msg = __import__("tools.sql_check", fromlist=["parse"]).parse(sql, d)
        assert ok, msg


def test_holdout_bucket_is_identical_in_python_duckdb_and_bigquery(con):
    users = [r[0] for r in con.execute("SELECT user_id FROM users").fetchall()]
    duck = dict(con.execute(f"SELECT user_id, {dialect('duckdb').holdout_bucket('user_id', 'salt_x')} FROM users").fetchall())
    bq = dict(con.execute(sqlglot.transpile(f"SELECT user_id, {dialect('bigquery').holdout_bucket('user_id', 'salt_x')} FROM users",
                                            read="bigquery", write="duckdb")[0]).fetchall())
    for u in users:
        assert duck[u] == bq[u] == holdout_bucket(u, "salt_x")
    share = sum(1 for u in users if holdout_bucket(u, "salt_x") < 15) / len(users)
    assert 0.09 < share < 0.21  # 400 users: a 15% target within sampling error


def test_snowflake_bucket_uses_the_documented_hex_parse():
    expr = dialect("snowflake").holdout_bucket("user_id", "s")
    assert "TO_NUMBER(SUBSTR(MD5(user_id || ':s'), 1, 7), 'XXXXXXX')" in expr  # TO_DECIMAL('ae5', 'XXX') = 2789 in Snowflake's docs


def test_define_on_fixtures_verifies_dependencies_and_estimates(brief):
    c = cohort_analyst.define(brief, str(FIX))
    deps = {d.name: d for d in c.data_dependencies}
    assert deps["feature_used (feature=reports)"].status == "verified"
    assert deps["email_consent"].fill_rate == pytest.approx(0.8625)
    assert c.monthly_entrants.label == "DEMO DATA" and c.monthly_entrants.value > 0
    assert 0 < c.baseline_conversion_rate.value < 1
    assert c.as_of == "2026-10-18" and c.entrants_on_as_of == len(_independent_entrants(date(2026, 10, 18), 14))


def test_a_missing_event_is_blocked(brief):
    b = brief.model_copy(update={"core_conversion_event": EventMatch(event="report_shared")})
    deps = {d.name: d.status for d in cohort_analyst.define(b, str(FIX)).data_dependencies}
    assert deps["report_shared"] == "blocked"


# ----------------------------------------------------------------------------------------------
# audit
# ----------------------------------------------------------------------------------------------

def test_robust_detection_flags_a_spike_and_ignores_noise():
    as_of = date(2026, 10, 18)
    base = {as_of - timedelta(days=i): 100 + (i % 5) for i in range(1, 29)}
    calm = cohort_analyst.detect({"active_users": {**base, as_of: 104}}, as_of)["active_users"]
    spike = cohort_analyst.detect({"active_users": {**base, as_of: 30}}, as_of)["active_users"]
    tiny = cohort_analyst.detect({"signups": {as_of: 0}}, as_of)["signups"]
    assert calm["status"] == "ok" and spike["status"] == "anomaly" and tiny["status"] == "too small to judge"


def test_audit_from_warehouse_results_matches_the_direct_run(brief, con, tmp_path):
    direct = cohort_analyst.audit(brief, str(FIX))
    q = cohort_analyst.audit_sql(brief, "duckdb", direct["as_of"])
    for name, sql in q.items():  # emulate bq --format=json: every value a string
        rows = [{k: str(v)[:10] if hasattr(v, "year") else str(v) for k, v in r.items()} for r in cohort_analyst._rows(con, sql)]
        (tmp_path / f"{name}.json").write_text(json.dumps(rows))
    via = cohort_analyst.audit(brief, results=tmp_path, as_of=direct["as_of"])
    assert via["metrics"] == direct["metrics"] and via["risk_bands"] == direct["risk_bands"] and via["funnel"] == direct["funnel"]


# ----------------------------------------------------------------------------------------------
# experiment design
# ----------------------------------------------------------------------------------------------

def _cohort(brief, entrants, baseline):
    c = cohort_analyst.define(brief)
    return c.model_copy(update={"monthly_entrants": c.monthly_entrants.model_copy(update={"value": entrants, "label": "SUPPLIED"}),
                                "baseline_conversion_rate": c.baseline_conversion_rate.model_copy(update={"value": baseline, "label": "SUPPLIED"})})


def test_holdout_sizing_uses_the_stats_tool(brief):
    from tools import stats
    d = experimentation_agent.design(brief, _cohort(brief, 5000, 0.10))
    f = stats.feasibility(5000, 0.15, 0.10, mde_rel=0.25, outcome_window_days=brief.outcome_window_days)
    assert d.read == f["expected_read"] and str(f["required_holdout_n"]) in d.sizing.replace(",", "")
    assert d.feasibility["months_to_read"] == f["months_to_read"]
    assert [m["months_of_entries"] for m in d.mde_by_horizon] == [3, 6, 12]


def test_unknown_volume_is_not_readable_and_shows_a_grid(brief):
    d = experimentation_agent.design(brief, _cohort(brief, None, None))
    assert d.read == "not readable" and len(d.sensitivity) == 9


def test_zero_holdout_validates_and_is_directional(brief):
    import jsonschema
    b = brief.model_copy(update={"holdout_pct": 0})
    d = experimentation_agent.design(b, _cohort(b, 5000, 0.1))
    spec = experimentation_agent.attach({"holdout": {}, "measurement": {}}, d)
    schema = json.loads((ROOT / "schemas/flow_spec.schema.json").read_text())
    jsonschema.validate(spec["holdout"], schema["properties"]["holdout"])
    assert d.read == "directional only" and spec["measurement"]["compared_against"] == "pre_post_baseline"


# ----------------------------------------------------------------------------------------------
# guardrails
# ----------------------------------------------------------------------------------------------

def test_gate_policy():
    assert guardrails.gate_policy("GREEN")["entry_filters"] == []
    assert guardrails.gate_policy("UNKNOWN")["effective_gate"] == "AMBER"
    assert guardrails.gate_policy("AMBER")["entry_filters"] == [guardrails.ENGAGED_RULE]
    assert guardrails.gate_policy("RED")["sending_allowed"] is False


@pytest.mark.parametrize("fields,policy,needle", [
    ({"subject": "RE: your account", "preheader": "x", "paragraphs": ["a"], "cta_label": "Go"}, "no discounts", "RE: or FW:"),
    ({"subject": "Last chance to come back", "preheader": "x", "paragraphs": ["a"], "cta_label": "Go"}, "no discounts", "fake urgency"),
    ({"subject": "20% off your next report", "preheader": "x", "paragraphs": ["a"], "cta_label": "Go"}, "no discounts or offers", "offer policy"),
    ({"subject": "Hi {company}", "preheader": "x", "paragraphs": ["a"], "cta_label": "Go"}, "none", "unknown personalization token"),
])
def test_copy_guardrails_block(fields, policy, needle):
    errors, _ = guardrails.check_email_fields(fields, policy)
    assert any(needle in e for e in errors), errors


def test_offers_allowed_when_the_policy_allows_them():
    errors, _ = guardrails.check_email_fields({"subject": "Free shipping on your next order", "preheader": "x", "paragraphs": ["a"],
                                               "cta_label": "Shop"}, "free shipping approved")
    assert not errors


def test_text_to_image():
    image_only = '<html><body><img src="a.png" alt="Sale"><img src="b.png" alt=""></body></html>'
    readable = "<html><body><p>" + "Your report templates are ready. " * 12 + '</p><img src="a.png" alt="logo"></body></html>'
    assert guardrails.text_to_image(image_only)["errors"]
    assert not guardrails.text_to_image(readable)["errors"]
    hidden = '<div style="display:none;max-height:0">' + "x " * 300 + '</div><img src="a.png" alt="x">'
    assert guardrails.text_to_image(hidden)["errors"], "a hidden preheader must not count as readable text"


def test_sms_guardrails():
    e, _w, r = guardrails.check_sms("Acme: your report is ready. Reply STOP to opt out.", "Acme", False)
    assert not e and r["segments"] == 1
    e, _w, _r = guardrails.check_sms("Acme: your report is ready.", "Acme", False)
    assert any("opt-out" in x for x in e)
