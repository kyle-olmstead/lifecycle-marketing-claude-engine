"""Golden runs: the finished examples in each demo workspace are what the engine writes today, with no fill-ins left.

Each one is a brief, written copy (drafts.json, the handoff path a marketer or a Claude chat would use) and the
workspace's lmm.yaml, planned against the workspace's data. A change to the engine that changes these plans shows up
here, and has to be regenerated on purpose."""
import json
import re

import pytest

from engine import workspace
from engine.brief import load_brief
from engine.pipeline import orchestrator
from tests.conftest import ROOT

RUNS = {
    "ridgeview_saas": ("briefs/winback_14d.yaml", "briefs/winback_14d.drafts.json", "runs/winback_14d"),
    "northfield_dtc": ("briefs/cart_abandon.yaml", "briefs/cart_abandon.drafts.json", "runs/cart_abandon"),
}


@pytest.fixture(scope="module", params=sorted(RUNS))
def golden(request):
    brand = request.param
    root = ROOT / "examples" / "brands" / brand
    brief_path, drafts_path, committed = RUNS[brand]
    brief = load_brief(root / brief_path, defaults=workspace.brief_defaults(workspace.Workspace(root)))
    drafts = json.loads((root / drafts_path).read_text())
    plan = orchestrator.run(brief, str(root / "data"), copy_mode="handoff", drafts=drafts)
    return brand, plan, root / committed


def test_the_committed_flow_and_simulation_match(golden):
    brand, plan, committed = golden
    assert json.loads((committed / "flow_spec.json").read_text()) == plan.flow.flow_spec, f"regenerate examples/brands/{brand}"
    assert json.loads((committed / "simulation.json").read_text()) == plan.flow.simulation


def test_the_copy_is_finished(golden):
    brand, plan, committed = golden
    copy_checks = [c for c in plan.qa.checks if c.name.startswith("copy")]
    assert copy_checks and all(c.status == "pass" for c in copy_checks), [(c.name, c.detail) for c in copy_checks if c.status != "pass"]
    for f in (committed / "copy").iterdir():
        text = f.read_text(encoding="utf-8")
        assert chr(0x2014) not in text and chr(0x2013) not in text, f.name
        if f.suffix not in (".txt", ".json"):        # the words: plain text, subject lines, SMS, push and in-app (not CSS)
            continue
        visible = re.sub(r"\{\{.*?\}\}|\{%.*?%\}|%%.*?%%|\$\{.*?\}|<[^>]+>", " ", text, flags=re.S)
        assert not re.search(r"\[[A-Za-z][^\]]{2,}\]", visible), (f.name, re.search(r"\[[A-Za-z][^\]]{2,}\]", visible).group(0))


def test_the_plan_is_not_blocked_and_every_step_has_two_variants(golden):
    brand, plan, committed = golden
    assert plan.qa.status != "BLOCKED"
    assert all(len(tc.variants) == 2 for tc in plan.copy_plan), [(tc.step_id, len(tc.variants)) for tc in plan.copy_plan]
    md = (committed / "plan.md").read_text(encoding="utf-8")
    assert md.startswith("# ") and "## Measurement" in md and "## QA" in md
