"""Teammate agents: briefs, the cohort SQL (semantics and dialect equivalence), experiment design, guardrails."""
import csv
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import sqlglot

from tests.conftest import ROOT
from engine.agents import campaign_architect, cohort_analyst, experimentation_agent
from engine.policy import guardrails
from engine.brief import load_brief, parse_text, to_yaml
from engine.models import Brief, EventMatch, base_form
from engine.sql.dialects import dialect, holdout_bucket

FIX = ROOT / "examples" / "brands" / "ridgeview_saas" / "data"
EXAMPLE = ROOT / "examples" / "brands" / "ridgeview_saas" / "briefs" / "winback_14d.yaml"
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
    ("3-step welcome series for new signups who haven't connected a data source yet. Klaviyo, email and SMS.",
     {"flow_type": "activation", "touches": 3, "platform": "klaviyo", "sms": True}),
    ("Activation flow: new signups who have not created a report", {"flow_type": "activation"}),
    ("nudge new users who haven't invited a teammate yet", {"flow_type": "activation"}),
    ("Bring back users who haven't created a report in 14 days, new dashboard feature launched", {"flow_type": "winback"}),
    ("Users who haven't logged in since their trial ended 2 weeks ago", {"flow_type": "winback", "churn_threshold_days": 14}),
    ("Customers who have not ordered yet after signing up: 3 emails", {"flow_type": "welcome", "touches": 3}),   # a store's
    ("Send new customers who haven't placed a second order in 30 days a 3-email series, Klaviyo",
     {"flow_type": "winback", "churn_threshold_days": 30, "platform": "klaviyo"}),
    ("win back customers who haven't purchased in 2 months via Braze push", {"churn_threshold_days": 60, "platform": "braze", "push": True}),
    ("Reactivate users inactive for three weeks, 20% holdout, HubSpot", {"churn_threshold_days": 21, "holdout_pct": 20.0, "platform": "hubspot"}),
])
def test_sentence_variants(text, expect):
    b = parse_text(text, "Acme")
    if b.flow_type == "activation":
        assert b.qualifying_event.event == "signed_up"
    for k, v in expect.items():
        if k in ("sms", "push"):
            assert k in b.channels
        else:
            assert getattr(b, k) == v, (k, getattr(b, k))


def test_template_loads_and_round_trips(tmp_path):
    b = load_brief(ROOT / "templates" / "brief.yaml")
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


@pytest.mark.parametrize("label,expect", [
    ("created a report", "create a report"), ("placed an order", "place an order"), ("logged in", "log in"),
    ("used the product", "use the product"), ("bought again", "buy again"), ("scheduled a report", "schedule a report"),
    ("visited the app", "visit the app"), ("upgraded", "upgrade"), ("submitted a form", "submit a form"),
    ("connected a data source", "connect a data source"), ("invited a teammate", "invite a teammate"), ("added a seat", "add a seat"),
    ("shared a dashboard", "share a dashboard"), ("booked a call", "book a call"), ("typed a note", "type a note"),
    ("enabled sync", "enable sync"), ("opened the app", "open the app"), ("applied a filter", "apply a filter"),
    ("entered data", "enter data"), ("configured an alert", "configure an alert"), ("treated", "treat"), ("filed a ticket", "file a ticket"),
])
def test_base_form(label, expect):
    assert base_form(label) == expect


def test_send_hour_inside_quiet_hours_is_rejected():
    with pytest.raises(ValueError, match="outside the permitted"):
        Brief(product_name="A", send_hour_local=7, quiet_hours="22:00 to 08:00")
    assert Brief(product_name="A", send_hour_local=7, quiet_hours="22:00 to 06:00").send_hour_local == 7
    with pytest.raises(ValueError, match="quiet_hours must read"):
        Brief(product_name="A", quiet_hours="evenings")


# ----------------------------------------------------------------------------------------------
# cohort SQL: semantics against an independent computation, and the same answer in every dialect
# ----------------------------------------------------------------------------------------------

def _independent_entrants(as_of: date, n: int, catch_up: int = cohort_analyst.CATCH_UP_DAYS, ledger=frozenset()):
    """Who should be pushed on as_of, computed from the CSVs in plain Python (no SQL): the anchor date (last core action,
    or qualification) is n to n + catch_up days back, and that lapse is not already in the ledger."""
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
        if as_of - timedelta(days=n + catch_up) <= anchor <= as_of - timedelta(days=n) and (u, anchor) not in ledger:
            out.add((u, anchor))
    return out


AS_OF_DATES = [date(2026, 10, 18) - timedelta(days=7 * k) for k in range(0, 40)]


def test_entry_query_matches_an_independent_computation(brief, con):
    total = 0
    for d in AS_OF_DATES:
        rows = cohort_analyst._rows(con, cohort_analyst.entry_sql(brief, "duckdb", d.isoformat()))
        assert {(r["user_id"], r["anchor_date"]) for r in rows} == _independent_entrants(d, 14), d
        assert all(14 <= r["days_inactive"] <= 14 + cohort_analyst.CATCH_UP_DAYS for r in rows)
        total += len(rows)
    assert total >= 20, "the fixture dates must exercise the query"


def _mark(con, brief, rows):
    for r in rows:
        con.execute(cohort_analyst.mark_sql(brief, "duckdb"), {"user_id": r["user_id"], "anchor_date": str(r["anchor_date"])})


def test_mark_touches_only_the_rows_it_is_given(brief):
    con = cohort_analyst.connect(str(FIX))
    cohort_analyst._rows(con, cohort_analyst.queue_sql(brief, "duckdb", "2026-10-17"))
    first = cohort_analyst._rows(con, cohort_analyst.pending_sql(brief, "duckdb", "2026-10-17"))
    cohort_analyst._rows(con, cohort_analyst.queue_sql(brief, "duckdb", "2026-10-18"))   # an overlapping run queues more
    _mark(con, brief, first)
    left = {r["user_id"] for r in cohort_analyst._rows(con, cohort_analyst.pending_sql(brief, "duckdb", "2026-10-18"))}
    assert first and left and not left & {r["user_id"] for r in first}, "rows queued by the overlapping run stay pending"


def test_daily_job_pushes_each_lapse_once_retries_failures_and_catches_up(brief):
    con = cohort_analyst.connect(str(FIX))
    pushed = []
    day = date(2026, 6, 1)
    while day <= date(2026, 10, 18):
        if day.day % 9 != 0:   # some days the job does not run at all
            cohort_analyst._rows(con, cohort_analyst.queue_sql(brief, "duckdb", day.isoformat()))
            rows = cohort_analyst._rows(con, cohort_analyst.pending_sql(brief, "duckdb", day.isoformat()))
            if day.day % 7 == 0:   # the push fails: nothing is marked, the rows stay pending
                day += timedelta(days=1)
                continue
            ok = rows if day.day % 5 else rows[1:]       # some days one request fails: that row stays pending
            pushed += [(r["user_id"], r["anchor_date"]) for r in ok]
            _mark(con, brief, ok)
        day += timedelta(days=1)
    assert len(pushed) == len(set(pushed)) > 20, "no lapse is pushed twice"
    expected = set()
    day = date(2026, 6, 1)
    while day <= date(2026, 10, 18):
        expected |= {x for x in _independent_entrants(day, 14, catch_up=0) if x[1] >= date(2026, 6, 1) - timedelta(days=14)}
        day += timedelta(days=1)
    caught = {x for x in expected if x[1] + timedelta(days=14 + cohort_analyst.CATCH_UP_DAYS) <= date(2026, 10, 18)}
    assert caught <= set(pushed), "every lapse is pushed within the catch-up window, even across skipped runs"
    ledger = con.execute("SELECT COUNT(*), COUNT(DISTINCT user_id || CAST(anchor_date AS VARCHAR)), "
                         "SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) FROM lcm_flow_entries").fetchone()
    assert ledger[0] == ledger[1] == len(pushed) + ledger[2]


def test_a_late_event_is_queued_later_not_lost(brief, tmp_path):
    """A core event loaded late moves a person's anchor; they are queued on a later run, never recorded without a push."""
    import shutil
    for t in ("users", "orders", "track_events"):
        shutil.copy(FIX / f"{t}.csv", tmp_path / f"{t}.csv")
    con = cohort_analyst.connect(str(tmp_path))
    cohort_analyst._rows(con, cohort_analyst.queue_sql(brief, "duckdb", "2026-10-18"))
    queued = {r["user_id"] for r in cohort_analyst._rows(con, cohort_analyst.pending_sql(brief, "duckdb", "2026-10-18"))}
    late_user = next(u for (u,) in con.execute("SELECT DISTINCT user_id FROM track_events WHERE event_name = 'onboarding_step_completed' "
                                                "AND json_extract_string(properties, '$.step_index') = '3' ORDER BY 1").fetchall()
                     if u not in queued)
    con.execute("INSERT INTO track_events VALUES ('late_0001', ?, NULL, 'feature_used', TIMESTAMP '2026-10-05 12:00:00', "
                "'{\"feature\":\"reports\"}')", [late_user])
    _mark(con, brief, cohort_analyst._rows(con, cohort_analyst.pending_sql(brief, "duckdb", "2026-10-18")))
    assert late_user not in {r[0] for r in con.execute("SELECT user_id FROM lcm_flow_entries").fetchall()}
    cohort_analyst._rows(con, cohort_analyst.queue_sql(brief, "duckdb", "2026-10-19"))
    assert late_user in {r["user_id"] for r in cohort_analyst._rows(con, cohort_analyst.pending_sql(brief, "duckdb", "2026-10-19"))}


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
    sqls = [cohort_analyst.entry_sql(brief, d), cohort_analyst.history_sql(brief, d), *cohort_analyst.daily_job_sql(brief, d).values(),
            *cohort_analyst.audit_sql(brief, d).values()]
    for sql in sqls:
        ok, msg = __import__("engine.sql.check", fromlist=["parse"]).parse(sql, d)
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


def test_activation_volume_counts_new_qualifiers(brief):
    a = brief.model_copy(update={"flow_type": "activation", "qualifying_event": EventMatch(event="signed_up", label="signed up"), "touches": 4})
    c = cohort_analyst.define(a, str(FIX))
    assert "new qualifiers" in c.monthly_entrants.basis and "went on to create a report" in c.baseline_conversion_rate.basis
    assert c.trigger_payload_example == {} and "re-entry off" in c.platform_pattern and c.job_sql == {}
    assert "new_qualifiers" in cohort_analyst.audit(a, str(FIX))["metrics"]
    assert cohort_analyst.LEDGER not in cohort_analyst.entry_sql(a, "postgres"), "activation needs no ledger table"


def test_activation_cannot_qualify_on_its_own_core_action():
    with pytest.raises(ValueError, match="same"):
        Brief(product_name="A", flow_type="activation", qualifying_event=EventMatch(event="order_completed"),
              core_conversion_event=EventMatch(event="order_completed"))
    b = Brief(product_name="A", flow_type="winback", qualifying_event=EventMatch(event="order_completed"),
              core_conversion_event=EventMatch(event="order_completed"))   # first order, then no repeat: a valid win-back
    assert b.flow_type == "winback"


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
    from engine.measurement import stats
    d = experimentation_agent.design(brief, _cohort(brief, 5000, 0.10))
    f = stats.feasibility(5000, 0.15, 0.10, mde_rel=0.25, outcome_window_days=campaign_architect.outcome_window(brief))
    assert d.read == f["expected_read"] and str(f["required_holdout_n"]) in d.sizing.replace(",", "")
    assert d.feasibility["months_to_read"] == f["months_to_read"]
    assert [m["months_of_entries"] for m in d.mde_by_horizon] == [3, 6, 12]


def test_a_baseline_with_no_room_for_the_lift_is_not_readable(brief):
    d = experimentation_agent.design(brief, _cohort(brief, 5000, 0.9))
    assert d.read == "not readable" and "no room" in d.sizing


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
    from datetime import datetime, timezone
    record = {"verdict": "GREEN", "assessed_at": datetime.now(timezone.utc).isoformat(), "auth_label": "LIVE PULL", "domain": "example.com",
              "signals": {"complaint_rate": 0.0004, "hard_bounce_rate": 0.002, "authentication_state": "verified"}}
    assert guardrails.gate_policy(record)["entry_filters"] == []
    # a GREEN record without the signals it is issued on reads AMBER
    assert guardrails.gate_policy({**record, "signals": {}})["effective_gate"] == "AMBER"
    # a bare GREEN is a claim with no evidence or date: it runs as AMBER
    assert guardrails.gate_policy("GREEN")["effective_gate"] == "AMBER"
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
