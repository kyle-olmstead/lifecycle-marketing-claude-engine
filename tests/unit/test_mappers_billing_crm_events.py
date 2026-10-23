"""The WP-A2 source adapters: Stripe, HubSpot, Customer.io, GA4 (BigQuery export), RudderStack and Segment.

Each mapper runs on its Ridgeview sample export (examples/brands/ridgeview_saas/exports/<source>/); the output must
load with engine/data/load.py, carry every required column, and keep ids unique. Each warehouse SQL model parses in
its dialect and, transpiled to DuckDB with sqlglot, returns exactly the rows the Python mapper writes from the same
sample. No network.
"""
import csv
import filecmp
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import sqlglot

from tests.conftest import ROOT
from adapters.sources import customerio_export, ga4_bigquery, hubspot_export, rudderstack, segment, stripe
from adapters.sources._mapping import TimeParser, country_iso2, norm
from adapters.sources.report import MappingReport
from engine.data.load import connect as load
from engine.data.schema import TABLES
from engine.sql import check as sql_check

EXPORTS = ROOT / "examples/brands/ridgeview_saas/exports"
DEMO_USERS = ROOT / "tests/fixtures/ridgeview_small/data/users.csv"        # the exports' ten people come from the small world
DEMO_EVENTS = ROOT / "tests/fixtures/ridgeview_small/data/track_events.csv"
SQL = ROOT / "adapters/sources/sql"
MAPPERS = {"stripe": stripe, "hubspot": hubspot_export, "customerio": customerio_export, "ga4": ga4_bigquery,
           "rudderstack": rudderstack, "segment": segment}
PRIMARY_KEY = {"users": "user_id", "track_events": "event_id", "accounts": "account_id", "subscriptions": "subscription_id",
               "invoices": "invoice_id", "message_events": "event_id"}
GA4_COLUMNS = {
    "event_date": "VARCHAR", "event_timestamp": "BIGINT", "event_name": "VARCHAR",
    "event_params": "STRUCT(key VARCHAR, value STRUCT(string_value VARCHAR, int_value BIGINT, float_value DOUBLE, double_value DOUBLE))[]",
    "event_bundle_sequence_id": "BIGINT", "batch_event_index": "BIGINT", "user_id": "VARCHAR", "user_pseudo_id": "VARCHAR",
    "items": "STRUCT(item_id VARCHAR, item_name VARCHAR, item_brand VARCHAR, item_variant VARCHAR, item_category VARCHAR, "
             "price DOUBLE, quantity BIGINT, item_revenue DOUBLE, coupon VARCHAR)[]",
    "geo": "STRUCT(country VARCHAR, continent VARCHAR)", "device": "STRUCT(category VARCHAR, operating_system VARCHAR)",
    "ecommerce": "STRUCT(transaction_id VARCHAR, purchase_revenue DOUBLE, total_item_quantity BIGINT)",
}


def rows(path: Path):
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def demo_users():
    return {r["user_id"]: r for r in rows(DEMO_USERS)}


@pytest.fixture(scope="module")
def mapped(tmp_path_factory):
    """Every mapper run once on its sample: name -> (report, out_dir)."""
    out = {}
    for name, mod in MAPPERS.items():
        d = tmp_path_factory.mktemp(f"out_{name}")
        out[name] = (mod.map(EXPORTS / name, d), d)
    return out


# ----------------------------------------------------------------------------------------------
# the shared interface, detection, loading
# ----------------------------------------------------------------------------------------------

def test_mapping_report_is_the_shared_interface():
    r = MappingReport(source="x")
    assert r.to_dict() == {"source": "x", "tables": {}, "unmapped_columns": {}, "assumptions": [], "warnings": []}
    for name, mod in MAPPERS.items():
        assert mod.NAME == name and isinstance(mod.DESCRIPTION, str) and mod.DESCRIPTION
        assert callable(mod.detect) and callable(mod.map)


def test_each_detector_claims_only_its_own_sample():
    folders = list(MAPPERS) + ["../data"]
    for name, mod in MAPPERS.items():
        claimed = [f for f in folders if mod.detect(EXPORTS / f)]
        assert claimed == [name], (name, claimed)
        assert mod.detect(ROOT / "docs") is False


def test_mappers_find_their_own_files_in_a_parent_folder(mapped, tmp_path):
    """`lcm import auto exports/` hands every mapper the parent folder: each must find its own subfolder, ignore the
    other vendors' files (Segment's users.csv is not Customer.io people; Customer.io people are not Stripe customers),
    and write exactly what it writes from its own folder."""
    for name, mod in MAPPERS.items():
        assert mod.detect(EXPORTS), name
        report = mod.map(EXPORTS, tmp_path / name)
        assert report.tables == mapped[name][0].tables, name
        for table in report.tables:
            assert filecmp.cmp(tmp_path / name / f"{table}.csv", mapped[name][1] / f"{table}.csv", shallow=False), (name, table)


def test_every_sample_maps_and_loads_with_required_columns_and_unique_ids(mapped):
    for name, (report, out) in mapped.items():
        assert report.source == name and report.tables, name
        for table, n in report.tables.items():
            assert (out / f"{table}.csv").is_file() and n > 0, (name, table)
            data = rows(out / f"{table}.csv")
            assert len(data) == n
            assert list(data[0].keys()) == TABLES[table].column_names, (name, table)
            for col in TABLES[table].columns:
                if col.required:
                    assert all(r[col.name] for r in data), (name, table, col.name)
            if table in PRIMARY_KEY:
                keys = [r[PRIMARY_KEY[table]] for r in data]
                assert len(keys) == len(set(keys)), (name, table)
        con, _notes = load(str(out), require=("users",) if "users" in report.tables else ("track_events",))
        for table, n in report.tables.items():
            assert con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == n, (name, table)
        if "track_events" in report.tables:
            bad = con.execute("SELECT COUNT(*) FROM track_events WHERE json_valid(properties::VARCHAR) = false").fetchone()[0]
            assert bad == 0, name


def test_timestamps_are_iso_utc_and_samples_carry_no_unexpected_warnings(mapped):
    for name, (report, out) in mapped.items():
        allowed = ["non-marketing contact"] if name == "hubspot" else []
        assert all(any(a in w for a in allowed) for w in report.warnings), (name, report.warnings)
        for table in report.tables:
            for col in TABLES[table].columns:
                if col.type == "TIMESTAMP":
                    for r in rows(out / f"{table}.csv"):
                        v = r[col.name]
                        assert not v or (v.endswith("Z") and "T" in v), (name, table, col.name, v)


def test_the_same_synthetic_people_appear_in_every_export(mapped):
    demo = demo_users()
    for name in ("stripe", "hubspot", "customerio", "rudderstack", "segment"):
        users = {r["user_id"]: r for r in rows(mapped[name][1] / "users.csv")}
        assert set(users) <= {f"u_{i:04d}" for i in range(1, 11)}, name
        for uid, u in users.items():
            assert u["email"] == demo[uid]["email"] and u["email"].endswith("@example.test")
            assert u["created_at"].replace("T", " ").rstrip("Z") == demo[uid]["created_at"], (name, uid)
            if u["country"]:
                assert u["country"] == demo[uid]["country"], (name, uid)
    assert len(rows(mapped["stripe"][1] / "users.csv")) == 9     # u_0008 never reached Stripe
    for name in ("hubspot", "customerio", "rudderstack", "segment"):
        assert len(rows(mapped[name][1] / "users.csv")) == 10, name


# ----------------------------------------------------------------------------------------------
# Stripe
# ----------------------------------------------------------------------------------------------

def test_stripe_subscriptions_mrr_and_statuses(mapped):
    report, out = mapped["stripe"]
    subs = {r["subscription_id"]: r for r in rows(out / "subscriptions.csv")}
    assert subs["sub_0002"]["mrr"] == "297.00" and subs["sub_0002"]["status"] == "active"     # 3 seats x 99
    assert subs["sub_0003"]["mrr"] == "29.00" and subs["sub_0003"]["status"] == "past_due"    # past_due still counts
    assert subs["sub_0004"]["mrr"] == "24.17" and subs["sub_0004"]["currency"] == "EUR"       # 290 a year / 12
    assert subs["sub_0005"]["mrr"] == ""                                                      # coupon value not exported
    assert subs["sub_0007"]["mrr"] == "499.00" and subs["sub_0007"]["currency"] == "CAD"
    assert {subs[s]["mrr"] for s in ("sub_0006", "sub_0009", "sub_0010")} == {"0.00"}         # canceled, trialing
    assert subs["sub_0004"]["canceled_at"] == "2026-09-15T10:00:00Z"
    assert any("coupon" in a and a.startswith("ASSUMED") for a in report.assumptions)
    accounts = {r["account_id"]: r for r in rows(out / "accounts.csv")}
    assert accounts["acc_0003"]["status"] == "past_due" and accounts["acc_0002"]["name"] == "Workspace 0002"
    users = {r["user_id"]: r for r in rows(out / "users.csv")}
    assert users["u_0002"]["account_id"] == "acc_0002" and users["u_0002"]["plan"] == "Ridgeview Growth"
    assert users["u_0001"]["account_id"] == "" and users["u_0001"]["plan"] == ""


def test_stripe_invoices_and_billing_moments(mapped):
    _report, out = mapped["stripe"]
    inv = {r["invoice_id"]: r for r in rows(out / "invoices.csv")}
    assert inv["in_0003_06"]["status"] == "open" and inv["in_0003_06"]["attempt_count"] == "3"
    assert inv["in_0003_06"]["next_payment_attempt_at"] == "2026-10-22T02:00:00Z" and inv["in_0003_06"]["paid_at"] == ""
    assert inv["in_0002_06"]["paid_at"] == "2026-05-25T06:00:00Z"
    events = rows(out / "track_events.csv")
    by = {}
    for e in events:
        by.setdefault(e["event_name"], []).append(e)
    assert {e["user_id"] for e in by["trial_started"]} == {"u_0002", "u_0005", "u_0006", "u_0010"}
    started = {e["user_id"]: e for e in by["subscription_started"]}
    assert set(started) == {"u_0002", "u_0003", "u_0004", "u_0005", "u_0007", "u_0009"}       # never u_0006 (cancelled in trial)
    assert started["u_0002"]["event_at"] == "2025-12-22T05:00:00Z"                              # first paid invoice, not the 0.00 one
    props = json.loads(started["u_0002"]["properties"])
    assert props["mrr"] == 297.0 and props["trial"] is True and props["interval"] == "month"
    assert "mrr" not in json.loads(started["u_0005"]["properties"])                             # coupon: unknown, not overstated
    failed = {e["event_id"]: json.loads(e["properties"]) for e in by["payment_failed"]}
    assert set(failed) == {"stripe:payment_failed:in_0002_06", "stripe:payment_failed:in_0003_06", "stripe:payment_failed:in_0009_02"}
    assert failed["stripe:payment_failed:in_0009_02"]["attempt_count"] == 4
    assert failed["stripe:payment_failed:in_0003_06"]["dated_at"] == "invoice_finalized"
    assert len(by["payment_succeeded"]) == sum(1 for r in inv.values() if r["status"] == "paid" and r["amount_paid"] != "0.00")
    cancelled = {e["user_id"]: json.loads(e["properties"]) for e in by["subscription_cancelled"]}
    assert cancelled["u_0004"] == {"subscription_id": "sub_0004", "reason": "price", "effective_at": "2026-12-08T19:00:00Z",
                                   "cancellation_reason": "cancellation_requested", "cancel_at_period_end": True}
    assert cancelled["u_0006"]["reason"] == "not_using"
    assert cancelled["u_0009"]["reason"] == "unknown" and cancelled["u_0009"]["cancellation_reason"] == "payment_failed"


def _stripe_api_fixture(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    customers = {"object": "list", "data": [
        {"id": "cus_api1", "object": "customer", "email": "U_0101@Example.test", "created": 1767225600, "phone": "+1 555 0101",
         "address": {"country": "JP"}, "business_name": "Workspace 0101", "metadata": {"user_id": "u_0101"}},
        {"id": "cus_api2", "object": "customer", "email": "u_0102@example.test", "created": 1767225600, "metadata": {}}]}
    subs = {"object": "list", "data": [
        {"id": "sub_api1", "object": "subscription", "customer": "cus_api1", "status": "active", "currency": "jpy",
         "start_date": 1767225600, "created": 1767225600, "trial_start": None, "trial_end": None, "canceled_at": None,
         "cancel_at": None, "cancel_at_period_end": False, "ended_at": None,
         "cancellation_details": {"reason": None, "feedback": None},
         "discounts": [{"coupon": {"percent_off": 10, "duration": "forever"}}],
         "items": {"object": "list", "data": [
             {"id": "si_1", "quantity": 2, "current_period_end": 1772323200,
              "price": {"id": "price_jp", "nickname": "team", "unit_amount": 1200, "currency": "jpy",
                        "recurring": {"interval": "month", "interval_count": 1, "usage_type": "licensed"}}},
             {"id": "si_2", "quantity": 1, "current_period_end": 1798761600,
              "price": {"id": "price_jp_y", "nickname": "addon", "unit_amount": 12000, "currency": "jpy",
                        "recurring": {"interval": "year", "interval_count": 1, "usage_type": "licensed"}}}]}},
        {"id": "sub_api2", "object": "subscription", "customer": "cus_api2", "status": "active", "currency": "usd",
         "start_date": 1767225600, "created": 1767225600, "items": {"object": "list", "data": [
             {"id": "si_3", "quantity": 1, "current_period_end": 1769904000,
              "price": {"id": "price_metered", "unit_amount": None, "currency": "usd",
                        "recurring": {"interval": "month", "interval_count": 1, "usage_type": "metered"}}}]}}]}
    invoices = {"object": "list", "data": [
        {"id": "in_api1", "object": "invoice", "customer": "cus_api1", "currency": "jpy", "amount_due": 3060, "amount_paid": 3060,
         "attempt_count": 1, "created": 1767225600, "status": "paid", "next_payment_attempt": None,
         "parent": {"type": "subscription_details", "subscription_details": {"subscription": "sub_api1"}},
         "status_transitions": {"finalized_at": 1767225600, "paid_at": 1767229200}},
        {"id": "in_api2", "object": "invoice", "customer": "cus_api2", "currency": "usd", "amount_due": 1999, "amount_paid": 0,
         "attempt_count": 2, "created": 1767225600, "status": "open", "subscription": "sub_api2", "next_payment_attempt": 1767484800,
         "status_transitions": {"finalized_at": 1767229200, "paid_at": None}}]}
    for name, doc in (("customers.json", customers), ("subscriptions.json", subs), ("invoices.json", invoices)):
        (folder / name).write_text(json.dumps(doc), encoding="utf-8")


def test_stripe_reads_the_api_json_shape_with_minor_units_and_basil_fields(tmp_path):
    _stripe_api_fixture(tmp_path / "in")
    assert stripe.detect(tmp_path / "in")
    report = stripe.map(tmp_path / "in", tmp_path / "out")
    users = {r["user_id"]: r for r in rows(tmp_path / "out/users.csv")}
    assert users["u_0101"]["email"] == "u_0101@example.test" and users["u_0101"]["country"] == "JP"
    assert users["u_0101"]["phone"] == "+15550101" and users["u_0101"]["account_id"] == "cus_api1"
    assert users["cus_api2"]["account_id"] == ""                                        # no metadata user id: falls back, says so
    assert any("business name but no account id" in a for a in report.assumptions)
    assert any("customer id (cus_...) is the user_id" in a for a in report.assumptions)
    subs = {r["subscription_id"]: r for r in rows(tmp_path / "out/subscriptions.csv")}
    # JPY is zero-decimal: (1200 x 2 + 12000 / 12) x 0.9 = 3060
    assert subs["sub_api1"]["mrr"] == "3060.00" and subs["sub_api1"]["currency"] == "JPY"
    assert subs["sub_api1"]["current_period_end_at"] == "2026-03-01T00:00:00Z"          # the earliest item period end
    assert subs["sub_api2"]["mrr"] == ""                                                 # metered: no fixed amount
    inv = {r["invoice_id"]: r for r in rows(tmp_path / "out/invoices.csv")}
    assert inv["in_api1"]["subscription_id"] == "sub_api1" and inv["in_api1"]["amount_paid"] == "3060.00"
    assert inv["in_api2"]["amount_due"] == "19.99" and inv["in_api2"]["next_payment_attempt_at"] == "2026-01-04T00:00:00Z"
    names = sorted(e["event_id"] for e in rows(tmp_path / "out/track_events.csv"))
    assert names == ["stripe:payment_failed:in_api2", "stripe:payment_succeeded:in_api1", "stripe:subscription_started:sub_api1"]
    assert any("metered" in w for w in report.warnings)


def test_stripe_without_a_customers_export_builds_users_from_subscriptions(tmp_path):
    (tmp_path / "in").mkdir()
    shutil.copy(EXPORTS / "stripe/subscriptions.csv", tmp_path / "in/subscriptions.csv")
    report = stripe.map(tmp_path / "in", tmp_path / "out")
    users = rows(tmp_path / "out/users.csv")
    assert {u["user_id"] for u in users} == {f"cus_{n}" for n in ("0002", "0003", "0004", "0005", "0006", "0007", "0009", "0010")}
    assert any("no customers export" in a for a in report.assumptions)
    assert any("no invoices export" in w for w in report.warnings)
    started = [e for e in rows(tmp_path / "out/track_events.csv") if e["event_name"] == "subscription_started"]
    # without invoices: trial conversions start at trial end, others at the start date; never a trial that was cancelled
    assert {e["user_id"] for e in started} == {"cus_0002", "cus_0003", "cus_0004", "cus_0005", "cus_0007", "cus_0009"}
    assert "accounts" not in report.tables


# ----------------------------------------------------------------------------------------------
# HubSpot
# ----------------------------------------------------------------------------------------------

def test_hubspot_users_and_conservative_opt_outs(mapped):
    report, out = mapped["hubspot"]
    users = {r["user_id"]: r for r in rows(out / "users.csv")}
    assert users["u_0010"]["lifecycle_stage"] == "opportunity" and users["u_0001"]["lifecycle_stage"] == "lead"
    assert users["u_0004"]["country"] == "DE"                                  # "Germany" -> DE
    assert all(not u["email_consent_at"] for u in users.values())             # never inferred
    consent = {r["user_id"]: r for r in rows(out / "consent_events.csv")}
    assert set(consent) == {"u_0004", "u_0006"}
    assert consent["u_0006"]["occurred_at"] == "2026-06-01T10:30:00Z" and consent["u_0006"]["status"] == "revoked"
    assert consent["u_0004"]["source"] == "import_undated" and consent["u_0004"]["purpose"] == "marketing"
    assert any("subscription types but not all" in a for a in report.assumptions)
    assert any("non-marketing" in w and "schema gap" in w for w in report.warnings)


def test_hubspot_time_zone_record_id_fallback_and_unknown_countries(tmp_path):
    (tmp_path / "in").mkdir()
    (tmp_path / "in/export.csv").write_text(
        "Record ID,Email,Create Date,Country/Region,Lifecycle Stage,Unsubscribed from all email,Time zone\n"
        "77,U_0201@Example.test,2026-01-15 09:00,Atlantis,marketingqualifiedlead,No,America/Chicago\n"
        "78,not-an-email,2026-01-15 09:00,USA,lead,No,\n", encoding="utf-8")
    report = hubspot_export.map(tmp_path / "in", tmp_path / "out", timezone="America/Chicago")
    users = rows(tmp_path / "out/users.csv")
    assert len(users) == 1 and users[0]["user_id"] == "77" and users[0]["email"] == "u_0201@example.test"
    assert users[0]["created_at"] == "2026-01-15T15:00:00Z"                    # 09:00 in Chicago is 15:00 UTC
    assert users[0]["lifecycle_stage"] == "marketing_qualified_lead" and users[0]["timezone"] == "America/Chicago"
    assert users[0]["country"] == ""
    assert any("Atlantis" in w for w in report.warnings) and any("valid email" in w for w in report.warnings)
    assert any("Record ID is the user_id" in a for a in report.assumptions)
    assert "consent_events" not in report.tables


# ----------------------------------------------------------------------------------------------
# Customer.io
# ----------------------------------------------------------------------------------------------

def test_customerio_message_events_and_unsubscribe(mapped):
    report, out = mapped["customerio"]
    msgs = {r["event_id"]: r for r in rows(out / "message_events.csv")}
    assert msgs["cio:dlv_0007:spammed"]["event_name"] == "complained"
    bounce = msgs["cio:dlv_0003:bounced"]
    assert bounce["smtp_status"] == "5.1.1" and "user unknown" in bounce["diagnostic"] and bounce["recipient_domain"] == "example.test"
    assert msgs["cio:dlv_0021:opened"]["channel"] == "push" and msgs["cio:dlv_0021:opened"]["recipient_domain"] == ""
    assert msgs["cio:dlv_0020:delivered"]["campaign"] == "Trial ending" and msgs["cio:dlv_0020:delivered"]["message_id"] == "44"
    assert not any(r["event_name"] == "sent" for r in msgs.values())
    consent = rows(out / "consent_events.csv")
    assert consent == [{"user_id": "u_0006", "channel": "email", "status": "revoked", "purpose": "marketing",
                        "source": "import_undated", "occurred_at": "2026-06-01T10:30:00Z"}]
    assert any("sent (10)" in a for a in report.assumptions)


def test_customerio_skips_channels_without_a_canonical_home(tmp_path):
    (tmp_path / "in").mkdir()
    header = "id,customer_id,template_id,channel,recipient,delivered,delivered_RFC3339,opened,opened_RFC3339\n"
    (tmp_path / "in/deliveries.csv").write_text(
        header + "d1,u_1,5,webhook,,1767225600,,,\n"
                 "d2,u_1,5,twilio,+15550100,1767225600,,,\n"
                 "d3,,5,email,x@example.test,,2026-01-01T00:00:00Z,,\n", encoding="utf-8")
    assert customerio_export.detect(tmp_path / "in")
    report = customerio_export.map(tmp_path / "in", tmp_path / "out")
    msgs = rows(tmp_path / "out/message_events.csv")
    assert [(m["event_id"], m["channel"], m["event_at"]) for m in msgs] == [("cio:d2:delivered", "sms", "2026-01-01T00:00:00Z")]
    assert any("webhook (1)" in w for w in report.warnings) and any("without a customer_id" in w for w in report.warnings)
    assert any("plain column" in a for a in report.assumptions)


# ----------------------------------------------------------------------------------------------
# GA4: Python mapper, Parquet, and the BigQuery SQL proved equal
# ----------------------------------------------------------------------------------------------

def _norm_ts(v):
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        dt = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return TimeParser().parse(v).astimezone(timezone.utc).replace(tzinfo=None)


def _canon(record, columns, ts_cols, json_cols=()):
    out = []
    for c in columns:
        v = record[c]
        if c in ts_cols:
            v = _norm_ts(v)
        elif c in json_cols:
            v = json.dumps(json.loads(v) if isinstance(v, str) else v, sort_keys=True)
        elif v == "":
            v = None
        out.append(v)
    return tuple(out)


def _typed_columns(table: str):
    """(timestamp columns, JSON columns) of a canonical table."""
    t = TABLES[table]
    ts = {c.name for c in t.columns if c.type == "TIMESTAMP"}
    js = {c.name for c in t.columns if c.type == "JSON"}
    return ts, js


def _ga4_duck(jsonl: Path):
    import duckdb
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    con.execute("CREATE SCHEMA analytics_123456789")
    spec = "{" + ", ".join(f"'{k}': '{v}'" for k, v in GA4_COLUMNS.items()) + "}"
    con.execute(f"""CREATE TABLE analytics_123456789."events_*" AS
                    SELECT * FROM read_json('{jsonl.as_posix()}', format='newline_delimited', columns={spec})""")
    return con


def _sql_rows(con, sql: str, dialect: str):
    cur = con.execute(sqlglot.transpile(sql, read=dialect, write="duckdb")[0])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def test_ga4_bigquery_sql_parses_and_matches_the_python_mapper(mapped):
    sql = (SQL / "ga4/track_events_bigquery.sql").read_text()
    assert sql_check.declared_dialect(sql) == "bigquery" and sql_check.parse(sql, "bigquery")[0]
    con = _ga4_duck(EXPORTS / "ga4/events-000000000000.ndjson")
    cols = TABLES["track_events"].column_names
    got = {_canon(r, cols, {"event_at"}, {"properties"}) for r in _sql_rows(con, sql, "bigquery")}
    want = {_canon(r, cols, {"event_at"}, {"properties"}) for r in rows(mapped["ga4"][1] / "track_events.csv")}
    assert len(want) == 79 and got == want


def test_ga4_event_names_ids_and_properties(mapped):
    events = {r["event_id"]: r for r in rows(mapped["ga4"][1] / "track_events.csv")}
    names = {r["event_name"] for r in events.values()}
    assert {"order_completed", "signed_up", "session_started", "page_view", "report_shared", "feature_used"} <= names
    assert not names & {"purchase", "sign_up", "login", "reportShared"}
    purchase = events["purchase:o_0002_01"]
    props = json.loads(purchase["properties"])
    assert purchase["user_id"] == "u_0002" and props["transaction_id"] == "o_0002_01" and props["value"] == 201.25
    assert props["items"][0]["quantity"] == 3 and props["items"][0]["item_variant"] is None
    assert props["geo_country"] == "United States" and props["device_category"] == "desktop"
    anon = [r for r in events.values() if r["event_name"] == "page_view"]
    assert anon and all(r["user_id"] == "" and r["anonymous_id"] for r in anon)
    shared = next(r for r in events.values() if r["event_name"] == "report_shared")
    assert shared["event_at"] == "2026-10-02T11:20:00.000250Z" and json.loads(shared["properties"])["recipients"] == 3


def test_ga4_reads_parquet_and_drops_repeated_purchases(tmp_path, mapped):
    con = _ga4_duck(EXPORTS / "ga4/events-000000000000.ndjson")
    (tmp_path / "pq").mkdir()
    con.execute(f"""COPY analytics_123456789."events_*" TO '{(tmp_path / "pq/events_20261019.parquet").as_posix()}' (FORMAT PARQUET)""")
    assert ga4_bigquery.detect(tmp_path / "pq")
    ga4_bigquery.map(tmp_path / "pq", tmp_path / "out_pq")
    assert rows(tmp_path / "out_pq/track_events.csv") == rows(mapped["ga4"][1] / "track_events.csv")

    lines = (EXPORTS / "ga4/events-000000000000.ndjson").read_text().splitlines()
    purchase = next(json.loads(x) for x in lines if '"purchase"' in x)
    again = dict(purchase, event_timestamp=str(int(purchase["event_timestamp"]) + 60_000_000), event_bundle_sequence_id="999")
    (tmp_path / "dup").mkdir()
    (tmp_path / "dup/rows.jsonl").write_text(json.dumps(again) + "\n" + json.dumps(purchase) + "\n")
    report = ga4_bigquery.map(tmp_path / "dup", tmp_path / "out_dup")
    out = rows(tmp_path / "out_dup/track_events.csv")
    assert len(out) == 1 and _norm_ts(out[0]["event_at"]) == _norm_ts(datetime.fromtimestamp(int(purchase["event_timestamp"]) / 1e6, timezone.utc))
    assert any("duplicates" in w for w in report.warnings)


# ----------------------------------------------------------------------------------------------
# Segment and RudderStack: Python mapper and the BigQuery and Snowflake SQL proved equal
# ----------------------------------------------------------------------------------------------

def _warehouse_duck(source: str):
    import duckdb
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    con.execute(f"CREATE SCHEMA {source}")
    for table in ("tracks", "identifies", "users"):
        p = EXPORTS / source / f"{table}.csv"
        if p.is_file():
            con.execute(f"CREATE TABLE {source}.{table} AS SELECT * FROM read_csv('{p.as_posix()}', header=true)")
    return con


@pytest.mark.parametrize("source", ["segment", "rudderstack"])
@pytest.mark.parametrize("dialect", ["bigquery", "snowflake"])
def test_warehouse_sql_matches_the_python_mapper(source, dialect, tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    for table in ("tracks", "identifies", "users"):     # the SQL reads only these, so the Python side gets only these too
        if (EXPORTS / source / f"{table}.csv").is_file():
            shutil.copy(EXPORTS / source / f"{table}.csv", base / f"{table}.csv")
    MAPPERS[source].map(base, tmp_path / "out")
    con = _warehouse_duck(source)
    for table in ("track_events", "users"):
        sql = (SQL / source / f"{table}_{dialect}.sql").read_text()
        assert sql_check.declared_dialect(sql) == dialect and sql_check.parse(sql, dialect)[0], (source, table, dialect)
        got_rows = _sql_rows(con, sql, dialect)
        assert set(got_rows[0]) <= set(TABLES[table].column_names)
        cols = list(got_rows[0])
        ts, js = _typed_columns(table)
        got = {_canon(r, cols, ts, js) for r in got_rows}
        want = {_canon(r, cols, ts, js) for r in rows(tmp_path / "out" / f"{table}.csv")}
        assert got == want, (source, table, dialect, got ^ want)
        assert len(want) == (78 if table == "track_events" else 10)


@pytest.mark.parametrize("source", ["segment", "rudderstack"])
def test_per_event_tables_restore_the_canonical_properties(source, mapped):
    """The samples were dumped from the demo's canonical events, so mapping them back must give the same events."""
    out = {r["event_id"]: r for r in rows(mapped[source][1] / "track_events.csv")}
    demo = {r["event_id"]: r for r in rows(DEMO_EVENTS) if r["event_id"] in out}
    assert len(demo) == len(out) == 78
    for eid, e in out.items():
        d = demo[eid]
        assert (e["user_id"], e["anonymous_id"], e["event_name"]) == (d["user_id"], d["anonymous_id"], d["event_name"])
        assert _norm_ts(e["event_at"]) == _norm_ts(d["event_at"])
        assert json.loads(e["properties"]) == json.loads(d["properties"]), eid


def test_warehouse_users_take_the_latest_trait_per_user(mapped):
    for source in ("segment", "rudderstack"):
        users = {r["user_id"]: r for r in rows(mapped[source][1] / "users.csv")}
        assert users["u_0002"]["timezone"] == "America/Chicago"      # the later identify wins
        assert users["u_0004"]["timezone"] == "Europe/Berlin" and users["u_0001"]["plan"] == "free"


def test_cdp_detection_needs_a_vendor_signal(tmp_path):
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain/tracks.csv").write_text("id,user_id,event,event_text,timestamp\ne1,u1,signed_up,Signed Up,2026-01-01T00:00:00Z\n")
    assert not segment.detect(tmp_path / "plain") and not rudderstack.detect(tmp_path / "plain")
    report = segment.map(tmp_path / "plain", tmp_path / "out")      # mapping still works when the source is named
    assert report.tables == {"track_events": 1} and any("no per-event tables" in w for w in report.warnings)


# ----------------------------------------------------------------------------------------------
# helpers, SQL files, and the sample generator
# ----------------------------------------------------------------------------------------------

def test_helpers_never_guess():
    assert norm("Current Period End (UTC)") == norm("current_period_end") == "currentperiodend"
    assert country_iso2("Germany") == "DE" and country_iso2("uk") == "GB" and country_iso2("Atlantis") is None
    assert country_iso2("Germany", names=False) is None and country_iso2("XX") is None
    tp = TimeParser()
    assert tp.parse("1767225600").isoformat() == "2026-01-01T00:00:00+00:00"
    assert tp.parse("1767225600000000").isoformat() == "2026-01-01T00:00:00+00:00"          # GA4 microseconds
    assert tp.parse("2026-01-01 00:00:00 UTC") == tp.parse("2026-01-01T00:00:00.000Z")
    assert tp.parse("2026-01-01 00:00:00.000 +0000") == tp.parse("2026-01-01T00:00:00Z")
    assert tp.parse("not a date") is None and tp.counts["unparsed"] == 1


def test_every_warehouse_sql_file_declares_and_parses_its_dialect():
    files = sorted(SQL.rglob("*.sql"))
    assert len(files) == 9
    for f in files:
        text = f.read_text()
        dialect = sql_check.declared_dialect(text)
        assert dialect == f.stem.rsplit("_", 1)[1], f
        assert sql_check.parse(text, dialect)[0], f


def test_sample_exports_are_deterministic(tmp_path):
    data, exports = tmp_path / "tests/fixtures/ridgeview_small/data", tmp_path / "examples/brands/ridgeview_saas/exports"
    data.mkdir(parents=True)
    exports.mkdir(parents=True)
    shutil.copy(DEMO_USERS, data / "users.csv")
    shutil.copy(DEMO_EVENTS, data / "track_events.csv")
    shutil.copy(EXPORTS / "make_exports.py", exports / "make_exports.py")
    subprocess.run([sys.executable, str(exports / "make_exports.py")], check=True, capture_output=True)
    for f in sorted(p for p in EXPORTS.rglob("*") if p.is_file() and p.name != "make_exports.py" and "__pycache__" not in p.parts):
        rel = f.relative_to(EXPORTS)
        assert filecmp.cmp(f, exports / rel, shallow=False), rel


def test_decimal_amounts_are_exact(mapped):
    inv = rows(mapped["stripe"][1] / "invoices.csv")
    total = sum(Decimal(r["amount_paid"]) for r in inv if r["currency"] == "USD")
    assert total == Decimal("3532.60")       # 10 x 297.00 + 5 x 29.00 + 9 x 46.40
