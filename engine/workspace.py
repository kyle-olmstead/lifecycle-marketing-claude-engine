"""The workspace: one folder per brand that holds everything the engine knows about it, in files code reads.

    my-brand/
      lmm.yaml            the program: platform, warehouse, sending gate, policies, brand kit path (schemas/workspace.schema.json)
      brand.yaml          logo, colors, footer, voice (schemas/brand_kit.schema.json)
      data/               canonical tables (users, orders, track_events, ...) or raw exports for `lcm import`
      briefs/active/      the flows that are live; the daily check, the cross-flow simulator and priorities read them
      runs/               every plan the engine wrote, resumable and revisable

`lcm init` creates one. Commands look for lmm.yaml in the current folder and its parents, or in the folder
named by LCM_WORKSPACE; with neither, they use the bundled demo workspace (examples/brands/ridgeview_saas) and
say so, so nothing reads or writes a folder the user did not choose.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from engine.paths import DEMO

WORKSPACE_FILE = "lmm.yaml"


@dataclass(frozen=True)
class Workspace:
    root: Path
    demo: bool = False

    @property
    def config_path(self) -> Path:
        return self.root / WORKSPACE_FILE

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def active_briefs(self) -> Path:
        return self.root / "briefs" / "active"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    def config(self) -> Dict[str, Any]:
        """The parsed lmm.yaml, or an empty mapping when the workspace has none yet."""
        if not self.config_path.exists():
            return {}
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{self.config_path}: lmm.yaml is a set of 'key: value' lines")
        return raw


def find(start: Optional[Path] = None, env: Optional[Dict[str, str]] = None) -> Workspace:
    """LCM_WORKSPACE, else the nearest folder at or above `start` holding lmm.yaml, else the demo workspace."""
    env = os.environ if env is None else env
    named = env.get("LCM_WORKSPACE")
    if named:
        return Workspace(Path(named).expanduser().resolve())
    here = Path(start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        if (folder / WORKSPACE_FILE).exists():
            return Workspace(folder, demo=folder.resolve() == DEMO.resolve())
    return Workspace(DEMO, demo=True)
