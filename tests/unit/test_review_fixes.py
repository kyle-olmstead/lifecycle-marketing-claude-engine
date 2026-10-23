"""Regression tests for the defects the independent v3 review found, one test per finding."""
from __future__ import annotations

import json
import re

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


# ----------------------------------------------------------------------------------------------
# Finding 3: a store gets a store's plans and copy
# ----------------------------------------------------------------------------------------------

SAAS_WORDS = ("workspace", "data source", "setup", "set up", "whoever needs it", "on the same day each week", "your account and data")


def test_store_winback_plans_on_orders():
    b, p = _plan(NORTHFIELD, "a 3-touch win-back")
    assert (b.qualifying_event.event, b.core_conversion_event.event) == ("order_completed", "order_completed")
    assert b.churn_threshold_days == 60
    assert p.qa.status != "BLOCKED"


@pytest.mark.parametrize("platform", ["klaviyo", "braze", "iterable"])
@pytest.mark.parametrize("sentence", ["a 5-touch win-back with sms", "a 4-touch welcome series", "a 4-touch abandoned cart flow",
                                      "a 3-touch browse abandonment flow", "a 2-touch sunset flow"])
def test_store_copy_says_nothing_about_software(sentence, platform):
    _, p = _plan(NORTHFIELD, sentence, platform=platform)
    words = json.dumps([tc.model_dump() for tc in p.copy_plan]).lower()
    assert not [w for w in SAAS_WORDS if w in words]


def test_item_flows_show_the_items_in_every_email():
    for sentence in ("a 4-touch abandoned cart flow", "a 3-touch browse abandonment flow"):
        _, p = _plan(NORTHFIELD, sentence)
        for tc in p.copy_plan:
            if tc.channel == "email":
                for v in tc.variants:
                    f = v.fields if hasattr(v, "fields") else v
                    assert "{product_block}" in json.dumps(f), (sentence, tc.touch_id if hasattr(tc, "touch_id") else tc)


@pytest.mark.parametrize("root,sentence,needle", [
    (NORTHFIELD, "a 3-touch activation flow", "welcome series"),
    (NORTHFIELD, "a trial conversion flow", "for software"),
    (DEMO, "an abandoned cart flow", "for stores"),
    (DEMO, "a 3-touch post-purchase flow", "for stores"),
])
def test_flows_that_do_not_fit_the_business_are_refused(root, sentence, needle):
    with pytest.raises(ValueError, match=needle):
        _plan(root, sentence)


def test_a_store_new_customer_sentence_is_a_welcome_series():
    b = parse_text("Customers who have not ordered yet after signing up: 3 emails",
                   defaults=workspace.brief_defaults(workspace.Workspace(NORTHFIELD)))
    assert (b.flow_type, b.core_conversion_event.event) == ("welcome", "order_completed")


def test_store_retention_is_monthly_on_orders():
    from engine.agents import retention
    r = retention.curve(str(NORTHFIELD / "data"))
    assert (r["unit"], r["core_action"]) == ("month", "placed an order")
    assert r["segments"]["all"]["points"][0]["rate"] > 0.1


def test_store_priorities_never_propose_software_flows(tmp_path):
    from engine.agents import priorities
    r = priorities.run(str(NORTHFIELD / "data"), out=tmp_path)
    text = json.dumps(r, default=str)
    assert "Win-back" in text and "Activation" not in text and "created a report" not in text


# ----------------------------------------------------------------------------------------------
# Finding 4: cart and browse buttons always go back to that cart or product
# ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("platform", ["klaviyo", "hubspot", "braze", "customerio", "sfmc", "iterable"])
@pytest.mark.parametrize("flow", ["abandoned cart", "browse abandonment"])
@pytest.mark.parametrize("cta", [None, "https://shop.example/help"])
def test_item_flow_buttons_return_to_the_trigger(platform, flow, cta):
    from engine.agents.copywriter import PLACEHOLDER_CTA
    b = parse_text(f"a 2-touch {flow} flow on {platform}", product_name="Acme",
                   overrides={"cta_url": cta} if cta else None, defaults={})
    p = orchestrator.run(b, None, copy_mode="offline")
    prop = "checkout_url" if flow == "abandoned cart" else ("URL" if platform == "klaviyo" else "url")
    for tc in p.copy_plan:
        for v in tc.variants:
            for name, body in (v.rendered or {}).items():
                if name.endswith(".html"):
                    assert PLACEHOLDER_CTA not in body
                    hrefs = re.findall(r"href=(?:'([^']*)'|\"([^\"]*)\")", body)
                    if flow == "abandoned cart":
                        assert any("checkout_url" in (a or b) for a, b in hrefs), hrefs
                    else:
                        assert any(prop in (a or b) for a, b in hrefs), hrefs


# ----------------------------------------------------------------------------------------------
# Finding 5: the program simulator checks every group of flows, and a broken cap needs attention
# ----------------------------------------------------------------------------------------------

def test_three_flows_that_pass_in_pairs_still_break_the_cap_together(tmp_path):
    from engine.brief import to_yaml
    from engine.policy import program
    from engine.policy.contracts import Cap, FrequencyPolicy
    over = {"platform": "klaviyo"}
    briefs = [parse_text(s, "Acme", dict(over), defaults={}) for s in
              ("a 1-touch abandoned cart flow", "a 1-touch browse abandonment flow", "a 2-touch welcome series")]
    policy = FrequencyPolicy(caps=[Cap(channels=["email"], max=3, window="P7D")])
    r = program.simulate(briefs, policy)
    cross = next(c for c in r["checks"] if c["name"].startswith("caps hold across flows"))
    assert cross["status"] == "warn" and "4 in P7D" in cross["detail"]
    assert all(len(s["flows"]) == 3 for s in r["scenarios"] if s["with_rule"])      # no pair breaks it
    assert "WARN" in program.render(r) and "NOTE" not in program.render(r)
    for b in briefs:
        (tmp_path / f"{b.slug}.yaml").write_text(to_yaml(b), encoding="utf-8")
    assert program._cli(["--briefs", str(tmp_path)]) == 1
