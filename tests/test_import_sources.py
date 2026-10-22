"""lcm import (adapters/sources): the registry, the Shopify and Klaviyo mappers on their sample exports and on
hand-made variants, the merge rules, and the Northfield demo brand's reproducibility."""
import csv
import filecmp
import gzip
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from adapters.sources import cli, export_io, klaviyo_export, registry, shopify
from adapters.sources.report import MappingReport
from engine.data import check
from tests.conftest import ROOT

BRAND = ROOT / "examples/brands/northfield_dtc"
EXPORTS = BRAND / "exports"
AS_OF = datetime(2026, 10, 19, tzinfo=timezone.utc)


def rows(path: Path):
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, header, body) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(body)
    return path


@pytest.fixture(scope="module")
def shopify_out(tmp_path_factory):
    out = tmp_path_factory.mktemp("shopify_out")
    return shopify.map(EXPORTS / "shopify", out), out


@pytest.fixture(scope="module")
def klaviyo_out(tmp_path_factory):
    out = tmp_path_factory.mktemp("klaviyo_out")
    return klaviyo_export.map(EXPORTS / "klaviyo", out), out


# ---- registry and interface -------------------------------------------------------------------------------------------


def test_registry_discovers_the_mappers_and_skips_helpers():
    names = registry.names()
    assert {"shopify", "klaviyo"} <= set(names)
    assert registry.get("klaviyo_export") is klaviyo_export and registry.get("Shopify") is shopify
    for m in registry.all():
        assert isinstance(m.NAME, str) and isinstance(m.DESCRIPTION, str) and callable(m.detect) and callable(m.map)
    assert not any(n in names for n in ("report", "registry", "cli", "export_io", "webhooks"))
    with pytest.raises(KeyError):
        registry.get("no_such_source")


def test_detect_finds_each_export_and_nothing_else(tmp_path):
    assert {m.NAME for m in registry.detect(EXPORTS)} >= {"shopify", "klaviyo"}
    assert shopify.detect(EXPORTS / "shopify") and not shopify.detect(EXPORTS / "klaviyo")
    assert klaviyo_export.detect(EXPORTS / "klaviyo") and not klaviyo_export.detect(EXPORTS / "shopify")
    write_csv(tmp_path / "notes.csv", ["a", "b"], [["1", "2"]])
    assert not shopify.detect(tmp_path) and not klaviyo_export.detect(tmp_path)


def test_mapping_report_is_the_shared_interface():
    r = MappingReport("x", tables={"users": 1}, assumptions=["a"])
    assert r.to_dict() == {"source": "x", "tables": {"users": 1}, "unmapped_columns": {}, "assumptions": ["a"], "warnings": []}


# ---- shared parsing rules ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("2026-03-15 14:22:10 -0500", "2026-03-15 19:22:10"),     # Shopify export style
    ("2026-03-15T14:22:10Z", "2026-03-15 14:22:10"),
    ("2026-03-15T14:22:10.250+02:00", "2026-03-15 12:22:10"),
    ("2026-03-15 14:22:10", "2026-03-15 14:22:10"),           # naive: read as UTC, counted
    ("1773584530", "2026-03-15 14:22:10"),                     # epoch seconds
    ("2026-03-15", "2026-03-15 00:00:00"),
])
def test_timestamps_become_utc(raw, expected):
    assert export_io.fmt_ts(export_io.parse_ts(raw)) == expected


def test_ambiguous_dates_are_refused_not_guessed():
    tp = export_io.TimeParser()
    assert tp("03/04/2026") is None and tp.failed == 1
    assert tp("2026-03-04 10:00:00") is not None and tp.naive == 1


def test_money_yes_and_columns():
    assert str(export_io.money("$1,234.5")) == "1234.50" and str(export_io.money("1.234,50")) == "1234.50"
    assert export_io.money("") is None and str(export_io.money("(12.00)")) == "-12.00"
    assert export_io.yes("Yes") is True and export_io.yes("no") is False and export_io.yes("") is None
    cols = export_io.Columns(["Lineitem compare at price", "Phone", "EMAIL", "Phone"])
    row = ["9.00", "", "A@B.TEST", "+15555550101"]
    assert cols.get(row, "Lineitem compare-at price") == "9.00"
    assert cols.get(row, "Phone") == "+15555550101"            # duplicate header: first non-blank copy
    assert cols.get(row, "email") == "A@B.TEST"
    assert cols.unmapped() == []
    assert export_io.email_user_id(" A@B.test ") == export_io.email_user_id("a@b.test")
    assert export_io.phone_e164("(555) 555-0101", "US") == "+15555550101"


def test_consent_rollup_newest_wins_and_ties_never_grant():
    recs = [{"user_id": "u", "channel": "email", "status": "granted", "occurred_at": "2026-01-01 00:00:00"},
            {"user_id": "u", "channel": "email", "status": "revoked", "occurred_at": "2026-02-01 00:00:00"},
            {"user_id": "u", "channel": "email", "status": "granted", "occurred_at": "2026-03-01 00:00:00"},
            {"user_id": "u", "channel": "email", "status": "granted", "occurred_at": "2026-04-01 00:00:00"},
            {"user_id": "v", "channel": "sms", "status": "granted", "occurred_at": "2026-05-01 00:00:00"},
            {"user_id": "v", "channel": "sms", "status": "revoked", "occurred_at": "2026-05-01 00:00:00"}]
    state = export_io.consent_rollup(recs)
    assert state[("u", "email")] == "2026-03-01 00:00:00" and state[("v", "sms")] == ""


# ---- Shopify ------------------------------------------------------------------------------------------------------


def test_shopify_mapper_on_the_sample_export(shopify_out):
    report, out = shopify_out
    assert set(report.tables) == {"users", "orders", "line_items", "products", "discounts", "refunds", "consent_events", "track_events"}
    assert report.tables["users"] == 2000
    orders = {r["order_id"]: r for r in rows(out / "orders.csv")}
    lines = rows(out / "line_items.csv")
    assert len(orders) == report.tables["orders"] and len(lines) >= len(orders)
    # every order in the export is one order, whatever its number of line rows
    export_rows = rows(EXPORTS / "shopify/orders_export_1.csv")
    assert len(orders) == len({r["Name"] for r in export_rows})
    # times converted from the store offset to UTC; money split into gross, discount, net
    first = next(r for r in export_rows if r["Discount Code"] and r["Refunded Amount"] == "0.00")
    o = orders[first["Id"]]
    assert o["order_at"] == export_io.fmt_ts(export_io.parse_ts(first["Created at"]))
    assert o["order_at"] != first["Created at"][:19]
    assert float(o["gross_amount"]) == pytest.approx(float(first["Subtotal"]) + float(first["Discount Amount"]))
    assert float(o["net_amount"]) == pytest.approx(float(first["Subtotal"]))
    assert o["status"] == "completed"
    refunded = next(r for r in export_rows if r["Financial Status"] == "refunded")
    assert orders[refunded["Id"]]["status"] in ("refunded", "canceled") and float(orders[refunded["Id"]]["net_amount"]) == 0
    assert {r["order_id"] for r in rows(out / "refunds.csv")} <= set(orders)
    assert {r["product_id"] for r in lines} == {r["product_id"] for r in rows(out / "products.csv")}
    events = rows(out / "track_events.csv")
    assert {e["event_name"] for e in events} == {"order_completed", "checkout_started"}
    props = json.loads(next(e for e in events if e["event_name"] == "order_completed")["properties"])
    assert {"order_id", "net_amount", "currency", "items", "is_first_order"} <= set(props)
    sources = {r["source"] for r in rows(out / "consent_events.csv")}
    assert {"checkout", "import_undated"} <= sources
    text = " ".join(report.assumptions)
    assert "ASSUMED" in text and "refund" in text and "Subtotal read as after discounts" in text
    assert any("SMS" in w for w in report.warnings)
    assert "shopify" not in " ".join(report.unmapped_columns) or all(not k.startswith("/") for k in report.unmapped_columns)
    assert "Billing Address1" in report.unmapped_columns["orders_export_1.csv"]


def test_shopify_mapper_is_tolerant_of_header_variants(tmp_path):
    src = tmp_path / "in"
    write_csv(src / "export.csv", ["customer id", "EMAIL", "accepts email marketing", "Default Address Country Code", "Phone",
                                   "Accepts SMS Marketing"],
              [["'7001", "Shopper@Example.test", "yes", "ca", "(555) 555-0102", "yes"],
               ["'7002", "other@example.test", "no", "US", "", "no"]])
    write_csv(src / "orders.csv", ["Name", "Email", "Financial Status", "Accepts Marketing", "Currency", "Subtotal", "Shipping",
                                   "Taxes", "Total", "Discount Code", "Discount Amount", "Created at", "Lineitem quantity",
                                   "Lineitem name", "Lineitem price", "Lineitem sku", "Cancelled at", "Refunded Amount"],
              [["#1", "shopper@example.test", "paid", "yes", "cad", "18.00", "0", "0", "18.00", "TEN", "2.00",
                "2026-05-01 10:00:00 +0100", "1", "Beans", "20.00", "B-1", "", "0"],
               ["#1", "", "", "", "", "", "", "", "", "", "", "", "2", "Filters", "0.00", "F-1", "", ""],
               ["#2", "other@example.test", "voided", "no", "USD", "5.00", "0", "0", "5.00", "", "0",
                "2026-05-02 10:00:00 -0700", "1", "Filters", "5.00", "F-1", "2026-05-02 12:00:00 -0700", "5.00"]])
    out = tmp_path / "out"
    report = shopify.map(src, out)
    users = {u["email"]: u for u in rows(out / "users.csv")}
    assert users["shopper@example.test"]["user_id"] == "7001" and users["shopper@example.test"]["country"] == "CA"
    assert users["shopper@example.test"]["phone"] == "+15555550102"
    orders = {o["order_id"]: o for o in rows(out / "orders.csv")}
    assert orders["#1"]["order_at"] == "2026-05-01 09:00:00" and orders["#1"]["currency"] == "CAD"
    assert orders["#2"]["status"] == "canceled"
    assert [li["quantity"] for li in rows(out / "line_items.csv") if li["order_id"] == "#1"] == ["1", "2"]
    refund = rows(out / "refunds.csv")[0]
    assert refund["refunded_at"] == "2026-05-02 19:00:00"       # dated at Canceled at
    assert users["shopper@example.test"]["email_consent_at"] == "2026-05-01 09:00:00"
    assert users["shopper@example.test"].get("sms_consent_at", "") == ""   # an undated SMS yes is not consent
    assert not any("no Customer ID column" in a for a in report.assumptions)
    assert any("SMS" in w for w in report.warnings)


# ---- Klaviyo ------------------------------------------------------------------------------------------------------


def test_klaviyo_mapper_on_the_sample_export(klaviyo_out):
    report, out = klaviyo_out
    assert {"users", "consent_events", "track_events", "message_events"} <= set(report.tables)
    events = rows(out / "track_events.csv")
    assert {e["event_name"] for e in events} == {"order_completed", "checkout_started", "product_viewed", "product_added",
                                                 "session_started"}
    msgs = rows(out / "message_events.csv")
    names = {m["event_name"] for m in msgs}
    assert {"delivered", "opened", "clicked", "bounced", "unsubscribed"} <= names
    opens = [m for m in msgs if m["event_name"] == "opened"]
    assert {m["machine_open"] for m in opens} == {"true", "false"}
    assert all(m["machine_open"] == "" for m in msgs if m["event_name"] != "opened")
    assert all(m["recipient_domain"] == "example.test" for m in msgs)
    viewed = json.loads(next(e for e in events if e["event_name"] == "product_viewed")["properties"])
    assert viewed["product_id"].startswith("NF-")                # Klaviyo ProductID normalized to the SKU
    consent = rows(out / "consent_events.csv")
    assert {"esp_unsubscribe", "signup_form", "import"} <= {c["source"] for c in consent}
    assert any("Fulfilled Order" in a for a in report.assumptions)
    users = rows(out / "users.csv")
    assert all(u["user_id"].startswith("em_") for u in users)
    assert sum(1 for u in users if u["timezone"]) > 1500 and sum(1 for u in users if u["sms_consent_at"]) > 100


def test_klaviyo_reads_api_json_with_metric_names(tmp_path):
    src = tmp_path / "in"
    write_csv(src / "profiles.csv", ["Email", "Profile ID", "Email Marketing Consent", "Email Marketing Consent Timestamp", "Country"],
              [["a@example.test", "P1", "Subscribed", "2026-04-01T10:00:00Z", "Canada"],
               ["b@example.test", "P2", "Unsubscribed", "", "United Kingdom"]])
    doc = {"data": [
        {"type": "event", "id": "E1", "attributes": {"datetime": "2026-04-02T10:00:00+00:00", "event_properties":
                                                     {"Apple Privacy Open": "True", "Campaign Name": "Spring", "$message": "m1"}},
         "relationships": {"metric": {"data": {"type": "metric", "id": "M1"}}, "profile": {"data": {"type": "profile", "id": "P1"}}}},
        {"type": "event", "id": "E2", "attributes": {"datetime": "2026-04-03T10:00:00+00:00", "event_properties": {"$value": 30}},
         "relationships": {"metric": {"data": {"type": "metric", "id": "M2"}}, "profile": {"data": {"type": "profile", "id": "P9"}}}}],
        "included": [{"type": "metric", "id": "M1", "attributes": {"name": "Opened Email"}},
                     {"type": "metric", "id": "M2", "attributes": {"name": "Placed Order"}},
                     {"type": "profile", "id": "P9", "attributes": {"email": "c@example.test"}}]}
    (src / "events.json").write_text(json.dumps(doc))
    out = tmp_path / "out"
    report = klaviyo_export.map(src, out)
    msgs = rows(out / "message_events.csv")
    assert msgs[0]["machine_open"] == "true" and msgs[0]["campaign"] == "Spring"
    order = rows(out / "track_events.csv")[0]
    assert order["event_name"] == "order_completed" and order["user_id"] == export_io.email_user_id("c@example.test")
    users = {u["email"]: u for u in rows(out / "users.csv")}
    assert users["a@example.test"]["country"] == "CA" and users["a@example.test"]["email_consent_at"] == "2026-04-01 10:00:00"
    assert users["b@example.test"]["country"] == "GB" and users["b@example.test"]["email_consent_at"] == ""
    assert any("Unsubscribed or Suppressed with no timestamp" in a for a in report.assumptions)


def test_klaviyo_events_without_metric_names_are_skipped_not_guessed(tmp_path):
    src = tmp_path / "in"
    write_csv(src / "profiles.csv", ["Email", "Klaviyo ID", "Email Marketing Consent"], [["a@example.test", "P1", "NEVER_SUBSCRIBED"]])
    write_csv(src / "events.csv", ["id", "metric_id", "profile_id", "event_properties", "datetime", "uuid"],
              [["E1", "Mx", "P1", "{}", "2026-04-02T10:00:00+00:00", ""]])
    report = klaviyo_export.map(src, tmp_path / "out")
    assert "track_events" not in report.tables and "message_events" not in report.tables
    assert any("could not be named" in w for w in report.warnings)


# ---- merge and CLI --------------------------------------------------------------------------------------------------


def test_merge_matches_by_email_and_keeps_conservative_consent():
    a = {"users": [{"user_id": "7001", "email": "x@example.test", "created_at": "2026-01-01 00:00:00", "country": "US"}],
         "consent_events": [{"user_id": "7001", "channel": "email", "status": "granted", "purpose": "marketing",
                             "source": "import_undated", "occurred_at": "2026-01-01 00:00:00"},
                            {"user_id": "7001", "channel": "email", "status": "revoked", "purpose": "marketing",
                             "source": "import_undated", "occurred_at": "2026-09-30 00:00:00"}],
         "track_events": [{"event_id": "shopify-order-1", "user_id": "7001", "event_name": "order_completed",
                           "event_at": "2026-02-01 00:00:00", "properties": '{"order_id":"1"}'}]}
    b = {"users": [{"user_id": "em_x", "email": "x@example.test", "created_at": "2025-12-01 00:00:00", "timezone": "America/Chicago"}],
         "consent_events": [{"user_id": "em_x", "channel": "email", "status": "granted", "purpose": "marketing",
                             "source": "import", "occurred_at": "2026-03-01 00:00:00"}],
         "track_events": [{"event_id": "ev_9", "user_id": "em_x", "event_name": "order_completed",
                           "event_at": "2026-02-01 00:00:00", "properties": '{"order_id":"1"}'},
                          {"event_id": "ev_10", "user_id": "em_x", "event_name": "product_viewed",
                           "event_at": "2026-02-02 00:00:00", "properties": "{}"}]}
    tables, notes = cli.merge([("shopify", a), ("klaviyo", b)])
    (user,) = tables["users"]
    assert user["user_id"] == "7001" and user["timezone"] == "America/Chicago" and user["created_at"] == "2025-12-01 00:00:00"
    assert [e["event_id"] for e in tables["track_events"]] == ["shopify-order-1", "ev_10"]
    kinds = {(c["source"], c["status"]) for c in tables["consent_events"]}
    assert ("import_undated", "granted") not in kinds            # dropped: another source has dated records
    assert ("import_undated", "revoked") in kinds                # undated revokes always stay
    assert user["email_consent_at"] == ""                        # the late revoke outranks the March grant
    assert any("matched" in n for n in notes)


def test_merge_prefers_a_real_created_at_over_an_assumed_one():
    a = {"users": [{"user_id": "u1", "email": "x@example.test", "created_at": "2026-01-01 00:00:00"}]}
    b = {"users": [{"user_id": "u1", "email": "x@example.test", "created_at": "2026-06-01 00:00:00"}]}
    tables, _ = cli.merge([("shopify", a), ("klaviyo", b)], [{"users.created_at": ["u1"]}, {}])
    assert tables["users"][0]["created_at"] == "2026-06-01 00:00:00"


def test_cli_errors_name_the_problem(tmp_path, capsys):
    assert cli._cli(["nope", str(EXPORTS), "--out", str(tmp_path / "o")]) == 2
    assert "no source mapper named" in capsys.readouterr().err
    (tmp_path / "empty").mkdir()
    assert cli._cli(["auto", str(tmp_path / "empty"), "--out", str(tmp_path / "o")]) == 2
    assert "no export recognized" in capsys.readouterr().err
    assert cli._cli(["--list"]) == 0
    assert "shopify" in capsys.readouterr().out


def test_import_reproduces_the_committed_northfield_data_and_it_passes_check(tmp_path):
    out = tmp_path / "data"
    r = subprocess.run([sys.executable, "adapters/sources/cli.py", "auto", str(EXPORTS), "--out", str(out), "--as-of", "2026-10-19"],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-2000:] + r.stdout[-2000:]
    assert "# Import: shopify + klaviyo" in r.stdout and "**PASS**" in r.stdout and "## Merge" in r.stdout
    committed = sorted(p.name for p in (BRAND / "data").iterdir() if p.is_file())
    assert committed == sorted(p.name for p in out.iterdir() if p.is_file())
    for name in committed:
        assert filecmp.cmp(out / name, BRAND / "data" / name, shallow=False), name
    report = check.check_folder(out, AS_OF)
    assert report["ok"] and report["counts"]["warning"] == 0
    summary = json.loads((out / "import_report.json").read_text())
    assert [s["source"] for s in summary["sources"]] == ["shopify", "klaviyo"]
    assert any("ASSUMED" in a for s in summary["sources"] for a in s["assumptions"])


def test_northfield_generator_is_deterministic(tmp_path):
    r = subprocess.run([sys.executable, str(BRAND / "make_exports.py"), "--out", str(tmp_path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    made = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file())
    committed = sorted(p.relative_to(EXPORTS).as_posix() for p in EXPORTS.rglob("*") if p.is_file())
    assert made == committed
    for rel in made:
        a, b = tmp_path / rel, EXPORTS / rel
        if rel.endswith(".gz"):      # compressors differ between platforms: compare what is inside
            assert gzip.decompress(a.read_bytes()) == gzip.decompress(b.read_bytes()), rel
        else:
            assert a.read_bytes() == b.read_bytes(), rel


def test_northfield_behaves_like_a_coffee_store():
    from engine.data.load import connect
    con, _ = connect(str(BRAND / "data"))
    q = lambda sql: con.execute(sql).fetchone()  # noqa: E731
    started, done_30m, done_72h = q("""
        WITH c AS (SELECT user_id, event_at FROM track_events WHERE event_name = 'checkout_started'),
             o AS (SELECT user_id, event_at FROM track_events WHERE event_name = 'order_completed')
        SELECT count(*),
               avg(CASE WHEN EXISTS (SELECT 1 FROM o WHERE o.user_id = c.user_id AND o.event_at BETWEEN c.event_at AND c.event_at + INTERVAL 30 MINUTE) THEN 1.0 ELSE 0 END),
               avg(CASE WHEN EXISTS (SELECT 1 FROM o WHERE o.user_id = c.user_id AND o.event_at BETWEEN c.event_at AND c.event_at + INTERVAL 72 HOUR) THEN 1.0 ELSE 0 END)
        FROM c""")
    abandoned = 1 - float(done_30m)
    assert 0.65 <= abandoned <= 0.75
    assert 0.08 <= (float(done_72h) - float(done_30m)) / abandoned <= 0.15
    (gap,) = q("""
        WITH x AS (SELECT o.user_id, o.order_at, sum(li.quantity) AS bags FROM orders o JOIN line_items li USING (order_id)
                   WHERE li.product_id LIKE '%-12' AND o.status = 'completed' GROUP BY 1, 2),
             y AS (SELECT user_id, bags, order_at, lag(order_at) OVER (PARTITION BY user_id ORDER BY order_at) AS prev,
                          lag(bags) OVER (PARTITION BY user_id ORDER BY order_at) AS prev_bags, count(*) OVER (PARTITION BY user_id) AS n FROM x)
        SELECT median(date_diff('day', prev, order_at) / prev_bags) FROM y WHERE prev IS NOT NULL AND n >= 4""")
    assert 18 <= gap <= 35                                           # a bag lasts three to four weeks for regulars
    (hunters,) = q("""SELECT count(*) FROM (SELECT user_id FROM orders GROUP BY 1
                      HAVING count(*) >= 3 AND avg(CASE WHEN discount_amount > 0 THEN 1.0 ELSE 0 END) >= 0.8)""")
    assert hunters >= 10
    (machine,) = q("SELECT avg(CASE WHEN machine_open THEN 1.0 ELSE 0 END) FROM message_events WHERE event_name = 'opened'")
    assert 0.3 <= float(machine) <= 0.9
    countries = dict(con.execute("SELECT country, count(*) FROM users GROUP BY 1").fetchall())
    assert set(countries) == {"US", "CA", "GB"} and countries["US"] > 0.7 * sum(countries.values())


def test_northfield_folder_stays_small():
    def committed(p):          # runs/ holds the one finished run; anything else there is a local try-out (gitignored)
        parts = p.relative_to(BRAND).parts
        return "__pycache__" not in parts and not (parts[0] == "runs" and len(parts) > 1 and parts[1] not in ("cart_abandon", "README.md"))
    size = sum(p.stat().st_size for p in BRAND.rglob("*") if p.is_file() and committed(p))
    assert size < 8 * 1024 * 1024, size
