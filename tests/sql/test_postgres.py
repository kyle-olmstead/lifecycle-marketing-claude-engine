"""The PostgreSQL-dialect cohort queries, executed on a real PostgreSQL and compared with DuckDB and Python.

Skipped unless LCM_PG holds a libpq connection string and psql is installed. CI runs it against a
postgres:16 service container (.github/workflows/ci.yml, job postgres). Locally:

    LCM_PG="host=localhost port=5432 user=postgres dbname=postgres" pytest -q tests/sql/test_postgres.py
"""
import json
import os
import shutil
import subprocess
import uuid
from datetime import date, timedelta

import pytest

from tests.conftest import ROOT
from engine.agents import cohort_analyst
from engine.brief import load_brief
from engine.sql.dialects import dialect, holdout_bucket

DSN = os.environ.get("LCM_PG")
pytestmark = pytest.mark.skipif(not DSN or not shutil.which("psql"), reason="set LCM_PG and install psql to run on PostgreSQL")
FIX = ROOT / "tests" / "fixtures" / "ridgeview_small" / "data"     # the 400-person world: CSVs, no accounts


@pytest.fixture(scope="module")
def pg():
    schema = f"lcm_{uuid.uuid4().hex[:8]}"
    env = {**os.environ, "PGOPTIONS": f"-c search_path={schema}"}

    def run(sql: str, at: bool = True) -> str:
        r = subprocess.run(["psql", DSN, "-v", "ON_ERROR_STOP=1", "-q"] + (["-At"] if at else []) + ["-c", sql],
                           capture_output=True, text=True, env=env, cwd=ROOT)
        if r.returncode:
            raise RuntimeError(r.stderr)
        return r.stdout

    subprocess.run(["psql", DSN, "-q", "-c", f"CREATE SCHEMA {schema}"], check=True, capture_output=True)
    run("CREATE TABLE users (user_id varchar primary key, email varchar, created_at timestamp, country varchar, plan varchar, "
        "lifecycle_stage varchar, email_consent_at timestamp, sms_consent_at timestamp)")
    run("CREATE TABLE orders (order_id varchar primary key, user_id varchar, order_at timestamp, status varchar, gross_amount numeric(18,2), "
        "discount_amount numeric(18,2), net_amount numeric(18,2), currency varchar)")
    run("CREATE TABLE track_events (event_id varchar primary key, user_id varchar, anonymous_id varchar, event_name varchar, "
        "event_at timestamp, properties jsonb)")
    for t in ("users", "orders", "track_events"):
        run(f"\\copy {t} from '{FIX / (t + '.csv')}' csv header null ''", at=False)
    run(cohort_analyst.ledger_ddl("postgres"))

    def query(sql: str):
        out = run(f"SELECT coalesce(json_agg(t), '[]'::json) FROM ({sql.rstrip().rstrip(';')}) t")
        return json.loads(out)

    query.run = run
    yield query
    subprocess.run(["psql", DSN, "-q", "-c", f"DROP SCHEMA {schema} CASCADE"], capture_output=True)


def _norm(rows):
    out = []
    for r in rows:
        vals = []
        for k, v in r.items():
            if k in ("email", "qualified_at", "last_core_at"):
                continue
            if hasattr(v, "year"):
                v = str(v)[:10]
            elif isinstance(v, str) and len(v) >= 10 and v[4] == "-" and v[7] == "-":
                v = v[:10]
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                v = int(v)
            vals.append(v)
        out.append(tuple(vals))
    return sorted(out, key=str)


def test_postgres_matches_duckdb(pg):
    b = load_brief(ROOT / "examples/brands/ridgeview_saas/briefs/winback_14d.yaml")
    con = cohort_analyst.connect(str(FIX))
    entered = 0
    for k in range(0, 40, 3):
        d = (date(2026, 10, 18) - timedelta(days=7 * k)).isoformat()
        ref = _norm(cohort_analyst._rows(con, cohort_analyst.entry_sql(b, "duckdb", d)))
        assert _norm(pg(cohort_analyst.entry_sql(b, "postgres", d))) == ref, d
        entered += len(ref)
    assert entered > 0
    assert _norm(pg(cohort_analyst.history_sql(b, "postgres", "2026-10-18"))) == _norm(cohort_analyst._rows(con, cohort_analyst.history_sql(b, "duckdb", "2026-10-18")))
    for name, sql in cohort_analyst.audit_sql(b, "postgres", "2026-10-18").items():
        assert _norm(pg(sql)) == _norm(cohort_analyst._rows(con, cohort_analyst.audit_sql(b, "duckdb", "2026-10-18")[name])), name


def test_postgres_daily_job_round_trip(pg):
    b = load_brief(ROOT / "examples/brands/ridgeview_saas/briefs/winback_14d.yaml")
    due = pg(cohort_analyst.entry_sql(b, "postgres", "2026-10-18"))
    assert due
    pg.run(cohort_analyst.queue_sql(b, "postgres", "2026-10-18"))
    pending = pg(cohort_analyst.pending_sql(b, "postgres", "2026-10-18"))
    assert {(r["user_id"], r["anchor_date"]) for r in pending} == {(r["user_id"], r["anchor_date"]) for r in due}
    assert pg(cohort_analyst.entry_sql(b, "postgres", "2026-10-18")) == [], "queued lapses are not due again"
    for r in pending:
        pg.run(cohort_analyst.mark_sql(b, "postgres").replace(":user_id", f"'{r['user_id']}'").replace(":anchor_date", f"'{r['anchor_date']}'"))
    assert pg(cohort_analyst.pending_sql(b, "postgres", "2026-10-18")) == []
    assert all(r["status"] == "pushed" for r in pg("SELECT status FROM lcm_flow_entries"))
    pg.run("DELETE FROM lcm_flow_entries")


def test_postgres_holdout_bucket_matches_python(pg):
    rows = pg(f"SELECT user_id, {dialect('postgres').holdout_bucket('user_id', 'ridgeview_winback_14d')} AS b FROM users")
    assert len(rows) == 400 and all(r["b"] == holdout_bucket(r["user_id"], "ridgeview_winback_14d") for r in rows)
