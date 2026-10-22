"""Which event fields a template reads, and whether the tracking plan says the trigger event carries them.

A template that reads event.items[].image_url when the event never sends image_url renders an empty image on every
send, and no linter that only checks syntax will notice. This compares the event fields a template references (in
Customer.io, Braze, Klaviyo and Iterable Handlebars syntax, for the engine's own events) with config/tracking_plan.json:

    from engine.render import fields
    fields.unknown(template_text, "customerio", "checkout_started")   # -> ["items[].colour"] when the plan has no colour

Fields Shopify's own Klaviyo events carry (event.extra.*, event.Name, event.URL) are Shopify's contract, not the
tracking plan's, and are not checked here.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import List, Optional, Set

from engine.paths import TRACKING_PLAN


@lru_cache(maxsize=1)
def _plan() -> dict:
    return json.loads(TRACKING_PLAN.read_text(encoding="utf-8"))


def allowed(event: str) -> Optional[Set[str]]:
    """The fields the tracking plan lets the event carry: top-level names, and items[].name for each line-item field.
    None when the event is not in the tracking plan (it comes from a warehouse job or a platform, not the event stream)."""
    defs = _plan()["$defs"]
    if event not in defs:
        return None
    props = defs[event].get("properties", {})
    out = set(props)

    def refs(node) -> List[str]:          # every $ref under a property (items, oneOf, anyOf)
        if isinstance(node, dict):
            return ([node["$ref"]] if "$ref" in node else []) + [r for v in node.values() for r in refs(v)]
        if isinstance(node, list):
            return [r for v in node for r in refs(v)]
        return []
    for k, v in props.items():
        if any(r.endswith("/line_item") for r in refs(v)):
            out |= {f"{k}[].{f}" for f in defs["line_item"]["properties"]}
    return out


def referenced(text: str, dialect: str) -> Set[str]:
    """Event fields the template reads, as 'field' or 'items[].field'."""
    out: Set[str] = set()
    if dialect in ("customerio", "klaviyo"):
        for m in re.finditer(r"\bevent\.(\w+)(?:\.(first|0)\.(\w+))?", text):
            if m.group(1) in ("extra", "Name", "URL", "ImageURL", "Price"):       # Shopify's own Klaviyo event fields
                continue
            out.add(f"{m.group(1)}[].{m.group(3)}" if m.group(3) else m.group(1))
        loops = re.findall(r"\{%-?\s*for\s+(\w+)\s+in\s+event\.(\w+)", text)
    elif dialect == "braze":
        for m in re.finditer(r"context\.\$\{(\w+)\}(?:\[0\]\.(\w+))?", text):
            out.add(f"{m.group(1)}[].{m.group(2)}" if m.group(2) else m.group(1))
        loops = re.findall(r"\{%-?\s*for\s+(\w+)\s+in\s+\{\{\s*context\.\$\{(\w+)\}\s*\}\}", text)
    elif dialect == "handlebars":
        return _handlebars(text)
    else:
        return out
    for var, coll in loops:
        out |= {f"{coll}[].{f}" for f in re.findall(rf"\b{var}\.(\w+)", text)}
    return out


# Iterable profile fields and built-in merge tags the copywriter writes: not event fields (an event field with the same name
# would win over the profile's, so the engine never sends one called firstName)
HANDLEBARS_NOT_EVENT = {"firstName", "lastName", "email", "unsubscribeUrl", "unsubscribeMessageTypeUrl", "hostedUnsubscribeUrl",
                        "unsubscribeByPhoneUrl", "physicalAddress", "companyName", "viewInBrowserUrl", "this"}


def _handlebars(text: str) -> Set[str]:
    """Iterable: a journey's triggering event fields are top-level names; inside {{#each items}} they are line-item fields."""
    from engine.render.template_lint import HB_TOKEN
    out: Set[str] = set()
    stack: List[str] = []
    for m in HB_TOKEN.finditer(text):
        if m.group(2) is not None:
            continue
        expr = (m.group(1) if m.group(1) is not None else m.group(3)).strip().strip("~").strip()
        if expr.startswith(("#", "^")):
            words = expr[1:].split()
            stack.append(words[1] if words and words[0] == "each" and len(words) > 1 else "")
            continue
        if expr.startswith("/"):
            if stack:
                stack.pop()
            continue
        if not expr or expr.startswith(("!", ">", "else")):
            continue
        words = expr.split()
        name = words[1] if words[0] == "defaultIfEmpty" and len(words) > 1 else (words[0] if len(words) == 1 else "")
        if not name or name.startswith(("@", '"', "'", "profile.", "../")) or name in HANDLEBARS_NOT_EVENT:
            continue
        loop = next((c for c in reversed(stack) if c), None)
        idx = re.match(r"^(\w+)\.\[\d+\]\.(\w+)$", name)
        if idx:
            out.add(f"{idx.group(1)}[].{idx.group(2)}")
        elif loop:
            out.add(f"{loop}[].{name.split('.')[0]}")
        else:
            out.add(name.split(".")[0])
    return out


def unknown(text: str, dialect: str, event: str) -> List[str]:
    """Fields the template reads that the tracking plan does not give the event (empty when the event is not in the plan)."""
    ok = allowed(event)
    if ok is None:
        return []
    return sorted(f for f in referenced(text, dialect) if f not in ok)
