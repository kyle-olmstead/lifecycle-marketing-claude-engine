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

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from engine.paths import DEMO

WORKSPACE_FILE = "lmm.yaml"


@dataclass(frozen=True)
class Workspace:
    root: Path
    demo: bool = False
    fallback: bool = False      # no workspace was found or named; the bundled demo stands in and supplies no brief defaults

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
    return Workspace(DEMO, demo=True, fallback=True)


# ----------------------------------------------------------------------------------------------
# what a brief inherits from the workspace
# ----------------------------------------------------------------------------------------------

def brief_defaults(ws: Optional[Workspace] = None) -> Dict[str, Any]:
    """Brief fields this workspace supplies, from lmm.yaml. A brief's own value always wins; these replace the engine's
    built-in defaults, so a plan never says 'platform not stated' when the workspace states it."""
    ws = ws or find()
    cfg = {} if ws.fallback else ws.config()
    if not cfg:
        return {}
    out: Dict[str, Any] = {}
    brand = cfg.get("brand") or {}
    for src, dst in (("name", "product_name"), ("business_model", "business_model"), ("postal_address", "postal_address"),
                     ("sender_name", "sender_name"), ("brand_voice", "brand_voice"), ("recipient_countries", "recipient_countries")):
        if brand.get(src) not in (None, "", []):
            out[dst] = brand[src]
    for key in ("platform", "warehouse_dialect", "frequency", "offer_policy"):
        if cfg.get(key) not in (None, "", []):
            out[key] = cfg[key]
    gate = cfg.get("sending_gate")
    if isinstance(gate, str) and gate.lower().endswith(".json"):
        path = (ws.root / gate) if not Path(gate).is_absolute() else Path(gate)
        if not path.exists():
            out["sending_gate"] = "UNKNOWN"
        else:
            try:
                out["sending_gate"] = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}: not a gate record ({e.msg}); write one with lcm deliverability check DOMAIN --write-gate {gate}") from None
    elif gate not in (None, ""):
        out["sending_gate"] = gate
    if cfg.get("brand_kit"):
        out["brand_kit"] = str((ws.root / cfg["brand_kit"]).resolve())
    m = cfg.get("measurement") or {}
    for key in ("randomization_unit", "global_holdout_pct", "stopping_rule"):
        if m.get(key) not in (None, ""):
            out[key] = m[key]
    return out


def validate(ws: Optional[Workspace] = None) -> List[str]:
    """Problems with lmm.yaml against schemas/workspace.schema.json (an empty list when it is valid or absent)."""
    from engine import schemas
    ws = ws or find()
    if not ws.config_path.exists():
        return []
    return schemas.errors("workspace.schema.json", ws.config())


def init(folder: Path, name: str, platform: Optional[str] = None, business_model: Optional[str] = None) -> List[Path]:
    """Create a workspace: lmm.yaml and brand.yaml from the commented templates, plus data/, briefs/active/ and runs/."""
    from engine.paths import TEMPLATES
    folder = Path(folder)
    if (folder / WORKSPACE_FILE).exists():
        raise FileExistsError(f"{folder / WORKSPACE_FILE} already exists; edit it instead")
    folder.mkdir(parents=True, exist_ok=True)
    quoted = json.dumps(name, ensure_ascii=False)          # a YAML double-quoted scalar: safe for "Acme: Co" or "#1 Coffee"
    text = (TEMPLATES / "workspace" / "lmm.yaml").read_text(encoding="utf-8").replace("name: Acme", f"name: {quoted}", 1)
    if platform:
        text = text.replace("platform: klaviyo ", f"platform: {platform} ", 1)
    if business_model:
        text = text.replace("business_model: ecommerce ", f"business_model: {business_model} ", 1)
    created = [folder / WORKSPACE_FILE]
    (folder / WORKSPACE_FILE).write_text(text, encoding="utf-8")
    brand_t = (TEMPLATES / "brand" / "brand.yaml").read_text(encoding="utf-8")
    brand_t = re.sub(r"^brand_name:.*$", lambda m: f"brand_name: {quoted}", brand_t, count=1, flags=re.M)
    (folder / "brand.yaml").write_text(brand_t, encoding="utf-8")
    created.append(folder / "brand.yaml")
    for sub, readme in (("data", "Put your canonical tables here (users.csv, track_events.csv, orders.csv, ...; see "
                                 "engine/data/schema.py), or map a vendor export with `lcm import <source> <export folder> --out data`. "
                                 "Check them with `lcm check-data data`.\n"),
                        ("briefs/active", "Copy the briefs of flows that are live into this folder. The daily check audits them, the "
                                          "program simulator checks their caps together, and priorities knows not to suggest them.\n"),
                        ("runs", "Every plan the engine writes lands here, resumable and revisable.\n")):
        (folder / sub).mkdir(parents=True, exist_ok=True)
        (folder / sub / "README.md").write_text(readme, encoding="utf-8")
        created.append(folder / sub / "README.md")
    return created


def apply_defaults(data: Dict[str, Any], assumed: set, defaults: Optional[Dict[str, Any]] = None) -> List[str]:
    """Fill a brief's unstated or assumed fields from the workspace. Returns the fields it filled, so the caller drops
    their recorded assumptions: a value the workspace states is not a guess."""
    defaults = brief_defaults() if defaults is None else defaults
    filled = []
    for k, v in defaults.items():
        if k not in data or k in assumed:
            data[k] = v
            filled.append(k)
    return filled
