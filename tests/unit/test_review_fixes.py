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


# ----------------------------------------------------------------------------------------------
# Finding 6: a gate record can lower the gate, never raise it
# ----------------------------------------------------------------------------------------------

GREEN = {"verdict": "GREEN", "assessed_at": "2026-10-20T12:00:00Z", "auth_label": "LIVE PULL", "domain": "x.example",
         "signals": {"complaint_rate": 0.0004, "hard_bounce_rate": 0.002, "authentication_state": "verified"}}


@pytest.mark.parametrize("change,needle", [
    ({"assessed_at": "2031-01-01T00:00:00Z"}, "in the future"),
    ({"auth_label": "SUPPLIED"}, "not observed"),
    ({"critical_findings": ["No DMARC record"]}, "critical findings"),
    ({"signals": {"complaint_rate": 0.0004}}, "lacks the signals"),
])
def test_a_green_record_without_its_evidence_reads_amber(change, needle):
    from datetime import date
    from engine.policy.contracts import effective_gate
    assert effective_gate(GREEN, today=date(2026, 10, 22))[0] == "GREEN"
    verdict, why = effective_gate({**GREEN, **change}, today=date(2026, 10, 22))
    assert verdict == "AMBER" and needle in why


def test_a_supplied_green_plans_as_amber_instead_of_failing():
    b = parse_text("a 3-touch win-back", "Acme", {"sending_gate": {**GREEN, "auth_label": "SUPPLIED"}}, defaults={})
    assert (b.sending_gate.verdict, b.sending_gate.recorded_verdict) == ("AMBER", "GREEN")
    p = orchestrator.run(b, str(DEMO / "data"), copy_mode="offline")
    gate = next(c for c in p.qa.checks if c.name == "sending gate")
    assert gate.status == "warn" and "reads AMBER" in gate.detail


# ----------------------------------------------------------------------------------------------
# Finding 7: flows the engine does not build are refused; synonyms reach the flow they name
# ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("sentence,flow", [
    ("a wishlist reminder", None), ("a cross-sell flow after purchase", None), ("a cross-sell flow after their first order", None),
    ("an upsell to annual plan", None), ("a cart flow", "cart_abandon"), ("recover abandoned baskets", "cart_abandon"),
    ("a flow for people who added to cart but did not buy", "cart_abandon"),
    ("people who viewed a product and left", "browse_abandon"), ("a post-purchase flow with a cross-sell touch", "post_purchase"),
])
def test_flow_names_are_refused_or_mapped(sentence, flow):
    from engine.brief import flow_type_of
    found, refusal = flow_type_of(sentence)
    if flow is None:
        assert found is None and refusal
        with pytest.raises(ValueError, match="not supported yet"):
            parse_text(sentence, "Acme", defaults={})
    else:
        assert found == flow and refusal is None


# ----------------------------------------------------------------------------------------------
# Finding 8: a cap in a sentence is a cap, never a touch count or a lapse
# ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("sentence,flow,touches,cap", [
    ("a win-back flow, no more than 2 emails a week", "winback", 5, (["email"], 2, "P7D")),
    ("a cart flow with sms, max 1 text per day", "cart_abandon", 3, (["sms"], 1, "P1D")),
    ("a 3-touch win-back, one email every 3 days", "winback", 3, (["email"], 1, "P3D")),
])
def test_a_cap_in_the_sentence_is_read_as_a_cap(sentence, flow, touches, cap):
    b = parse_text(sentence, "Acme", defaults={})
    assert (b.flow_type, b.touches, b.churn_threshold_days) == (flow, touches, 14)
    assert cap in [(c.channels, c.max, c.window) for c in b.freq.caps]


@pytest.mark.parametrize("text,caps", [
    ("2 SMS daily", [(["sms"], 2, "P1D")]),
    ("one email every 3 days", [(["email"], 1, "P3D")]),
    ("twice a week", [(["email", "sms", "push"], 2, "P7D")]),
    ("2 emails and 1 text a day", [(["email"], 2, "P1D"), (["sms"], 1, "P1D")]),
    ("4 push notifications a week", [(["push"], 4, "P7D")]),
])
def test_cap_text_reads_each_item_and_period(text, caps):
    from engine.policy.contracts import parse_cap_text
    assert [(c.channels, c.max, c.window) for c in parse_cap_text(text)] == caps


# ----------------------------------------------------------------------------------------------
# Finding 9: sentence commands work inside the workspace
# ----------------------------------------------------------------------------------------------

def test_a_sentence_plan_writes_to_runs_and_uses_the_workspace_data(tmp_path, monkeypatch):
    import shutil
    from engine.cli import main as cli
    ws = tmp_path / "nf"
    shutil.copytree(NORTHFIELD, ws, ignore=shutil.ignore_patterns("runs", "out"))
    monkeypatch.chdir(ws)
    monkeypatch.delenv("LCM_WORKSPACE", raising=False)
    job = cli.route("plan a 2-touch browse abandonment flow", {"copy_mode": "offline"})
    assert job.args["out"] == str(ws / "runs") and job.args["data"] == str(ws / "data")
    assert cli.run(job) == 0
    assert (ws / "runs" / "northfield_coffee_browse_abandon_2h" / "plan.md").exists() and not (ws / "out").exists()
    assert "ESTIMATE" in (ws / "runs" / "northfield_coffee_browse_abandon_2h" / "plan.md").read_text()
    newest = cli.newest_run()
    assert newest and "browse_abandon" in newest
    revise = cli.route("make it 3 touches", {})
    assert revise.args["run"] == newest and revise.args["changes"] == {"touches": 3}


def test_plan_text_without_data_uses_the_workspace_data(tmp_path, monkeypatch):
    import shutil
    from engine.pipeline import orchestrator as orch
    ws = tmp_path / "nf"
    shutil.copytree(NORTHFIELD, ws, ignore=shutil.ignore_patterns("runs", "out"))
    monkeypatch.chdir(ws)
    monkeypatch.delenv("LCM_WORKSPACE", raising=False)
    assert orch._cli(["plan", "--text", "a 2-touch abandoned cart flow", "--copy-mode", "offline"]) in (0, 3)
    assert "ESTIMATE" in (ws / "runs" / "northfield_coffee_cart_abandon_1h" / "plan.md").read_text()


def test_lmm_yaml_data_names_the_folder(tmp_path):
    (tmp_path / "lmm.yaml").write_text("brand: {name: Acme}\ndata: exports/current\n")
    (tmp_path / "exports" / "current").mkdir(parents=True)
    (tmp_path / "exports" / "current" / "users.csv").write_text("user_id\nu1\n")
    w = workspace.Workspace(tmp_path)
    assert w.data_dir == tmp_path / "exports" / "current" and w.data() == str(tmp_path / "exports" / "current")


@pytest.mark.parametrize("question,kind", [
    ("how many people are in the win-back flow right now?", "audit"),
    ("how much revenue did the cart flow drive?", "readout"),
    ("how many people do I need for a 20% lift on a 10% baseline?", "size"),
])
def test_marketer_questions_route_to_the_job_that_answers_them(question, kind):
    from engine.cli.main import classify
    assert classify(question) == kind


# ----------------------------------------------------------------------------------------------
# Finding 10: a store's export alone (no message events) does not block a win-back on AMBER
# ----------------------------------------------------------------------------------------------

def test_no_message_events_is_a_warning_that_says_why(tmp_path):
    import shutil
    shutil.copytree(NORTHFIELD / "data", tmp_path / "data")
    (tmp_path / "data" / "message_events.csv").unlink()
    b = parse_text("a 5-touch win-back for customers who have not ordered in 60 days",
                   defaults=workspace.brief_defaults(workspace.Workspace(NORTHFIELD)))
    p = orchestrator.run(b, str(tmp_path / "data"), copy_mode="offline")
    aud = next(c for c in p.qa.checks if c.name == "audience after the gate")
    assert aud.status == "warn" and "no message events" in aud.detail and "lcm import" in aud.detail
    assert p.qa.status != "BLOCKED"


def test_a_button_with_no_deep_link_goes_to_the_brand_website():
    from engine.agents.copywriter import PLACEHOLDER_CTA
    _, p = _plan(NORTHFIELD, "a 3-touch win-back")
    bodies = [body for tc in p.copy_plan for v in tc.variants for k, body in (v.rendered or {}).items() if k.endswith(".html")]
    assert bodies and all(PLACEHOLDER_CTA not in b and "northfieldcoffee.example" in b for b in bodies)


# ----------------------------------------------------------------------------------------------
# Findings 11 and 12: check-data and doctor
# ----------------------------------------------------------------------------------------------

def test_check_data_finds_future_signups_and_negative_amounts(tmp_path):
    import csv
    import shutil
    from engine.data.check import check_folder
    shutil.copytree(NORTHFIELD / "data", tmp_path / "d")
    for name, col, value in (("users", "created_at", "2031-01-01 00:00:00"), ("orders", "net_amount", "-12.50")):
        path = tmp_path / "d" / f"{name}.csv"
        rows = list(csv.DictReader(path.open()))
        rows[0][col] = value
        with path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    found = {(f["check"], f["table"], f["column"]) for f in check_folder(tmp_path / "d")["findings"]}
    assert {("future_event", "users", "created_at"), ("negative_amount", "orders", "net_amount")} <= found
    clean = {f["check"] for f in check_folder(NORTHFIELD / "data")["findings"]}
    assert not clean & {"future_event", "negative_amount"}


def test_doctor_counts_the_tables_files_were_found_for(monkeypatch):
    from engine.cli.commands import diagnose
    monkeypatch.setenv("LCM_WORKSPACE", str(NORTHFIELD))
    row = next(r for r in diagnose() if r["name"] == "data")
    files = [p for p in (NORTHFIELD / "data").glob("*.csv")]
    assert row["detail"].startswith(f"{len(files)} table(s) from files")
