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

Read from vendor documentation on 2026-10-20 (config/esp_payload_schemas.json has the links).
Versions and limits change; check before a production sync.

    python3 tools/event_payloads.py klaviyo out/entrants.csv --event core_action_lapsed_14d --out out/klaviyo_events
    python3 tools/event_payloads.py hubspot out/entrants.csv --event core_action_lapsed_14d --hub-id 12345678 --out out/hubspot_events
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

KLAVIYO_REVISION = "2026-10-15"
HUBSPOT_VERSION = "2026-09"
HUBSPOT_MAX_BATCH = 500
BRAZE_MAX_OBJECTS = 75
BRAZE_RESERVED = {"time", "event_name"}
EVENT_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
PLATFORMS = ("klaviyo", "hubspot", "braze", "customerio")


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
    if platform == "sfmc":
        raise ValueError("Salesforce Marketing Cloud entry is an API Event into Journey Builder; it is not built here (see CONNECTORS.md)")
    raise ValueError(f"platform must be one of {PLATFORMS}")


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
    ap = argparse.ArgumentParser(description="Cohort entry rows to ESP trigger-event request bodies (nothing is sent)")
    ap.add_argument("platform", choices=PLATFORMS)
    ap.add_argument("input", help="CSV or JSON rows from the cohort entry query")
    ap.add_argument("--event", required=True)
    ap.add_argument("--flow", default="lcm_flow")
    ap.add_argument("--as-of", default=datetime.now(timezone.utc).date().isoformat())
    ap.add_argument("--hub-id", default="00000000")
    ap.add_argument("--braze-instance", default="iad-01")
    ap.add_argument("--eu", action="store_true", help="Customer.io EU workspace")
    ap.add_argument("--out", required=True)
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
