"""Group-sequential boundaries (Lan-DeMets spending, recursive integration) and the always-valid mSPRT, checked against
published values and seeded Monte Carlo."""
import json
import math
import random
import subprocess
import sys

import pytest

from tests.conftest import ROOT
from engine.measurement import sequential as Q
from engine.measurement import stats as S

EQ5 = [0.2, 0.4, 0.6, 0.8, 1.0]


def test_lan_demets_obrien_fleming_boundaries_match_published_values():
    # K = 5 equally spaced, two-sided alpha 0.05 (ldbounds / gsDesign, Lan and DeMets 1983)
    b = Q.boundaries(EQ5, 0.05, "obf")["boundaries"]
    for got, want in zip(b, [4.877, 3.357, 2.680, 2.290, 2.031]):
        assert abs(got - want) < 0.01, b
    two = Q.boundaries([0.5, 1.0], 0.05, "obf")["boundaries"]
    assert abs(two[0] - 2.963) < 0.005 and abs(two[1] - 1.969) < 0.005


def test_lan_demets_pocock_boundaries_match_published_values():
    b = Q.boundaries(EQ5, 0.05, "pocock")["boundaries"]
    for got, want in zip(b, [2.438, 2.427, 2.410, 2.397, 2.386]):
        assert abs(got - want) < 0.005, b
    assert all(abs(x - 2.41) < 0.04 for x in b)


@pytest.mark.parametrize("k, pocock, obf", [(2, 2.178, 1.977), (3, 2.289, 2.004), (4, 2.361, 2.024), (5, 2.413, 2.040)])
def test_classic_constants_match_jennison_turnbull_tables_2_1_and_2_3(k, pocock, obf):
    assert abs(Q.constant_boundaries(k, 0.05, "pocock")["constant"] - pocock) < 0.001
    assert abs(Q.constant_boundaries(k, 0.05, "obf")["constant"] - obf) < 0.001


@pytest.mark.parametrize("k, rp80, rb80, rp90, rb90", [
    (2, 1.110, 1.008, 1.100, 1.007), (3, 1.166, 1.017, 1.151, 1.016), (4, 1.202, 1.024, 1.183, 1.022), (5, 1.229, 1.028, 1.207, 1.026)])
def test_inflation_factors_match_jennison_turnbull_tables_2_2_and_2_4(k, rp80, rb80, rp90, rb90):
    t = [(i + 1) / k for i in range(k)]
    cp = Q.constant_boundaries(k, 0.05, "pocock")["boundaries"]
    cb = Q.constant_boundaries(k, 0.05, "obf")["boundaries"]
    assert abs(Q.inflation_factor(t, 0.05, 0.80, bounds=cp)["inflation_factor"] - rp80) < 0.002
    assert abs(Q.inflation_factor(t, 0.05, 0.80, bounds=cb)["inflation_factor"] - rb80) < 0.002
    assert abs(Q.inflation_factor(t, 0.05, 0.90, bounds=cp)["inflation_factor"] - rp90) < 0.002
    assert abs(Q.inflation_factor(t, 0.05, 0.90, bounds=cb)["inflation_factor"] - rb90) < 0.002


def _simulate(bounds, fractions, sims, seed, drift=0.0):
    """Seeded Brownian motion in information time: per-look first-crossing frequencies of |Z_k| >= b_k."""
    rng = random.Random(seed)
    hits = [0] * len(fractions)
    for _ in range(sims):
        s, prev = 0.0, 0.0
        for k, (t, b) in enumerate(zip(fractions, bounds)):
            s += rng.gauss(drift * (t - prev), math.sqrt(t - prev))
            prev = t
            if abs(s / math.sqrt(t)) >= b:
                hits[k] += 1
                break
    return [h / sims for h in hits]


@pytest.mark.parametrize("fractions, spending", [(EQ5, "obf"), ([0.3, 0.55, 0.8, 1.0], "pocock"), ([0.1, 0.25, 0.7, 1.0], "obf")])
def test_overall_type_one_error_by_seeded_monte_carlo(fractions, spending):
    d = Q.boundaries(fractions, 0.05, spending)
    sims = 120_000
    freq = _simulate(d["boundaries"], fractions, sims, seed=2026)
    se = math.sqrt(0.05 * 0.95 / sims)
    assert abs(sum(freq) - 0.05) < 4 * se, sum(freq)
    increments = [c - p for c, p in zip(d["cumulative_alpha"], [0.0] + d["cumulative_alpha"][:-1])]
    for f, inc in zip(freq, increments):     # each look spends what the spending function allowed
        assert abs(f - inc) < 4 * math.sqrt(max(inc, 1e-4) / sims) + 2e-4, (freq, increments)


def test_power_and_expected_sample_by_monte_carlo():
    inf = Q.inflation_factor(EQ5, 0.05, 0.80, "obf")
    freq = _simulate(inf["boundaries"], EQ5, 60_000, seed=7, drift=inf["drift"])
    assert abs(sum(freq) - 0.80) < 0.01
    assert 1.0 < inf["inflation_factor"] < 1.05 and inf["expected_information_h1"] < 0.85


def test_look_decisions_follow_the_boundaries():
    first = Q.look(2.9, 0.2)
    assert first["decision"] == "continue" and abs(first["boundary"] - 4.877) < 0.01
    stop = Q.look(3.5, 0.4, previous_fractions=[0.2])
    assert stop["decision"] == "stop for efficacy" and stop["significant"] and "treatment better" in stop["detail"]
    harm = Q.look(-3.5, 0.4, previous_fractions=[0.2])
    assert harm["decision"] == "stop for efficacy" and "treatment worse" in harm["detail"]
    final_no = Q.look(1.9, 1.0, previous_fractions=EQ5[:-1])
    assert final_no["decision"] == "final read" and not final_no["significant"] and "inconclusive" in final_no["detail"]
    assert abs(final_no["boundary"] - 2.031) < 0.01 and final_no["cumulative_alpha"] == 0.05
    final_yes = Q.look(2.1, 1.0, previous_fractions=EQ5[:-1])
    assert final_yes["significant"]


def test_looks_can_move_and_an_early_end_spends_the_rest():
    moved = Q.look(2.0, 0.33, previous_fractions=[0.15])
    planned = Q.boundaries([0.15, 0.33, 1.0], 0.05, "obf")["boundaries"][1]
    assert abs(moved["boundary"] - planned) < 1e-6
    ended = Q.look(2.1, 0.7, previous_fractions=[0.35], final=True)   # stopped at 70% of the planned information
    assert ended["final"] and ended["cumulative_alpha"] == 0.05
    first = Q.look(0.0, 0.35)["boundary"]
    total = Q.crossing_probabilities([first, ended["boundary"]], [0.35, 0.7])["total"]
    assert abs(total - 0.05) < 1e-6   # the first look spent alpha(0.35) of the plan; the early final look spent the rest


def test_look_from_counts_skips_looks_below_the_floor_and_gives_a_repeated_ci():
    early = Q.look_from_counts(12, 100, 1, 20, 8000, 2000)
    assert early["decision"] == "continue" and not early["counted_as_look"]
    r = Q.look_from_counts(460, 4000, 80, 1000, 8000, 2000, previous_fractions=[0.25])
    assert r["counted_as_look"] and 0.49 < r["information_fraction"] < 0.51
    lo, hi = r["repeated_ci"]
    assert lo < r["diff_abs"] < hi and (hi - lo) > 2 * 1.96 * math.sqrt(0.115 * 0.885 / 4000 + 0.08 * 0.92 / 1000)
    srm = Q.look_from_counts(460, 4000, 80, 2000, 8000, 2000, expected_holdout=0.2)
    assert srm["decision"].startswith("stop: sample ratio")


def test_sequential_plan_sizes_and_floors():
    p = Q.sequential_plan(0.10, 0.12, looks=4, spending="obf", ratio=4)
    fixed = S.sample_size(0.10, 0.12, ratio=4, apply_readout_floor=False)
    assert p["max_n_control"] == math.ceil(fixed["n_control"] * p["inflation_factor"] - 1e-9) or p["max_n_control"] >= fixed["n_control"]
    assert p["max_n_control"] > fixed["n_control"] and p["looks_detail"][-1]["holdout_n"] == p["max_n_control"]
    assert p["expected_n_control_if_effect"] < p["max_n_control"]
    tiny = Q.sequential_plan(0.01, 0.05, looks=3, spending="pocock")
    assert tiny["size_set_by"] == "readout floor" and tiny["max_n_control"] * 0.01 >= S.MIN_EVENTS_FOR_LIFT
    assert not tiny["looks_detail"][0]["efficacy_look"] and tiny["looks_detail"][-1]["efficacy_look"]


# ----------------------------------------------------------------------------------------------
# always-valid inference
# ----------------------------------------------------------------------------------------------

def test_msprt_statistic_equals_numerical_integration_of_the_mixture():
    """Closed form vs Simpson integration of N(d; theta, v) / N(d; 0, v) over theta ~ N(0, tau^2)."""
    x_t, n_t, x_c, n_c, tau = 620, 5000, 100, 1000, 0.02
    r = Q.msprt(x_t, n_t, x_c, n_c, tau)
    p_t, p_c = x_t / n_t, x_c / n_c
    d, v = p_t - p_c, p_t * (1 - p_t) / n_t + p_c * (1 - p_c) / n_c
    lo, hi, steps = -12 * tau, 12 * tau, 4000
    h = (hi - lo) / steps
    total = 0.0
    for i in range(steps + 1):
        th = lo + i * h
        f = math.exp(-((d - th) ** 2 - d ** 2) / (2 * v)) * math.exp(-th * th / (2 * tau * tau)) / math.sqrt(2 * math.pi * tau * tau)
        total += f * (1 if i in (0, steps) else 4 if i % 2 else 2)
    assert abs(math.log(total * h / 3) - r["log_likelihood_ratio"]) < 1e-6


def _monitor(rng, sims, p_t, p_c, inc_t, inc_c, looks, tau):
    """Continuous monitoring: always-valid rejections, naive repeated-readout rejections, and confidence-sequence misses."""
    rej_m = rej_naive = miss = 0
    truth = p_t - p_c
    for _ in range(sims):
        xt = nt = xc = nc = 0
        prev = None
        hit_m = hit_n = missed = False
        for _ in range(looks):
            xt += rng.binomialvariate(inc_t, p_t)
            nt += inc_t
            xc += rng.binomialvariate(inc_c, p_c)
            nc += inc_c
            r = Q.msprt(xt, nt, xc, nc, tau, previous=prev)
            prev = r
            cs = r["confidence_sequence"]
            if cs and not cs[0] <= truth <= cs[1]:
                missed = True
            hit_m = hit_m or r["decision"] == "stop for efficacy"
            if not hit_n:
                rr = S.readout(xt, nt, xc, nc)
                hit_n = rr.get("p_value") is not None and rr["p_value"] < 0.05
        rej_m += hit_m
        rej_naive += hit_n
        miss += missed
    return rej_m / sims, rej_naive / sims, miss / sims


def test_msprt_keeps_type_one_error_under_continuous_monitoring():
    rej, naive, miss = _monitor(random.Random(1), 800, 0.10, 0.10, 100, 100, 150, 0.02)
    assert rej <= 0.05 + 0.01, rej          # checked 150 times, still about alpha or below
    assert naive > 0.2, naive               # the fixed-horizon readout, checked as often, is not
    assert miss <= 0.05 + 0.01, miss
    rej90, naive90, _ = _monitor(random.Random(2), 800, 0.10, 0.10, 450, 50, 100, 0.01)   # a 10% holdout
    assert rej90 <= 0.06 and naive90 > 0.2


def test_msprt_confidence_sequence_covers_a_real_effect_and_detects_it():
    rej, _naive, miss = _monitor(random.Random(3), 400, 0.13, 0.10, 400, 100, 100, 0.02)
    assert miss <= 0.06, miss
    assert rej > 0.8, rej


def test_msprt_carries_the_running_minimum_and_intersection():
    a = Q.msprt(600, 5000, 100, 1000, 0.02)
    b = Q.msprt(1150, 10000, 230, 2000, 0.02, previous=a)
    assert b["p_value"] <= a["p_value"]
    assert b["confidence_sequence"][0] >= a["confidence_sequence"][0] and b["confidence_sequence"][1] <= a["confidence_sequence"][1]
    small = Q.msprt(3, 40, 1, 10, 0.02, previous=a)
    assert small["read"] == "not readable" and small["p_value"] == a["p_value"]


def test_sequential_cli_runs():
    for args in (["boundaries", "--looks", "5", "--spending", "obf"],
                 ["plan", "--baseline", "0.10", "--mde-abs", "0.02", "--looks", "4", "--holdout", "0.2"],
                 ["look", "--z", "2.9", "--t", "0.5", "--previous", "0.25"],
                 ["look", "--xt", "460", "--nt", "4000", "--xc", "80", "--nc", "1000", "--planned-nt", "8000", "--planned-nc", "2000"],
                 ["msprt", "--xt", "600", "--nt", "5000", "--xc", "100", "--nc", "1000", "--mde-abs", "0.02"]):
        r = subprocess.run([sys.executable, "engine/measurement/sequential.py", *args], cwd=ROOT, capture_output=True, text=True)
        assert r.returncode == 0, (args, r.stderr[-400:])
        json.loads(r.stdout)
