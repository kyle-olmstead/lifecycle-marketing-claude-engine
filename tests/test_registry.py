"""The experiment registry: schema (valid entries accepted, bad cases rejected), the checks a schema cannot express,
entry_from_design on the experiment agent's real output, and the CLI in a temporary workspace."""
import copy
import json
import os
import subprocess
import sys
from datetime import date

import jsonschema
import pytest

from tests.conftest import ROOT
from engine.measurement import registry as R

SCHEMA = json.loads((ROOT / "schemas/experiment.schema.json").read_text())
V = jsonschema.Draft202012Validator(SCHEMA)


def user_entry(**over):
    e = {
        "experiment_id": "winback_14d_holdout", "flow": "winback_14d", "unit": "user",
        "assignment": {"hash_field": "user_id", "salt": "winback_14d"}, "holdout_share": 0.15,
        "primary_metric": {"event": "order_completed", "window_days": 60},
        "plan": {"baseline": 0.10, "mde_abs": 0.02, "n_per_arm": {"holdout": 3000, "treatment": 17000}, "design_effect": 1.0,
                 "read_on": "2027-05-10"},
        "stopping_rule": {"design": "fixed_horizon", "looks": 1, "information_fractions": [1.0]},
        "status": "running", "launched_on": "2026-11-02", "looks_taken": [],
    }
    e.update(over)
    return e


def account_entry(**over):
    e = user_entry(experiment_id="trial_accounts", flow="trial_conversion", unit="account",
                   assignment={"hash_field": "account_id", "salt": "trial_conversion"})
    e["plan"] = dict(e["plan"], design_effect=1.42, icc=0.06, mean_cluster_size=8)
    e.update(over)
    return e


def test_schema_is_valid_2020_12_and_accepts_good_entries():
    jsonschema.Draft202012Validator.check_schema(SCHEMA)
    gs = user_entry(experiment_id="onb_gs", stopping_rule={"design": "group_sequential", "looks": 3,
                                                           "information_fractions": [0.33, 0.67, 1.0], "spending": "obf"})
    av = user_entry(experiment_id="nurture_av", stopping_rule={"design": "always_valid", "tau": 0.02})
    for e in (user_entry(), account_entry(), gs, av):
        assert not list(V.iter_errors(e)), [x.message for x in V.iter_errors(e)]


@pytest.mark.parametrize("mutate, needle", [
    (lambda e: e["assignment"].update(hash_field="user_id"), "account_id"),          # account-level test hashing people
    (lambda e: e["plan"].pop("design_effect"), "design_effect"),                      # account-level test with no design effect
    (lambda e: e.pop("stopping_rule"), "stopping_rule"),                              # no stopping rule
    (lambda e: e["stopping_rule"].update(design="group_sequential", looks=3), "information_fractions"),
    (lambda e: e["stopping_rule"].update(looks=2), "1 was expected"),                 # fixed horizon has one look
    (lambda e: e["stopping_rule"].update(design="always_valid"), "tau"),
    (lambda e: e.update(holdout_share=0.0), "holdout_share"),
])
def test_schema_rejects_the_bad_cases(mutate, needle):
    e = account_entry()
    mutate(e)
    errors = R.validate_entry(e)
    assert errors and any(needle in x for x in errors), errors


def test_user_entry_hashing_account_id_is_rejected():
    e = user_entry(assignment={"hash_field": "account_id", "salt": "x"})
    assert any("user_id" in x for x in R.validate_entry(e))


def test_peeking_is_flagged_but_guardrail_checks_are_not():
    e = user_entry(looks_taken=[{"at": "2026-12-01", "kind": "guardrail_only"}])
    assert not [i for i in R.check_entry(e, date(2026, 12, 2)) if i["code"] == "peeking"]
    e["looks_taken"].append({"at": "2026-12-15", "kind": "primary", "z": 2.3})
    peek = [i for i in R.check_entry(e, date(2026, 12, 16)) if i["code"] == "peeking"]
    assert peek and peek[0]["severity"] == "error" and "2026-12-15" in peek[0]["message"]
    on_time = user_entry(looks_taken=[{"at": "2027-05-10", "kind": "primary"}])
    assert not [i for i in R.check_entry(on_time, date(2027, 5, 11)) if i["code"] == "peeking"]


def test_salt_collisions_between_running_experiments():
    a = user_entry()
    b = user_entry(experiment_id="onboarding_holdout", flow="onboarding")
    issues = R.check({"version": 1, "experiments": [a, b]}, date(2026, 11, 3))
    assert {i["experiment_id"] for i in issues if i["code"] == "salt_collision" and i["severity"] == "error"} == {a["experiment_id"], b["experiment_id"]}
    b_disjoint = copy.deepcopy(b)
    b_disjoint["audience"] = {"description": "new signups only", "disjoint_from": [a["experiment_id"]]}
    assert not [i for i in R.check({"version": 1, "experiments": [a, b_disjoint]}, date(2026, 11, 3)) if i["code"] == "salt_collision"]
    b_done = user_entry(experiment_id="onboarding_holdout", flow="onboarding", status="completed")
    assert not [i for i in R.check({"version": 1, "experiments": [a, b_done]}, date(2026, 11, 3)) if i["code"] == "salt_collision"]


def test_other_checks():
    from engine.measurement.global_holdout import GLOBAL_SALT
    g = user_entry(assignment={"hash_field": "user_id", "salt": GLOBAL_SALT})
    assert any(i["code"] == "global_salt" for i in R.check_entry(g, date(2026, 11, 3)))
    small = user_entry(plan={"baseline": 0.01, "mde_abs": 0.03, "n_per_arm": {"holdout": 267, "treatment": 2403}, "read_on": "2027-01-01"})
    assert any(i["code"] == "readout_floor" for i in R.check_entry(small, date(2026, 11, 3)))
    flat = account_entry()
    flat["plan"].update(design_effect=1.0, icc=None)
    assert any(i["code"] == "design_effect" for i in R.check_entry(flat, date(2026, 11, 3)))
    gs = user_entry(stopping_rule={"design": "group_sequential", "looks": 2, "information_fractions": [0.5, 1.0], "spending": "pocock"},
                    looks_taken=[{"at": f"2026-12-0{d}", "kind": "primary", "information_fraction": f} for d, f in ((1, 0.3), (2, 0.6), (3, 0.9))])
    assert any(i["code"] == "too_many_looks" for i in R.check_entry(gs, date(2026, 12, 4)))
    overdue = user_entry()
    assert any(i["code"] == "overdue" for i in R.check_entry(overdue, date(2027, 6, 1)))


def test_entry_from_the_experiment_agents_real_design():
    from engine.agents import cohort_analyst, experimentation_agent
    from engine.brief import load_brief
    brief = load_brief(ROOT / "examples/brands/ridgeview_saas/briefs/winback_14d.yaml")
    c = cohort_analyst.define(brief)
    c = c.model_copy(update={"monthly_entrants": c.monthly_entrants.model_copy(update={"value": 5000, "label": "SUPPLIED"}),
                             "baseline_conversion_rate": c.baseline_conversion_rate.model_copy(update={"value": 0.10, "label": "SUPPLIED"})})
    d = experimentation_agent.design(brief, c).model_dump()
    e = R.entry_from_design(d, brief.slug, metric_event=brief.core_conversion_event.event, launched_on="2026-11-02")
    assert not R.validate_entry(e)
    assert e["assignment"] == {"hash_field": "user_id", "salt": d["salt"], "rule": d["assignment_rule"]}
    assert e["plan"]["n_per_arm"]["holdout"] == d["feasibility"]["required_holdout_n"]
    assert e["plan"]["read_on"] == R.read_on_from("2026-11-02", d["feasibility"]["months_to_read"])
    assert e["holdout_share"] == d["holdout_pct"] / 100 and e["primary_metric"]["window_days"] == d["outcome_window_days"]
    acct = dict(d, unit="account", feasibility=dict(d["feasibility"], design_effect=1.3))
    ea = R.entry_from_design(acct, brief.slug, metric_event="x", icc=0.04, mean_cluster_size=8.5)
    assert ea["assignment"]["hash_field"] == "account_id" and ea["plan"]["design_effect"] == 1.3 and not R.validate_entry(ea)
    assert R.entry_from_design(dict(d, holdout_pct=0), brief.slug) is None


def test_add_and_record_look_through_the_stopping_rules(tmp_path):
    path = tmp_path / "registry" / "experiments.json"
    R.add(user_entry(status="draft"), path, date(2026, 11, 3))
    with pytest.raises(R.RegistryError, match="already registered"):
        R.add(user_entry(), path)
    out = R.record_look("winback_14d_holdout", path, date(2026, 12, 16), kind="primary", at="2026-12-15", x_t=500, n_t=5000, x_c=80, n_c=880)
    assert out["look"]["decision"] == "peek before the read date" and any(i["code"] == "peeking" for i in out["issues"])
    gs = user_entry(experiment_id="onb_gs", assignment={"hash_field": "user_id", "salt": "onboarding"},
                    stopping_rule={"design": "group_sequential", "looks": 3, "information_fractions": [0.33, 0.67, 1.0], "spending": "obf"})
    R.add(gs, path)
    first = R.record_look("onb_gs", path, kind="primary", at="2026-12-01", x_t=1400, n_t=6000, x_c=220, n_c=1000)["look"]
    assert first["decision"] == "continue" and first["counted"] and 0.3 < first["information_fraction"] < 0.4
    second = R.record_look("onb_gs", path, kind="primary", at="2027-01-05", x_t=3600, n_t=12000, x_c=470, n_c=2000)
    assert second["look"]["decision"] == "stop for efficacy" and second["status"] == "stopped"
    av = user_entry(experiment_id="nurture_av", assignment={"hash_field": "user_id", "salt": "nurture"},
                    stopping_rule={"design": "always_valid", "tau": 0.02})
    R.add(av, path)
    a1 = R.record_look("nurture_av", path, at="2026-12-01", x_t=600, n_t=5000, x_c=100, n_c=1000)["look"]
    a2 = R.record_look("nurture_av", path, at="2026-12-08", x_t=1200, n_t=10000, x_c=205, n_c=2000)["look"]
    assert a2["p_value"] <= a1["p_value"] and a2["confidence_sequence"][1] <= a1["confidence_sequence"][1]
    doc = R.load(path)
    assert [e["experiment_id"] for e in doc["experiments"]] == ["winback_14d_holdout", "onb_gs", "nurture_av"]
    assert all(not R.validate_entry(e) for e in doc["experiments"])


def _env_without_workspace():
    env = dict(os.environ)
    env.pop("LCM_WORKSPACE", None)
    return env


def test_registry_cli_in_a_workspace(tmp_path):
    (tmp_path / "lmm.yaml").write_text("name: test brand\n")
    (tmp_path / "entry.json").write_text(json.dumps(user_entry(status="draft")))
    cmd = [sys.executable, str(ROOT / "engine/measurement/registry.py"), "--workspace", str(tmp_path)]
    r = subprocess.run(cmd + ["add", str(tmp_path / "entry.json")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "registry" / "experiments.json").exists()
    r = subprocess.run(cmd + ["list"], capture_output=True, text=True)
    assert r.returncode == 0 and "winback_14d_holdout" in r.stdout and "fixed_horizon" in r.stdout
    r = subprocess.run(cmd + ["record-look", "winback_14d_holdout", "--kind", "guardrail_only", "--at", "2026-12-01", "--nt", "5000", "--nc", "880"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    r = subprocess.run(cmd + ["check", "--today", "2026-12-02"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
    r = subprocess.run(cmd + ["record-look", "winback_14d_holdout", "--at", "2026-12-15", "--xt", "500", "--nt", "5000", "--xc", "80", "--nc", "880"],
                       capture_output=True, text=True)
    assert r.returncode == 1 and "peeking" in r.stdout
    r = subprocess.run(cmd + ["check", "--today", "2026-12-16"], capture_output=True, text=True)
    assert r.returncode == 1 and "[peeking]" in r.stdout
    # found from the working directory's lmm.yaml without --workspace
    r = subprocess.run([sys.executable, str(ROOT / "engine/measurement/registry.py"), "list"], cwd=tmp_path, capture_output=True, text=True,
                       env=_env_without_workspace())
    assert r.returncode == 0 and "winback_14d_holdout" in r.stdout


def test_registry_refuses_to_write_into_the_demo_workspace(tmp_path):
    (tmp_path / "entry.json").write_text(json.dumps(user_entry()))
    r = subprocess.run([sys.executable, str(ROOT / "engine/measurement/registry.py"), "add", str(tmp_path / "entry.json")],
                       cwd=tmp_path, capture_output=True, text=True, env=_env_without_workspace())
    assert r.returncode == 2 and "read-only" in r.stderr
    assert not (ROOT / "examples/brands/ridgeview_saas/registry").exists()
