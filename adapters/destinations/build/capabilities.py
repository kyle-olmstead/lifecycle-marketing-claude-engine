"""Capability checks: does this account, on this platform, have what this flow needs?

The checks live in config/esp_capabilities.json, each with the fact behind it, its source URL and its confidence; this
module only evaluates them against a pack. What a flow needs is computed from the pack (a custom-event trigger, an email
step, a frequency cap, a send window, the product block, a create-API artifact, waits shorter than the platform's unit),
and the account comes from an optional dict ({platform, tier, options}, later the workspace's `esp` key in lmm.yaml).

Rules: a requirement that is met passes; one that is unmet fails when it is hard (the pack is BLOCKED with the reason) and
warns otherwise; an unknown tier or option warns, never passes. Advisories warn with the instruction to follow; info
passes with its note.
"""
from __future__ import annotations

from typing import Any, Dict, List, Set

from adapters.destinations.build.context import Ctx, capabilities
from adapters.destinations.build.model import CapabilityCheck


def needs(ctx: Ctx, mode: str, steps: List[Any], artifacts: List[Any]) -> Set[str]:
    """What this pack needs from the platform, as the `when` names the checks use."""
    from engine.render.product_blocks import EVENT_FLOWS
    out = {"always"}
    kinds = {a.kind for a in artifacts}
    if "create_request" in kinds:
        out.add("create_api")
    if "duplicate_request" in kinds:
        out.add("duplicate_request")
    if mode in ("native_event", "warehouse_job") and ctx.platform == "hubspot":
        out.add("custom_event_trigger")
    if mode in ("native_event", "native_segment", "scheduled_segment", "api_event"):
        out.add("native_mode")
    if mode == "warehouse_job":
        out.add("warehouse_job")
    if mode == "scheduled_segment":
        out.add("scheduled_segment")
    if any(s.channel == "email" for s in steps if s.kind == "send"):
        out.add("email_step")
    if any(s.channel in ("sms", "push", "in_app") for s in steps if s.kind == "send"):
        out.add("non_email_step")
    if any(s.channel == "in_app" for s in steps if s.kind == "send"):
        out.add("in_app_step")
    if ctx.spec.get("frequency", {}).get("caps"):
        out.add("frequency_cap")
    if any(s.respect_send_window for s in steps):
        out.add("send_window")
    if ctx.brief.flow.product_block and ctx.flow_type in EVENT_FLOWS:
        out.add("product_block")
    unit = ctx.caps["min_delay"]["value"]["seconds"]
    waits = [s.wait.seconds for s in steps if s.wait and s.wait.seconds]
    if any(w % unit for w in waits):
        out.add("sub_unit_wait")
    if any(w % 3600 for w in waits):
        out.add("sub_hour_wait")
    if any(0 < w < 3600 for w in waits):
        out.add("short_wait")
    return out


def _events(ctx: Ctx, mode: str) -> List[str]:
    """Engine events the platform must receive for this pack to work."""
    ft = ctx.flow_type
    core = ctx.core
    if mode == "warehouse_job":
        return [core]
    if ft == "sunset":
        return []                                        # the platform's own email engagement metrics
    evs = [ctx.qualifying, core] + list(ctx.brief.flow.exclude_events)
    return list(dict.fromkeys(e for e in evs if e))


def evaluate(ctx: Ctx, mode: str, steps: List[Any], artifacts: List[Any]) -> List[CapabilityCheck]:
    caps = capabilities()
    want = needs(ctx, mode, steps, artifacts)
    tier = ctx.account.get("tier")
    opts = ctx.options
    out: List[CapabilityCheck] = []
    for c in caps["checks"]:
        if c["platform"] not in ("*", ctx.platform) or c["when"] not in want:
            continue
        kind = c["kind"]
        req = c.get("requires") or {}
        status, detail = "pass", c["requirement"]
        if kind == "advisory":
            status = "warn"
        elif kind == "limit":
            creates = sum(1 for a in artifacts if a.kind == "create_request" and any(t in (a.url or "") for t in ("/api/flows", "/api/segments")))
            per_day = c["limit"]["per_day"]
            status = "pass" if creates <= per_day else "fail"
            detail = f"{c['requirement']}: this pack makes {creates} such create call(s)"
        elif kind == "requirement":
            if "tier_in" in req:
                allowed = req["tier_in"]
                if not tier:
                    status, detail = "warn", f"{c['requirement']}. Account tier unknown: set esp.tier ({' or '.join(allowed)} needed)"
                elif tier in allowed:
                    detail = f"{c['requirement']}. Account tier: {tier}"
                else:
                    status = "fail" if c.get("hard") else "warn"
                    detail = f"{c['requirement']}. Account tier is {tier}: needs {' or '.join(allowed)}"
            elif "option" in req:
                name, want_v = req["option"], req["equals"]
                have = opts.get(name)
                if have is None:
                    status, detail = "warn", f"{c['requirement']}. Unknown for this account: set esp.options.{name}"
                elif have == want_v:
                    detail = f"{c['requirement']}. esp.options.{name}: {have}"
                else:
                    status = "fail" if c.get("hard") else "warn"
                    detail = f"{c['requirement']}. esp.options.{name} is {have!r}, needs {want_v!r}"
            elif req.get("events_in_esp"):
                events = _events(ctx, mode)
                listed = opts.get("events_in_esp")
                if not events:
                    detail = f"{c['requirement']}. This pack reads only the platform's own email engagement data"
                elif listed is None:
                    status = "warn"
                    detail = (f"{c['requirement']}. Confirm these reach {ctx.label}: "
                              + ", ".join(f"{e} (as {ctx.event_name(e)})" for e in events) + " (or list them in esp.options.events_in_esp)")
                else:
                    missing = [e for e in events if e not in listed]
                    if missing:
                        status = "fail" if c.get("hard") else "warn"
                        hint = " Build with --warehouse-job instead." if mode in ("native_segment", "scheduled_segment") or (
                            mode == "native_event" and ctx.brief.flow.kind == "absence") else " Send them to the platform first."
                        detail = f"{c['requirement']}. Not in {ctx.label}: {', '.join(missing)}.{hint}"
                    else:
                        detail = f"{c['requirement']}. All of {', '.join(events)} are in {ctx.label}"
            elif req.get("whole_units"):
                unit = ctx.caps["min_delay"]["value"]
                bad = [s.id for s in steps if s.wait and s.wait.seconds % unit["seconds"]]
                if bad:
                    status = "fail" if c.get("hard") else "warn"
                    detail = f"{c['requirement']} ({unit['unit']} on {ctx.label}); not whole: {', '.join(bad)}"
                else:
                    detail = f"{c['requirement']} ({unit['unit']} on {ctx.label})"
            elif req.get("dialect_matches"):
                from engine.models import PLATFORM_DIALECT
                d = ctx.spec.get("template_dialect")
                if ctx.dialect_ok:
                    detail = f"{c['requirement']}: {d}"
                else:
                    status = "fail"
                    detail = (f"The copy was rendered in {d} for {ctx.brief.platform}; {ctx.label} renders {PLATFORM_DIALECT[ctx.platform]}. "
                              f"Re-plan for {ctx.platform} (python3 -m engine.pipeline.orchestrator revise --run <run> --set platform={ctx.platform}) "
                              f"and build again")
        out.append(CapabilityCheck(id=c["id"], status=status, hard=bool(c.get("hard")), requirement=c["requirement"], detail=detail,
                                   source=c["source"], confidence=c["confidence"]))
    if ctx.account.get("note"):
        out.append(CapabilityCheck(id="all.account_platform", status="warn", requirement="The account settings describe this platform",
                                   detail=ctx.account["note"], source="lmm.yaml esp", confidence="VERIFIED"))
    return out
