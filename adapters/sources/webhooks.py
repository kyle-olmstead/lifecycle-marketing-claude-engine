"""Event stream ingest: Segment and GA4-shaped webhooks into the canonical track_events table.

The teammate's cohort queries read one table shape (docs/SQL_SCHEMA_CONVENTIONS.md):
event_id, user_id, anonymous_id, event_name, event_at, properties. This parser turns what a CDP
sends into exactly that, validates every event against the tracking plan
(config/tracking_plan.json), and writes:

  <LCM_EVENTS_DIR>/track_events/<date>.ndjson   accepted events, one per line, deduplicated on event_id
  <LCM_EVENTS_DIR>/users/<date>.ndjson          identify traits (email, consent timestamps) for the users table
  <LCM_EVENTS_DIR>/quarantine/<date>.ndjson     rejected events with the reason, never silently dropped

Load the ndjson into the warehouse with its native loader, or point the cohort analyst at a folder
of CSVs written with --to-csv.

Sources
  Segment  POST /webhooks/segment. Works with the Webhooks (Actions) destination and the classic
           Webhooks destination. Requests are signed: header X-Signature, hex HMAC-SHA1 of the body
           with the destination's shared secret, over the whole body (the raw bytes, or the same JSON
           compactly serialized). With batching on, Segment signs only the first event of a batch,
           which would let anyone holding one signed event append forged ones, so a batch is accepted
           only when its signature covers the whole body: turn batching off in the destination.
  GA4      POST /webhooks/ga4. GA4 does not push webhooks itself; this accepts the Measurement
           Protocol body shape ({client_id, user_id, timestamp_micros, events: [{name, params}]})
           as forwarded by server-side Google Tag Manager or a relay. Authenticated with a shared
           token in the X-LCM-Token header. GA4 recommended names are mapped to the tracking plan
           (sign_up to signed_up, purchase to order_completed, and so on); others are snake_cased.

Secrets come from the environment only: SEGMENT_SHARED_SECRET, LCM_GA4_TOKEN. With no secret set
the endpoint refuses traffic unless LCM_ALLOW_UNSIGNED=1 (local testing only). Replays: over HTTP,
events older than 72 hours are refused, and event ids already written in the last 3 days are
dropped as duplicates, including across restarts (the sink re-reads its recent files at start).

Real-time trigger forwarding (off by default). An activation flow starts on an event the ESP must see
as it happens, carrying the holdout assignment (engine/agents/campaign_architect.py build step 1). With
LCM_FORWARD=1, every accepted event that matches the qualifying event of an activation brief in
briefs/active/ (or LCM_BRIEFS_DIR) is sent on to that brief's platform, once per brief, with flow,
holdout_bucket and in_holdout added, computed with the same formula as the warehouse
(engine/sql/dialects.py). Requests are built by adapters/destinations/event_payloads.py; credentials come from the
environment (KLAVIYO_PRIVATE_KEY, HUBSPOT_PRIVATE_APP_TOKEN plus HUBSPOT_HUB_ID, BRAZE_REST_API_KEY
plus BRAZE_INSTANCE, CUSTOMERIO_PIPELINES_API_KEY_BASE64 plus CUSTOMERIO_EU=1 for EU workspaces). A
request with a missing credential is not sent. Failed requests are written to
<LCM_EVENTS_DIR>/forward_failed/<date>.ndjson to replay; win-back flows never forward here, since
an absence is computed by the daily warehouse job.

    uvicorn adapters.sources.webhooks:app --port 8080                       # serve
    python3 adapters/sources/webhooks.py parse --source segment payload.json --to-csv out/events   # offline
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

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import jsonschema  # noqa: E402

try:  # the HTTP app needs FastAPI; the offline parser does not
    from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
except ImportError:  # pragma: no cover
    BackgroundTasks = FastAPI = HTTPException = Request = None  # type: ignore[assignment,misc]

PLAN_PATH = ROOT / "config" / "tracking_plan.json"
PLAN = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
PLAN_EVENTS = set(PLAN["properties"]["event"]["enum"])
MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_FUTURE = timedelta(hours=1)
HTTP_MAX_AGE = timedelta(hours=72)   # older events over HTTP are refused as possible replays; backfill through the CLI
DEDUPE_DAYS = 3

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
    """True only when the signature covers the whole body: its raw bytes, or the same JSON compactly serialized."""
    if not signature:
        return False
    sig = signature.strip().lower()
    candidates = [raw]
    try:
        candidates.append(json.dumps(json.loads(raw), separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
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
    from engine.schemas import FORMAT_CHECKER
    v = jsonschema.Draft202012Validator(PLAN, format_checker=FORMAT_CHECKER)
    return [f"{'/'.join(map(str, e.path)) or 'event'}: {e.message}" for e in v.iter_errors(doc)][:5]


def _common(row: Dict[str, Any], source: str, received: datetime,
            max_age: Optional[timedelta] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not row.get("event_id"):
        return None, "no idempotency key (Segment messageId, or an event_id param for GA4)"
    if not (row.get("user_id") or row.get("anonymous_id")):
        return None, "no user_id or anonymous_id"
    if row["_at"] is None:
        return None, "missing or unreadable timestamp"
    if row["_at"] > received + MAX_FUTURE:
        return None, f"timestamp {row['_at'].isoformat()} is in the future: client clock is wrong"
    if max_age is not None and row["_at"] < received - max_age:
        return None, f"timestamp {row['_at'].isoformat()} is more than {max_age.days * 24 + max_age.seconds // 3600} hours old: refused over HTTP as a possible replay; backfill with the CLI"
    row["event_at"] = _iso(row.pop("_at"))
    errs = _validate(row["event_name"], row, source)
    if errs:
        return None, "does not match the tracking plan: " + "; ".join(errs)
    row["in_tracking_plan"] = row["event_name"] in PLAN_EVENTS
    return row, None


def from_segment(doc: Any, received: Optional[datetime] = None,
                 max_age: Optional[timedelta] = None) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
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
            why = ("identify without userId" if not m.get("userId") else
                   "identify without a messageId (its idempotency key)" if not m.get("messageId") else
                   "missing or unreadable timestamp" if at is None else
                   "identify timestamp is in the future" if at > received + MAX_FUTURE else
                   "identify is more than 72 hours old: refused over HTTP as a possible replay" if max_age is not None and at < received - max_age
                   else None)
            if why:
                rejected.append({"source": "segment", "reason": why, "payload": m})
                continue
            users.append({"message_id": str(m["messageId"]), "user_id": str(m["userId"]),
                          "email": (traits.get("email") or "").strip().lower() or None, "traits": traits, "seen_at": _iso(at)})
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
        ok, why = _common(row, "segment", received, max_age)
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


def from_ga4(doc: Dict[str, Any], received: Optional[datetime] = None,
             max_age: Optional[timedelta] = None) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
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
        ok, why = _common(row, "ga4", received, max_age)
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
    """Append-only NDJSON by UTC day, deduplicated on event_id. At start it re-reads the last DEDUPE_DAYS of files, so a
    restart does not reopen the door to replays. One process per events folder: run uvicorn with a single worker, or put
    a unique key on event_id where the files are loaded."""

    def __init__(self, root: Path, dedupe: int = 500_000) -> None:
        self.root = Path(root)
        self.seen: "OrderedDict[str, None]" = OrderedDict()
        self.dedupe = dedupe
        self.lock = threading.Lock()
        today = datetime.now(timezone.utc).date()
        for k in range(DEDUPE_DAYS, -1, -1):
            day = (today - timedelta(days=k)).isoformat()
            for folder, key, prefix in (("track_events", "event_id", ""), ("users", "message_id", "identify:")):
                path = self.root / folder / f"{day}.ndjson"
                if path.exists():
                    for line in path.read_text(encoding="utf-8").splitlines():
                        try:
                            self._remember(prefix + json.loads(line)[key])
                        except (ValueError, KeyError):
                            continue

    def _remember(self, event_id: str) -> None:
        self.seen[event_id] = None
        if len(self.seen) > self.dedupe:
            self.seen.popitem(last=False)

    def _write(self, folder: str, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        day = datetime.now(timezone.utc).date().isoformat()
        path = self.root / folder / f"{day}.ndjson"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    def put(self, events: List[Dict[str, Any]], users: List[Dict[str, Any]], rejected: List[Dict[str, Any]],
            fresh_out: Optional[List[Dict[str, Any]]] = None) -> Dict[str, int]:
        """Write what is new. fresh_out, when given, receives the events accepted by this call (not duplicates)."""
        with self.lock:
            fresh = []
            dupes = 0
            for e in events:
                if e["event_id"] in self.seen:
                    dupes += 1
                    continue
                self._remember(e["event_id"])
                fresh.append(e)
            fresh_users = []
            for u in users:
                key = f"identify:{u['message_id']}"
                if key in self.seen:
                    dupes += 1
                    continue
                self._remember(key)
                fresh_users.append(u)
            self._write("track_events", fresh)
            self._write("users", fresh_users)
            if fresh_out is not None:
                fresh_out.extend(fresh)
            users = fresh_users
            stamp = datetime.now(timezone.utc).isoformat()
            self._write("quarantine", [{**r, "quarantined_at": stamp} for r in rejected])
        return {"accepted": len(fresh), "duplicates": dupes, "users": len(users), "quarantined": len(rejected),
                "outside_tracking_plan": sum(1 for e in fresh if not e.get("in_tracking_plan"))}


def to_csv(events: List[Dict[str, Any]], folder: Path) -> Path:
    """Write the canonical track_events.csv (the shape examples/brands/ridgeview_saas/data uses) for the cohort analyst."""
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
# real-time trigger forwarding (opt-in)
# ----------------------------------------------------------------------------------------------

PLACEHOLDER = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _text(v: Any) -> str:
    return ("true" if v else "false") if isinstance(v, bool) else str(v)


class Forwarder:
    """Sends qualifying events for activation briefs on to the ESP, with the holdout assignment added."""

    def __init__(self, briefs: List[Any], root: Path, env: Optional[Dict[str, str]] = None, transport: Any = None, timeout: float = 5.0) -> None:
        self.briefs = [b for b in briefs if b.flow_type == "activation"]
        self.root = Path(root)
        self.env = dict(os.environ if env is None else env)
        self.transport = transport
        self.timeout = timeout

    @classmethod
    def from_env(cls, root: Path, env: Optional[Dict[str, str]] = None, transport: Any = None) -> Optional["Forwarder"]:
        env = dict(os.environ if env is None else env)
        if env.get("LCM_FORWARD") != "1":
            return None
        from engine.brief import load_brief
        from engine import workspace
        folder = Path(env.get("LCM_BRIEFS_DIR") or workspace.find(env=env).active_briefs)
        briefs = [load_brief(p) for p in sorted(folder.glob("*.yaml"))]
        return cls(briefs, root, env, transport)

    @staticmethod
    def matches(brief: Any, e: Dict[str, Any]) -> bool:
        q = brief.qualifying_event
        props = e.get("properties") or {}
        return bool(e.get("user_id")) and e["event_name"] == q.event and all(_text(props.get(k)) == _text(v) for k, v in q.where.items())

    def requests(self, events: List[Dict[str, Any]]) -> List[Tuple[Any, Dict[str, Any], Optional[str]]]:
        """(brief, request, reason it cannot be sent or None) for every event and matching brief."""
        from engine.sql.dialects import holdout_bucket
        from adapters.destinations import event_payloads
        out = []
        for e in events:
            for b in self.briefs:
                if not self.matches(b, e):
                    continue
                bucket = holdout_bucket(str(e["user_id"]), b.slug)
                props = {k: v for k, v in (e.get("properties") or {}).items() if k not in event_payloads.BRAZE_RESERVED}
                props.update(flow=b.slug, holdout_bucket=bucket, in_holdout=bucket < b.holdout_pct)
                ev = {"user_id": e["user_id"], "email": props.pop("email", None), "event": e["event_name"], "properties": props,
                      "occurred_at": e["event_at"], "event_id": f"{e['event_id']}:{b.slug}"}
                reason = None
                if b.platform == "sfmc":
                    out.append((b, {}, "Salesforce Marketing Cloud entry is a Journey Builder API Event; forward it from your own integration"))
                    continue
                if b.platform == "hubspot" and not ev["email"]:
                    reason = "HubSpot custom events need the contact's email on the event"
                try:
                    req = event_payloads.build(b.platform, ev, hub_id=self.env.get("HUBSPOT_HUB_ID", "0").strip(),
                                               braze_instance=self.env.get("BRAZE_INSTANCE", "iad-01").strip(), eu=self.env.get("CUSTOMERIO_EU") == "1")
                except Exception as err:  # noqa: BLE001 - one brief's bad setting must not stop the others
                    out.append((b, {"event": ev}, f"could not build the request: {type(err).__name__}: {err}"))
                    continue
                missing = sorted({m for v in req["headers"].values() for m in PLACEHOLDER.findall(v) if not self.env.get(m)})
                if b.platform == "hubspot" and not self.env.get("HUBSPOT_HUB_ID"):
                    missing.append("HUBSPOT_HUB_ID")
                if missing and not reason:
                    reason = "missing credential: " + ", ".join(missing)
                out.append((b, req, reason))
        return out

    def forward(self, events: List[Dict[str, Any]]) -> Dict[str, int]:
        import httpx
        result = {"forwarded": 0, "not_sent": 0, "failed": 0}
        failed: List[Dict[str, Any]] = []
        todo = self.requests(events)
        if not todo:
            return result
        with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
            for b, req, reason in todo:
                if reason:
                    result["not_sent"] += 1
                    failed.append({"flow": b.slug, "reason": reason, "request": req})
                    continue
                headers = {k: PLACEHOLDER.sub(lambda m: self.env[m.group(1)], v) for k, v in req["headers"].items()}
                try:
                    resp = client.request(req["method"], req["url"], headers=headers, json=req["body"])
                    ok = 200 <= resp.status_code < 300
                    detail = f"HTTP {resp.status_code}: {resp.text[:200]}"
                except httpx.HTTPError as e:
                    ok, detail = False, f"{type(e).__name__}: {e}"
                if ok:
                    result["forwarded"] += 1
                else:
                    result["failed"] += 1
                    failed.append({"flow": b.slug, "reason": detail, "request": req})   # headers still hold ${PLACEHOLDER}s, never secrets
        if failed:
            day = datetime.now(timezone.utc).date().isoformat()
            path = self.root / "forward_failed" / f"{day}.ndjson"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                for f in failed:
                    fh.write(json.dumps({**f, "at": datetime.now(timezone.utc).isoformat()}, default=str) + "\n")
        return result


# ----------------------------------------------------------------------------------------------
# HTTP app
# ----------------------------------------------------------------------------------------------

def create_app(events_dir: Optional[Path] = None):
    if FastAPI is None:
        raise RuntimeError("the HTTP app needs FastAPI: pip install -r requirements.txt")
    from engine import __version__
    app = FastAPI(title="LMM Engine event ingest", version=__version__)
    root = Path(events_dir or os.environ.get("LCM_EVENTS_DIR", "out/events"))
    sink = Sink(root)
    allow_unsigned = os.environ.get("LCM_ALLOW_UNSIGNED") == "1"
    forwarder = Forwarder.from_env(root)

    def _accept(parsed: Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]], background: BackgroundTasks) -> Dict[str, Any]:
        fresh: List[Dict[str, Any]] = []
        result: Dict[str, Any] = sink.put(*parsed, fresh_out=fresh)
        if forwarder is not None and fresh:
            background.add_task(forwarder.forward, fresh)   # after the response: the CDP is never kept waiting on the ESP
            result["forwarding"] = len([1 for e in fresh for b in forwarder.briefs if forwarder.matches(b, e)])
        return result

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
        return {"ok": True, "tracking_plan_events": sorted(PLAN_EVENTS),
                "forwarding": None if forwarder is None else [f"{b.slug} ({b.platform}): {b.qualifying_event.event}" for b in forwarder.briefs]}

    @app.post("/webhooks/segment")
    async def segment(request: Request, background: BackgroundTasks) -> Dict[str, Any]:
        raw, doc = await _body(request)
        secret = os.environ.get("SEGMENT_SHARED_SECRET")
        if secret:
            if not verify_segment_signature(raw, request.headers.get("x-signature"), secret):
                raise HTTPException(401, "X-Signature does not match the shared secret")
        elif not allow_unsigned:
            raise HTTPException(503, "SEGMENT_SHARED_SECRET is not set; refusing unsigned events (LCM_ALLOW_UNSIGNED=1 for local tests)")
        return _accept(from_segment(doc, max_age=HTTP_MAX_AGE), background)

    @app.post("/webhooks/ga4")
    async def ga4(request: Request, background: BackgroundTasks) -> Dict[str, Any]:
        raw, doc = await _body(request)
        token = os.environ.get("LCM_GA4_TOKEN")
        if token:
            if not verify_token(request.headers.get("x-lcm-token"), token):
                raise HTTPException(401, "X-LCM-Token does not match")
        elif not allow_unsigned:
            raise HTTPException(503, "LCM_GA4_TOKEN is not set; refusing unauthenticated events (LCM_ALLOW_UNSIGNED=1 for local tests)")
        return _accept(from_ga4(doc, max_age=HTTP_MAX_AGE), background)

    return app


def __getattr__(name: str):  # lazily build the ASGI app for `uvicorn adapters.sources.webhooks:app`
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
