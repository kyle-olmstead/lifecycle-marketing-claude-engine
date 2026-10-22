"""Request bodies for pushing a trigger event into an ESP (the warehouse-computed absence trigger).

Most platforms cannot see an absence, so the teammate's win-back flows are triggered by a daily
warehouse job: it runs the cohort entry query and pushes one event per entrant, carrying the
holdout assignment. This tool turns the query's rows into request bodies for each platform, within
each platform's documented limits. Nothing is sent from here, and no credential is ever written:
headers carry ${PLACEHOLDER} names for secrets the job's environment supplies.

  Klaviyo     POST https://a.klaviyo.com/api/events             revision 2026-10-15, one event per request
              (a bulk endpoint, /api/event-bulk-create-jobs, takes up to 1,000; this tool does not build it)
  HubSpot     POST https://api.hubapi.com/events/2026-09/send/batch    up to 500 inputs per request;
              eventName is pe<hub id>_<name>; /events/v3/send remains available as a legacy path
  Braze       POST https://rest.<instance>.braze.com/users/track     up to 75 objects per request
  Customer.io POST https://cdp.customer.io/v1/track (Pipelines API, which Customer.io recommends over the
              Track API v1); EU workspaces use cdp-eu.customer.io; one event per request
  Iterable    POST https://api.iterable.com/api/events/track    one event per request, the key in the Api-Key header
              (keys in the query string or body face stricter limits); id is the event id: an event that repeats an id
              updates the first instead of adding a second (LIKELY: read on an API mirror, not Iterable's own reference),
              so a retry never double-counts. trackBulk is not built: its per-request maximum is not documented.
              (support.iterable.com/hc/en-us/articles/41044692130196; config/esp_payload_schemas.json iterable_track_event)

Read from vendor documentation on 2026-10-20 (config/esp_payload_schemas.json has the links).
Versions and limits change; check before a production sync.

    python3 adapters/destinations/event_payloads.py klaviyo out/entrants.csv --event core_action_lapsed_14d --out out/klaviyo_events
    python3 adapters/destinations/event_payloads.py hubspot out/entrants.csv --event core_action_lapsed_14d --hub-id 12345678 --out out/hubspot_events
"""
from __future__ import annotations

if __package__ in (None, ""):  # run as a file (python3 adapters/destinations/event_payloads.py): make the repository importable
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

KLAVIYO_REVISION = "2026-10-15"
HUBSPOT_VERSION = "2026-09"
HUBSPOT_MAX_BATCH = 500
BRAZE_MAX_OBJECTS = 75
BRAZE_RESERVED = {"time", "event_name"}
EVENT_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
PLATFORMS = ("klaviyo", "hubspot", "braze", "customerio", "iterable")
ITERABLE_MAX_ID_BYTES = 512


def _iso(ts: Optional[str]) -> str:
    if not ts:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return ts


def _props(p: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in (p or {}).items() if v is not None}


def build(platform: str, e: Dict[str, Any], hub_id: str = "00000000", braze_instance: str = "iad-01", eu: bool = False) -> Dict[str, Any]:
    """One request for one event. e: user_id, email, event, properties, occurred_at, event_id."""
    if not EVENT_NAME.match(e["event"]):
        raise ValueError(f"event names are snake_case: {e['event']!r}")
    props = _props(e.get("properties", {}))
    when = _iso(e.get("occurred_at"))
    if platform == "klaviyo":
        profile: Dict[str, Any] = {}
        if e.get("email"):
            profile["email"] = str(e["email"]).lower()
        if e.get("user_id"):
            profile["external_id"] = str(e["user_id"])
        body = {"data": {"type": "event", "attributes": {
            "properties": props, "time": when, "unique_id": e.get("event_id") or f"{e['event']}:{e.get('user_id')}:{when}",
            "metric": {"data": {"type": "metric", "attributes": {"name": e["event"]}}},
            "profile": {"data": {"type": "profile", "attributes": profile}}}}}
        return {"method": "POST", "url": "https://a.klaviyo.com/api/events",
                "headers": {"Authorization": "Klaviyo-API-Key ${KLAVIYO_PRIVATE_KEY}", "revision": KLAVIYO_REVISION,
                            "Content-Type": "application/vnd.api+json", "accept": "application/vnd.api+json"}, "body": body}
    if platform == "hubspot":
        return {"method": "POST", "url": f"https://api.hubapi.com/events/{HUBSPOT_VERSION}/send",
                "headers": {"Authorization": "Bearer ${HUBSPOT_PRIVATE_APP_TOKEN}", "Content-Type": "application/json"},
                "body": _hubspot_input(e, props, when, hub_id)}
    if platform == "braze":
        clash = BRAZE_RESERVED & set(props)
        if clash:
            raise ValueError(f"Braze reserves event property keys {sorted(clash)}")
        return {"method": "POST", "url": f"https://rest.{braze_instance}.braze.com/users/track",
                "headers": {"Authorization": "Bearer ${BRAZE_REST_API_KEY}", "Content-Type": "application/json"},
                "body": {"events": [{"external_id": str(e["user_id"]), "name": e["event"], "time": when, "properties": props}]}}
    if platform == "customerio":
        host = "cdp-eu.customer.io" if eu else "cdp.customer.io"
        body = {"userId": str(e["user_id"]), "event": e["event"], "properties": props, "timestamp": when,
                "messageId": e.get("event_id") or f"{e['event']}:{e.get('user_id')}:{when}"}
        return {"method": "POST", "url": f"https://{host}/v1/track",
                "headers": {"Authorization": "Basic ${CUSTOMERIO_PIPELINES_API_KEY_BASE64}", "Content-Type": "application/json"}, "body": body}
    if platform == "iterable":
        return _iterable(e, props, when, eu)
    if platform == "sfmc":
        raise ValueError("Salesforce Marketing Cloud entry is an API Event into Journey Builder; it is not built here (see CONNECTORS.md)")
    raise ValueError(f"platform must be one of {PLATFORMS}")


def _epoch_seconds(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def _iterable(e: Dict[str, Any], props: Dict[str, Any], when: str, eu: bool = False) -> Dict[str, Any]:
    """One Iterable custom event. Field names (email, userId, eventName, id, createdAt in epoch seconds, dataFields) are LIKELY:
    they come from an API mirror of Iterable's reference; the endpoint and the header key are from Iterable's own guide."""
    body: Dict[str, Any] = {"eventName": e["event"], "dataFields": props, "createdAt": _epoch_seconds(when)}
    if e.get("email"):
        body["email"] = str(e["email"]).lower()
    if e.get("user_id"):
        body["userId"] = str(e["user_id"])
    event_id = e.get("event_id") or f"{e['event']}:{e.get('user_id')}:{when}"
    if len(event_id.encode("utf-8")) > ITERABLE_MAX_ID_BYTES:
        raise ValueError(f"Iterable event ids are at most {ITERABLE_MAX_ID_BYTES} bytes")
    body["id"] = event_id
    host = "api.eu.iterable.com" if eu else "api.iterable.com"   # EU data center host: LIKELY, not in the verified research
    return {"method": "POST", "url": f"https://{host}/api/events/track",
            "headers": {"Api-Key": "${ITERABLE_API_KEY}", "Content-Type": "application/json"}, "body": body}


def _hubspot_input(e: Dict[str, Any], props: Dict[str, Any], when: str, hub_id: str) -> Dict[str, Any]:
    if not re.fullmatch(r"\d+", str(hub_id)):
        raise ValueError("HubSpot hub id is digits (the portal id)")
    out: Dict[str, Any] = {"eventName": f"pe{hub_id}_{e['event']}", "occurredAt": when,
                           "properties": {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in props.items()}}
    if e.get("email"):
        out["email"] = str(e["email"]).lower()
    if e.get("event_id"):
        out["uuid"] = str(e["event_id"])
    return out


def batches(platform: str, events: Iterable[Dict[str, Any]], hub_id: str = "00000000", braze_instance: str = "iad-01",
            eu: bool = False) -> List[Dict[str, Any]]:
    """Requests for many events, packed to each platform's limit."""
    events = list(events)
    if platform == "hubspot":
        inputs = [_hubspot_input(e, _props(e.get("properties", {})), _iso(e.get("occurred_at")), hub_id) for e in events]
        return [{"method": "POST", "url": f"https://api.hubapi.com/events/{HUBSPOT_VERSION}/send/batch",
                 "headers": {"Authorization": "Bearer ${HUBSPOT_PRIVATE_APP_TOKEN}", "Content-Type": "application/json"},
                 "body": {"inputs": inputs[i:i + HUBSPOT_MAX_BATCH]}} for i in range(0, len(inputs), HUBSPOT_MAX_BATCH)]
    if platform == "braze":
        singles = [build("braze", e, braze_instance=braze_instance)["body"]["events"][0] for e in events]
        return [{"method": "POST", "url": f"https://rest.{braze_instance}.braze.com/users/track",
                 "headers": {"Authorization": "Bearer ${BRAZE_REST_API_KEY}", "Content-Type": "application/json"},
                 "body": {"events": singles[i:i + BRAZE_MAX_OBJECTS]}} for i in range(0, len(singles), BRAZE_MAX_OBJECTS)]
    return [build(platform, e, hub_id=hub_id, eu=eu) for e in events]


def validate_request(req: Dict[str, Any]) -> Tuple[Optional[str], List[str]]:
    """(schema name, error messages) for one request this module built, against config/esp_payload_schemas.json.

    Iterable is checked here; every other endpoint goes to engine/policy/simulator.validate_request, which predates Iterable."""
    if "iterable.com/api/events/track" in str(req.get("url", "")):
        import jsonschema
        from engine.paths import ESP_PAYLOAD_SCHEMAS
        from engine.schemas import FORMAT_CHECKER
        cfg = json.loads(ESP_PAYLOAD_SCHEMAS.read_text(encoding="utf-8"))
        sub = {"$schema": cfg["$schema"], "$defs": cfg["$defs"], "$ref": "#/$defs/iterable_track_event"}
        v = jsonschema.Draft202012Validator(sub, format_checker=FORMAT_CHECKER)
        return "iterable_track_event", [f"{'/'.join(map(str, e.path)) or '(body)'}: {e.message}" for e in v.iter_errors(req.get("body"))]
    from engine.policy import simulator
    return simulator.validate_request(req)


def rows_to_events(rows: Iterable[Dict[str, Any]], event: str, flow: str, as_of: str) -> List[Dict[str, Any]]:
    """Rows of the cohort entry query to events. Holdout members are pushed too: they enter, then exit at the holdout split."""
    out = []
    for r in rows:
        if not r.get("user_id"):
            continue
        hold = r.get("in_holdout")
        hold = hold if isinstance(hold, bool) else str(hold).lower() in ("true", "1", "t")
        anchor = str(r.get("anchor_date") or "")[:10] or as_of
        props = {"flow": flow, "in_holdout": hold, "holdout_bucket": int(r["holdout_bucket"]) if r.get("holdout_bucket") not in (None, "") else None,
                 "days_inactive": int(r["days_inactive"]) if r.get("days_inactive") not in (None, "") else None, "anchor_date": anchor}
        # keyed on the lapse (user and anchor date), not the run date, so a re-sent lapse carries the same id
        out.append({"user_id": r["user_id"], "email": r.get("email") or None, "event": event, "properties": props,
                    "occurred_at": f"{as_of}T05:00:00Z", "event_id": f"{flow}:{r['user_id']}:{anchor}"})
    return out


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="lcm events", description="Cohort entry rows to ESP trigger-event request bodies (nothing is sent)")
    ap.add_argument("platform", choices=PLATFORMS, help="the platform to build requests for")
    ap.add_argument("input", help="CSV or JSON rows from the cohort entry query")
    ap.add_argument("--event", required=True, help="the trigger event name the flow listens for, e.g. core_action_lapsed_14d")
    ap.add_argument("--flow", default="lcm_flow", help="flow name stamped on each event and its id (default: lcm_flow)")
    ap.add_argument("--as-of", default=datetime.now(timezone.utc).date().isoformat(),
                    help="the run date, YYYY-MM-DD; sets each event's timestamp (default: today, UTC)")
    ap.add_argument("--hub-id", default="00000000", help="your HubSpot hub id, used in the event name pe<hub id>_<event> (HubSpot only)")
    ap.add_argument("--braze-instance", default="iad-01", help="the REST instance in your Braze dashboard URL (default: iad-01)")
    ap.add_argument("--eu", action="store_true", help="Customer.io EU workspace, or Iterable EU project")
    ap.add_argument("--out", required=True, help="folder to write one JSON file per request into")
    a = ap.parse_args(argv)
    p = Path(a.input)
    text = p.read_text(encoding="utf-8")
    rows = json.loads(text) if p.suffix == ".json" else list(csv.DictReader(text.splitlines()))
    reqs = batches(a.platform, rows_to_events(rows, a.event, a.flow, a.as_of), a.hub_id, a.braze_instance, a.eu)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for i, r in enumerate(reqs, 1):
        (out / f"{a.platform}_request_{i:04d}.json").write_text(json.dumps(r, indent=2), encoding="utf-8")
    print(json.dumps({"requests": len(reqs), "events": len(rows), "platform": a.platform}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
