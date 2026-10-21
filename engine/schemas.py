"""Validate documents against the engine's JSON Schemas, with every schema registered and formats really checked.

Two things the plain jsonschema calls in v2 got wrong: a schema that $refs another (the flow spec's typed trigger) needs a
registry, and `format: date-time` is silently ignored unless an optional package is installed, so an event with
`"timestamp": "yesterday"` passed. FORMAT_CHECKER here checks date-time (RFC 3339), date and ISO 8601 durations with no
optional dependency.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from functools import lru_cache
from typing import Any, Dict, List

import jsonschema
from referencing import Registry, Resource

from engine.paths import CONFIG, SCHEMAS

RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$")
DURATION = re.compile(r"^P(?!$)(\d+D)?(T(?=\d)(\d+H)?(\d+M)?)?$")
FORMAT_CHECKER = jsonschema.FormatChecker()


@FORMAT_CHECKER.checks("date-time", raises=ValueError)
def _date_time(value: Any) -> bool:
    if not isinstance(value, str):
        return True
    if not RFC3339.match(value):
        raise ValueError(f"{value!r} is not an RFC 3339 date-time (2026-10-18T05:00:00Z)")
    datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    return True


@FORMAT_CHECKER.checks("date", raises=ValueError)
def _date(value: Any) -> bool:
    if isinstance(value, str):
        date.fromisoformat(value)
    return True


@FORMAT_CHECKER.checks("duration", raises=ValueError)
def _duration(value: Any) -> bool:
    if isinstance(value, str) and not DURATION.match(value):
        raise ValueError(f"{value!r} is not an ISO 8601 duration in days, hours or minutes")
    return True


@lru_cache(maxsize=1)
def registry() -> Registry:
    resources = []
    for path in sorted(SCHEMAS.glob("*.schema.json")) + [CONFIG / "tracking_plan.json"]:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if "$id" in doc:
            resources.append((doc["$id"], Resource.from_contents(doc)))
    return Registry().with_resources(resources)


@lru_cache(maxsize=None)
def load(name: str) -> Dict[str, Any]:
    path = SCHEMAS / name if not name.startswith("config/") else CONFIG / name.split("/", 1)[1]
    return json.loads(path.read_text(encoding="utf-8"))


def validator(schema: Any) -> jsonschema.Draft202012Validator:
    """A validator for a schema name in schemas/ ('flow_spec.schema.json'), 'config/tracking_plan.json', or a schema dict."""
    doc = load(schema) if isinstance(schema, str) else schema
    return jsonschema.Draft202012Validator(doc, registry=registry(), format_checker=FORMAT_CHECKER)


def errors(schema: Any, instance: Any, limit: int = 20) -> List[str]:
    v = validator(schema)
    out = sorted(v.iter_errors(instance), key=lambda e: list(e.path))
    return [f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message}" for e in out[:limit]]
