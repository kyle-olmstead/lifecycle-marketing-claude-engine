"""lcm check-data (engine/data/check.py): a clean folder passes, and each planted defect is caught with its column named."""
import csv
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from engine.data import check
from tests.conftest import ROOT

AS_OF = datetime(2026, 10, 19, tzinfo=timezone.utc)
NORTHFIELD_DATA = ROOT / "examples/brands/northfield_dtc/data"


def _write(folder: Path, name: str, header, rows) -> None:
    with open(folder / f"{name}.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def clean(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    _write(folder, "users", ["user_id", "email", "created_at", "country", "timezone", "email_consent_at", "phone"], [
        ["u_0001", "u_0001@example.test", "2026-01-05 10:00:00", "US", "America/Chicago", "2026-01-05 10:00:00", "+15555550101"],
        ["u_0002", "u_0002@example.test", "2026-02-11T08:30:00Z", "GB", "Europe/London", "", ""],
        ["u_0003", "u_0003@example.test", "2026-03-01 12:00:00", "CA", "", "", ""],
    ])
    _write(folder, "track_events", ["event_id", "user_id", "anonymous_id", "event_name", "event_at", "properties"], [
        ["ev_1", "u_0001", "", "product_viewed", "2026-10-10 09:00:00", '{"product_id":"NF-HOUSE-12"}'],
        ["ev_2", "u_0001", "", "order_completed", "2026-10-10 09:05:00", '{"order_id":"o_1","net_amount":17.0,"currency":"USD"}'],
        ["ev_3", "", "anon_9", "session_started", "2026-10-18 20:00:00", ""],
        ["ev_4", "u_0002", "", "checkout_started", "2026-10-17 11:00:00", "{}"],
    ])
    _write(folder, "orders", ["order_id", "user_id", "order_at", "status", "gross_amount", "discount_amount", "net_amount", "currency"], [
        ["o_1", "u_0001", "2026-10-10 09:05:00", "completed", "17.00", "0.00", "17.00", "USD"],
        ["o_2", "u_0002", "2026-10-12 09:05:00", "refunded", "20.00", "2.00", "0.00", "USD"],
    ])
    _write(folder, "line_items", ["order_id", "line_id", "product_id", "quantity", "unit_price", "line_amount"], [
        ["o_1", "o_1-1", "NF-HOUSE-12", "1", "17.00", "17.00"],
        ["o_2", "o_2-1", "NF-DARK-12", "1", "20.00", "18.00"],
    ])
    _write(folder, "consent_events", ["user_id", "channel", "status", "purpose", "source", "occurred_at"], [
        ["u_0001", "email", "granted", "marketing", "signup_form", "2026-01-05 10:00:00"],
    ])
    return folder


def findings(report, check_name=None, where=None):
    return [f for f in report["findings"] if (check_name is None or f["check"] == check_name) and (where is None or f["where"] == where)]


def test_a_clean_folder_passes(tmp_path):
    report = check.check_folder(clean(tmp_path / "data"), AS_OF)
    assert report["ok"], report["findings"]
    assert report["counts"]["error"] == 0 and report["counts"]["warning"] == 0, report["findings"]
    assert {t["table"] for t in report["tables"]} == {"users", "track_events", "orders", "line_items", "consent_events"}
    assert findings(report, "freshness")[0]["severity"] == "info"


def _plant(folder: Path, table: str, column: str, row: int, value: str) -> None:
    path = folder / f"{table}.csv"
    rows = list(csv.reader(path.open(encoding="utf-8")))
    rows[row + 1][rows[0].index(column)] = value
    with path.open("w", encoding="utf-8", newline="") as fh:
        csv.writer(fh, lineterminator="\n").writerows(rows)


@pytest.mark.parametrize("table,column,row,value,check_name,severity", [
    ("track_events", "event_at", 1, "10/18/2026 10:00", "type", "error"),                 # bad timestamp
    ("users", "user_id", 1, "u_0001", "primary_key", "error"),                             # duplicate id
    ("track_events", "event_name", 0, "productViewed", "event_name_format", "warning"),    # camelCase event
    ("track_events", "user_id", 3, "u_9999", "reference", "error"),                        # orphan user_id
    ("users", "timezone", 0, "Mars/Olympus_Mons", "timezone", "warning"),                  # invalid timezone
    ("orders", "net_amount", 0, "$17.00", "type", "error"),                                # money with a symbol
    ("track_events", "properties", 0, "[1, 2]", "type", "error"),                          # JSON that is not an object
    ("users", "email_consent_at", 0, "2027-01-01 00:00:00", "consent_future", "error"),    # consent in the future
    ("users", "country", 0, "United States", "country", "warning"),
    ("users", "email", 0, "U_0001@Example.test", "email_case", "warning"),
    ("users", "created_at", 0, "2026-01-05 10:00:00+02:00", "utc_offset", "warning"),
    ("orders", "status", 0, "paid", "enum", "warning"),
    ("orders", "currency", 0, "usd", "currency", "warning"),
    ("track_events", "event_at", 2, "2026-12-01 00:00:00", "future_event", "warning"),
    ("line_items", "order_id", 1, "o_404", "reference", "error"),
])
def test_each_planted_defect_is_caught_with_its_column(tmp_path, table, column, row, value, check_name, severity):
    folder = clean(tmp_path / "data")
    _plant(folder, table, column, row, value)
    report = check.check_folder(folder, AS_OF)
    hits = findings(report, check_name, f"{table}.{column}")
    assert hits, (check_name, f"{table}.{column}", report["findings"])
    assert hits[0]["severity"] == severity
    assert hits[0]["rows"] >= 1 and hits[0]["fix"]
    assert report["ok"] is (severity != "error" and report["counts"]["error"] == 0)
    if check_name not in ("primary_key", "reference"):
        assert any(value.strip()[:20] in e for e in hits[0]["examples"]), hits[0]["examples"]


def test_a_missing_required_column_and_table_are_errors(tmp_path):
    folder = clean(tmp_path / "data")
    rows = list(csv.reader((folder / "orders.csv").open(encoding="utf-8")))
    idx = rows[0].index("status")
    _write(folder, "orders", [c for i, c in enumerate(rows[0]) if i != idx], [[c for i, c in enumerate(r) if i != idx] for r in rows[1:]])
    (folder / "track_events.csv").unlink()
    report = check.check_folder(folder, AS_OF)
    assert not report["ok"]
    assert findings(report, "missing_column", "orders.status")[0]["severity"] == "error"
    assert findings(report, "missing_table", "track_events")[0]["severity"] == "error"


def test_unknown_columns_are_notes_with_a_suggestion(tmp_path):
    folder = clean(tmp_path / "data")
    rows = list(csv.reader((folder / "users.csv").open(encoding="utf-8")))
    rows[0][rows[0].index("timezone")] = "time_zone"
    _write(folder, "users", rows[0], rows[1:])
    report = check.check_folder(folder, AS_OF)
    note = findings(report, "unknown_column", "users.time_zone")[0]
    assert note["severity"] == "info" and "timezone" in note["message"]


def test_stale_data_is_a_warning_not_an_error(tmp_path):
    report = check.check_folder(clean(tmp_path / "data"), datetime(2026, 12, 1, tzinfo=timezone.utc))
    assert report["ok"]
    assert findings(report, "freshness")[0]["severity"] == "warning"


def test_cli_exit_codes_and_formats(tmp_path):
    folder = clean(tmp_path / "data")
    r = subprocess.run([sys.executable, "engine/data/check.py", str(folder), "--as-of", "2026-10-19"], cwd=ROOT,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("# Data check") and "**PASS**" in r.stdout
    _plant(folder, "users", "user_id", 1, "u_0001")
    assert check._cli([str(folder), "--as-of", "2026-10-19"]) == 1
    r = subprocess.run([sys.executable, "-m", "engine.data.check", str(folder), "--json", "--as-of", "2026-10-19"], cwd=ROOT,
                       capture_output=True, text=True)
    assert r.returncode == 1
    doc = json.loads(r.stdout)
    assert doc["ok"] is False and findings(doc, "primary_key", "users.user_id")


def test_parquet_folders_are_checked_too(tmp_path):
    import duckdb
    folder = clean(tmp_path / "csv")
    pq = tmp_path / "pq"
    pq.mkdir()
    con = duckdb.connect()
    for name in ("users", "track_events", "orders"):
        con.execute(f"COPY (SELECT * FROM read_csv('{folder / (name + '.csv')}', all_varchar=true, header=true)) "
                    f"TO '{pq / (name + '.parquet')}' (FORMAT parquet)")
    report = check.check_folder(pq, AS_OF)
    assert report["ok"], report["findings"]
    assert {t["file"] for t in report["tables"]} == {"users.parquet", "track_events.parquet", "orders.parquet"}


def test_northfield_canonical_data_passes():
    report = check.check_folder(NORTHFIELD_DATA, AS_OF)
    assert report["ok"], [f for f in report["findings"] if f["severity"] == "error"]
    assert report["counts"]["warning"] == 0, [f for f in report["findings"] if f["severity"] == "warning"]
    rows = {t["table"]: t["rows"] for t in report["tables"]}
    assert rows["users"] == 2000 and rows["orders"] > 1000 and rows["message_events"] > 5000
