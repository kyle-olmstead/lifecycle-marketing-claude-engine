"""The program-wide global holdout: assignment identical in Python and SQL, independent of flow holdouts, and a
program-level readout that carries the same refusals as a flow readout."""
import random
import subprocess
import sys

import pytest

from tests.conftest import ROOT
from engine.measurement import global_holdout as G
from engine.measurement import stats as S
from engine.sql import check as sql_check
from engine.sql.dialects import DIALECTS, holdout_bucket

DEMO = ROOT / "tests" / "fixtures" / "ridgeview_small" / "data"   # the 400-person world these numbers are pinned to


def test_python_and_sql_agree_on_every_demo_user():
    from engine.data.load import connect
    con, _ = connect(str(DEMO))
    rows = con.execute(f"SELECT user_id, {G.bucket_sql('duckdb', 'user_id')} FROM users").fetchall()
    assert rows and all(G.global_bucket(u) == b for u, b in rows)
    members = con.execute(f"SELECT COUNT(*) FROM users WHERE {G.membership_predicate('duckdb')}").fetchone()[0]
    rest = con.execute(f"SELECT COUNT(*) FROM users WHERE {G.entry_filter('duckdb')}").fetchone()[0]
    assert members == sum(G.in_global_holdout(u) for u, _ in rows) and members + rest == len(rows)


def test_postgres_bucket_runs_on_the_fixtures():
    con = sql_check.connect()
    _cols, rows = sql_check.run(f"SELECT user_id, {G.bucket_sql('postgres', 'user_id')} AS b FROM users", con)
    assert all(G.global_bucket(u) == b for u, b in rows)


def test_global_salt_is_independent_of_flow_salts():
    ids = [f"user_{i}" for i in range(40000)]
    g = [G.in_global_holdout(i) for i in ids]
    f = [holdout_bucket(i, "winback_14d") < 15 for i in ids]
    share_g, share_f = sum(g) / len(ids), sum(f) / len(ids)
    both = sum(a and b for a, b in zip(g, f)) / len(ids)
    assert abs(share_g - 0.05) < 0.005 and abs(share_f - 0.15) < 0.008
    assert abs(both - share_g * share_f) < 0.0025      # no more overlap than chance


def test_share_must_be_whole_buckets():
    with pytest.raises(ValueError, match="whole percent"):
        G.entry_filter("postgres", share=0.055)
    with pytest.raises(ValueError):
        G.entry_filter("postgres", share=0.6)
    assert G.entry_filter("snowflake", share=0.10).endswith(">= 10")


def test_sql_parses_in_every_dialect():
    for d in DIALECTS:
        for unit in ("user", "account"):
            ok, msg = sql_check.parse(G.arms_sql(d, "2026-04-01", "2026-10-01", unit=unit), d)
            assert ok, (d, unit, msg)
        ok, msg = sql_check.parse(G.membership_sql(d), d)
        assert ok, (d, msg)


def test_program_readout_uses_the_flow_machinery_and_labels_the_scope():
    rng = random.Random(8)
    def arm(n, p, spend):
        rev = [rng.expovariate(1 / spend) if rng.random() < p else 0.0 for _ in range(n)]
        return {"n": n, "conversions": sum(1 for r in rev if r > 0), "revenue": sum(rev), "revenue_sq": sum(r * r for r in rev),
                "largest_person_revenue": max(rev)}
    t, c = arm(19000, 0.12, 80), arm(1000, 0.10, 80)
    x = G.program_readout(t, c, 0.05, "2026 H1", window_days=181)
    ref = S.readout(t["conversions"], t["n"], c["conversions"], c["n"], expected_holdout=0.05)
    assert x["conversion_readout"]["diff_ci"] == ref["diff_ci"] and x["read"] == ref["read"]
    assert x["incremental_revenue"] == pytest.approx((t["revenue"] / t["n"] - c["revenue"] / c["n"]) * t["n"])
    lo, hi = x["incremental_revenue_ci"]
    assert lo < x["incremental_revenue"] < hi
    assert x["scope"].startswith("program-level") and any("not the sum of flow-level lifts" in cv for cv in x["caveats"])
    small = G.program_readout({"n": 600, "conversions": 70, "revenue": 5000}, {"n": 25, "conversions": 2, "revenue": 90}, 0.05)
    assert small["read"] == "not readable" and small["incremental_revenue"] is None


def test_program_readout_on_the_demo_data_refuses_a_lift():
    from engine.data.load import connect
    con, _ = connect(str(DEMO))
    x = G.program_readout_duckdb(con, "2026-04-01", "2026-10-01")
    assert x["holdout_n"] < S.MIN_ARM_FOR_LIFT and x["read"] == "not readable"
    assert x["conversion_readout"]["srm"]["mismatch"] is False


def test_account_level_global_holdout_uses_the_clustered_readout():
    import duckdb
    from engine.data.schema import TABLES
    con = duckdb.connect()
    for name in ("users", "orders"):
        con.execute(f"CREATE TABLE {name} ({TABLES[name].ddl})")
    rng = random.Random(30)
    users, orders = [], []
    for a in range(800):
        acct = f"acct_{a}"
        held = G.in_global_holdout(acct)
        rate = rng.betavariate(2, 14) * (0.8 if held else 1.0)
        for j in range(rng.randint(1, 9)):
            uid = f"{acct}_u{j}"
            users.append((uid, f"{uid}@example.test", "2026-01-01 00:00:00", "2026-01-01 00:00:00", acct))
            if rng.random() < rate:
                orders.append((f"o_{uid}", uid, "2026-05-01 00:00:00", "completed", round(rng.uniform(20, 200), 2)))
    con.executemany("INSERT INTO users (user_id, email, created_at, email_consent_at, account_id) VALUES (?, ?, ?, ?, ?)", users)
    con.executemany("INSERT INTO orders (order_id, user_id, order_at, status, net_amount) VALUES (?, ?, ?, ?, ?)", orders)
    x = G.program_readout_duckdb(con, "2026-04-01", "2026-10-01", unit="account")
    conv = x["conversion_readout"]
    assert x["unit"] == "account" and conv["unit"] == "account" and conv["srm"]["counted"].startswith("accounts")
    assert conv["holdout"]["clusters"] == sum(G.in_global_holdout(f"acct_{a}") for a in range(800))
    if conv["read"] != "not readable":
        assert x["incremental_revenue_ci"][0] < x["incremental_revenue"] < x["incremental_revenue_ci"][1]


def test_global_holdout_cli():
    for args in (["filter", "--dialect", "bigquery"], ["sql", "--dialect", "snowflake", "--start", "2026-04-01", "--end", "2026-10-01"],
                 ["membership", "--dialect", "postgres"],
                 ["readout", "--data", str(DEMO), "--start", "2026-04-01", "--end", "2026-10-01", "--markdown"]):
        r = subprocess.run([sys.executable, "engine/measurement/global_holdout.py", *args], cwd=ROOT, capture_output=True, text=True)
        assert r.returncode == 0, (args, r.stderr[-400:])
    assert "not readable" in r.stdout and "program-level" in r.stdout
