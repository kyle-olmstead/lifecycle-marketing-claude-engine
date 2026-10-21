"""
The global holdout: a small share of people held out of every lifecycle flow, so the whole program can be read.

Flow-level holdouts answer "did this flow add anything for the people who entered it?". They cannot answer the
CFO's question, "what does lifecycle marketing as a whole add?", because flows overlap: the people a win-back
holds out still get onboarding, nurture and post-purchase messages. A global holdout answers it. A separate salt
("lmm_global") and a share (default 5%) define a group that every flow's entry query excludes. Comparing all
lifecycle-eligible people outside the group with the people inside it, over the same period, reads total
lifecycle incrementality.

The bucket is the engine's standard assignment (engine/sql/dialects.py): the first 7 hex digits of
md5(id || ':' || salt), as an integer, modulo 100, identical in Python, Postgres, Snowflake, BigQuery and DuckDB.
Because the salt differs from every flow's salt, a flow's own holdout is drawn independently inside the 95%
that the global holdout leaves.

  global_bucket / in_global_holdout   Python assignment for one id
  bucket_sql / membership_predicate   the bucket and the "is held out" test in a warehouse dialect
  entry_filter                        the clause every flow's entry query ANDs in: bucket >= share x 100
  arms_sql                            per-arm totals (people, converters, revenue, revenue squared) for a warehouse
  program_readout                     program-level incrementality from those totals, labeled honestly
  program_readout_duckdb              the same straight from the canonical tables in DuckDB (user or account level)

    python3 engine/measurement/global_holdout.py filter --dialect snowflake
    python3 engine/measurement/global_holdout.py sql --dialect bigquery --start 2026-04-01 --end 2026-10-01
    python3 engine/measurement/global_holdout.py readout --data examples/brands/ridgeview_saas/data --start 2026-04-01 --end 2026-10-01

What the readout is, in one line: the effect of receiving the lifecycle program (every flow together) compared
with receiving none of it, for the eligible population, over the period. It is not the sum of the flow lifts and
it does not say which flow did the work. It is read with the same machinery and the same refusals as a flow
holdout (engine/measurement/stats.py incrementality): no lift below the readout floor, an SRM check against the
planned share, and a revenue interval only when per-person spread is known.
"""
from __future__ import annotations

if __package__ in (None, ""):  # run as a file (python3 engine/measurement/global_holdout.py): make the repository importable
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse
import json
import math
import sys
from typing import Any, Dict, List, Optional

from engine.measurement import stats
from engine.sql.dialects import DIALECTS, dialect, holdout_bucket

GLOBAL_SALT = "lmm_global"
DEFAULT_SHARE = 0.05
_FLOAT = {"postgres": "DOUBLE PRECISION", "snowflake": "DOUBLE", "bigquery": "FLOAT64", "duckdb": "DOUBLE"}


def _pct(share: float) -> int:
    """The share as whole buckets (0 to 99). The bucket has 100 values, so the share is a whole percent."""
    if not 0 < share <= 0.5:
        raise ValueError("the global holdout share is above 0 and at most 0.5 (5% is usual)")
    pct = round(share * 100, 9)
    if abs(pct - round(pct)) > 1e-9:
        raise ValueError("the bucket has 100 values: give the share as a whole percent (0.05, not 0.055)")
    return int(round(pct))


def _q(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def global_bucket(unit_id: str, salt: str = GLOBAL_SALT) -> int:
    """0 to 99, exactly engine.sql.dialects.holdout_bucket with the global salt."""
    return holdout_bucket(unit_id, salt)


def in_global_holdout(unit_id: str, share: float = DEFAULT_SHARE, salt: str = GLOBAL_SALT) -> bool:
    return global_bucket(unit_id, salt) < _pct(share)


def bucket_sql(dialect_name: str, id_column: str = "user_id", salt: str = GLOBAL_SALT) -> str:
    """The global bucket of id_column in a warehouse dialect (postgres, snowflake, bigquery, duckdb)."""
    return dialect(dialect_name).holdout_bucket(id_column, salt)


def membership_predicate(dialect_name: str, id_column: str = "user_id", share: float = DEFAULT_SHARE, salt: str = GLOBAL_SALT) -> str:
    """True for members of the global holdout."""
    return f"{bucket_sql(dialect_name, id_column, salt)} < {_pct(share)}"


def entry_filter(dialect_name: str, id_column: str = "user_id", share: float = DEFAULT_SHARE, salt: str = GLOBAL_SALT) -> str:
    """The clause every flow's entry query ANDs in, so nobody in the global holdout enters any flow.

    Use the same id_column as the program's unit: account_id when whole accounts are held out (then a user with no
    account_id is not covered by the global holdout, so decide and say how those people are handled).
    """
    return f"{bucket_sql(dialect_name, id_column, salt)} >= {_pct(share)}"


def membership_sql(dialect_name: str, share: float = DEFAULT_SHARE, id_column: str = "user_id", salt: str = GLOBAL_SALT) -> str:
    """Every user with their global bucket and membership, for the ESP sync (a suppression list for all flows)."""
    b = bucket_sql(dialect_name, f"u.{id_column}", salt)
    return (f"SELECT u.user_id, u.{id_column} AS assigned_id, {b} AS global_bucket, "
            f"CASE WHEN {b} < {_pct(share)} THEN 1 ELSE 0 END AS in_global_holdout FROM users u WHERE u.{id_column} IS NOT NULL")


def default_eligible_sql(end: str) -> str:
    """Lifecycle-eligible people: email consent on record and signed up before the period ended."""
    return f"SELECT u.user_id FROM users u WHERE u.email_consent_at IS NOT NULL AND u.created_at < CAST({_q(end)} AS TIMESTAMP)"


def arms_sql(dialect_name: str, start: str, end: str, share: float = DEFAULT_SHARE, salt: str = GLOBAL_SALT,
             eligible_sql: Optional[str] = None, unit: str = "user") -> str:
    """Totals per arm for a warehouse: people, converters (one or more completed orders in [start, end)), revenue
    (sum of net_amount) and revenue squared (for the per-person spread). With unit = "account" the query returns one
    row per account instead (arm, account_id, people, converters, revenue) for the clustered readout."""
    if dialect_name not in DIALECTS:
        raise ValueError(f"dialect must be one of {DIALECTS}")
    if unit not in ("user", "account"):
        raise ValueError("unit is user or account")
    pct = _pct(share)
    fl = _FLOAT[dialect_name]
    eligible = eligible_sql or default_eligible_sql(end)
    id_col = "u.user_id" if unit == "user" else "u.account_id"
    b = bucket_sql(dialect_name, id_col, salt)
    head = (f"WITH eligible AS ({eligible}), "
            f"outcome AS (SELECT o.user_id, COUNT(*) AS orders, SUM(CAST(o.net_amount AS {fl})) AS revenue FROM orders o "
            f"WHERE o.status = 'completed' AND o.order_at >= CAST({_q(start)} AS TIMESTAMP) AND o.order_at < CAST({_q(end)} AS TIMESTAMP) "
            f"GROUP BY o.user_id), "
            f"assigned AS (SELECT u.user_id, {id_col} AS assigned_id, "
            f"CASE WHEN {b} < {pct} THEN 'global_holdout' ELSE 'treatment' END AS arm, "
            f"CASE WHEN oc.user_id IS NULL THEN 0 ELSE 1 END AS converted, COALESCE(oc.revenue, 0) AS revenue "
            f"FROM users u JOIN eligible e ON e.user_id = u.user_id LEFT JOIN outcome oc ON oc.user_id = u.user_id "
            f"WHERE {id_col} IS NOT NULL) ")
    if unit == "user":
        return (head + "SELECT arm, COUNT(*) AS n, SUM(converted) AS conversions, SUM(revenue) AS revenue, "
                "SUM(revenue * revenue) AS revenue_sq, MAX(revenue) AS largest_person_revenue FROM assigned GROUP BY arm ORDER BY arm")
    return (head + "SELECT arm, assigned_id AS account_id, COUNT(*) AS n, SUM(converted) AS conversions, SUM(revenue) AS revenue "
            "FROM assigned GROUP BY arm, assigned_id ORDER BY arm, assigned_id")


def _sd(total: float, total_sq: float, n: int) -> Optional[float]:
    if n < 2:
        return None
    return math.sqrt(max(0.0, (total_sq - total * total / n) / (n - 1)))


def _labels(share: float, period: Optional[str]) -> Dict[str, Any]:
    return {
        "scope": (f"program-level: every lifecycle-eligible person outside the global holdout vs the {share:.0%} held out of every "
                  f"flow{', ' + period if period else ''}"),
        "caveats": [
            "this is the effect of the whole lifecycle program against none of it; it is not the sum of flow-level lifts and does not "
            "say which flow did the work",
            "people who entered flows before the global holdout went live were exposed: start the period after it went live",
            "sends that still reach the global holdout (newsletters, sales outreach, transactional mail) are common to both arms, so "
            "the read is the flows' effect on top of them",
        ],
    }


def program_readout(treatment: Dict[str, Any], holdout: Dict[str, Any], share: float = DEFAULT_SHARE, period: Optional[str] = None,
                    alpha: float = 0.05, window_days: Optional[int] = None) -> Dict[str, Any]:
    """Program-level incrementality from per-arm totals: n, conversions, revenue and optionally revenue_sq
    (sum of squared per-person revenue) and largest_person_revenue. Uses stats.incrementality, so it carries the
    same refusals (readout floor, SRM against the planned share) and the revenue interval only when the spread is known."""
    _pct(share)
    lab = _labels(share, period)

    def arm(a: Dict[str, Any]) -> Dict[str, Any]:
        out = {"n": int(a["n"]), "conversions": int(a["conversions"])}
        if a.get("revenue") is not None:
            out["revenue"] = float(a["revenue"])
            if a.get("revenue_sq") is not None:
                out["revenue_sd"] = _sd(float(a["revenue"]), float(a["revenue_sq"]), int(a["n"]))
        if a.get("largest_person_revenue"):
            out["largest_transaction"] = float(a["largest_person_revenue"])
        return out

    doc = {"flow": "lifecycle program (all flows) vs global holdout", "period": period, "design": "persistent_holdout",
           "planned_holdout_share": share, "treatment": arm(treatment), "holdout": arm(holdout), "alpha": alpha,
           "caveats": list(lab["caveats"])}
    if window_days is not None:
        doc["window_days"], doc["window_reason"] = window_days, "it is the readout period: every eligible person is followed for all of it"
    x = stats.incrementality(doc)
    x["scope"] = lab["scope"]
    x["unit"] = "user"
    x["revenue_per_eligible_person"] = {
        "treatment": doc["treatment"]["revenue"] / doc["treatment"]["n"] if doc["treatment"].get("revenue") is not None else None,
        "holdout": doc["holdout"]["revenue"] / doc["holdout"]["n"] if doc["holdout"].get("revenue") is not None else None}
    return x


def program_readout_clustered(rows: List[Dict[str, Any]], share: float = DEFAULT_SHARE, period: Optional[str] = None,
                              alpha: float = 0.05) -> Dict[str, Any]:
    """Program-level incrementality when whole accounts are held out: rows are per account (arm, n, conversions, revenue).
    Conversion is read with engine/measurement/cluster.py cluster_readout; revenue per person with its ratio_difference."""
    from engine.measurement import cluster
    t = [r for r in rows if r["arm"] == "treatment"]
    c = [r for r in rows if r["arm"] == "global_holdout"]
    conv = cluster.cluster_readout([(r["n"], r["conversions"]) for r in t], [(r["n"], r["conversions"]) for r in c], alpha,
                                   expected_holdout=share)
    lab = _labels(share, period)
    out: Dict[str, Any] = {"flow": "lifecycle program (all flows) vs global holdout", "period": period, "unit": "account",
                           "scope": lab["scope"], "conversion_readout": conv, "read": conv["read"], "caveats": lab["caveats"]}
    if conv["read"] != "not readable":
        rev = cluster.ratio_difference([(r["n"], r["revenue"]) for r in t], [(r["n"], r["revenue"]) for r in c], alpha)
        n_t = sum(r["n"] for r in t)
        out["revenue_per_eligible_person"] = {"treatment": rev["treatment"]["rate"], "holdout": rev["holdout"]["rate"],
                                              "diff": rev["diff"], "ci": rev["ci"]}
        out["incremental_revenue"] = rev["diff"] * n_t
        out["incremental_revenue_ci"] = None if rev["ci"] is None else [rev["ci"][0] * n_t, rev["ci"][1] * n_t]
    return out


def program_readout_duckdb(con: Any, start: str, end: str, share: float = DEFAULT_SHARE, salt: str = GLOBAL_SALT,
                           eligible_sql: Optional[str] = None, unit: str = "user", alpha: float = 0.05) -> Dict[str, Any]:
    """Program-level incrementality straight from the canonical tables in a DuckDB connection (engine/data/load.py)."""
    from datetime import date
    period = f"orders from {start} to {end} (end exclusive)"
    days = (date.fromisoformat(end[:10]) - date.fromisoformat(start[:10])).days
    rows = con.execute(arms_sql("duckdb", start, end, share, salt, eligible_sql, unit)).fetchall()
    if unit == "account":
        recs = [{"arm": a, "n": int(n), "conversions": int(x), "revenue": float(rv or 0)} for a, _acct, n, x, rv in rows]
        return program_readout_clustered(recs, share, period, alpha)
    by = {r[0]: {"n": r[1], "conversions": r[2], "revenue": float(r[3] or 0), "revenue_sq": float(r[4] or 0),
                 "largest_person_revenue": float(r[5] or 0)} for r in rows}
    empty = {"n": 0, "conversions": 0, "revenue": 0.0, "revenue_sq": 0.0}
    t, c = by.get("treatment", empty), by.get("global_holdout", empty)
    if not t["n"] or not c["n"]:
        return {"flow": "lifecycle program (all flows) vs global holdout", "period": period, "read": "not readable",
                "treatment_n": t["n"], "holdout_n": c["n"], "note": "one arm is empty: no eligible people in the treatment or the global holdout"}
    return program_readout(t, c, share, period, alpha, window_days=days)


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Program-wide global holdout: assignment SQL and the program-level readout")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("filter", help="the clause every flow's entry query ANDs in")
    s = sub.add_parser("sql", help="per-arm totals for a warehouse")
    mem = sub.add_parser("membership", help="every user's global bucket, for the ESP suppression sync")
    for p in (f, s, mem):
        p.add_argument("--dialect", choices=DIALECTS, default="postgres")
        p.add_argument("--share", type=float, default=DEFAULT_SHARE)
        p.add_argument("--salt", default=GLOBAL_SALT)
    for p in (f, mem):
        p.add_argument("--id-column", default="user_id", help="user_id, or account_id when whole accounts are held out")
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    s.add_argument("--unit", choices=("user", "account"), default="user")
    r = sub.add_parser("readout", help="program-level incrementality from a data folder")
    r.add_argument("--data", required=True)
    r.add_argument("--start", required=True)
    r.add_argument("--end", required=True)
    r.add_argument("--share", type=float, default=DEFAULT_SHARE)
    r.add_argument("--salt", default=GLOBAL_SALT)
    r.add_argument("--unit", choices=("user", "account"), default="user")
    r.add_argument("--markdown", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "filter":
            print(entry_filter(a.dialect, a.id_column, a.share, a.salt))
        elif a.cmd == "sql":
            print(f"-- dialect: {a.dialect}\n" + arms_sql(a.dialect, a.start, a.end, a.share, a.salt, unit=a.unit))
        elif a.cmd == "membership":
            print(f"-- dialect: {a.dialect}\n" + membership_sql(a.dialect, a.share, a.id_column, a.salt))
        elif a.cmd == "readout":
            from engine.data.load import connect
            con, _notes = connect(a.data, require=("users", "track_events", "orders"))
            x = program_readout_duckdb(con, a.start, a.end, a.share, a.salt, unit=a.unit)
            if a.markdown and x.get("unit") == "user" and "conversion_readout" in x:
                print(x["scope"])
                print(stats.render_incrementality(x))
            else:
                print(json.dumps(x, indent=2, default=str))
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
