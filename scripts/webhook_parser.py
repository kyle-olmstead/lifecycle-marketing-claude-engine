"""Event stream ingest: Segment and GA4-shaped webhooks into the canonical track_events table.

The teammate's cohort queries read one table shape (docs/SQL_SCHEMA_CONVENTIONS.md):
event_id, user_id, anonymous_id, event_name, event_at, properties. This parser turns what a CDP
sends into exactly that, validates every event against the tracking plan
(modules/02_behavioral_triggers_and_flows/schemas/custom_event_definitions.json), and writes:

  <LCM_EVENTS_DIR>/track_events/<date>.ndjson   accepted events, one per line, deduplicated on event_id
  <LCM_EVENTS_DIR>/users/<date>.ndjson          identify traits (email, consent timestamps) for the users table
  <LCM_EVENTS_DIR>/quarantine/<date>.ndjson     rejected events with the reason, never silently dropped

Load the ndjson into the warehouse with its native loader, or point the cohort analyst at a folder
of CSVs written with --to-csv.

Sources
  Segment  POST /webhooks/segment. Works with the Webhooks (Actions) destination and the classic
           Webhooks destination. Requests are signed: header X-Signature, hex HMAC-SHA1 of the body
           with the destination's shared secret. Segment's documentation computes it over the JSON
           body as serialized by Segment, so the parser accepts a match on the raw bytes or on the
           compact re-serialization, and, for batches, on the first event (Segment signs the first
           event of a batch). Single events, arrays and {"batch": [...]} are all accepted.
  GA4      POST /webhooks/ga4. GA4 does not push webhooks itself; this accepts the Measurement
           Protocol body shape ({client_id, user_id, timestamp_micros, events: [{name, params}]})
           as forwarded by server-side Google Tag Manager or a relay. Authenticated with a shared
           token in the X-LCM-Token header. GA4 recommended names are mapped to the tracking plan
           (sign_up to signed_up, purchase to order_completed, and so on); others are snake_cased.

Secrets come from the environment only: SEGMENT_SHARED_SECRET, LCM_GA4_TOKEN. With no secret set
the endpoint refuses traffic unless LCM_ALLOW_UNSIGNED=1 (local testing only).

    uvicorn scripts.webhook_parser:app --port 8080                       # serve
    python3 scripts/webhook_parser.py parse --source segment payload.json --to-csv out/events   # offline
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import os
import re
import sys
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import jsonschema  # noqa: E402

try:  # the HTTP app needs FastAPI; the offline parser does not
    from fastapi import FastAPI, HTTPException, Request
except ImportError:  # pragma: no cover
    FastAPI = HTTPException = Request = None  # type: ignore[assignment,misc]

PLAN_PATH = ROOT / "modules" / "02_behavioral_triggers_and_flows" / "schemas" / "custom_event_definitions.json"
PLAN = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
PLAN_EVENTS = set(PLAN["properties"]["event"]["enum"])
MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_FUTURE = timedelta(hours=1)

GA4_TO_PLAN = {"sign_up": "signed_up", "session_start": "session_started", "purchase": "order_completed",
               "begin_checkout": "checkout_started", "view_item": "product_viewed", "login": "logged_in",
               "first_visit": "first_visited", "page_view": "page_viewed", "screen_view": "screen_viewed"}
SEGMENT_SPEC_TO_PLAN = {"Signed Up": "signed_up", "Order Completed": "order_completed", "Checkout Started": "checkout_started",
                        "Product Viewed": "product_viewed", "Subscription Started": "subscription_started",
                        "Subscription Cancelled": "subscription_cancelled", "Application Opened": "session_started"}


def snake(name: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name.strip())
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    return s or "unnamed_event"


def _utc(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1_000_000 if value > 1e14 else value / 1000 if value > 1e11 else value, tz=timezone.utc)
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")


# ----------------------------------------------------------------------------------------------
# signatures
# ----------------------------------------------------------------------------------------------

def verify_segment_signature(raw: bytes, signature: Optional[str], secret: str) -> bool:
    if not signature:
        return False
    sig = signature.strip().lower()
    candidates = [raw]
    try:
        doc = json.loads(raw)
        candidates.append(json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        first = doc[0] if isinstance(doc, list) and doc else (doc.get("batch") or [None])[0] if isinstance(doc, dict) and "batch" in doc else None
        if first is not None:
            candidates.append(json.dumps(first, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    except (ValueError, TypeError):
        pass
    return any(hmac.compare_digest(hmac.new(secret.encode("utf-8"), c, hashlib.sha1).hexdigest(), sig) for c in candidates)


def verify_token(given: Optional[str], expected: str) -> bool:
    return bool(given) and hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


# ----------------------------------------------------------------------------------------------
# normalization
# ----------------------------------------------------------------------------------------------

def _validate(event_name: str, row: Dict[str, Any], source: str) -> List[str]:
    """Plan events must validate against the tracking plan; unplanned events pass through, flagged."""
    if event_name not in PLAN_EVENTS:
        return []
    doc = {"type": "track", "event": event_name, "event_id": row["event_id"], "timestamp": row["event_at"].replace(" ", "T") + "Z",
           "properties": row["properties"]}
    if row.get("user_id"):
        doc["user_id"] = row["user_id"]
    if row.get("anonymous_id"):
        doc["anonymous_id"] = row["anonymous_id"]
    v = jsonschema.Draft202012Validator(PLAN, format_checker=jsonschema.FormatChecker())
    return [f"{'/'.join(map(str, e.path)) or 'event'}: {e.message}" for e in v.iter_errors(doc)][:5]


def _common(row: Dict[str, Any], source: str, received: datetime) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not row.get("event_id"):
        return None, "no idempotency key (Segment messageId, or an event_id param for GA4)"
    if not (row.get("user_id") or row.get("anonymous_id")):
        return None, "no user_id or anonymous_id"
    if row["_at"] is None:
        return None, "missing or unreadable timestamp"
    if row["_at"] > received + MAX_FUTURE:
        return None, f"timestamp {row['_at'].isoformat()} is in the future: client clock is wrong"
    row["event_at"] = _iso(row.pop("_at"))
    errs = _validate(row["event_name"], row, source)
    if errs:
        return None, "does not match the tracking plan: " + "; ".join(errs)
    row["in_tracking_plan"] = row["event_name"] in PLAN_EVENTS
    return row, None


def from_segment(doc: Any, received: Optional[datetime] = None) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    received = received or datetime.now(timezone.utc)
    items = doc if isinstance(doc, list) else (doc.get("batch") if isinstance(doc, dict) and isinstance(doc.get("batch"), list) else [doc])
    events, users, rejected = [], [], []
    for m in items:
        if not isinstance(m, dict):
            rejected.append({"source": "segment", "reason": "not a JSON object", "payload": m})
            continue
        kind = (m.get("type") or "track").lower()
        at = _utc(m.get("timestamp") or m.get("originalTimestamp") or m.get("sentAt") or m.get("receivedAt"))
        if kind == "identify":
            traits = m.get("traits") or (m.get("context") or {}).get("traits") or {}
            if not m.get("userId"):
                rejected.append({"source": "segment", "reason": "identify without userId", "payload": m})
                continue
            users.append({"user_id": str(m["userId"]), "email": (traits.get("email") or "").strip().lower() or None,
                          "traits": traits, "seen_at": _iso(at or received)})
            continue
        if kind not in ("track", "page", "screen"):
            continue
        raw_name = m.get("event") if kind == "track" else f"{kind}_viewed"
        if not raw_name:
            rejected.append({"source": "segment", "reason": "track call without an event name", "payload": m})
            continue
        name = SEGMENT_SPEC_TO_PLAN.get(raw_name, snake(raw_name))
        props = dict(m.get("properties") or {})
        if kind == "page" and m.get("name"):
            props.setdefault("name", m["name"])
        row = {"event_id": m.get("messageId"), "user_id": str(m["userId"]) if m.get("userId") else None,
               "anonymous_id": str(m["anonymousId"]) if m.get("anonymousId") else None, "event_name": name, "properties": props,
               "source": "segment", "_at": at}
        ok, why = _common(row, "segment", received)
        if ok:
            events.append(ok)
        else:
            rejected.append({"source": "segment", "reason": why, "payload": m})
    return events, users, rejected


def _ga4_items(items: Any) -> List[Dict[str, Any]]:
    out = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and it.get("item_id"):
            out.append({"product_id": str(it["item_id"]), "name": it.get("item_name"), "quantity": int(it.get("quantity", 1) or 1),
                        "price": round(float(it.get("price", 0) or 0), 2)})
    return out


def _ga4_to_plan(name: str, p: Dict[str, Any], event_id: str) -> Dict[str, Any]:
    """GA4 recommended-event parameters to the tracking plan's property names."""
    currency = str(p.get("currency", "USD")).upper()
    if name == "order_completed":
        return {"order_id": str(p.get("transaction_id", event_id)), "net_amount": round(float(p.get("value", 0) or 0), 2), "currency": currency}
    if name == "checkout_started":
        return {"checkout_id": str(p.get("checkout_id", event_id)), "value": round(float(p.get("value", 0) or 0), 2), "currency": currency,
                "items": _ga4_items(p.get("items"))}
    if name == "product_viewed":
        items = p.get("items") if isinstance(p.get("items"), list) else []
        first = items[0] if items and isinstance(items[0], dict) else {}
        out = {"product_id": str(first.get("item_id", p.get("item_id", ""))) or None, "category": first.get("item_category"), "currency": currency}
        if first.get("price") is not None:
            out["price"] = round(float(first["price"]), 2)
        return {k: v for k, v in out.items() if v is not None}
    if name == "signed_up":
        return {"method": str(p.get("method", "email")).lower(), "plan": str(p.get("plan", "free"))}
    return p


def from_ga4(doc: Dict[str, Any], received: Optional[datetime] = None) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    received = received or datetime.now(timezone.utc)
    if not isinstance(doc, dict) or not isinstance(doc.get("events"), list):
        return [], [], [{"source": "ga4", "reason": "expected a Measurement Protocol body with an events array", "payload": doc}]
    base_at = _utc(doc.get("timestamp_micros")) or received
    anon = doc.get("client_id") or doc.get("app_instance_id")
    uid = doc.get("user_id")
    events, rejected = [], []
    for i, e in enumerate(doc["events"][:25]):
        params = dict(e.get("params") or {})
        at = _utc(params.pop("timestamp_micros", None)) or base_at
        event_id = params.pop("event_id", None)
        if not event_id and e.get("name") == "purchase" and params.get("transaction_id"):
            event_id = f"purchase:{params['transaction_id']}"
        if not event_id:
            event_id = hashlib.sha1(f"{anon}|{uid}|{e.get('name')}|{at.isoformat()}|{i}".encode()).hexdigest()
        name = GA4_TO_PLAN.get(e.get("name", ""), snake(e.get("name", "")))
        params = _ga4_to_plan(name, params, str(event_id))
        if name == "session_started":
            params = {"platform": "web" if doc.get("client_id") else "ios"}
        for k in ("engagement_time_msec", "session_id", "debug_mode"):
            params.pop(k, None)
        row = {"event_id": str(event_id), "user_id": str(uid) if uid else None, "anonymous_id": str(anon) if anon else None,
               "event_name": name, "properties": params, "source": "ga4", "_at": at}
        ok, why = _common(row, "ga4", received)
        if ok:
            events.append(ok)
        else:
            rejected.append({"source": "ga4", "reason": why, "payload": e})
    if len(doc["events"]) > 25:
        rejected.append({"source": "ga4", "reason": f"{len(doc['events']) - 25} events beyond the Measurement Protocol limit of 25 per request",
                         "payload": None})
    return events, [], rejected


# ----------------------------------------------------------------------------------------------
# sink
# ----------------------------------------------------------------------------------------------

class Sink:
    """Append-only NDJSON by UTC day, deduplicated on event_id within this process (recent 200,000 ids)."""

    def __init__(self, root: Path, dedupe: int = 200_000) -> None:
        self.root = Path(root)
        self.seen: "OrderedDict[str, None]" = OrderedDict()
        self.dedupe = dedupe
        self.lock = threading.Lock()

    def _write(self, folder: str, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        day = datetime.now(timezone.utc).date().isoformat()
        path = self.root / folder / f"{day}.ndjson"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    def put(self, events: List[Dict[str, Any]], users: List[Dict[str, Any]], rejected: List[Dict[str, Any]]) -> Dict[str, int]:
        with self.lock:
            fresh = []
            dupes = 0
            for e in events:
                if e["event_id"] in self.seen:
                    dupes += 1
                    continue
                self.seen[e["event_id"]] = None
                if len(self.seen) > self.dedupe:
                    self.seen.popitem(last=False)
                fresh.append(e)
            self._write("track_events", fresh)
            self._write("users", users)
            stamp = datetime.now(timezone.utc).isoformat()
            self._write("quarantine", [{**r, "quarantined_at": stamp} for r in rejected])
        return {"accepted": len(fresh), "duplicates": dupes, "users": len(users), "quarantined": len(rejected),
                "outside_tracking_plan": sum(1 for e in fresh if not e.get("in_tracking_plan"))}


def to_csv(events: List[Dict[str, Any]], folder: Path) -> Path:
    """Write the canonical track_events.csv (the shape tests/fixtures uses) for the cohort analyst."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "track_events.csv"
    new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["event_id", "user_id", "anonymous_id", "event_name", "event_at", "properties"])
        for e in events:
            w.writerow([e["event_id"], e.get("user_id") or "", e.get("anonymous_id") or "", e["event_name"], e["event_at"],
                        json.dumps(e["properties"], separators=(",", ":"))])
    return path


# ----------------------------------------------------------------------------------------------
# HTTP app
# ----------------------------------------------------------------------------------------------

def create_app(events_dir: Optional[Path] = None):
    if FastAPI is None:
        raise RuntimeError("the HTTP app needs FastAPI: pip install -r requirements.txt")
    app = FastAPI(title="LMM Engine event ingest", version="2.1.0")
    sink = Sink(Path(events_dir or os.environ.get("LCM_EVENTS_DIR", "out/events")))
    allow_unsigned = os.environ.get("LCM_ALLOW_UNSIGNED") == "1"

    async def _body(request: Request) -> Tuple[bytes, Any]:
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            raise HTTPException(413, "body over 5 MB")
        try:
            return raw, json.loads(raw)
        except ValueError:
            raise HTTPException(400, "body is not JSON") from None

    @app.get("/healthz")
    def healthz() -> Dict[str, Any]:
        return {"ok": True, "tracking_plan_events": sorted(PLAN_EVENTS)}

    @app.post("/webhooks/segment")
    async def segment(request: Request) -> Dict[str, Any]:
        raw, doc = await _body(request)
        secret = os.environ.get("SEGMENT_SHARED_SECRET")
        if secret:
            if not verify_segment_signature(raw, request.headers.get("x-signature"), secret):
                raise HTTPException(401, "X-Signature does not match the shared secret")
        elif not allow_unsigned:
            raise HTTPException(503, "SEGMENT_SHARED_SECRET is not set; refusing unsigned events (LCM_ALLOW_UNSIGNED=1 for local tests)")
        return sink.put(*from_segment(doc))

    @app.post("/webhooks/ga4")
    async def ga4(request: Request) -> Dict[str, Any]:
        raw, doc = await _body(request)
        token = os.environ.get("LCM_GA4_TOKEN")
        if token:
            if not verify_token(request.headers.get("x-lcm-token"), token):
                raise HTTPException(401, "X-LCM-Token does not match")
        elif not allow_unsigned:
            raise HTTPException(503, "LCM_GA4_TOKEN is not set; refusing unauthenticated events (LCM_ALLOW_UNSIGNED=1 for local tests)")
        return sink.put(*from_ga4(doc))

    return app


def __getattr__(name: str):  # lazily build the ASGI app for `uvicorn scripts.webhook_parser:app`
    if name == "app":
        return create_app()
    raise AttributeError(name)


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Normalize Segment or GA4 payloads to canonical track_events")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("parse")
    p.add_argument("--source", required=True, choices=["segment", "ga4"])
    p.add_argument("payload", help="a JSON file: one payload, or a list of payloads")
    p.add_argument("--to-csv", help="append accepted events to <folder>/track_events.csv")
    a = ap.parse_args(argv)
    doc = json.loads(Path(a.payload).read_text(encoding="utf-8"))
    if a.source == "segment":
        ev, users, rej = from_segment(doc)
    else:
        docs = doc if isinstance(doc, list) else [doc]
        ev, users, rej = [], [], []
        for d in docs:
            e, u, r = from_ga4(d)
            ev, users, rej = ev + e, users + u, rej + r
    if a.to_csv:
        print(f"wrote {to_csv(ev, Path(a.to_csv))}")
    print(json.dumps({"accepted": ev, "users": users, "quarantined": rej}, indent=2, default=str))
    return 0 if not rej else 1


if __name__ == "__main__":
    sys.exit(_cli())
