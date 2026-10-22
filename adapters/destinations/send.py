"""Send ESP requests safely: dry run by default, and with --approve, batched, idempotent, retried and receipted.

Why: the daily warehouse job, the engagement sync and the build packs all end in "POST this to the ESP". Done by hand that
step double-sends on a rerun, hammers a rate limit, or retries a request the API rejected for good. This module is the one
place requests leave the machine (it becomes `lcm send`), and it never sends without both an explicit --approve flag and
the platform's key in an environment variable. Without them it prints exactly what it would send.

Inputs (one per run):
  --rows FILE            the pending rows of a warehouse job (CSV or JSON from cohort/job/pending_<dialect>.sql); with
                         --run, the platform, trigger event and flow come from the plan; one trigger event per row
  --requests DIR         request files written by adapters/destinations/event_payloads.py or payloads.py (complete
                         {method, url, headers, body} requests, or bare bodies with --platform)
  --profile-updates FILE person-attribute rows (user_id, email, lcm_sunset, lcm_engagement_tier, lcm_holdout_<flow>, ...)
                         turned into the platform's attribute-sync requests (payloads.attribute_sync)

Behavior with --approve:
  batching       each platform's documented limit (HubSpot events 500 per batch, contacts 100; Braze 75 objects per
                 /users/track request; Klaviyo profile import 10,000; Customer.io batch 450,000 bytes); oversized
                 request files are split, never sent as they are
  idempotency    every item carries a key: Klaviyo unique_id, HubSpot uuid, Customer.io messageId, Iterable id (an upsert).
                 Braze has no dedupe, so the receipt log is its client-side sent log (keyed on flow, user and anchor date)
  retries        429 and 5xx (and connection errors) are retried with exponential backoff and jitter, honoring
                 Retry-After (seconds or an HTTP date) and Braze's X-RateLimit-Retry-After / X-RateLimit-Reset; any
                 other 4xx is never retried. Create calls (lcm build --create) retry 429 only: a 5xx after a create may
                 have created the object, so a retry could make a second one
  receipts       a JSONL log in the run folder (send/receipts.jsonl): a rerun skips every item already accepted, so
                 nothing is sent twice; accepted warehouse rows go to send/accepted_rows.csv for the ledger's mark step
  summary        counts, and every failure by request with its HTTP status and the API's message

Vendor sources (retry rules): Klaviyo developers.klaviyo.com/en/docs/rate_limits_and_error_handling (Retry-After on 429;
retry 429 and 503 with backoff and jitter); HubSpot developers.hubspot.com/docs/developer-tooling/platform/usage-guidelines
(treat 5xx like 429); Braze braze.com/docs/api/api_limits (X-RateLimit-Retry-After, else X-RateLimit-Reset); Iterable
support.iterable.com/hc/en-us/articles/41044692130196 (429, exponential backoff); Salesforce Marketing Cloud rate limiting
errors (429 with Retry-After: 5). Customer.io publishes no retry guidance (UNVERIFIED): the same policy applies.

    python3 adapters/destinations/send.py --run out/ridgeview_winback_14d --rows pending.csv                # dry run
    python3 adapters/destinations/send.py --run out/ridgeview_winback_14d --rows pending.csv --approve      # sends
    python3 adapters/destinations/send.py --requests out/klaviyo_events --approve
    python3 adapters/destinations/send.py --platform braze --profile-updates sunset.csv --receipts out/sync/receipts.jsonl
"""
from __future__ import annotations

if __package__ in (None, ""):  # run as a file (python3 <path>): make the repository importable
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse
import csv
import email.utils
import hashlib
import json
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
SECRET_HEADERS = {"authorization", "api-key", "x-api-key"}
LIMITS = {"hubspot_events_batch": 500, "hubspot_contacts_upsert": 100, "braze_track": 75, "klaviyo_profile_import": 10_000}
CIO_BATCH_BYTES = 450_000
KEY_ENV = {"klaviyo": "KLAVIYO_PRIVATE_KEY", "hubspot": "HUBSPOT_PRIVATE_APP_TOKEN", "braze": "BRAZE_REST_API_KEY",
           "customerio": "CUSTOMERIO_PIPELINES_API_KEY_BASE64", "iterable": "ITERABLE_API_KEY", "sfmc": "SFMC_ACCESS_TOKEN"}


class MissingCredential(RuntimeError):
    """A ${PLACEHOLDER} in a request header has no environment variable behind it."""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()[:20]


def _bytes(obj: Any) -> int:
    return len(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def redact(headers: Mapping[str, str]) -> Dict[str, str]:
    """Headers safe to print: placeholders stay placeholders, and a literal secret is masked."""
    return {k: (v if PLACEHOLDER.search(v) or k.lower() not in SECRET_HEADERS else "***") for k, v in headers.items()}


# ----------------------------------------------------------------------------------------------
# the safe client
# ----------------------------------------------------------------------------------------------

@dataclass
class Policy:
    max_attempts: int = 6
    base_seconds: float = 1.0
    cap_seconds: float = 60.0
    max_retry_after: float = 900.0
    retry_5xx: bool = True


@dataclass
class Outcome:
    ok: bool
    status: Optional[int]
    attempts: int
    detail: str
    body: Any = None
    waits: List[float] = field(default_factory=list)


def retry_after_seconds(headers: Mapping[str, str], now: Optional[float] = None) -> Optional[float]:
    """Seconds to wait before retrying, from Retry-After (seconds or an HTTP date), Braze's X-RateLimit-Retry-After, or
    Braze's X-RateLimit-Reset (a Unix time). None when the response names no wait."""
    h = {k.lower(): v for k, v in headers.items()}
    now = time.time() if now is None else now
    for name in ("retry-after", "x-ratelimit-retry-after"):
        v = (h.get(name) or "").strip()
        if not v:
            continue
        try:
            return max(0.0, float(v))
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(v)
                return max(0.0, when.timestamp() - now)
            except (TypeError, ValueError):
                continue
    reset = (h.get("x-ratelimit-reset") or "").strip()
    if reset:
        try:
            r = float(reset)
            return max(0.0, r - now) if r > 1_000_000_000 else max(0.0, r)
        except ValueError:
            return None
    return None


class SafeClient:
    """One request at a time, with the credential filled from the environment only at send time.

    transport is an httpx transport (tests pass httpx.MockTransport); sleep and rand are injectable so retries are
    deterministic in tests."""

    def __init__(self, env: Mapping[str, str], transport: Any = None, sleep: Callable[[float], None] = time.sleep,
                 rand: Callable[[], float] = random.random, policy: Optional[Policy] = None, timeout: float = 30.0):
        self.env = env
        self.transport = transport
        self.sleep = sleep
        self.rand = rand
        self.policy = policy or Policy()
        self.timeout = timeout

    def missing(self, req: Dict[str, Any]) -> List[str]:
        names = {m for v in (req.get("headers") or {}).values() for m in PLACEHOLDER.findall(str(v))}
        names |= set(PLACEHOLDER.findall(str(req.get("url", ""))))
        return sorted(n for n in names if not self.env.get(n))

    def _fill(self, text: str) -> str:
        return PLACEHOLDER.sub(lambda m: self.env[m.group(1)], text)

    def backoff(self, attempt: int) -> float:
        """Exponential backoff with equal jitter: half the step fixed, half random (attempt 1 is the first retry)."""
        step = min(self.policy.cap_seconds, self.policy.base_seconds * (2 ** (attempt - 1)))
        return step / 2 + step / 2 * self.rand()

    def request(self, req: Dict[str, Any], retry_5xx: Optional[bool] = None) -> Outcome:
        import httpx
        miss = self.missing(req)
        if miss:
            raise MissingCredential("missing environment variable(s): " + ", ".join(miss))
        retry_5xx = self.policy.retry_5xx if retry_5xx is None else retry_5xx
        headers = {k: self._fill(str(v)) for k, v in (req.get("headers") or {}).items()}
        url = self._fill(str(req["url"]))
        method = req.get("method", "POST").upper()
        body = req.get("body")
        waits: List[float] = []
        detail, status = "", None
        with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
            for attempt in range(1, self.policy.max_attempts + 1):
                try:
                    kw: Dict[str, Any] = {"headers": headers}
                    if body is not None and method not in ("GET", "DELETE"):
                        kw["content"] = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                    resp = client.request(method, url, **kw)
                except httpx.TransportError as e:
                    status, detail = None, f"{type(e).__name__}: {e}"
                    retryable, wait = retry_5xx, None
                else:
                    status = resp.status_code
                    if 200 <= status < 300:
                        try:
                            parsed = resp.json() if resp.content else None
                        except ValueError:
                            parsed = resp.text
                        return Outcome(True, status, attempt, f"HTTP {status}", parsed, waits)
                    detail = f"HTTP {status}: {resp.text[:300]}"
                    retryable = status == 429 or (status >= 500 and retry_5xx)
                    wait = retry_after_seconds(resp.headers)
                if not retryable or attempt == self.policy.max_attempts:
                    return Outcome(False, status, attempt, detail, None, waits)
                delay = min(wait, self.policy.max_retry_after) if wait is not None else self.backoff(attempt)
                waits.append(round(delay, 3))
                self.sleep(delay)
        return Outcome(False, status, self.policy.max_attempts, detail, None, waits)


# ----------------------------------------------------------------------------------------------
# items, idempotency keys and batching per endpoint
# ----------------------------------------------------------------------------------------------

def endpoint_kind(req: Dict[str, Any]) -> str:
    url, body = str(req.get("url", "")), req.get("body") or {}
    if "klaviyo.com/api/events" in url:
        return "klaviyo_event"
    if "klaviyo.com/api/profile-bulk-import-jobs" in url:
        return "klaviyo_profile_import"
    if "hubapi.com/events" in url:
        return "hubspot_events_batch" if url.rstrip("/").endswith("/batch") else "hubspot_event"
    if "hubapi.com/crm" in url and "batch/upsert" in url:
        return "hubspot_contacts_upsert"
    if "braze.com/users/track" in url:
        return "braze_track"
    if "customer.io/v1/track" in url or "track.customer.io" in url:
        return "customerio_track"
    if "customer.io/v1/batch" in url:
        return "customerio_batch"
    if "iterable.com/api/events/track" in url:
        return "iterable_track"
    return "generic"


def _braze_key(kind: str, obj: Dict[str, Any]) -> str:
    props = obj.get("properties") or {}
    if kind == "events" and props.get("flow") and props.get("anchor_date"):
        return f"{props['flow']}:{obj.get('external_id')}:{props['anchor_date']}"
    stable = {k: v for k, v in obj.items() if k != "time"}
    return f"braze:{kind}:{obj.get('external_id')}:{_sha(stable)}"


def unpack(req: Dict[str, Any]) -> Tuple[str, List[Tuple[str, Any]], Callable[[List[Any]], Dict[str, Any]]]:
    """(kind, [(idempotency key, item)], rebuild(items) -> request) for one request."""
    kind = endpoint_kind(req)
    body = req.get("body") or {}

    def same(items: List[Any]) -> Dict[str, Any]:
        return dict(req, body=items[0])

    if kind == "klaviyo_event":
        attrs = body.get("data", {}).get("attributes", {})
        return kind, [(str(attrs.get("unique_id") or _sha(body)), body)], same
    if kind == "hubspot_event":
        return kind, [(str(body.get("uuid") or _sha(body)), body)], same
    if kind == "hubspot_events_batch":
        items = [(str(i.get("uuid") or _sha(i)), i) for i in body.get("inputs", [])]
        return kind, items, lambda xs: dict(req, body={**body, "inputs": xs})
    if kind == "hubspot_contacts_upsert":
        items = [(f"{i.get('idProperty')}:{i.get('id')}:{_sha(i.get('properties'))}", i) for i in body.get("inputs", [])]
        return kind, items, lambda xs: dict(req, body={**body, "inputs": xs})
    if kind == "klaviyo_profile_import":
        data = body["data"]
        profs = data["attributes"]["profiles"]["data"]
        items = [(f"profile:{p['attributes'].get('email') or p['attributes'].get('external_id')}:{_sha(p)}", p) for p in profs]

        def rebuild(xs: List[Any]) -> Dict[str, Any]:
            d = json.loads(json.dumps(data))
            d["attributes"]["profiles"]["data"] = xs
            return dict(req, body={**body, "data": d})
        return kind, items, rebuild
    if kind == "braze_track":
        items = [(_braze_key(k, o), (k, o)) for k in ("attributes", "events", "purchases") for o in body.get(k, [])]

        def rebuild_braze(xs: List[Any]) -> Dict[str, Any]:
            out: Dict[str, Any] = {k: v for k, v in body.items() if k not in ("attributes", "events", "purchases")}
            for k, o in xs:
                out.setdefault(k, []).append(o)
            return dict(req, body=out)
        return kind, items, rebuild_braze
    if kind == "customerio_track":
        return kind, [(str(body.get("messageId") or body.get("id") or _sha(body)), body)], same
    if kind == "customerio_batch":
        items = [(str(c.get("messageId") or f"{c.get('type')}:{c.get('userId')}:{_sha(c)}"), c) for c in body.get("batch", [])]
        return kind, items, lambda xs: dict(req, body={**body, "batch": xs})
    if kind == "iterable_track":
        return kind, [(str(body.get("id") or _sha(body)), body)], same
    return kind, [(_sha({"url": req.get("url"), "body": body}), body)], same


def _chunks(kind: str, items: List[Tuple[str, Any]]) -> List[List[Tuple[str, Any]]]:
    if kind == "customerio_batch":
        out: List[List[Tuple[str, Any]]] = [[]]
        size = 0
        for it in items:
            b = _bytes(it[1]) + 1
            if out[-1] and size + b + 64 > CIO_BATCH_BYTES:
                out.append([])
                size = 0
            out[-1].append(it)
            size += b
        return [c for c in out if c]
    limit = LIMITS.get(kind, 1)          # single-item endpoints send one item per request
    return [items[i:i + limit] for i in range(0, len(items), limit)]


@dataclass
class Planned:
    request: Dict[str, Any]
    keys: List[str]
    kind: str


def plan_requests(reqs: Sequence[Dict[str, Any]], accepted: Optional[set] = None) -> Tuple[List[Planned], int]:
    """Requests to send: items already accepted are dropped, and every request is cut to its endpoint's limit.
    Returns (planned requests, number of items skipped as already accepted)."""
    accepted = accepted or set()
    out: List[Planned] = []
    skipped = 0
    seen: set = set()
    for req in reqs:
        kind, items, rebuild = unpack(req)
        fresh = []
        for key, item in items:
            if key in accepted or key in seen:
                skipped += 1
                continue
            seen.add(key)
            fresh.append((key, item))
        for chunk in _chunks(kind, fresh):
            out.append(Planned(rebuild([i for _, i in chunk]), [k for k, _ in chunk], kind))
    return out, skipped


# ----------------------------------------------------------------------------------------------
# receipts
# ----------------------------------------------------------------------------------------------

class ReceiptLog:
    """Append-only JSONL: one line per request sent, with the item keys it carried and whether it was accepted."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def accepted(self) -> set:
        keys: set = set()
        if not self.path.is_file():
            return keys
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("status") == "accepted":
                keys.update(rec.get("items") or [])
        return keys

    def append(self, rec: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": now_iso(), **rec}, default=str) + "\n")


def send_all(planned: List[Planned], client: SafeClient, log: ReceiptLog, platform: str, retry_5xx: bool = True) -> Dict[str, Any]:
    """Send planned requests in order, logging each. Failures never stop the run: each is reported, and its items stay
    unaccepted so the next run sends them."""
    summary: Dict[str, Any] = {"requests_sent": 0, "accepted_requests": 0, "accepted_items": 0, "failed_requests": 0, "failures": []}
    for i, p in enumerate(planned, 1):
        out = client.request(p.request, retry_5xx=retry_5xx)
        summary["requests_sent"] += 1
        status = "accepted" if out.ok else "failed"
        log.append({"platform": platform, "request": i, "kind": p.kind, "url": p.request.get("url"), "items": p.keys, "status": status,
                    "http": out.status, "attempts": out.attempts, "waits": out.waits, "detail": out.detail})
        if out.ok:
            summary["accepted_requests"] += 1
            summary["accepted_items"] += len(p.keys)
        else:
            summary["failed_requests"] += 1
            summary["failures"].append({"request": i, "kind": p.kind, "http": out.status, "attempts": out.attempts, "items": p.keys[:5],
                                        "item_count": len(p.keys), "detail": out.detail})
    return summary


# ----------------------------------------------------------------------------------------------
# inputs
# ----------------------------------------------------------------------------------------------

def read_rows(path: Path) -> List[Dict[str, Any]]:
    from adapters.destinations.payloads import read_rows as _read
    return _read(Path(path))


def _bare_body_request(body: Dict[str, Any], platform: Optional[str]) -> Dict[str, Any]:
    """A request for a bare body written by payloads.py's CLI (Klaviyo profile import or HubSpot contacts upsert)."""
    from adapters.destinations import payloads
    if isinstance(body, dict) and body.get("data", {}).get("type") == "profile-bulk-import-job":
        return {"method": "POST", "url": "https://a.klaviyo.com/api/profile-bulk-import-jobs",
                "headers": {"Authorization": "Klaviyo-API-Key ${KLAVIYO_PRIVATE_KEY}", "revision": payloads.KLAVIYO_REVISION,
                            "Content-Type": "application/vnd.api+json", "accept": "application/vnd.api+json"}, "body": body}
    if isinstance(body, dict) and body.get("inputs") and "idProperty" in body["inputs"][0]:
        return {"method": "POST", "url": f"https://api.hubapi.com/crm/objects/{payloads.HUBSPOT_VERSION}/contacts/batch/upsert",
                "headers": {"Authorization": "Bearer ${HUBSPOT_PRIVATE_APP_TOKEN}", "Content-Type": "application/json"}, "body": body}
    raise ValueError(f"a bare request body that is not a Klaviyo profile import or a HubSpot contacts upsert (platform {platform}): "
                     "write complete {method, url, headers, body} requests")


def requests_from_folder(folder: Path, platform: Optional[str] = None) -> List[Dict[str, Any]]:
    out = []
    for f in sorted(Path(folder).glob("*.json")):
        doc = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(doc, dict) and {"url", "body"} <= set(doc):
            out.append(doc)
        else:
            out.append(_bare_body_request(doc, platform))
    return out


def plan_context(run: Path) -> Dict[str, Any]:
    """Platform, trigger event, flow slug and the payload settings, from a plan folder."""
    from adapters.destinations.build.context import load_plan, plan_folder
    folder = plan_folder(run)
    p = load_plan(folder)
    return {"folder": folder, "platform": p.brief.platform, "event": p.cohort.trigger_event, "flow": p.brief.slug,
            "trigger_source": p.cohort.trigger_source}


def requests_from_rows(rows: List[Dict[str, Any]], platform: str, event: str, flow: str, as_of: str, hub_id: str = "00000000",
                       braze_instance: str = "iad-01", eu: bool = False) -> List[Dict[str, Any]]:
    from adapters.destinations import event_payloads
    if platform == "sfmc":
        raise ValueError("Salesforce Marketing Cloud entry is a Journey Builder API Event: it is not sent by lcm send")
    events = event_payloads.rows_to_events(rows, event, flow, as_of)
    return event_payloads.batches(platform, events, hub_id=hub_id, braze_instance=braze_instance, eu=eu)


def requests_from_profile_updates(rows: List[Dict[str, Any]], platform: str, braze_instance: str = "iad-01",
                                  eu: bool = False) -> List[Dict[str, Any]]:
    from adapters.destinations import payloads
    if platform not in payloads.PLATFORMS:
        raise ValueError(f"profile updates are built for {', '.join(payloads.PLATFORMS)}; {platform} is not supported yet "
                         "(its user-update endpoint is not in the verified research)")
    reqs, _report = payloads.attribute_sync(platform, rows, braze_instance=braze_instance, eu=eu)
    return reqs


def _platform_of(reqs: List[Dict[str, Any]]) -> Optional[str]:
    for r in reqs:
        url = str(r.get("url", ""))
        for host, name in (("klaviyo.com", "klaviyo"), ("hubapi.com", "hubspot"), ("braze.com", "braze"), ("customer.io", "customerio"),
                           ("iterable.com", "iterable"), ("marketingcloudapis.com", "sfmc")):
            if host in url:
                return name
    return None


def accepted_rows(rows: List[Dict[str, Any]], flow: str, accepted: set) -> List[Dict[str, str]]:
    """Warehouse rows whose trigger event was accepted, for the ledger's mark step (cohort/job/mark_<dialect>.sql)."""
    out = []
    for r in rows:
        anchor = str(r.get("anchor_date") or "")[:10]
        if f"{flow}:{r.get('user_id')}:{anchor}" in accepted:
            out.append({"user_id": str(r.get("user_id")), "anchor_date": anchor})
    return out


def _cli(argv: Optional[List[str]] = None, env: Optional[Mapping[str, str]] = None, transport: Any = None,
         sleep: Callable[[float], None] = time.sleep) -> int:
    import os
    ap = argparse.ArgumentParser(description="Send ESP requests: a dry run unless --approve, then batched, idempotent, retried, receipted")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--rows", help="pending warehouse-job rows (CSV or JSON)")
    src.add_argument("--requests", help="folder of request files (event_payloads.py or payloads.py output)")
    src.add_argument("--profile-updates", help="person-attribute rows (CSV or JSON) to sync")
    ap.add_argument("--run", help="the plan folder: platform, event and flow come from it, and receipts go to <run>/send/")
    ap.add_argument("--platform", choices=sorted(KEY_ENV))
    ap.add_argument("--event", help="trigger event name, with --rows when there is no --run")
    ap.add_argument("--flow", help="flow slug, with --rows when there is no --run")
    ap.add_argument("--as-of", default=datetime.now(timezone.utc).date().isoformat())
    ap.add_argument("--hub-id", default=None, help="HubSpot portal id (default: HUBSPOT_HUB_ID)")
    ap.add_argument("--braze-instance", default=None, help="Braze REST instance (default: BRAZE_INSTANCE or iad-01)")
    ap.add_argument("--eu", action="store_true", help="Customer.io or Iterable EU data center")
    ap.add_argument("--receipts", help="receipt log path (default: <run>/send/receipts.jsonl, else beside the input)")
    ap.add_argument("--approve", action="store_true", help="really send (needs the platform's key in the environment)")
    ap.add_argument("--max-attempts", type=int, default=6)
    a = ap.parse_args(argv)
    env = os.environ if env is None else env

    ctx: Dict[str, Any] = plan_context(Path(a.run)) if a.run else {}
    platform = a.platform or ctx.get("platform")
    hub_id = a.hub_id or env.get("HUBSPOT_HUB_ID", "00000000")
    braze_instance = a.braze_instance or env.get("BRAZE_INSTANCE", "iad-01")
    rows: List[Dict[str, Any]] = []
    flow = a.flow or ctx.get("flow")
    try:
        if a.rows:
            event = a.event or ctx.get("event")
            if not (platform and event and flow):
                ap.error("--rows needs --run, or --platform, --event and --flow")
            rows = read_rows(Path(a.rows))
            reqs = requests_from_rows(rows, platform, event, flow, a.as_of, hub_id, braze_instance, a.eu)
            source = Path(a.rows)
        elif a.requests:
            reqs = requests_from_folder(Path(a.requests), platform)
            platform = platform or _platform_of(reqs)
            source = Path(a.requests)
        else:
            if not platform:
                ap.error("--profile-updates needs --platform (or --run)")
            reqs = requests_from_profile_updates(read_rows(Path(a.profile_updates)), platform, braze_instance, a.eu)
            source = Path(a.profile_updates)
    except ValueError as e:
        print(f"lcm send: {e}")
        return 2
    receipts = Path(a.receipts) if a.receipts else ((ctx["folder"] / "send" / "receipts.jsonl") if ctx else
                                                    (source if source.is_dir() else source.parent) / "send" / "receipts.jsonl")
    log = ReceiptLog(receipts)
    accepted = log.accepted()
    planned, skipped = plan_requests(reqs, accepted)
    items = sum(len(p.keys) for p in planned)
    head: Dict[str, Any] = {"platform": platform, "items_to_send": items, "already_accepted": skipped, "requests": len(planned),
                            "receipts": str(receipts)}
    if not a.approve:
        first = planned[0].request if planned else None
        print(json.dumps({"mode": "dry run: nothing was sent (add --approve, with the platform's key in the environment)", **head,
                          "first_request": {**first, "headers": redact(first.get("headers") or {})} if first else None}, indent=2, default=str))
        return 0
    if not planned:
        print(json.dumps({"mode": "approved", **head, "sent": 0, "note": "nothing to send: every item is already accepted"}, indent=2))
        return 0
    client = SafeClient(env, transport=transport, sleep=sleep, policy=Policy(max_attempts=a.max_attempts))
    miss = client.missing(planned[0].request)
    if miss:
        print(f"lcm send: refusing to send: set {', '.join(miss)} in the environment (the key is read only at send time and never written)")
        return 2
    summary = send_all(planned, client, log, platform or "unknown")
    out: Dict[str, Any] = {"mode": "approved", **head, **summary}
    if rows and flow:
        done = accepted_rows(rows, flow, log.accepted())
        path = receipts.parent / "accepted_rows.csv"
        with path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["user_id", "anchor_date"])
            w.writeheader()
            w.writerows(done)
        out["accepted_rows"] = {"count": len(done), "path": str(path), "next": "run cohort/job/mark_<dialect>.sql once per row in it"}
    print(json.dumps(out, indent=2, default=str))
    return 1 if summary["failed_requests"] else 0


if __name__ == "__main__":
    sys.exit(_cli())
