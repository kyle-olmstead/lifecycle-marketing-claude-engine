"""Validate build-pack artifacts against config/esp_artifact_schemas.json, and name the fields its schema marks UNVERIFIED."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

ARTIFACT_SCHEMAS = Path(__file__).resolve().parents[3] / "config" / "esp_artifact_schemas.json"


@lru_cache(maxsize=1)
def schemas() -> Dict[str, Any]:
    return json.loads(ARTIFACT_SCHEMAS.read_text(encoding="utf-8"))


def errors(body: Any, ref: str) -> List[str]:
    import jsonschema
    cfg = schemas()
    if ref not in cfg["$defs"]:
        return [f"no schema {ref} in config/esp_artifact_schemas.json"]
    sub = {"$schema": cfg["$schema"], "$defs": cfg["$defs"], "$ref": f"#/$defs/{ref}"}
    v = jsonschema.Draft202012Validator(sub)
    out = []
    for e in sorted(v.iter_errors(body), key=lambda e: list(map(str, e.path))):
        out.append(f"{'/'.join(map(str, e.path)) or '(body)'}: {e.message[:300]}")
    return out


def _present(body: Any, path: str) -> bool:
    """Whether a dotted x-unverified path (with [] and [type=X] selectors) reaches anything in the body."""
    if path == "(body)":
        return body is not None
    nodes = [body]
    for part in path.split("."):
        m = re.fullmatch(r"(\w+)(\[(?:type=([\w-]+))?\])?", part)
        if not m:
            return False
        name, arr, typ = m.group(1), m.group(2), m.group(3)
        nxt = []
        for n in nodes:
            if isinstance(n, dict) and name in n:
                v = n[name]
                if arr:
                    items = v if isinstance(v, list) else []
                    nxt += [i for i in items if not typ or (isinstance(i, dict) and i.get("type") == typ)]
                else:
                    nxt.append(v)
        nodes = nxt
        if not nodes:
            return False
    return True


def unverified(body: Any, ref: str) -> List[Dict[str, str]]:
    """The schema's x-unverified entries that this body actually uses."""
    d = schemas()["$defs"].get(ref, {})
    return [u for u in d.get("x-unverified", []) if _present(body, u["path"])]


def endpoint(ref: str) -> Dict[str, Any]:
    return schemas()["$defs"].get(ref, {}).get("x-endpoint", {})
