"""lcm build: write a plan's ESP build pack beside it, and with --create --approve, create the artifacts through the API.

What it writes (in <plan folder>/build/ unless --out): BUILD.md (the pack in plain English, click by click where a person
builds it), pack.json (everything), capabilities.json (the capability report), artifacts/*.json (create-API requests
or the build sheet) and, when the holdout reads a person property, holdout_sync_<dialect>.sql.

--create sends the create requests in order through the same safe client as lcm send (adapters/destinations/send.py):
Klaviyo templates, then the segment, then the flow (Draft); the HubSpot workflow (off); the Marketing Cloud journey
(Draft); the Braze Canvas copy. It is a dry run that prints the calls unless --approve is also given and the platform's
key is in the environment. It refuses a BLOCKED pack, an artifact with an UNVERIFIED shape unless --allow-unverified,
and any request with a placeholder still unresolved (metric ids are looked up, template and segment ids come from the
earlier responses, the rest are --id NAME=VALUE). Create calls retry 429 only, never a 5xx (the object may exist), and
every accepted call is written to build/create_receipts.jsonl so a rerun never creates the same object twice.

The account (tier and options for the capability checks) comes from the workspace's lmm.yaml (platform, esp.tier,
esp.options), then --account FILE, then --tier and --option.

    python3 adapters/destinations/build/cli.py --run out/northfield_cart_abandon_1h
    python3 adapters/destinations/build/cli.py --run out/ridgeview_winback_14d --warehouse-job
    python3 adapters/destinations/build/cli.py --run out/northfield_cart_abandon_1h --create --approve
"""
from __future__ import annotations

if __package__ in (None, ""):  # run as a file (python3 <path>): make the repository importable
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from adapters.destinations.build import account_from_workspace, build, write
from adapters.destinations.build.artifacts import PLACEHOLDER, fill, placeholders
from adapters.destinations.build.context import PLATFORMS, plan_folder
from adapters.destinations.build.model import Artifact, BuildPack


def _kv(items: List[str], what: str, parse: bool = True) -> Dict[str, Any]:
    """NAME=VALUE pairs. Options are read as YAML (true, [a, b]); ids stay text, since an id like 0123 is not a number."""
    out: Dict[str, Any] = {}
    for item in items:
        k, eq, v = item.partition("=")
        if not eq:
            raise ValueError(f"{what} takes NAME=VALUE (got {item!r})")
        if parse:
            import yaml
            out[k.strip()] = yaml.safe_load(v)
        else:
            out[k.strip()] = v.strip()
    return out


def load_account(path: Optional[str], tier: Optional[str], options: List[str], use_workspace: bool = True) -> Dict[str, Any]:
    acc: Dict[str, Any] = account_from_workspace() if use_workspace else {}
    acc = {k: v for k, v in acc.items() if v is not None}
    if path:
        import yaml
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        doc = doc.get("esp", doc) if isinstance(doc, dict) else {}
        acc.update({k: v for k, v in doc.items() if k in ("platform", "tier", "options")})
    if tier:
        acc["tier"] = tier
    if options:
        acc["options"] = {**(acc.get("options") or {}), **_kv(options, "--option")}
    return acc


def _needs_unverified(a: Artifact) -> bool:
    from adapters.destinations.build import validate as V
    ep = V.endpoint(a.schema_ref) if a.schema_ref else {}
    return ep.get("confidence") == "UNVERIFIED" or any("(UNVERIFIED)" in u for u in a.unverified_fields)


def _request(a: Artifact, body: Any) -> Dict[str, Any]:
    return {"method": a.method or "POST", "url": a.url, "headers": dict(a.headers), "body": body}


def _created_id(resp: Any) -> Optional[str]:
    if isinstance(resp, dict):
        if isinstance(resp.get("data"), dict) and resp["data"].get("id"):
            return str(resp["data"]["id"])            # Klaviyo (JSON:API)
        for k in ("id", "canvas_id", "flowId"):
            if resp.get(k):
                return str(resp[k])                    # HubSpot flow, Marketing Cloud interaction, Braze copy
    return None


def klaviyo_metric_ids(client: Any, names: List[str]) -> Dict[str, str]:
    """Metric ids by name: Klaviyo cannot filter metrics by name, so the list is paged (GET /api/metrics, read only)."""
    from adapters.destinations.build.artifacts import K_HEADERS
    found: Dict[str, str] = {}
    url: Optional[str] = "https://a.klaviyo.com/api/metrics"
    pages = 0
    while url and pages < 50 and len(found) < len(names):
        out = client.request({"method": "GET", "url": url, "headers": {k: v for k, v in K_HEADERS.items() if k != "Content-Type"}, "body": None})
        if not out.ok:
            raise RuntimeError(f"could not list Klaviyo metrics: {out.detail}")
        doc = out.body or {}
        for m in doc.get("data", []):
            name = (m.get("attributes") or {}).get("name")
            if name in names and name not in found:
                found[name] = str(m["id"])
        url = (doc.get("links") or {}).get("next")
        pages += 1
    return found


def create(pack: BuildPack, out_dir: Path, approve: bool, allow_unverified: bool, env: Mapping[str, str], transport: Any = None,
           sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    """Create the pack's artifacts in order. Returns a summary; raises nothing for an API refusal (it is reported)."""
    from adapters.destinations.send import Policy, ReceiptLog, SafeClient, _sha, redact
    todo = sorted([a for a in pack.artifacts if a.kind in ("create_request", "duplicate_request")], key=lambda a: a.order)
    summary: Dict[str, Any] = {"platform": pack.platform, "mode": "approved" if approve else "dry run: nothing was created", "calls": [],
                               "created": [], "skipped": [], "refused": None}
    if pack.status == "BLOCKED":
        summary["refused"] = "the pack is BLOCKED: " + "; ".join(pack.blocked_reasons)
        return summary
    if not todo:
        summary["refused"] = f"{pack.platform_label} has no create API for this flow: follow build/BUILD.md"
        return summary
    risky = [a.name for a in todo if _needs_unverified(a)]
    if risky and not allow_unverified:
        summary["refused"] = (f"{', '.join(risky)} rel{'ies' if len(risky) == 1 else 'y'} on UNVERIFIED request fields; pass --allow-unverified "
                              "to create anyway (test in a sandbox account first)")
        return summary
    log = ReceiptLog(out_dir / "create_receipts.jsonl")
    done: Dict[str, Dict[str, Any]] = {}
    if log.path.is_file():
        for line in log.path.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line) if line.strip() else {}
            if rec.get("status") == "accepted":
                done[rec["artifact"]] = rec
    values: Dict[str, Any] = {r["produces"]: r["id"] for r in done.values() if r.get("produces") and r.get("id")}
    if not approve:
        for a in todo:
            body = fill(a.body, values)
            summary["calls"].append({"artifact": a.name, "method": a.method, "url": a.url, "headers": redact(a.headers),
                                     "unresolved": placeholders(body), "already_created": a.name in done})
        return summary
    client = SafeClient(env, transport=transport, sleep=sleep, policy=Policy(retry_5xx=False))
    missing = client.missing(_request(todo[0], {}))
    if missing:
        summary["refused"] = f"set {', '.join(missing)} in the environment (the key is read only at send time and never written)"
        return summary
    if pack.platform == "klaviyo":
        names = sorted({m.group(2) for a in todo for m in PLACEHOLDER.finditer(json.dumps(a.body)) if m.group(1) == "klaviyo_metric"})
        if names:
            try:
                ids = klaviyo_metric_ids(client, names)
            except RuntimeError as e:
                summary["refused"] = str(e)
                return summary
            values.update({f"<<klaviyo_metric:{n}>>": i for n, i in ids.items()})
    for a in todo:
        if a.name in done:
            summary["skipped"].append({"artifact": a.name, "id": done[a.name].get("id"), "why": "already created (create_receipts.jsonl)"})
            continue
        body = fill(a.body, values)
        left = placeholders(body)
        if left:
            summary["refused"] = f"{a.name}: unresolved {', '.join(left)}; give them with --id NAME=VALUE (see build/BUILD.md Inputs)"
            break
        out = client.request(_request(a, body), retry_5xx=False)
        rec = {"artifact": a.name, "url": a.url, "status": "accepted" if out.ok else "failed", "http": out.status, "attempts": out.attempts,
               "body_sha": _sha(body), "produces": a.produces, "id": _created_id(out.body) if out.ok else None, "detail": out.detail}
        log.append(rec)
        summary["calls"].append({k: rec[k] for k in ("artifact", "status", "http", "id")})
        if not out.ok:
            summary["refused"] = f"{a.name} failed: {out.detail} (a 5xx is not retried: check the account before running again)"
            break
        summary["created"].append({"artifact": a.name, "id": rec["id"], "state": a.creates_status})
        if a.produces and rec["id"]:
            values[a.produces] = rec["id"]
    return summary


def _cli(argv: Optional[List[str]] = None, env: Optional[Mapping[str, str]] = None, transport: Any = None,
         sleep: Callable[[float], None] = time.sleep) -> int:
    import os
    ap = argparse.ArgumentParser(prog="lcm build", description="Write the plan's ESP build pack (BUILD.md, artifacts, capability report); "
                                                               "with --create --approve, create them through the API: the flow switched off, its "
                                                               "templates and segments beside it (they send nothing on their own)")
    ap.add_argument("--run", required=True, help="the plan folder (it holds plan.json), a plan.json, or a run folder under it")
    ap.add_argument("--platform", choices=PLATFORMS, help="build for this platform (default: the plan's; the copy must be in its dialect)")
    ap.add_argument("--warehouse-job", action="store_true",
                    help="keep the warehouse job as the trigger for win-back, replenishment and sunset instead of the native segment")
    ap.add_argument("--account", help="a YAML or JSON file with tier and options (or an esp block); default: lmm.yaml's esp")
    ap.add_argument("--tier", help="the account's plan tier on the platform, e.g. professional (overrides lmm.yaml esp.tier)")
    ap.add_argument("--option", action="append", default=[], metavar="NAME=VALUE",
                    help="an account option, e.g. canvas_context=true or events_in_esp=[order_completed] (repeatable)")
    ap.add_argument("--id", action="append", default=[], metavar="NAME=VALUE",
                    help="fill a placeholder, e.g. hubspot_email:t1_email=123456 or from_email=hello@example.test (repeatable)")
    ap.add_argument("--braze-template-canvas", help="Braze: a template Canvas id to copy with /canvas/duplicate (UNVERIFIED request)")
    ap.add_argument("--out", help="folder to write the pack into (default: <plan folder>/build)")
    ap.add_argument("--create", action="store_true", help="create the artifacts through the API (a dry run without --approve)")
    ap.add_argument("--approve", action="store_true", help="with --create: really create them (needs the platform's key in the environment)")
    ap.add_argument("--allow-unverified", action="store_true", help="with --create: allow artifacts whose request shape is UNVERIFIED")
    ap.add_argument("--json", action="store_true", help="print the pack summary as JSON")
    a = ap.parse_args(argv)
    env = os.environ if env is None else env
    try:
        folder = plan_folder(a.run)
        acc = load_account(a.account, a.tier, a.option)
        pack = build(folder, platform=a.platform, native=not a.warehouse_job, account=acc, template_canvas=a.braze_template_canvas,
                     ids=_kv(a.id, "--id", parse=False))
    except (FileNotFoundError, ValueError) as e:
        print(f"lcm build: {e}")
        return 2
    out = Path(a.out) if a.out else folder / "build"
    write(pack, out)
    head = {"status": pack.status, "platform": pack.platform, "mode": pack.mode, "build": str(out / "BUILD.md"),
            "artifacts": [f"{x.path}{'' if x.valid is not False else ' (INVALID)'}" for x in pack.artifacts],
            "automatic": pack.automatic, "checks": {s: sum(c.status == s for c in pack.checks) for s in ("pass", "warn", "fail")},
            "inputs_required": sorted(pack.inputs_required), "unverified": len(pack.unverified)}
    if pack.blocked_reasons:
        head["blocked"] = pack.blocked_reasons
    rc = 1 if pack.status == "BLOCKED" else 0
    if a.create:
        result = create(pack, out, a.approve, a.allow_unverified, env, transport=transport, sleep=sleep)
        head["create"] = result
        if result.get("refused"):
            rc = rc or 2
    if a.json:
        print(json.dumps(head, indent=2, default=str))
    else:
        print(f"{pack.status}: {pack.flow} on {pack.platform_label} ({pack.mode.replace('_', ' ')})")
        print(f"  pack:  {out / 'BUILD.md'}")
        print(f"  {pack.automatic}")
        for c in pack.checks:
            if c.status != "pass":
                print(f"  {c.status.upper():4}  {c.id}: {c.detail[:150]}")
        if pack.inputs_required:
            print(f"  inputs: {len(pack.inputs_required)} placeholder(s) to supply or resolve (BUILD.md, Inputs to supply)")
        if a.create:
            print(json.dumps(head["create"], indent=2, default=str))
    return rc


if __name__ == "__main__":
    sys.exit(_cli())
