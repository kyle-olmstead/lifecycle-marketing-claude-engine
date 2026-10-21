"""
Discount dependency: who only buys on a discount, and how much of the program's revenue now rides on one.

Two questions a CFO asks before approving another offer:

  1. Which customers have learned to wait for a code? Per customer, the share of their last 3 completed orders
     that used a discount, and the flag discount_share_over_50pct_last_3_orders. The offer policy excludes
     flagged people from offers, because sending them a code mostly pays for an order that was coming anyway.
  2. Is the business drifting onto discounts? Per month, the share of net revenue from discounted orders, the
     share of orders discounted, and discount dollars over gross, with a linear trend over complete months.

An order counts as discounted when orders.discount_amount is above zero or the order appears in the optional
discounts table with a code (free-shipping codes included, whatever their amount). Only completed orders count,
as everywhere in the engine. A customer is flagged only with at least min_orders (default 2) completed orders,
so one welcome-code first purchase does not mark someone dependent for life; pass min_orders=1 to flag on any
order history. With fewer than 3 orders the share is over the orders they have.

The Python functions work on plain rows (dicts), so they run on any export; the SQL functions write the same
logic for a warehouse (postgres, snowflake, bigquery, duckdb), and tests/test_discounts.py checks that the two
agree on the demo data.

    python3 engine/measurement/discounts.py report --data examples/brands/ridgeview_saas/data
    python3 engine/measurement/discounts.py customers --data examples/brands/ridgeview_saas/data --flagged-only
    python3 engine/measurement/discounts.py sql --dialect snowflake --which customers

The trend is ordinary least squares of the monthly share on the month number, with a t test on the slope
(n - 2 degrees of freedom); with fewer than 4 complete months no trend is reported. Monthly shares are serially
correlated, so read a "rising" label as a prompt to look, not as proof.
"""
from __future__ import annotations

if __package__ in (None, ""):  # run as a file (python3 engine/measurement/discounts.py): make the repository importable
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse
import calendar
import json
import math
import sys
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from engine.measurement import stats
from engine.sql.dialects import DIALECTS

LAST_N = 3
THRESHOLD = 0.5
MIN_ORDERS = 2
MIN_MONTHS_FOR_TREND = 4
MIN_ORDERS_PER_MONTH = 20      # thinner months are shown but left out of the trend: their shares are mostly noise
_FLOAT = {"postgres": "DOUBLE PRECISION", "snowflake": "DOUBLE", "bigquery": "FLOAT64", "duckdb": "DOUBLE"}


def _num(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    return float(v)


def _ts(v: Any) -> str:
    """Timestamps as sortable ISO text, whether they arrive as strings or datetimes."""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.isoformat() + " 00:00:00"
    return str(v).replace("T", " ")


def _discount_orders(discounts: Optional[Iterable[Dict[str, Any]]]) -> Set[str]:
    return {str(d["order_id"]) for d in (discounts or []) if d.get("order_id") and str(d.get("code") or "").strip()}


def _completed(orders: Iterable[Dict[str, Any]], discounts: Optional[Iterable[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    coded = _discount_orders(discounts)
    out = []
    for o in orders:
        if str(o.get("status") or "").lower() != "completed":
            continue
        disc_amt = _num(o.get("discount_amount"))
        net = _num(o.get("net_amount"))
        gross = _num(o.get("gross_amount"))
        out.append({"order_id": str(o["order_id"]), "user_id": str(o["user_id"]), "order_at": _ts(o["order_at"]),
                    "discounted": (disc_amt or 0) > 0 or str(o["order_id"]) in coded,
                    "net": net or 0.0, "discount": disc_amt or 0.0,
                    "gross": gross if gross is not None else (net or 0.0) + (disc_amt or 0.0)})
    return out


# ----------------------------------------------------------------------------------------------
# per customer
# ----------------------------------------------------------------------------------------------

def customer_dependency(orders: Iterable[Dict[str, Any]], discounts: Optional[Iterable[Dict[str, Any]]] = None,
                        last_n: int = LAST_N, threshold: float = THRESHOLD, min_orders: int = MIN_ORDERS) -> List[Dict[str, Any]]:
    """Per customer: their last `last_n` completed orders (newest first, ties broken by order_id), how many used a
    discount, the share, and discount_share_over_50pct_last_3_orders (share above `threshold` with at least
    `min_orders` orders considered). A missing discount_amount counts as no discount unless the discounts table
    holds a code for the order (the SQL version does the same)."""
    by_user: Dict[str, List[Dict[str, Any]]] = {}
    for o in _completed(orders, discounts):
        by_user.setdefault(o["user_id"], []).append(o)
    out = []
    for uid in sorted(by_user):
        recent = sorted(by_user[uid], key=lambda o: (o["order_at"], o["order_id"]), reverse=True)[:last_n]
        k = sum(1 for o in recent if o["discounted"])
        share = k / len(recent)
        out.append({"user_id": uid, "orders_considered": len(recent), "discounted_orders": k,
                    "discount_share_last_3": share, "last_order_at": recent[0]["order_at"],
                    "discount_share_over_50pct_last_3_orders": bool(len(recent) >= min_orders and share > threshold)})
    return out


def flagged_users(orders: Iterable[Dict[str, Any]], discounts: Optional[Iterable[Dict[str, Any]]] = None,
                  min_orders: int = MIN_ORDERS) -> List[str]:
    """The user ids the offer policy should exclude from offers."""
    return [r["user_id"] for r in customer_dependency(orders, discounts, min_orders=min_orders)
            if r["discount_share_over_50pct_last_3_orders"]]


# ----------------------------------------------------------------------------------------------
# program level, by month
# ----------------------------------------------------------------------------------------------

def _month_end(month: str) -> date:
    y, m = int(month[:4]), int(month[5:7])
    return date(y, m, calendar.monthrange(y, m)[1])


def linear_trend(values: Sequence[float]) -> Dict[str, Any]:
    """OLS slope of values on 0, 1, 2, ... with its standard error, t and two-sided p (Student t, n - 2 df)."""
    n = len(values)
    if n < 3:
        return {"slope": None, "note": "fewer than 3 points"}
    xs = list(range(n))
    mx, my = sum(xs) / n, sum(values) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, values)) / sxx
    intercept = my - slope * mx
    resid = [y - (intercept + slope * x) for x, y in zip(xs, values)]
    s2 = sum(r * r for r in resid) / (n - 2)
    se = math.sqrt(s2 / sxx) if s2 > 0 else 0.0
    if se == 0:
        return {"slope": slope, "intercept": intercept, "se": 0.0, "t": None, "p_value": 0.0 if slope else 1.0, "df": n - 2}
    t = slope / se
    return {"slope": slope, "intercept": intercept, "se": se, "t": t, "p_value": 2 * stats.t_sf(abs(t), n - 2), "df": n - 2}


def summarize_months(rows: Iterable[Dict[str, Any]], as_of: Optional[str] = None, alpha: float = 0.05,
                     min_orders_per_month: int = MIN_ORDERS_PER_MONTH) -> Dict[str, Any]:
    """Shares and the trend from monthly totals (month YYYY-MM, orders, discounted_orders, revenue, discounted_revenue,
    discount_amount, gross). The month containing as_of is marked partial unless as_of is its last day; months with
    fewer than min_orders_per_month completed orders are marked thin. Partial and thin months are left out of the trend."""
    months = sorted(({**r, "month": str(r["month"])[:7]} for r in rows), key=lambda r: r["month"])
    if not months:
        return {"months": [], "trend": {"label": "no completed orders"}}
    last_day = date.fromisoformat(as_of[:10]) if as_of else None
    out_rows = []
    for r in months:
        rev, gross, orders = float(r["revenue"] or 0), float(r["gross"] or 0), int(r["orders"] or 0)
        partial = last_day is not None and r["month"] == last_day.isoformat()[:7] and last_day < _month_end(r["month"])
        out_rows.append({"month": r["month"], "orders": orders, "discounted_orders": int(r["discounted_orders"] or 0),
                         "revenue": round(rev, 2), "discounted_revenue": round(float(r["discounted_revenue"] or 0), 2),
                         "discount_amount": round(float(r["discount_amount"] or 0), 2),
                         "discounted_revenue_share": (float(r["discounted_revenue"] or 0) / rev) if rev > 0 else None,
                         "discounted_order_share": (int(r["discounted_orders"] or 0) / orders) if orders else None,
                         "discount_rate": (float(r["discount_amount"] or 0) / gross) if gross > 0 else None,
                         "partial": partial, "thin": orders < min_orders_per_month})
    complete = [r for r in out_rows if not r["partial"] and not r["thin"] and r["discounted_revenue_share"] is not None]
    trend: Dict[str, Any] = {"metric": "discounted_revenue_share", "months_used": len(complete),
                             "excluded": f"partial months and months with fewer than {min_orders_per_month} completed orders"}
    if len(complete) < MIN_MONTHS_FOR_TREND:
        trend["label"] = f"not enough complete months for a trend (have {len(complete)}, need {MIN_MONTHS_FOR_TREND})"
    else:
        fit = linear_trend([r["discounted_revenue_share"] for r in complete])
        trend.update({"slope_pts_per_month": None if fit["slope"] is None else fit["slope"] * 100, "p_value": fit.get("p_value"),
                      "first_month": complete[0]["month"], "last_month": complete[-1]["month"]})
        if fit.get("p_value") is not None and fit["p_value"] < alpha:
            trend["label"] = "rising" if fit["slope"] > 0 else "falling"
        else:
            trend["label"] = "no clear trend"
        trend["note"] = ("linear least squares over complete months; monthly shares are serially correlated, so treat the label as a "
                         "prompt to look at the offers that ran, not as proof")
    tot_rev = sum(r["revenue"] for r in out_rows)
    return {"months": out_rows, "trend": trend,
            "overall_discounted_revenue_share": (sum(r["discounted_revenue"] for r in out_rows) / tot_rev) if tot_rev else None}


def monthly_totals(orders: Iterable[Dict[str, Any]], discounts: Optional[Iterable[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Monthly totals over completed orders, the input summarize_months() takes."""
    acc: Dict[str, Dict[str, Any]] = {}
    for o in _completed(orders, discounts):
        m = o["order_at"][:7]
        a = acc.setdefault(m, {"month": m, "orders": 0, "discounted_orders": 0, "revenue": 0.0, "discounted_revenue": 0.0,
                               "discount_amount": 0.0, "gross": 0.0})
        a["orders"] += 1
        a["revenue"] += o["net"]
        a["discount_amount"] += o["discount"]
        a["gross"] += o["gross"]
        if o["discounted"]:
            a["discounted_orders"] += 1
            a["discounted_revenue"] += o["net"]
    return [acc[m] for m in sorted(acc)]


def monthly_discount_share(orders: Iterable[Dict[str, Any]], discounts: Optional[Iterable[Dict[str, Any]]] = None,
                           as_of: Optional[str] = None) -> Dict[str, Any]:
    """The program-level view: share of revenue on discount by month and its trend."""
    orders = list(orders)
    if as_of is None:
        done = [_ts(o["order_at"]) for o in orders if str(o.get("status") or "").lower() == "completed"]
        as_of = max(done)[:10] if done else None
    return summarize_months(monthly_totals(orders, discounts), as_of)


# ----------------------------------------------------------------------------------------------
# warehouse SQL
# ----------------------------------------------------------------------------------------------

def _month_sql(dialect_name: str, expr: str) -> str:
    if dialect_name == "bigquery":
        return f"DATE_TRUNC(DATE({expr}), MONTH)"
    return f"CAST(DATE_TRUNC('month', {expr}) AS DATE)"


def _discounted_sql(with_discounts_table: bool) -> str:
    return ("CASE WHEN COALESCE(o.discount_amount, 0) > 0" + (" OR d.order_id IS NOT NULL" if with_discounts_table else "")
            + " THEN 1 ELSE 0 END")


def _base_cte(with_discounts_table: bool) -> str:
    return ("discount_orders AS (SELECT DISTINCT order_id FROM discounts WHERE code IS NOT NULL AND TRIM(code) <> ''), "
            if with_discounts_table else "")


def customer_dependency_sql(dialect_name: str, last_n: int = LAST_N, threshold: float = THRESHOLD, min_orders: int = MIN_ORDERS,
                            with_discounts_table: bool = True) -> str:
    """Per customer, in a warehouse: orders considered, discounted orders, share, and the dependency flag (1/0)."""
    if dialect_name not in DIALECTS:
        raise ValueError(f"dialect must be one of {DIALECTS}")
    fl = _FLOAT[dialect_name]
    join = " LEFT JOIN discount_orders d ON d.order_id = o.order_id" if with_discounts_table else ""
    return ("WITH " + _base_cte(with_discounts_table)
            + f"completed AS (SELECT o.user_id, o.order_id, o.order_at, {_discounted_sql(with_discounts_table)} AS discounted, "
            "ROW_NUMBER() OVER (PARTITION BY o.user_id ORDER BY o.order_at DESC, o.order_id DESC) AS rn "
            f"FROM orders o{join} WHERE LOWER(o.status) = 'completed') "
            f"SELECT user_id, COUNT(*) AS orders_considered, SUM(discounted) AS discounted_orders, "
            f"CAST(SUM(discounted) AS {fl}) / COUNT(*) AS discount_share_last_3, "
            f"CASE WHEN COUNT(*) >= {int(min_orders)} AND CAST(SUM(discounted) AS {fl}) / COUNT(*) > {float(threshold)} THEN 1 ELSE 0 END "
            f"AS discount_share_over_50pct_last_3_orders "
            f"FROM completed WHERE rn <= {int(last_n)} GROUP BY user_id ORDER BY user_id")


def monthly_sql(dialect_name: str, with_discounts_table: bool = True) -> str:
    """Monthly totals over completed orders, in a warehouse; feed the rows to summarize_months()."""
    if dialect_name not in DIALECTS:
        raise ValueError(f"dialect must be one of {DIALECTS}")
    fl = _FLOAT[dialect_name]
    join = " LEFT JOIN discount_orders d ON d.order_id = o.order_id" if with_discounts_table else ""
    return ("WITH " + _base_cte(with_discounts_table)
            + f"completed AS (SELECT {_month_sql(dialect_name, 'o.order_at')} AS month, "
            f"CAST(COALESCE(o.net_amount, 0) AS {fl}) AS net, CAST(COALESCE(o.discount_amount, 0) AS {fl}) AS discount, "
            f"CAST(COALESCE(o.gross_amount, COALESCE(o.net_amount, 0) + COALESCE(o.discount_amount, 0)) AS {fl}) AS gross, "
            f"{_discounted_sql(with_discounts_table)} AS discounted "
            f"FROM orders o{join} WHERE LOWER(o.status) = 'completed') "
            "SELECT month, COUNT(*) AS orders, SUM(discounted) AS discounted_orders, SUM(net) AS revenue, "
            "SUM(CASE WHEN discounted = 1 THEN net ELSE 0 END) AS discounted_revenue, SUM(discount) AS discount_amount, "
            "SUM(gross) AS gross FROM completed GROUP BY month ORDER BY month")


# ----------------------------------------------------------------------------------------------
# DuckDB over the canonical tables
# ----------------------------------------------------------------------------------------------

def _rows(con: Any, sql: str) -> List[Dict[str, Any]]:
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def report_duckdb(con: Any, min_orders: int = MIN_ORDERS, as_of: Optional[str] = None) -> Dict[str, Any]:
    """Both views from a DuckDB connection over the canonical tables (engine/data/load.py connect)."""
    orders = _rows(con, "SELECT order_id, user_id, order_at, status, gross_amount, discount_amount, net_amount FROM orders")
    discounts = _rows(con, "SELECT order_id, code, discount_type, amount FROM discounts")
    if not orders:
        return {"note": "no orders table rows: discount dependency needs orders (discount_amount) or the discounts table"}
    has_info = any(o.get("discount_amount") is not None for o in orders) or bool(discounts)
    customers = customer_dependency(orders, discounts, min_orders=min_orders)
    flagged = [c for c in customers if c["discount_share_over_50pct_last_3_orders"]]
    eligible = [c for c in customers if c["orders_considered"] >= min_orders]
    out = {"customers": len(customers), "customers_with_enough_orders": len(eligible), "flagged": len(flagged),
           "flagged_share_of_eligible": (len(flagged) / len(eligible)) if eligible else None,
           "rule": (f"discount on more than {THRESHOLD:.0%} of the last {LAST_N} completed orders, with at least {min_orders} orders; "
                    "a discount is discount_amount > 0 or a code in the discounts table"),
           "program": monthly_discount_share(orders, discounts, as_of), "flagged_user_ids": [c["user_id"] for c in flagged]}
    if not has_info:
        out["note"] = "orders carry no discount_amount and there is no discounts table: no one can be flagged"
    return out


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Discount dependency per customer and across the program")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("report", help="counts, the monthly share and its trend")
    cu = sub.add_parser("customers", help="per-customer shares and flags")
    for p in (rp, cu):
        p.add_argument("--data", required=True)
        p.add_argument("--min-orders", type=int, default=MIN_ORDERS)
    rp.add_argument("--as-of", help="YYYY-MM-DD; the month containing it is partial unless it is the month's last day")
    cu.add_argument("--flagged-only", action="store_true")
    sq = sub.add_parser("sql", help="the warehouse SQL")
    sq.add_argument("--dialect", choices=DIALECTS, default="postgres")
    sq.add_argument("--which", choices=("customers", "monthly"), default="customers")
    sq.add_argument("--no-discounts-table", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "sql":
            body = (customer_dependency_sql(a.dialect, with_discounts_table=not a.no_discounts_table) if a.which == "customers"
                    else monthly_sql(a.dialect, with_discounts_table=not a.no_discounts_table))
            print(f"-- dialect: {a.dialect}\n{body}")
            return 0
        from engine.data.load import connect
        con, _notes = connect(a.data)
        if a.cmd == "report":
            r = report_duckdb(con, a.min_orders, a.as_of)
            r.pop("flagged_user_ids", None)
            print(json.dumps(r, indent=2))
        else:
            orders = _rows(con, "SELECT order_id, user_id, order_at, status, gross_amount, discount_amount, net_amount FROM orders")
            discounts = _rows(con, "SELECT order_id, code, discount_type, amount FROM discounts")
            rows = customer_dependency(orders, discounts, min_orders=a.min_orders)
            if a.flagged_only:
                rows = [r for r in rows if r["discount_share_over_50pct_last_3_orders"]]
            print(json.dumps(rows, indent=2, default=str))
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
