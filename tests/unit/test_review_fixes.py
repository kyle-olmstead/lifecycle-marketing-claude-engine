"""Regression tests for the defects the independent v3 review found, one test per finding."""
from __future__ import annotations

import pytest

from engine import workspace
from engine.agents import cohort_analyst
from engine.brief import parse_text
from engine.paths import DEMO, NORTHFIELD
from engine.pipeline import orchestrator


def _plan(root, sentence, **overrides):
    defaults = workspace.brief_defaults(workspace.Workspace(root))
    brief = parse_text(sentence, overrides=overrides or None, defaults=defaults)
    return brief, orchestrator.run(brief, str(root / "data"), copy_mode="offline")


@pytest.mark.parametrize("root", [DEMO, NORTHFIELD])
def test_account_holdout_plans_with_and_without_data(root):
    """Finding 1: an account-level holdout crashed the experiment stage, with data and without."""
    sentence = "a 3-touch win-back" if root == DEMO else "a 3-touch abandoned cart flow"
    _, p = _plan(root, sentence, randomization_unit="account")
    assert p.experiment is not None and p.experiment.hash_field == "account_id"
    assert "design effect" in p.experiment.sizing


@pytest.mark.parametrize("accounts", [3, 40])
def test_account_holdout_with_accounts_in_the_data(tmp_path, accounts):
    """With account_id in the data, many accounts measure the ICC and few fall back to an assumed design effect."""
    import csv
    import shutil
    shutil.copytree(DEMO / "data", tmp_path / "data")
    users = tmp_path / "data" / "users.csv"
    rows = list(csv.DictReader(users.open()))
    for i, r in enumerate(rows):
        r["account_id"] = f"a{i % accounts}"
    with users.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    b = parse_text("a 3-touch win-back", overrides={"randomization_unit": "account"},
                   defaults=workspace.brief_defaults(workspace.Workspace(DEMO)))
    p = orchestrator.run(b, str(tmp_path / "data"), copy_mode="offline")
    assert "design effect" in p.experiment.sizing


def test_account_holdout_without_data():
    b = parse_text("a 3-touch win-back for Ridgeview", overrides={"randomization_unit": "account"})
    p = orchestrator.run(b, None, copy_mode="offline")
    assert p.experiment.hash_field == "account_id"


@pytest.mark.parametrize("flow", ["welcome", "trial_convert", "activation"])
def test_new_qualifier_flows_use_the_new_qualifier_sql(flow):
    """Finding 2: welcome and trial conversion got the win-back lapse query."""
    sentence = {"welcome": "a 4-touch welcome series for new signups", "trial_convert": "a trial conversion flow",
                "activation": "a 3-touch activation flow"}[flow]
    b = parse_text(sentence, defaults=workspace.brief_defaults(workspace.Workspace(DEMO)))
    assert b.flow_type == flow
    for d in ("postgres", "snowflake", "bigquery"):
        sql = cohort_analyst.entry_sql(b, d)
        assert "since the last core action" not in sql
        assert "qualified on the run date" in sql


@pytest.mark.parametrize("root,low,high", [(DEMO, 0.15, 0.40), (NORTHFIELD, 0.20, 0.35)])
def test_welcome_baseline_counts_new_signups(root, low, high):
    _, p = _plan(root, "a 4-touch welcome series for new signups")
    rate = p.cohort.baseline_conversion_rate.value
    assert rate is not None and low < rate < high, rate
