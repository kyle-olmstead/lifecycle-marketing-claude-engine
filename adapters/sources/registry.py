"""Finds the source mappers: every adapters/sources/<name>.py that defines NAME, DESCRIPTION, detect(folder) and
map(folder, out_dir).

Why discovery instead of a hand-kept list: a new mapper is one file dropped into adapters/sources/, and `lcm import`
offers it at once, with nothing else to edit (and two people adding mappers in parallel never touch the same line).

    from adapters.sources import registry
    registry.all()                       # every mapper, sorted by NAME
    registry.get("shopify")              # one mapper module, by NAME or by file name (klaviyo_export)
    registry.detect(Path("exports/"))    # the mappers whose detect() recognizes files in the folder

A module is only imported when its text defines all four names, so helper modules (report.py, export_io.py, cli.py,
webhooks.py) are never loaded by discovery. A mapper that fails to import is skipped and listed in `errors()`.
"""
from __future__ import annotations

import builtins
import importlib
import re
from pathlib import Path
from types import ModuleType
from typing import Dict, List, Optional

HERE = Path(__file__).resolve().parent
_REQUIRED = (re.compile(r"^NAME\s*[:=]", re.M), re.compile(r"^DESCRIPTION\s*[:=]", re.M),
             re.compile(r"^def detect\(", re.M), re.compile(r"^def map\(", re.M))
_cache: Optional[Dict[str, ModuleType]] = None
_errors: Dict[str, str] = {}


def _discover() -> Dict[str, ModuleType]:
    global _cache
    if _cache is not None:
        return _cache
    found: Dict[str, ModuleType] = {}
    for path in sorted(HERE.glob("*.py")):
        if path.name.startswith("_"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not builtins.all(p.search(text) for p in _REQUIRED):
            continue
        try:
            module = importlib.import_module(f"adapters.sources.{path.stem}")
        except Exception as e:  # noqa: BLE001
            _errors[path.stem] = f"{type(e).__name__}: {e}"
            continue
        if builtins.all(hasattr(module, n) for n in ("NAME", "DESCRIPTION", "detect", "map")):
            found[str(module.NAME)] = module
    _cache = dict(sorted(found.items()))
    return _cache


def all() -> List[ModuleType]:  # noqa: A001 (the interface name the CLI and other mappers use)
    """Every mapper module, sorted by NAME."""
    return list(_discover().values())


def names() -> List[str]:
    return list(_discover())


def get(name: str) -> ModuleType:
    """The mapper whose NAME, or whose file name, is `name` (case-insensitive). KeyError names the known ones."""
    key = name.strip().lower()
    for n, module in _discover().items():
        if key in (n.lower(), module.__name__.rsplit(".", 1)[-1].lower()):
            return module
    raise KeyError(f"no source mapper named {name!r}; known: {', '.join(names()) or 'none'}")


def detect(folder: Path) -> List[ModuleType]:
    """The mappers that recognize files in `folder` (searched recursively), sorted by NAME."""
    out = []
    for module in all():
        try:
            if module.detect(Path(folder)):
                out.append(module)
        except Exception as e:  # noqa: BLE001
            _errors[str(module.NAME)] = f"detect failed: {type(e).__name__}: {e}"
    return out


def errors() -> Dict[str, str]:
    """Mappers that could not be imported or whose detect() raised, with the reason."""
    _discover()
    return dict(_errors)
