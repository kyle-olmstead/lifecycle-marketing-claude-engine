"""
SQL checks for modules/04: parse in a declared dialect, and execute PostgreSQL-dialect queries
end to end on DuckDB against the deterministic fixtures in tests/fixtures.

What a pass means, precisely:
  * parse   sqlglot (an open-source SQL parser) accepted the statement in the named dialect. It is
            a syntax check, not a compile against a live Snowflake or BigQuery account; a function
            the warehouse does not have can still parse.
  * run     the PostgreSQL-dialect statement executed on DuckDB over the canonical tables and
            returned rows. DuckDB implements the Postgres features these queries use; it is not
            Postgres, so a run is strong evidence, not proof, for a Postgres warehouse.

    python3 tools/sql_check.py modules/04_data_schemas_and_sql/queries/rolling_d30_retention.sql
    python3 tools/sql_check.py my_query.sql --dialect snowflake
    python3 tools/sql_check.py my_query.sql --run --show 10
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
DIALECT_ALIASES = {"postgresql": "postgres", "postgres": "postgres", "pg": "postgres", "snowflake": "snowflake",
                   "bigquery": "bigquery", "bq": "bigquery", "duckdb": "duckdb"}
# PostgreSQL JSON builders that DuckDB names differently; rewritten only for the fixture run
PG_TO_DUCKDB = {"jsonb_build_object": "json_object", "json_build_object": "json_object", "jsonb_agg": "json_group_array", "json_agg": "json_group_array"}
TABLE_DDL = {
    "users": "user_id VARCHAR, email VARCHAR, created_at TIMESTAMP, country VARCHAR, plan VARCHAR, lifecycle_stage VARCHAR, "
             "email_consent_at TIMESTAMP, sms_consent_at TIMESTAMP",
    "orders": "order_id VARCHAR, user_id VARCHAR, order_at TIMESTAMP, status VARCHAR, gross_amount DECIMAL(18,2), "
              "discount_amount DECIMAL(18,2), net_amount DECIMAL(18,2), currency VARCHAR",
    "track_events": "event_id VARCHAR, user_id VARCHAR, anonymous_id VARCHAR, event_name VARCHAR, event_at TIMESTAMP, properties JSON",
    # email engagement exported from the ESP (2.2): delivered, opened (machine_open marks privacy-proxy opens), clicked,
    # bounced (hard), complained, unsubscribed
    "message_events": "event_id VARCHAR, user_id VARCHAR, channel VARCHAR, event_name VARCHAR, event_at TIMESTAMP, message_id VARCHAR, "
                      "campaign VARCHAR, machine_open BOOLEAN",
    # the teammate's ledger of flow entries already pushed (2.1); empty unless a fixture file exists
    "lcm_flow_entries": "flow VARCHAR, user_id VARCHAR, anchor_date DATE, holdout_bucket INTEGER, in_holdout BOOLEAN, status VARCHAR, "
                        "queued_at TIMESTAMP, pushed_at TIMESTAMP",
}
OPTIONAL_TABLES = ("lcm_flow_entries", "message_events")


def declared_dialect(sql: str) -> Optional[str]:
    m = re.search(r"--\s*dialect:\s*([A-Za-z]+)", sql)
    return DIALECT_ALIASES.get(m.group(1).lower()) if m else None


def parse(sql: str, dialect: str) -> Tuple[bool, str]:
    import sqlglot
    from sqlglot.errors import ErrorLevel, ParseError
    try:
        stmts = [s for s in sqlglot.parse(sql, read=DIALECT_ALIASES.get(dialect, dialect), error_level=ErrorLevel.RAISE) if s is not None]
        if not stmts:
            return False, "no statement found"
        return True, f"{len(stmts)} statement(s)"
    except ParseError as e:
        return False, str(e).splitlines()[0]


def connect(fixtures: Path = FIXTURES):
    import duckdb
    con = duckdb.connect()
    # the reference SQL is PostgreSQL, where integer / integer truncates; DuckDB's default returns a float, which would
    # let an integer-division bug pass here and fail in the warehouse
    con.execute("SET integer_division = true")
    for table, cols in TABLE_DDL.items():
        path = fixtures / f"{table}.csv"
        con.execute(f"CREATE TABLE {table} ({cols})")
        if table in OPTIONAL_TABLES and not path.exists():
            continue
        con.execute(f"INSERT INTO {table} SELECT * FROM read_csv('{path.as_posix()}', header=true, all_varchar=true, nullstr='')")
    return con


def run(sql: str, con=None) -> Tuple[List[str], List[tuple]]:
    """Execute a PostgreSQL-dialect query on DuckDB. Falls back to a sqlglot transpile if DuckDB rejects the raw text."""
    con = con or connect()
    try:
        cur = con.execute(sql)
    except Exception:
        compat = sql
        for pg, duck in PG_TO_DUCKDB.items():
            compat = re.sub(r"\b" + pg + r"\s*\(", duck + "(", compat, flags=re.I)
        # PostgreSQL's hex-to-integer idiom ('x' || hex)::bit(n)::int, used by the teammate's holdout bucket
        compat = re.sub(r"\('x' \|\| (substr\(md5\(.+?\), 1, 7\))\)::bit\(28\)::int", r"CAST('0x' || \1 AS INTEGER)", compat, flags=re.I)
        try:
            cur = con.execute(compat)
        except Exception:
            import sqlglot
            cur = con.execute(sqlglot.transpile(compat, read="postgres", write="duckdb")[0])
    if not cur.description:  # a statement without a result set (DDL, INSERT)
        return [], []
    cols = [d[0] for d in cur.description]
    return cols, cur.fetchall()


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Parse SQL in a dialect; run PostgreSQL-dialect SQL on DuckDB fixtures")
    ap.add_argument("path")
    ap.add_argument("--dialect", help="defaults to the '-- dialect:' header")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--fixtures", default=str(FIXTURES))
    ap.add_argument("--show", type=int, default=5)
    a = ap.parse_args(argv)
    sql = Path(a.path).read_text(encoding="utf-8")
    dialect = DIALECT_ALIASES.get((a.dialect or "").lower()) or declared_dialect(sql)
    if not dialect:
        print("no dialect: add '-- dialect: postgresql|snowflake|bigquery' as the first line or pass --dialect")
        return 2
    ok, msg = parse(sql, dialect)
    print(f"parse ({dialect}): {'ok' if ok else 'FAILED'}: {msg}")
    if not ok:
        return 1
    if a.run:
        if dialect != "postgres":
            print("run: only PostgreSQL-dialect SQL executes on the DuckDB fixtures")
            return 2
        cols, rows = run(sql, connect(Path(a.fixtures)))
        print(f"run: ok: {len(rows)} rows; columns: {', '.join(cols)}")
        for r in rows[: a.show]:
            print("  ", r)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
