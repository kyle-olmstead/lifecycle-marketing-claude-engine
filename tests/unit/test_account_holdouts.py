"""Account-level holdouts on Ridgeview at full size: sized on the people the gate lets in, with a design effect measured on
the flow's own entrants at the size the holdout will have when it reads."""
from __future__ import annotations

import json
import random
from datetime import date

import pytest

from engine import workspace
from engine.agents import cohort_analyst, experimentation_agent as ex, priorities
from engine.brief import load_brief
from engine.measurement import cluster, stats
from engine.paths import DEMO, DEMO_BRIEF, SMALL_DATA


@pytest.fixture(scope="module")
def demo():
    brief = load_brief(DEMO_BRIEF, defaults=workspace.brief_defaults(workspace.Workspace(DEMO)))
    cohort = cohort_analyst.define(brief, str(DEMO / "data"))
    return brief, cohort, ex.design(brief, cohort)


def test_the_demo_randomizes_accounts_and_reads_inside_a_year(demo):
    brief, cohort, design = demo
    assert brief.randomization_unit == "account" and design.hash_field == "account_id"
    assert design.read in ("causal", "causal, slow")
    assert 2 <= design.feasibility["months_to_read"] <= 9
    assert "account-level: design effect" in design.sizing and "measured on the flow's own entrants" in design.sizing
    assert "--design-effect" in design.sizing                       # the printed command reproduces the plan's number
    assert "error" not in (design.registry_entry or {})


def test_an_amber_win_back_is_sized_on_the_people_the_gate_lets_in(demo):
    brief, cohort, design = demo
    gated = cohort.audience_after_gate["monthly"]
    assert gated["rule"] == "clicked or replied in 90 days"
    assert cohort.monthly_entrants.value == gated["monthly_entrants"] < gated["monthly_entrants_before"]
    assert "pass the AMBER gate's entry filter" in cohort.monthly_entrants.basis
    assert "pass the gate's filter" in cohort.baseline_conversion_rate.basis
    assert design.feasibility["entries_per_month"] == cohort.monthly_entrants.value


def test_the_design_effect_is_measured_on_entrants_not_on_everyone_in_an_account(demo):
    _brief, cohort, design = demo
    cl = cohort.cluster
    assert cl["basis"] == "flow" and cl["clusters"] > 500 and cl["accounts_with_repeat_entrants"] >= 30
    # entrants per account over six months, not people per account: Ridgeview's accounts run to forty seats
    assert 1 < cl["mean_size"] < 4 and cl["size_weighted_mean"] < 8
    grid = cl["by_entrants"]
    assert [g["entrants"] for g in grid] == sorted(g["entrants"] for g in grid) and grid[-1]["entrants"] == cl["entrants_in_window"]
    lo, hi = cl["design_effect_range"]
    assert 1.0 <= lo <= cl["design_effect"] <= hi < 2.0
    assert "95% range" in design.sizing
    # the same entries as the baseline: matured, gated, in the six months the baseline reads
    assert cl["entrants_in_window"] + cl["unassigned_entrants"] >= cohort.audience_after_gate["monthly"]["matured"] - cl["unassigned_entrants"]


def test_the_measured_design_effect_is_the_readouts_own_estimator():
    """robust_design_effect is cluster.py's ratio-estimator variance over the binomial one: all-or-nothing accounts of four
    give four, independent people give about one."""
    same = [(4, 4)] * 50 + [(4, 0)] * 50
    assert cohort_analyst.robust_design_effect(same) == pytest.approx(4 * 100 / 99)
    rng = random.Random(3)
    indep = [(m, sum(rng.random() < 0.3 for _ in range(m))) for m in (rng.randint(1, 6) for _ in range(4000))]
    assert cohort_analyst.robust_design_effect(indep) == pytest.approx(1.0, abs=0.08)
    arm = cluster._ratio_arm(indep)
    p = arm["rate"]
    assert cohort_analyst.robust_design_effect(indep) == pytest.approx(arm["var"] / (p * (1 - p) / arm["n"]))


def _curve(*points):
    return {"by_entrants": [{"entrants": n, "accounts": a, "design_effect": d} for n, a, d in points], "design_effect": points[-1][2],
            "clusters": points[-1][1], "label": "DEMO DATA", "window_months": 5.0}


def test_the_design_effect_is_read_off_the_measured_curve_and_extended_in_proportion():
    cl = _curve((1000, 600, 1.1), (3000, 900, 1.3))
    assert ex.design_effect_at(0, cl) == 1.0
    assert ex.design_effect_at(500, cl) == pytest.approx(1.05)
    assert ex.design_effect_at(2000, cl) == pytest.approx(1.2)
    assert ex.design_effect_at(6000, cl) == pytest.approx(1.6)        # past the window: the excess grows with entrants
    assert ex.design_effect_at(3000, cl, top=1.6) == pytest.approx(1.6)


def test_the_read_is_the_first_month_the_holdout_is_big_enough_for_its_own_design_effect():
    cl = _curve((1000, 600, 1.1), (3000, 900, 1.3))
    feas, deff, _m, note = ex.account_design(800, 0.15, 0.25, 0.25, 30, cl)
    t = feas["required_holdout_n"] / (800 * 0.15)
    assert deff == pytest.approx(ex.design_effect_at(800 * t, cl), abs=0.01)
    sooner = t - 0.1
    need = stats.feasibility(800, 0.15, 0.25, mde_rel=0.25, outcome_window_days=30,
                             design_effect=ex.design_effect_at(800 * sooner, cl))["required_holdout_n"]
    assert need > 800 * 0.15 * sooner                                  # a tenth of a month sooner is not enough
    assert "Accounts cap" not in note


def test_near_the_cap_the_scan_does_not_stop_early():
    """The reviewer's case: the fixed-point iteration stopped on a small step and called a directional test causal."""
    cl = _curve((5000 * 5.62, 1100, 1 + 52 * 0.2))
    feas, deff, _m, _note = ex.account_design(5000, 0.15, 0.2919, 0.25, 30, cl)
    t = feas["required_holdout_n"] / (5000 * 0.15)
    exact = stats.feasibility(5000, 0.15, 0.2919, mde_rel=0.25, outcome_window_days=30, design_effect=ex.design_effect_at(5000 * t, cl))
    assert exact["required_holdout_n"] <= 5000 * 0.15 * t + 1e-6
    assert feas["expected_read"] == exact["expected_read"]


def test_heavy_clustering_caps_the_test_instead_of_promising_a_date():
    cl = _curve((300, 20, 4.0), (1500, 40, 16.0))                     # accounts that behave as one, each sending many entrants
    feas, _deff, _m, note = ex.account_design(300, 0.15, 0.25, 0.25, 30, cl)
    assert feas["expected_read"] == "directional only" and feas["months_to_read"] is None
    assert "Accounts cap this test" in note


def test_too_few_repeat_accounts_falls_back_to_an_assumed_design_effect():
    rows = [{"user_id": f"u{i}", "account_id": f"a{i}", "entered_on": date(2026, 7, 1 + i % 28), "matured": 1, "returned": i % 3 == 0,
             "clicked_90": True, "new_30": False} for i in range(300)]
    cl = cohort_analyst._flow_cluster(rows, None, date(2026, 10, 18), "DEMO DATA")
    assert cl["accounts_with_repeat_entrants"] == 0 and "too few" in cl["note"] and "design_effect" not in cl


def test_the_plan_paragraph_names_both_audiences():
    import tempfile
    from pathlib import Path
    from engine.pipeline import orchestrator
    brief = load_brief(DEMO_BRIEF, defaults=workspace.brief_defaults(workspace.Workspace(DEMO)))
    plan = orchestrator.run(brief, str(DEMO / "data"), copy_mode="offline")
    with tempfile.TemporaryDirectory() as tmp:
        md = (Path(orchestrator.write_outputs(plan, Path(tmp))) / "plan.md").read_text()
    assert "who qualify, the AMBER gate lets in those who clicked or replied in 90 days" in md
    assert "| Holdout | 15%, account level: persistent, by account" in md


def test_a_conversion_is_worth_a_seat_when_people_belong_to_accounts():
    con = cohort_analyst.connect(str(DEMO / "data"))
    as_of, _ = cohort_analyst.latest_complete_day(con)
    seat, label = priorities.value_and_basis(con, as_of)
    assert label == priorities.VALUE_PER_SEAT
    total, payers = con.execute(
        f"SELECT SUM(CAST(net_amount AS DOUBLE)), COUNT(DISTINCT user_id) FROM orders WHERE status = 'completed' "
        f"AND CAST(order_at AS DATE) > DATE '{as_of}' - 90 AND CAST(order_at AS DATE) <= DATE '{as_of}'").fetchone()
    assert seat < total / payers / 3          # less than a whole account's monthly bill
    small = cohort_analyst.connect(str(SMALL_DATA))
    assert priorities.value_and_basis(small, cohort_analyst.latest_complete_day(small)[0])[1] == priorities.VALUE_PER_CUSTOMER


def test_without_account_ids_the_holdout_falls_back_to_an_assumed_design_effect():
    brief = load_brief(DEMO_BRIEF, defaults=workspace.brief_defaults(workspace.Workspace(DEMO)))
    cohort = cohort_analyst.define(brief, str(SMALL_DATA))
    design = ex.design(brief, cohort)
    assert cohort.cluster.get("basis") != "flow" and "ASSUMED (ICC 0.05" in design.sizing
    assert json.dumps(design.mde_by_horizon)          # the horizon table still renders
