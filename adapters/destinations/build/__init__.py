"""ESP build packs: everything needed to stand a planned flow up natively in Klaviyo, HubSpot, Braze, Customer.io, Iterable
or Salesforce Marketing Cloud, without an engineer on most platforms.

What: build(plan, platform=None, native=True, account=None) turns an ExecutionPlan (or its folder) into a BuildPack: the
native trigger (exact type, filters, exit criteria, re-entry), each step as the platform's own action with its wait in
the platform's smallest delay unit, the explicit splits (holdout first, then each SMS or push consent split with its
email fallback), the goal, exit, send-window and frequency settings, the copy files each message uses, capability checks
against the account, and the artifacts: create-API requests where the platform has a create API (Klaviyo flows,
segments and templates; HubSpot automation v4, beta; Marketing Cloud journeys), else a click-by-click build sheet.

Why: v2 triggered every win-back with a warehouse job an engineer had to run. Klaviyo, HubSpot and Customer.io trigger
absence flows natively from a segment, and Braze, Iterable and Marketing Cloud from a daily scheduled audience; the
warehouse job stays as the alternative when the core event does not live in the ESP (native=False, --warehouse-job).

Usage:
    from adapters.destinations.build import build, write
    pack = build("out/northfield_cart_abandon_1h")                    # the plan folder, a plan.json, or an ExecutionPlan
    pack = build(plan, platform="klaviyo", account={"tier": None, "options": {"shopify": True}})
    write(pack, plan_folder / "build", ctx_plan=plan)                  # BUILD.md, pack.json, capabilities.json, artifacts/

    python3 adapters/destinations/build/cli.py --run out/northfield_cart_abandon_1h            # lcm build
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from engine.models import PLATFORM_LABEL, ExecutionPlan

from adapters.destinations.build import artifacts as A
from adapters.destinations.build import capabilities as C
from adapters.destinations.build import validate as V
from adapters.destinations.build.context import Ctx, make_ctx
from adapters.destinations.build.model import Artifact, BuildPack, Fact, Step, TriggerConfig
from adapters.destinations.build.platforms import settings as make_settings, steps as make_steps
from adapters.destinations.build.triggers import decide_mode, trigger as make_trigger

__all__ = ["build", "write", "BuildPack", "account_from_workspace"]
SHEET_PLATFORMS = ("braze", "customerio", "iterable")


def account_from_workspace(ws: Any = None) -> Dict[str, Any]:
    """The account the workspace describes: lmm.yaml's platform and its esp block ({tier, options})."""
    from engine import workspace
    ws = ws or workspace.find()
    if getattr(ws, "fallback", False):
        return {}                     # the bundled demo stands in for a missing workspace: it describes no real account
    try:
        cfg = ws.config()
    except Exception:  # noqa: BLE001 - a broken lmm.yaml is lcm doctor's report, not a build failure
        return {}
    esp = cfg.get("esp") or {}
    return {"platform": cfg.get("platform"), "tier": esp.get("tier"), "options": dict(esp.get("options") or {})}


def holdout_sync_sql(ctx: Ctx, dialect_name: str) -> str:
    """Rows for the per-flow holdout property a segment trigger splits on, with the plan's own assignment rule."""
    rule = ctx.plan.experiment.assignment_sql.get(dialect_name)
    prop = ctx.holdout_property
    return (f"-- dialect: {dialect_name}\n"
            f"-- Holdout property for {ctx.slug}: {prop} is true for the {ctx.brief.holdout_pct:g}% holdout, by the same rule as the plan\n"
            f"-- (experiment.json assignment_sql). Run once, then weekly; send the results with:\n"
            f"--   lcm send --profile-updates <results.csv> --platform {ctx.platform} --approve\n"
            f"SELECT user_id, email, CASE WHEN {rule} THEN 'true' ELSE 'false' END AS {prop}\n"
            f"FROM users\nWHERE email IS NOT NULL\n")


def _manual(ctx: Ctx, mode: str, trig: TriggerConfig, steps: List[Step], has_create: bool) -> List[str]:
    p = ctx.platform
    spec = ctx.spec
    out: List[str] = []
    profile_holdout = ctx.brief.holdout_pct > 0 and any(s.split and s.split.kind == "holdout" and s.split.source == "profile_property" for s in steps)
    if profile_holdout and not (p == "sfmc" and mode == "scheduled_segment"):
        out.append(f"Create the person property {ctx.holdout_property} (true or false). Run build/holdout_sync_{ctx.brief.warehouse_dialect}.sql "
                   f"and send its rows with `lcm send --profile-updates <results.csv> --platform {p} --approve`; repeat weekly so new people "
                   f"carry it before they can qualify")
    if trig.segment:
        out += [f"Data needed for the segment: {d}" for d in trig.segment.data_prerequisites]
        if not (p == "klaviyo" and has_create) and not (p == "hubspot" and has_create):
            out.append(f"Create the {trig.segment.kind} '{trig.segment.name}': " + "; ".join(trig.segment.conditions))
    if p in ("hubspot", "sfmc"):
        out.append("Create one email per message from the rendered copy (copy/<step>_<variant>.html, subject and preview text in "
                   "copy/<step>_<variant>.subject.txt) and give each id with --id (see Inputs)")
    if p == "hubspot":
        out.append(f"In the workflow editor: set the goal ({trig.exit_criteria[0]})" +
                   ("; add a 'Time of day' delay before each email for the daily send hour" if any(s.wait and s.wait.until_local_time for s in steps) else "") +
                   ("; set the workflow time window (Settings > Run actions at specific times)" if any(s.respect_send_window for s in steps) else ""))
    if p == "sfmc":
        out.append(f"In Journey Builder: create the entry event definition, set the goal and exit criteria ({trig.exit_criteria[0]}), and check "
                   "the Decision Split's criteria; then publish")
    if p == "klaviyo" and ctx.flow_type == "browse_abandon":
        out.append("Add the flow filter 'has not been in the abandoned cart flow in the last 7 days' in the flow builder (it names another flow)")
    for s in steps:
        if s.manual:
            out.append(f"{s.id}: {s.note or 'add by hand'}" if s.note else f"{s.id}: add {s.platform_action} by hand")
        elif s.split and s.split.note:
            out.append(f"{s.id}: {s.split.note}")
    unwritten = [s.id for s in steps if s.kind == "send" and not s.manual and not (s.content and s.content.chosen)]
    if unwritten:
        out.append(f"Copy is not written yet for {', '.join(unwritten)}: answer copy_briefs.md, plan again with --drafts, then build again")
    if any(s.kind == "update_profile" for s in steps):
        out.append("Make lcm_sunset = true a suppression in every marketing send")
    filters = [f["condition"] for f in spec.get("entry_filters", [])]
    if filters:
        out.append("Entry filters to confirm (checked once at entry): " + "; ".join(filters))
    if spec.get("suppressions"):
        out.append("Suppressions on every step: " + "; ".join(spec["suppressions"]))
    out.append("Before going live: send every message to a seed list, run `lcm simulate` on flow_spec.json, and get approval")
    return out


def _automatic(ctx: Ctx, arts: List[Artifact]) -> str:
    creates = [a for a in arts if a.kind == "create_request"]
    if ctx.platform == "klaviyo":
        parts = [f"{sum(1 for a in creates if 'templates' in (a.url or ''))} email template(s)"]
        if any("segments" in (a.url or "") for a in creates):
            parts.append("the segment")
        parts.append("the flow, in Draft")
        return "lcm build --create --approve makes " + ", ".join(parts) + " through Klaviyo's API"
    if ctx.platform == "hubspot":
        flag = " --allow-unverified" if any(x.unverified_fields and any("(UNVERIFIED)" in u for u in x.unverified_fields) for x in creates) else ""
        return (f"lcm build --create --approve{flag} makes the workflow, off, through HubSpot's automation v4 API (beta); emails are made "
                f"by hand first" + (" (--allow-unverified: the list branch's type value is not documented)" if flag else ""))
    if ctx.platform == "sfmc":
        return ("lcm build --create --approve --allow-unverified makes the journey, in Draft, through Journey Builder's API; emails, the entry "
                "event and publishing stay by hand")
    if ctx.platform == "braze" and any(a.kind == "duplicate_request" for a in arts):
        return "lcm build --create --approve --allow-unverified copies your template Canvas (UNVERIFIED request); the rest follows the build sheet"
    return f"{PLATFORM_LABEL[ctx.platform]} has no create API for this: a person follows the build sheet"


def build(plan: Union[ExecutionPlan, Dict[str, Any], str, Path], platform: Optional[str] = None, native: bool = True,
          account: Optional[Dict[str, Any]] = None, template_canvas: Optional[str] = None, ids: Optional[Dict[str, Any]] = None) -> BuildPack:
    """The build pack for one plan on one platform (default: the plan's). account: {platform, tier, options}; ids fills
    placeholders (`<<hubspot_email:t1_email>>` or `hubspot_email:t1_email` as keys)."""
    ctx = make_ctx(plan, platform, native, account)
    if ids:
        ctx.account.setdefault("options", {}).setdefault("ids", {}).update(ids)
    mode = decide_mode(ctx)
    trig = make_trigger(ctx, mode)
    steps = make_steps(ctx, mode)
    sett = make_settings(ctx, mode, trig)
    arts: List[Artifact] = []
    if ctx.platform == "klaviyo":
        arts += A.klaviyo(ctx, mode, trig, steps)
    elif ctx.platform == "hubspot":
        arts += A.hubspot(ctx, mode, trig, steps)
    elif ctx.platform == "sfmc":
        arts += A.sfmc(ctx, mode, trig, steps)
    elif ctx.platform == "braze":
        dup = A.braze_duplicate(ctx, template_canvas)
        if dup:
            arts.append(dup)
    has_create = any(a.kind == "create_request" for a in arts)
    manual = _manual(ctx, mode, trig, steps, has_create)
    if ctx.platform in SHEET_PLATFORMS:
        arts.insert(0, A.build_sheet(ctx, mode, trig, steps, sett, manual))
    if ctx.brief.holdout_pct > 0 and any(s.split and s.split.kind == "holdout" and s.split.source == "profile_property" for s in steps):
        for d in ("postgres", "snowflake", "bigquery"):
            arts.append(Artifact(name=f"holdout_sync_{d}", kind="holdout_sync_sql", path=f"holdout_sync_{d}.sql", body=holdout_sync_sql(ctx, d),
                                 note="rows for lcm send --profile-updates"))
    known = A.known_values(ctx)
    facts: List[Fact] = []
    for a in arts:
        if a.kind == "holdout_sync_sql":
            continue
        a.body = A.fill(a.body, known)
        a.placeholders = A.placeholders(a.body)
        errs = V.errors(a.body, a.schema_ref) if a.schema_ref else []
        a.valid, a.errors = not errs, errs
        uv = V.unverified(a.body, a.schema_ref) if a.schema_ref else []
        a.unverified_fields = [f"{u['path']} ({u['confidence']}): {u['why']}" for u in uv]
        ep = V.endpoint(a.schema_ref) if a.schema_ref else {}
        facts += [Fact(fact=f"{a.name}: {u['path']}: {u['why']}", source=(ep.get("docs") or ["config/esp_artifact_schemas.json"])[0],
                       confidence=u["confidence"]) for u in uv]
        if ep.get("confidence") and ep["confidence"] != "VERIFIED":
            facts.append(Fact(fact=f"{a.name}: the endpoint's request shape ({ep.get('note', '')})", source=(ep.get("docs") or [""])[0],
                              confidence=ep["confidence"]))
    checks = C.evaluate(ctx, mode, steps, arts)
    facts += [Fact(fact=f"{c.id}: {c.requirement}", source=c.source, confidence=c.confidence) for c in checks if c.confidence != "VERIFIED"]
    if trig.confidence != "VERIFIED":
        facts.append(Fact(fact=f"the {mode.replace('_', ' ')} recipe on {ctx.label} (the trigger type is verified, the recipe is not)",
                          source=ctx.caps["trigger_types"]["source"], confidence=trig.confidence))
    src = ctx.caps["trigger_types"]["source"]
    for where, text in [("holdout split", trig.holdout), ("re-entry", str(trig.re_entry.get("setting", "")))] + \
            [(f"step {s.id}", f"{s.platform_action} {s.window or ''}") for s in steps]:
        for word in ("UNVERIFIED", "LIKELY"):
            if word in text:
                facts.append(Fact(fact=f"{where}: {text.strip()}", source=src, confidence=word))
                break
    if ctx.platform == "klaviyo" and any(ph.startswith(("<<klaviyo_metric:Clicked Email", "<<klaviyo_metric:Received Email"))
                                         for a in arts for ph in A.placeholders(a.body)):
        f = ctx.caps["built_in_metric_names"]
        facts.append(Fact(fact="Klaviyo's built-in email metric names (Received Email, Clicked Email)", source=f["source"], confidence=f["confidence"]))
    if any(s.wait and s.wait.unit == "minutes" for s in steps) and ctx.platform == "customerio":
        f = ctx.caps["minutes_delay"]
        facts.append(Fact(fact="Customer.io Time Delay in minutes", source=f["source"], confidence=f["confidence"]))
    seen = set()
    facts = [f for f in facts if not (f.fact in seen or seen.add(f.fact))]
    inputs = {ph: A.describe(ph) for a in arts for ph in a.placeholders}
    blocked = [f"{c.id}: {c.detail}" for c in checks if c.status == "fail"] + [f"{a.name}: {e}" for a in arts if a.valid is False for e in a.errors[:3]]
    status = "BLOCKED" if blocked else ("READY WITH WARNINGS" if any(c.status == "warn" for c in checks) or facts else "READY")
    return BuildPack(platform=ctx.platform, platform_label=ctx.label, flow_type=ctx.flow_type, flow=ctx.spec["flow"], slug=ctx.slug,
                     run_id=ctx.plan.run_id, status=status, blocked_reasons=blocked, mode=mode, trigger=trig, steps=steps, settings=sett,
                     content=[s.content for s in steps if s.content], checks=checks, artifacts=arts, inputs_required=inputs, manual_steps=manual,
                     unverified=facts, account={k: v for k, v in ctx.account.items() if k != "options"} | {"options": sorted(ctx.options)},
                     automatic=_automatic(ctx, arts))


def write(pack: BuildPack, out: Path) -> List[Path]:
    """BUILD.md, pack.json, capabilities.json and every artifact, under out (the plan folder's build/ by default)."""
    from adapters.destinations.build.markdown import render
    out = Path(out)
    (out / "artifacts").mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    for a in pack.artifacts:
        path = out / a.path
        if a.kind == "holdout_sync_sql":
            path.write_text(str(a.body), encoding="utf-8")
        elif a.kind == "build_sheet":
            path.write_text(json.dumps(a.body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        else:
            path.write_text(json.dumps({"method": a.method, "url": a.url, "headers": a.headers, "body": a.body}, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8")
        written.append(path)
    (out / "pack.json").write_text(pack.model_dump_json(indent=2), encoding="utf-8")
    (out / "capabilities.json").write_text(json.dumps([c.model_dump() for c in pack.checks], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "BUILD.md").write_text(render(pack), encoding="utf-8")
    return written + [out / "pack.json", out / "capabilities.json", out / "BUILD.md"]
