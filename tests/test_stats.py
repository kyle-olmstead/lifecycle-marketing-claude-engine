import json

import pytest

from tests.conftest import ROOT
from tools import stats as S


def test_sample_size_matches_reference():
    r = S.sample_size(0.10, 0.12)
    assert (r["n_control"], r["n_treatment"]) == (3841, 3841)
    h = S.sample_size(0.04, 0.048, ratio=9)
    assert (h["n_control"], h["n_treatment"]) == (5884, 52956)


@pytest.mark.parametrize("args, expected", [
    ((56, 70, 48, 80), (0.052431, 0.333873)),         # Newcombe (1998) example; values checked against statsmodels when written
    ((412, 9000, 38, 1000), (-0.006556, 0.018924)),
    ((30, 200, 12, 180), (0.020288, 0.146048)),
])
def test_newcombe_interval_matches_reference_values(args, expected):
    lo, hi = S.newcombe_diff_ci(*args)
    assert (round(lo, 6), round(hi, 6)) == expected


@pytest.mark.parametrize("x, df, expected", [(3.84, 1, 0.05004352), (10.83, 1, 0.00099869), (25, 3, 1.544e-05), (7.8, 2, 0.02024191)])
def test_chi_square_tail_matches_reference_values(x, df, expected):
    assert abs(S._chi2_sf(x, df) - expected) < 1e-8


def test_mde_and_feasibility():
    m = S.minimum_detectable_effect(1000, 9000, 0.04)
    assert abs(m["mde_abs"] - 0.021014) < 1e-5
    f = S.feasibility(1200, 0.10, 0.04, mde_rel=0.20)
    assert f["expected_read"] == "directional only" and f["months_to_read"] == 49.0
    assert S.feasibility(3000, 0.10, 0.28, mde_abs=0.05)["required_holdout_n"] == 749


def test_read_date_includes_the_outcome_window():
    f = S.feasibility(2500, 0.20, 0.04, mde_abs=0.015, outcome_window_days=60)
    assert f["required_holdout_n"] == 2039 and f["enrollment_months"] == 4.1 and f["months_to_read"] == 6.0


def test_clustered_randomization_inflates_the_sample():
    base = S.feasibility(3000, 0.10, 0.28, mde_abs=0.05, outcome_window_days=9)
    clustered = S.feasibility(3000, 0.10, 0.28, mde_abs=0.05, outcome_window_days=9, design_effect=1.15)
    assert base["required_holdout_n"] == 749 and clustered["required_holdout_n"] == 862
    assert clustered["months_to_read"] == 3.2 > base["months_to_read"]
    with pytest.raises(ValueError):
        S.feasibility(3000, 0.10, 0.28, mde_abs=0.05, design_effect=0.9)


def test_readout_refuses_small_samples_and_flags_srm():
    small = S.readout(3, 20, 1, 20)
    assert small["read"] == "not readable" and "lift_rel" not in small
    srm = S.readout(500, 9000, 60, 2000, expected_holdout=0.10)
    assert srm["srm"]["mismatch"] and srm["read"] == "not readable"


def test_incrementality_example_three_numbers():
    x = S.incrementality(json.loads((ROOT / "examples/stats/winback_readout.json").read_text()))
    assert round(x["incremental_revenue"]) == 15880 and round(x["gap"]) == 26020
    assert x["attributed_revenue"] <= 61800  # attribution can only credit revenue the treatment arm produced
    assert x["read"] == "causal, inconclusive"
    out = S.render_incrementality(x)
    conversion_line = next(line for line in out.splitlines() if line.strip().startswith("Conversion:"))
    revenue_line = next(line for line in out.splitlines() if line.strip().startswith("Incremental:"))
    assert "not distinguishable from zero" in conversion_line and "CI" in conversion_line
    assert "CI" not in revenue_line and "point estimate" in revenue_line


def test_attributed_above_treatment_revenue_is_refused():
    doc = json.loads((ROOT / "examples/stats/winback_readout.json").read_text())
    doc["attributed_revenue"] = doc["treatment"]["revenue"] + 1
    with pytest.raises(ValueError, match="exceeds the treatment arm"):
        S.incrementality(doc)


def test_revenue_interval_when_per_entrant_sds_are_supplied():
    doc = json.loads((ROOT / "examples/stats/winback_readout.json").read_text())
    doc["treatment"]["revenue_sd"], doc["holdout"]["revenue_sd"] = 42.5, 36.5
    x = S.incrementality(doc)
    lo, hi = x["incremental_revenue_ci"]
    assert lo < x["incremental_revenue"] < hi and round(hi - lo) == round(2 * 1.959964 * (42.5 ** 2 / 8160 + 36.5 ** 2 / 2040) ** 0.5 * 8160)
    assert "95% CI $" in S.render_incrementality(x)


def test_pre_post_is_labeled_directional():
    doc = json.loads((ROOT / "examples/stats/winback_readout.json").read_text())
    doc["design"] = "pre_post"
    assert S.incrementality(doc)["read"] == "directional"


def test_interval_label_follows_alpha():
    doc = {"flow": "f", "period": "q", "alpha": 0.10, "treatment": {"n": 4000, "conversions": 480}, "holdout": {"n": 1000, "conversions": 100}}
    text = S.render_incrementality(S.incrementality(doc))
    assert "90% CI" in text and "95% CI" not in text


def test_no_larger_holdout_suggested_when_already_at_25_percent():
    at_15 = S.feasibility(entries_per_month=1500, holdout_share=0.15, p_control=0.10, mde_rel=0.20)
    at_25 = S.feasibility(entries_per_month=1500, holdout_share=0.25, p_control=0.10, mde_rel=0.20)
    assert at_15["expected_read"] == "causal, slow" and "a 25% holdout would read" in at_15["recommended_design"]
    assert at_25["expected_read"] == "causal, slow" and "a 25% holdout would read" not in at_25["recommended_design"]


def test_fractional_interval_levels_are_not_rounded():
    doc = {"flow": "f", "period": "q", "alpha": 0.025, "treatment": {"n": 4000, "conversions": 480}, "holdout": {"n": 1000, "conversions": 100}}
    assert "97.5% CI" in S.render_incrementality(S.incrementality(doc))
