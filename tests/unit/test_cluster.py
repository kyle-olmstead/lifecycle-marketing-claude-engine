"""Clustered (account-randomized) experiments: ICC, design effect, sizing and readout, checked against synthetic data
with a known ICC and against independent computations."""
import json
import math
import random
import subprocess
import sys

import pytest

from tests.conftest import ROOT
from engine.measurement import cluster as C
from engine.measurement import stats as S


def betabinomial_clusters(rng, sizes, mean, icc):
    """Accounts whose conversion rates are Beta distributed: the within-account correlation of two 0/1 outcomes is
    exactly 1 / (a + b + 1) = icc."""
    s = 1 / icc - 1
    a, b = mean * s, (1 - mean) * s
    return [(m, rng.binomialvariate(m, rng.betavariate(a, b))) for m in sizes]


def test_icc_matches_an_independent_one_way_anova_on_individual_outcomes():
    cl = betabinomial_clusters(random.Random(3), [3, 5, 8, 12] * 10, 0.3, 0.1)
    groups = [[1] * y + [0] * (m - y) for m, y in cl]
    values = [v for g in groups for v in g]
    n, k = len(values), len(groups)
    grand = sum(values) / n
    ssb = sum(len(g) * (sum(g) / len(g) - grand) ** 2 for g in groups)
    ssw = sum((v - sum(g) / len(g)) ** 2 for g in groups for v in g)
    msb, msw = ssb / (k - 1), ssw / (n - k)
    m0 = (n - sum(len(g) ** 2 for g in groups) / n) / (k - 1)
    assert abs(C.icc_anova(cl)["icc"] - (msb - msw) / (msb + (m0 - 1) * msw)) < 1e-12


@pytest.mark.parametrize("icc, mean", [(0.02, 0.15), (0.05, 0.20), (0.15, 0.30)])
def test_icc_recovers_a_known_value(icc, mean):
    rng = random.Random(int(icc * 1000))
    estimates = [C.icc_anova(betabinomial_clusters(rng, [rng.randint(2, 20) for _ in range(1500)], mean, icc))["icc"] for _ in range(8)]
    avg = sum(estimates) / len(estimates)
    assert abs(avg - icc) < 0.01 + 0.1 * icc, (avg, estimates)


def test_icc_interval_covers_and_edge_cases_are_honest():
    rng = random.Random(21)
    covered = 0
    for _ in range(200):
        r = C.icc_anova(betabinomial_clusters(rng, [8] * 60, 0.25, 0.08))
        covered += r["icc_ci"][0] <= 0.08 <= r["icc_ci"][1]
    assert covered / 200 > 0.88   # approximate interval: nominal 95%, balanced sizes
    assert C.icc_anova([(1, 0), (1, 1), (1, 0)])["icc_for_design"] == 0.0
    assert C.icc_anova([(5, 2)])["icc"] is None
    assert "fewer than 30 accounts" in C.icc_anova(betabinomial_clusters(rng, [6] * 12, 0.3, 0.1))["note"]
    with pytest.raises(ValueError):
        C.icc_anova([(3, 4), (2, 1)])


def test_f_quantiles_match_published_tables():
    assert abs(C._f_ppf(0.975, 4, 20) - 3.51) < 0.005
    assert abs(C._f_ppf(0.95, 3, 10) - 3.71) < 0.005
    assert abs(C._f_ppf(0.99, 5, 30) - 3.70) < 0.005


def test_design_effect_formulas():
    assert C.design_effect(0.05, 8)["design_effect"] == pytest.approx(1 + 7 * 0.05)
    sizes = [1, 2, 3, 5, 8, 13, 21, 34]
    de = C.design_effect(0.08, cluster_sizes=sizes)
    assert de["design_effect"] == pytest.approx(1 + (sum(m * m for m in sizes) / sum(sizes) - 1) * 0.08)
    assert de["design_effect"] == pytest.approx(C.design_effect(0.08, de["mean_cluster_size"], cv=de["cv"])["design_effect"])
    assert C.design_effect(-0.02, 10)["design_effect"] == 1.0


def test_design_effect_matches_the_simulated_variance_of_the_pooled_rate():
    """Kerry and Bland's unequal-size design effect against the variance of the pooled rate over 20,000 seeded replicates."""
    rng = random.Random(5)
    sizes = [rng.choice([1, 2, 3, 5, 8, 13, 21, 34]) for _ in range(60)]
    n = sum(sizes)
    rates = [sum(y for _, y in betabinomial_clusters(rng, sizes, 0.2, 0.08)) / n for _ in range(20000)]
    mean = sum(rates) / len(rates)
    simulated = sum((r - mean) ** 2 for r in rates) / (len(rates) - 1) / (0.2 * 0.8 / n)
    formula = C.design_effect(0.08, cluster_sizes=sizes)["design_effect"]
    equal_size = 1 + (n / len(sizes) - 1) * 0.08
    assert abs(simulated - formula) / formula < 0.05, (simulated, formula)
    assert abs(simulated - equal_size) / equal_size > 0.25    # ignoring unequal sizes would understate it badly


def test_cluster_readout_holds_its_error_rate_where_the_naive_test_does_not():
    rng = random.Random(11)
    sizes = list(range(5, 41))
    sims, rej_cluster, rej_naive = 1500, 0, 0
    for _ in range(sims):
        r = C.cluster_readout(betabinomial_clusters(rng, [rng.choice(sizes) for _ in range(80)], 0.15, 0.05),
                              betabinomial_clusters(rng, [rng.choice(sizes) for _ in range(20)], 0.15, 0.05))
        lo, hi = r["diff_ci"]
        rej_cluster += lo > 0 or hi < 0
        rej_naive += abs(r["diff_abs"] / r["naive_se"]) > 1.959964
    assert 0.03 < rej_cluster / sims < 0.08, rej_cluster / sims
    assert rej_naive / sims > 0.15, rej_naive / sims


def test_cluster_readout_refusals_and_srm_on_accounts():
    rng = random.Random(4)
    few = C.cluster_readout(betabinomial_clusters(rng, [20] * 40, 0.2, 0.05), betabinomial_clusters(rng, [20] * 6, 0.2, 0.05))
    assert few["read"] == "not readable" and "lift_rel" not in few
    # 100 of 480 entrants (21%) looks like the planned 20% split, but only 10 of 200 accounts (5%) were held out
    t = betabinomial_clusters(rng, [2] * 190, 0.2, 0.05)
    c = betabinomial_clusters(rng, [10] * 10, 0.2, 0.05)
    assert not S.srm_check([380, 100], [0.8, 0.2])["mismatch"]
    r = C.cluster_readout(t, c, expected_holdout=0.20)
    assert r["srm"]["observed"] == [190, 10] and r["srm"]["mismatch"] and r["read"] == "not readable"
    ok = C.cluster_readout(betabinomial_clusters(rng, [6] * 160, 0.2, 0.05), betabinomial_clusters(rng, [6] * 40, 0.2, 0.05),
                           expected_holdout=0.20)
    assert ok["srm"]["counted"].startswith("accounts") and ok["read"] in ("causal", "causal, inconclusive")
    assert ok["df"] < 160 + 40 - 2   # Satterthwaite: driven by the small holdout


def test_ratio_difference_equals_a_hand_computation():
    t = [(4, 1), (6, 3), (10, 2)]
    c = [(5, 0), (5, 2)]
    r = C.ratio_difference(t, c)
    rt, rc = 6 / 20, 2 / 10
    vt = 3 / 2 * sum((y - rt * m) ** 2 for m, y in t) / 20 ** 2
    vc = 2 / 1 * sum((y - rc * m) ** 2 for m, y in c) / 10 ** 2
    assert r["diff"] == pytest.approx(rt - rc) and r["se"] == pytest.approx(math.sqrt(vt + vc))
    assert r["df"] == pytest.approx((vt + vc) ** 2 / (vt ** 2 / 2 + vc ** 2 / 1))


def test_cluster_sizing_inflates_by_the_design_effect_and_reaches_its_power():
    p = C.cluster_sample_size(0.10, 0.16, 0.05, 10, 0.5, ratio=3)
    z = C.cluster_sample_size(0.10, 0.16, 0.05, 10, 0.5, ratio=3, small_sample=False)
    ind = S.sample_size(0.10, 0.16, ratio=3, apply_readout_floor=False)
    assert p["design_effect"] == pytest.approx(1 + ((1 + 0.25) * 10 - 1) * 0.05, abs=1e-4)
    assert z["holdout_entrants"] >= ind["n_control"] * p["design_effect"] - 10
    assert p["holdout_clusters"] >= z["holdout_clusters"] and p["df"] is not None
    # seeded simulation of the planned design: power close to the 80% asked for
    rng = random.Random(9)
    rej = 0
    for _ in range(600):
        t = betabinomial_clusters(rng, [max(1, round(rng.gammavariate(4, 2.5))) for _ in range(p["treatment_clusters"])], 0.16, 0.05)
        c = betabinomial_clusters(rng, [max(1, round(rng.gammavariate(4, 2.5))) for _ in range(p["holdout_clusters"])], 0.10, 0.05)
        rej += C.cluster_readout(t, c)["diff_ci"][0] > 0
    assert 0.74 < rej / 600 < 0.90, rej / 600


def test_cluster_sizing_never_plans_below_the_floor():
    big_lift = C.cluster_sample_size(0.10, 0.60, 0.01, 2, 0.0)
    assert big_lift["holdout_clusters"] >= S.MIN_CLUSTERS_PER_ARM and big_lift["size_set_by"] == "readout floor"
    rare = C.cluster_sample_size(0.005, 0.05, 0.01, 3, 0.0)
    assert rare["holdout_entrants"] * 0.005 >= S.MIN_EVENTS_FOR_LIFT


def _canonical_duckdb():
    import duckdb
    from engine.data.schema import TABLES
    con = duckdb.connect()
    for name in ("users", "track_events"):
        con.execute(f"CREATE TABLE {name} ({TABLES[name].ddl})")
    return con


def test_icc_from_the_canonical_tables_in_duckdb():
    rng = random.Random(17)
    con = _canonical_duckdb()
    clusters = betabinomial_clusters(rng, [rng.randint(2, 12) for _ in range(300)], 0.25, 0.10)
    users, events, uid = [], [], 0
    for a, (m, y) in enumerate(clusters):
        for j in range(m):
            uid += 1
            users.append((f"u{uid}", f"u{uid}@example.test", "2026-01-01 00:00:00", f"acct_{a}"))
            if j < y:
                events.append((f"e{uid}", f"u{uid}", "report_created", "2026-02-01 00:00:00"))
    users += [("u_noacct", "n@example.test", "2026-01-01 00:00:00", None)]
    con.executemany("INSERT INTO users (user_id, email, created_at, account_id) VALUES (?, ?, ?, ?)", users)
    con.executemany("INSERT INTO track_events (event_id, user_id, event_name, event_at) VALUES (?, ?, ?, ?)", events)
    r = C.icc_from_duckdb(con, C.event_outcome_sql("report_created"))
    direct = C.icc_anova(clusters)
    assert r["icc"] == pytest.approx(direct["icc"]) and r["clusters"] == 300 and r["unassigned_users"] == 1
    assert r["design_effect"] == pytest.approx(C.design_effect(direct["icc_for_design"], cluster_sizes=[m for m, _ in clusters])["design_effect"])
    windowed = C.icc_from_duckdb(con, C.event_outcome_sql("report_created", start="2026-03-01"))
    assert windowed["icc"] is None   # no events after March: the outcome does not vary


def test_icc_cli_on_data_without_accounts_says_so():
    r = subprocess.run([sys.executable, "engine/measurement/cluster.py", "icc", "--data", "tests/fixtures/ridgeview_small/data",
                        "--event", "report_created"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["design_effect"] == 1.0 and "no user has account_id" in out["note"]


def test_cluster_cli_size_deff_and_readout(tmp_path):
    for args in (["deff", "--icc", "0.05", "--mean-size", "8", "--cv", "0.6"],
                 ["size", "--baseline", "0.10", "--mde-abs", "0.03", "--icc", "0.05", "--mean-size", "8", "--cv", "0.6", "--holdout", "0.2"]):
        r = subprocess.run([sys.executable, "engine/measurement/cluster.py", *args], cwd=ROOT, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    rng = random.Random(2)
    doc = {"treatment": betabinomial_clusters(rng, [8] * 80, 0.2, 0.05), "holdout": betabinomial_clusters(rng, [8] * 20, 0.2, 0.05)}
    (tmp_path / "c.json").write_text(json.dumps(doc))
    r = subprocess.run([sys.executable, "engine/measurement/cluster.py", "readout", str(tmp_path / "c.json"), "--expected-holdout", "0.2"],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["unit"] == "account"
