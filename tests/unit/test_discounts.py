"""Discount dependency: per-customer flags on hand-checked synthetic rows, the monthly share and its trend, and the
warehouse SQL agreeing with the Python on the demo data."""
import statistics
import subprocess
import sys

import pytest

from tests.conftest import ROOT
from engine.measurement import discounts as D
from engine.sql import check as sql_check
from engine.sql.dialects import DIALECTS

DEMO = ROOT / "examples/brands/ridgeview_saas/data"


def o(oid, uid, at, disc, net=100.0, status="completed"):
    return {"order_id": oid, "user_id": uid, "order_at": at, "status": status, "discount_amount": disc, "net_amount": net,
            "gross_amount": net + (disc or 0)}


ROWS = [
    # a: 2 of the last 3 discounted -> flagged
    o("a1", "a", "2026-01-01 10:00:00", 10), o("a2", "a", "2026-02-01 10:00:00", 0), o("a3", "a", "2026-03-01 10:00:00", 5),
    # b: 1 of 3 -> not flagged; its oldest (4th) discounted order is outside the last 3
    o("b0", "b", "2025-12-01 10:00:00", 9), o("b1", "b", "2026-01-01 10:00:00", 0), o("b2", "b", "2026-02-01 10:00:00", 0),
    o("b3", "b", "2026-03-01 10:00:00", 4),
    # c: one discounted order -> not flagged at min_orders 2, flagged at 1
    o("c1", "c", "2026-03-05 10:00:00", 15),
    # d: refunds and cancellations do not count; a coded order in the discounts table counts even with discount_amount 0
    o("d1", "d", "2026-01-10 10:00:00", 20, status="refunded"), o("d2", "d", "2026-02-10 10:00:00", 0),
    o("d3", "d", "2026-03-10 10:00:00", None),
    # e: two orders at the same instant, ties broken by order_id
    o("e1", "e", "2026-03-01 10:00:00", 0), o("e2", "e", "2026-03-01 10:00:00", 3), o("e3", "e", "2026-02-01 10:00:00", 3),
    o("e0", "e", "2026-01-01 10:00:00", 0),
]
CODES = [{"order_id": "d3", "code": "FREESHIP", "discount_type": "free_shipping", "amount": 0}, {"order_id": "d2", "code": "  "}]


def test_customer_flags_on_hand_checked_rows():
    got = {r["user_id"]: r for r in D.customer_dependency(ROWS, CODES)}
    assert (got["a"]["discounted_orders"], got["a"]["orders_considered"], got["a"]["discount_share_over_50pct_last_3_orders"]) == (2, 3, True)
    assert (got["b"]["discounted_orders"], got["b"]["discount_share_over_50pct_last_3_orders"]) == (1, False)
    assert got["c"]["discount_share_last_3"] == 1.0 and not got["c"]["discount_share_over_50pct_last_3_orders"]
    assert (got["d"]["orders_considered"], got["d"]["discounted_orders"], got["d"]["discount_share_over_50pct_last_3_orders"]) == (2, 1, False)
    assert (got["e"]["discounted_orders"], got["e"]["discount_share_over_50pct_last_3_orders"]) == (2, True)   # e2, e1, e3
    assert D.flagged_users(ROWS, CODES) == ["a", "e"]
    assert D.flagged_users(ROWS, CODES, min_orders=1) == ["a", "c", "e"]
    no_codes = {r["user_id"]: r for r in D.customer_dependency(ROWS)}
    assert no_codes["d"]["discounted_orders"] == 0


def test_linear_trend_matches_the_standard_library():
    ys = [0.10, 0.13, 0.12, 0.16, 0.19, 0.18, 0.22]
    fit = D.linear_trend(ys)
    ref = statistics.linear_regression(list(range(len(ys))), ys)
    assert fit["slope"] == pytest.approx(ref.slope) and fit["intercept"] == pytest.approx(ref.intercept)
    assert fit["p_value"] < 0.01 and fit["df"] == 5


def _monthly(shares, orders=40, revenue=10000.0):
    return [{"month": f"2026-{i + 1:02d}", "orders": orders, "discounted_orders": round(orders * s), "revenue": revenue,
             "discounted_revenue": revenue * s, "discount_amount": revenue * s * 0.15, "gross": revenue * (1 + s * 0.15)}
            for i, s in enumerate(shares)]


def test_monthly_share_trend_labels():
    rising = D.summarize_months(_monthly([0.10, 0.12, 0.14, 0.16, 0.18, 0.20]), as_of="2026-06-30")
    assert rising["trend"]["label"] == "rising" and rising["trend"]["slope_pts_per_month"] == pytest.approx(2.0)
    flat = D.summarize_months(_monthly([0.15, 0.17, 0.14, 0.16, 0.15, 0.16]), as_of="2026-06-30")
    assert flat["trend"]["label"] == "no clear trend"
    short = D.summarize_months(_monthly([0.1, 0.2, 0.3]), as_of="2026-03-31")
    assert short["trend"]["label"].startswith("not enough complete months")
    partial = D.summarize_months(_monthly([0.10, 0.12, 0.14, 0.16, 0.40]), as_of="2026-05-14")
    assert partial["months"][-1]["partial"] and partial["trend"]["months_used"] == 4
    thin = D.summarize_months(_monthly([0.10, 0.12, 0.14, 0.16]) + [dict(_monthly([0.9], orders=3)[0], month="2026-05")], as_of="2026-05-31")
    assert thin["months"][-1]["thin"] and thin["trend"]["months_used"] == 4


def test_monthly_totals_from_rows():
    m = {r["month"]: r for r in D.monthly_totals(ROWS, CODES)}
    assert m["2026-03"]["orders"] == 6 and m["2026-03"]["discounted_orders"] == 5   # a3, b3, c1, d3 (code), e2
    assert m["2026-01"]["orders"] == 3    # a1, b1, e0; the refunded d1 is out


def test_sql_matches_python_on_the_demo_data():
    from engine.data.load import connect
    con, _ = connect(str(DEMO))
    orders = D._rows(con, "SELECT * FROM orders")
    py = D.customer_dependency(orders, [])
    sql = D._rows(con, D.customer_dependency_sql("duckdb"))
    assert len(py) == len(sql) > 200
    for a, b in zip(sql, py):
        assert (a["user_id"], a["orders_considered"], a["discounted_orders"]) == (b["user_id"], b["orders_considered"], b["discounted_orders"])
        assert bool(a["discount_share_over_50pct_last_3_orders"]) == b["discount_share_over_50pct_last_3_orders"]
    flagged = sum(b["discount_share_over_50pct_last_3_orders"] for b in py)
    assert 0 < flagged < len(py)
    months_sql = D._rows(con, D.monthly_sql("duckdb"))
    months_py = D.monthly_totals(orders, [])
    assert [str(r["month"])[:7] for r in months_sql] == [r["month"] for r in months_py]
    for a, b in zip(months_sql, months_py):
        assert a["orders"] == b["orders"] and a["revenue"] == pytest.approx(b["revenue"]) and a["discounted_revenue"] == pytest.approx(b["discounted_revenue"])


def test_postgres_sql_runs_on_the_fixtures_and_every_dialect_parses():
    con = sql_check.connect()
    cols, rows = sql_check.run(D.customer_dependency_sql("postgres"), con)
    assert "discount_share_over_50pct_last_3_orders" in cols and rows
    _cols, months = sql_check.run(D.monthly_sql("postgres"), con)
    assert len(months) >= 12
    for d in DIALECTS:
        for with_table in (True, False):
            for q in (D.customer_dependency_sql(d, with_discounts_table=with_table), D.monthly_sql(d, with_discounts_table=with_table)):
                ok, msg = sql_check.parse(q, d)
                assert ok, (d, msg)


def test_demo_report():
    from engine.data.load import connect
    con, _ = connect(str(DEMO))
    r = D.report_duckdb(con)
    assert r["flagged"] == len(r["flagged_user_ids"]) and r["customers_with_enough_orders"] <= r["customers"]
    assert r["program"]["months"][-1]["partial"] and r["program"]["trend"]["label"] in ("rising", "falling", "no clear trend")


def test_discounts_cli():
    for args in (["report", "--data", str(DEMO)], ["customers", "--data", str(DEMO), "--flagged-only"],
                 ["sql", "--dialect", "bigquery", "--which", "monthly"]):
        r = subprocess.run([sys.executable, "engine/measurement/discounts.py", *args], cwd=ROOT, capture_output=True, text=True)
        assert r.returncode == 0, (args, r.stderr[-400:])
