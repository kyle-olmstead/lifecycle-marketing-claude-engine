import sqlglot

from tests.conftest import ROOT, fenced
from tools import sql_check

RFM = ROOT / "modules/04_data_schemas_and_sql/prompts/rfm_segmentation_builder.md"
LTV = ROOT / "modules/04_data_schemas_and_sql/prompts/ltv_cohort_query_generator.md"


def _first(path, dialect):
    blocks = fenced(path, "sql", dialect)
    return blocks[0]


def test_retention_query_runs_and_labels_small_cohorts(duck):
    sql = (ROOT / "modules/04_data_schemas_and_sql/queries/rolling_d30_retention.sql").read_text()
    cols, rows = sql_check.run(sql, duck)
    assert "d30_read" in cols and rows
    reads = {r[cols.index("d30_read")] for r in rows}
    assert reads <= {"readable", "too small to read", "immature"}


def test_rfm_postgres_assigns_every_customer_exactly_one_segment(duck):
    cols, rows = sql_check.run(_first(RFM, "postgres"), duck)
    seg = cols.index("rfm_segment")
    assert len(rows) == 238 and len({r[0] for r in rows}) == 238
    assert "unclassified" not in {r[seg] for r in rows}
    assert {r[cols.index("repeat_window_days")] for r in rows} == {58}
    for c in ("r_score", "f_score", "m_score"):
        assert {r[cols.index(c)] for r in rows} <= {1, 2, 3, 4, 5}


def _equivalent(path, dialect, duck, key_cols, val_cols):
    pg_cols, pg_rows = sql_check.run(_first(path, "postgres"), duck)
    src = _first(path, dialect)
    assert sql_check.parse(src, dialect)[0]
    # Snowflake and BigQuery divide integers to a decimal, so their transpiled SQL runs without PostgreSQL's integer division
    cur = sql_check.connect(integer_division=False).execute(sqlglot.transpile(src, read=dialect, write="duckdb")[0])
    cols = [d[0] for d in cur.description]
    other = cur.fetchall()

    def index(c, rows):
        return {tuple(str(r[c.index(k)])[:10] for k in key_cols): tuple(float(r[c.index(v)]) if isinstance(r[c.index(v)], (int, float)) or hasattr(r[c.index(v)], "as_integer_ratio") else r[c.index(v)] for v in val_cols) for r in rows}
    return index(pg_cols, pg_rows) == index(cols, other)


def test_rfm_snowflake_and_bigquery_reproduce_postgres(duck):
    for d in ("snowflake", "bigquery"):
        assert _equivalent(RFM, d, duck, ["user_id"], ["rfm_segment", "r_score", "f_score", "m_score"]), d


def test_ltv_snowflake_and_bigquery_reproduce_postgres(duck):
    for d in ("snowflake", "bigquery"):
        assert _equivalent(LTV, d, duck, ["cohort_quarter", "period_30d"], ["customers_observed", "cumulative_ltv_per_customer"]), d


def test_ltv_censoring_never_counts_unobserved_periods(duck):
    _cols, rows = sql_check.run(_first(LTV, "postgres"), duck)
    by_cohort = {}
    for r in rows:
        by_cohort.setdefault(r[0], []).append((r[1], r[2]))
    for points in by_cohort.values():
        observed = [n for _, n in sorted(points)]
        assert observed == sorted(observed, reverse=True)  # later periods never have more observed customers


def test_parse_rejects_wrong_dialect_syntax():
    assert not sql_check.parse("SELECT FROM WHERE", "postgres")[0]


def test_duckdb_divides_integers_like_each_warehouse(duck):
    assert duck.execute("SELECT 7 / 2").fetchone()[0] == 3                                           # PostgreSQL
    assert sql_check.connect(integer_division=False).execute("SELECT 7 / 2").fetchone()[0] == 3.5    # Snowflake, BigQuery


def test_ltv_repeat_share_is_not_trivially_one(duck):
    cols, rows = sql_check.run(_first(LTV, "postgres"), duck)
    share = [r[cols.index("repeat_share")] for r in rows]
    assert "share_with_revenue" not in cols
    assert min(share) < 1 and all(0 <= float(s) <= 1 for s in share)
    period0 = [float(r[cols.index("repeat_share")]) for r in rows if r[cols.index("period_30d")] == 0]
    assert period0 and all(s == 0 for s in period0)   # nobody has revenue after their first order in the first order's period
