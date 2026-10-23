"""Workflows: every bundled job graph names real commands, fills its inputs, and runs end to end on a demo workspace."""
import shlex

import pytest

from engine.cli.commands import COMMANDS
from engine.paths import NORTHFIELD, WORKFLOWS
from engine.workflows import runner

BUNDLED = sorted(p.stem for p in WORKFLOWS.glob("*.yaml"))


def test_the_three_blueprint_workflows_exist():
    assert {"morning_check", "new_flow", "quarterly_review"} <= set(BUNDLED)


@pytest.mark.parametrize("name", BUNDLED)
def test_every_step_is_a_real_command_with_help(name):
    wf = runner.load(WORKFLOWS / f"{name}.yaml")
    assert wf.description
    for s in wf.steps:
        cmd = shlex.split(s.run.replace("${", "X").replace("}", ""))[0]
        assert cmd in COMMANDS, (name, s.id, cmd)
        assert s.help, (name, s.id)


def test_a_dry_run_fills_inputs_and_skips_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("LCM_WORKSPACE", str(NORTHFIELD))
    r = runner.run(runner.load(WORKFLOWS / "morning_check.yaml"), {}, tmp_path, dry_run=True)
    steps = {s["id"]: s for s in r["steps"]}
    assert steps["data"]["run"] == f"check-data {NORTHFIELD / 'data'}"
    assert steps["authentication"]["status"] == "skipped" and "domain" in steps["authentication"]["detail"]
    assert r["status"] == "planned" and not (tmp_path / "workflows").exists()


def test_a_bad_workflow_is_refused(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: bad\nsteps:\n  - {id: a, run: doctor, needs: [b]}\n  - {id: b, run: doctor}\n")
    with pytest.raises(ValueError, match="earlier steps"):
        runner.load(bad)
    bad.write_text("name: bad\nsteps:\n  - {id: a, run: doctor, on_fail: explode}\n")
    with pytest.raises(ValueError, match="on_fail"):
        runner.load(bad)


def test_stop_ends_the_run_and_dependents_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("LCM_WORKSPACE", str(NORTHFIELD))
    wf = tmp_path / "w.yaml"
    wf.write_text("name: w\ndescription: t\nsteps:\n"
                  "  - {id: broken, run: check-data /no/such/folder, on_fail: continue}\n"
                  "  - {id: after, run: doctor, needs: [broken]}\n"
                  "  - {id: fatal, run: size --baseline nope, on_fail: stop}\n"
                  "  - {id: never, run: doctor}\n")
    r = runner.run(runner.load(wf), {}, tmp_path)
    s = {x["id"]: x for x in r["steps"]}
    assert s["broken"]["status"] in ("error", "attention")
    assert s["after"]["status"] in ("skipped", "ok")
    assert s["fatal"]["status"] == "error" and s["never"]["status"] == "stopped"
    assert (tmp_path / "workflows" / "w").exists() and r["status"] == "error"


def test_morning_check_runs_on_the_northfield_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("LCM_WORKSPACE", str(NORTHFIELD))
    r = runner.run(runner.load(WORKFLOWS / "morning_check.yaml"), {}, tmp_path)
    s = {x["id"]: x for x in r["steps"]}
    assert s["data"]["status"] == "ok", s["data"]
    assert all(x["status"] != "error" for x in r["steps"]), [(x["id"], x.get("detail")) for x in r["steps"]]
    out = tmp_path / "workflows" / "morning_check"
    run_dir = next(out.iterdir())
    assert (run_dir / "summary.md").exists() and (run_dir / "tracking.txt").read_text().startswith("$ lcm tracking")


def test_a_step_skipped_for_a_missing_input_leaves_the_run_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("LCM_WORKSPACE", str(NORTHFIELD))
    wf = tmp_path / "wf.yaml"
    wf.write_text("name: wf\ninputs:\n  domain: {help: the sending domain}\nsteps:\n"
                  "  - {id: version, run: doctor --json, help: the install}\n"
                  "  - id: auth\n    run: deliverability check ${domain}\n    help: needs a domain\n")
    r = runner.run(runner.load(wf), {}, tmp_path / "out")
    steps = {s["id"]: s for s in r["steps"]}
    assert steps["auth"]["status"] == "skipped" and steps["auth"]["reason"] == "input"
    assert r["status"] == ("ok" if steps["version"]["status"] == "ok" else steps["version"]["status"])
