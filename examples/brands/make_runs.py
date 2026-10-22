"""Regenerate the finished runs in each demo workspace, the way tests/test_golden_runs.py checks them.

    python3 examples/brands/make_runs.py

Each run is the workspace's brief, planned against its data with the written copy in the drafts.json beside the brief
(the handoff path a marketer or a Claude chat uses), and the workspace's lmm.yaml for everything the brief leaves out.
The platform build pack (`lcm build`) is written into the run's build/ folder.
"""
from __future__ import annotations

import io
import json
import os
import shutil
from contextlib import redirect_stdout
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from engine import workspace  # noqa: E402
from engine.brief import load_brief  # noqa: E402
from engine.pipeline import orchestrator  # noqa: E402

RUNS = {"ridgeview_saas": ("briefs/winback_14d.yaml", "briefs/winback_14d.drafts.json", "runs/winback_14d"),
        "northfield_dtc": ("briefs/cart_abandon.yaml", "briefs/cart_abandon.drafts.json", "runs/cart_abandon")}


def main() -> int:
    for brand, (brief_path, drafts_path, out) in RUNS.items():
        root = ROOT / "examples" / "brands" / brand
        brief = load_brief(root / brief_path, defaults=workspace.brief_defaults(workspace.Workspace(root)))
        drafts = json.loads((root / drafts_path).read_text(encoding="utf-8"))
        plan = orchestrator.run(brief, str(root / "data"), copy_mode="handoff", drafts=drafts)
        with tempfile.TemporaryDirectory() as tmp:
            written = orchestrator.write_outputs(plan, Path(tmp))
            target = root / out
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(written, target)
        from adapters.destinations.build import cli as build_cli
        with redirect_stdout(io.StringIO()):
            build_cli._cli(["--run", str(target)], env={**os.environ, "LCM_WORKSPACE": str(root)})
        print(f"{brand}: {plan.qa.status} -> {(root / out).relative_to(ROOT)} (with build/)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
