"""
Turn segment rows into API request bodies for Klaviyo, HubSpot, Braze and Customer.io (the segment export step).

Input is the CSV or JSON Lines output of a segmentation query: one row per person, an identifier
column, and the properties to write. Output is one request body per batch, written to a folder,
ready to POST. Nothing is sent from here.

  Klaviyo  POST https://a.klaviyo.com/api/profile-bulk-import-jobs   (header: revision: <date>)
           up to 10,000 profiles and 5 MB per job, 100 KB per profile; null values cannot clear a
           property through this endpoint, so nulls are dropped and reported
  HubSpot  POST /crm/objects/<version>/contacts/batch/upsert          (date-versioned paths since 2026)
           up to 100 inputs per request; property internal names must already exist on the
           contact object (lowercase, letters, digits, underscores)

attribute_sync() builds the person-attribute updates engine/agents/engagement.py writes (lcm_engagement_tier,
lcm_sunset) for all four platforms, as complete requests with ${PLACEHOLDER} secrets:

  Braze        POST https://rest.<instance>.braze.com/users/track    up to 75 attribute objects per request;
               _update_existing_only keeps the sync from creating users
  Customer.io  POST https://cdp.customer.io/v1/batch (Pipelines API identify calls; cdp-eu for EU workspaces)
               packed to 450,000 bytes per request and 30,000 per call (the limits are 500 KB and 32 KB)

Values are written as they appear in the source, with one exception each way: numbers are sent as
numbers only when they are plainly numbers (a ZIP code like 02134 or a 16-digit id stays a string),
and HubSpot receives every value as a string, booleans as "true" or "false", which is what its
property API expects.

API versions change. The limits above were read from the vendors' documentation in October 2026;
check them before a production sync. Request files are written compact, exactly as sized.

    python3 adapters/destinations/payloads.py klaviyo out/rfm.csv --id-column email --properties rfm_segment,r_score,f_score,m_score --out out/klaviyo
    python3 adapters/destinations/payloads.py hubspot out/rfm.csv --id-column email --properties rfm_segment=lmm_rfm_segment,r_score=lmm_rfm_r --out out/hubspot
"""
from __future__ import annotations

if __package__ in (None, ""):  # run as a file (python3 adapters/destinations/payloads.py): make the repository importable
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse
import csv
import io
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

KLAVIYO_MAX_PROFILES = 10_000
KLAVIYO_MAX_BYTES = 5 * 1024 * 1024
KLAVIYO_MAX_PROFILE_BYTES = 100 * 1024
HUBSPOT_MAX_INPUTS = 100
HUBSPOT_VERSION = "2026-09"
HUBSPOT_PROPERTY = re.compile(r"^[a-z][a-z0-9_]*$")
BRAZE_MAX_OBJECTS = 75
CIO_MAX_BATCH_BYTES = 450_000      # documented: 500 KB per request and 32 KB per call (the batch endpoint's page); margin for the envelope
CIO_MAX_CALL_BYTES = 30_000
KLAVIYO_REVISION = "2026-10-15"
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PLATFORMS = ("klaviyo", "hubspot", "braze", "customerio")
INT = re.compile(r"-?(0|[1-9]\d{0,14})")          # no leading zeros, and short enough to survive a JavaScript number
FLOAT = re.compile(r"-?(0|[1-9]\d{0,14})\.\d+")


def dumps(obj: Any) -> str:
    """The exact text written and sent: compact, so the size checked is the size posted."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _bytes(obj: Any) -> int:
    return len(dumps(obj).encode("utf-8"))


def read_rows(path: Path) -> List[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8-sig")   # a BOM from Excel would otherwise rename the first column
    if path.suffix.lower() in (".jsonl", ".ndjson"):
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    # newline="" keeps line breaks inside quoted fields part of the field
    return [{(k or "").strip(): v for k, v in row.items()} for row in csv.DictReader(io.StringIO(text, newline=""))]


def _coerce(v: Any) -> Any:
    if isinstance(v, str):
        s = v.strip()
        if s == "":
            return None
        if INT.fullmatch(s):
            return int(s)
        if FLOAT.fullmatch(s):
            return float(s)
        if s.lower() in ("true", "false"):
            return s.lower() == "true"
        return s
    return v


def _ident(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _hubspot_value(v: Any) -> str:
    """HubSpot property values are strings. The source text is sent as written (02134 stays 02134)."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        s = v.strip()
        return s.lower() if s.lower() in ("true", "false") else s
    return str(v)


def _mapping(spec: str) -> List[Tuple[str, str]]:
    out = []
    for part in [p.strip() for p in spec.split(",") if p.strip()]:
        src, _, dst = part.partition("=")
        out.append((src.strip(), (dst or src).strip()))
    return out


def klaviyo_jobs(rows: Iterable[Dict[str, Any]], id_column: str, props: List[Tuple[str, str]],
                 list_id: Optional[str] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    report: Dict[str, Any] = {"rows": 0, "skipped_no_identifier": 0, "dropped_null_values": 0, "skipped_oversize": 0}
    profiles: List[Dict[str, Any]] = []
    id_key = "email" if id_column.lower() == "email" else ("phone_number" if "phone" in id_column.lower() else "external_id")
    for row in rows:
        report["rows"] += 1
        ident = _ident(row.get(id_column))
        if not ident or (id_key == "email" and not EMAIL.match(ident)):
            report["skipped_no_identifier"] += 1
            continue
        properties = {}
        for src, dst in props:
            v = _coerce(row.get(src))
            if v is None:
                report["dropped_null_values"] += 1
                continue
            properties[dst] = v
        prof = {"type": "profile", "attributes": {id_key: str(ident).lower() if id_key == "email" else str(ident), "properties": properties}}
        if _bytes(prof) > KLAVIYO_MAX_PROFILE_BYTES:
            report["skipped_oversize"] += 1
            continue
        profiles.append(prof)
    jobs, batch, size = [], [], 0
    overhead = 300
    for p in profiles:
        b = _bytes(p) + 1
        if batch and (len(batch) >= KLAVIYO_MAX_PROFILES or size + b + overhead > KLAVIYO_MAX_BYTES):
            jobs.append(batch)
            batch, size = [], 0
        batch.append(p)
        size += b
    if batch:
        jobs.append(batch)
    bodies = []
    for b in jobs:
        body: Dict[str, Any] = {"data": {"type": "profile-bulk-import-job", "attributes": {"profiles": {"data": b}}}}
        if list_id:
            body["data"]["relationships"] = {"lists": {"data": [{"type": "list", "id": list_id}]}}
        bodies.append(body)
    report["jobs"] = len(bodies)
    report["profiles"] = len(profiles)
    if report["dropped_null_values"]:
        report["note"] = "null values were dropped: the bulk import endpoint cannot clear a property; use the profile update endpoint for clears"
    return bodies, report


def hubspot_batches(rows: Iterable[Dict[str, Any]], id_column: str, props: List[Tuple[str, str]],
                    id_property: str = "email") -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    bad = [dst for _, dst in props if not HUBSPOT_PROPERTY.match(dst)]
    if bad:
        raise ValueError(f"HubSpot property internal names must match {HUBSPOT_PROPERTY.pattern}: {bad}")
    report: Dict[str, Any] = {"rows": 0, "skipped_no_identifier": 0}
    inputs = []
    for row in rows:
        report["rows"] += 1
        ident = _ident(row.get(id_column))
        if not ident or (id_property == "email" and not EMAIL.match(ident)):
            report["skipped_no_identifier"] += 1
            continue
        properties = {dst: _hubspot_value(row.get(src)) for src, dst in props}
        inputs.append({"idProperty": id_property, "id": ident.lower() if id_property == "email" else ident, "properties": properties})
    bodies = [{"inputs": inputs[i:i + HUBSPOT_MAX_INPUTS]} for i in range(0, len(inputs), HUBSPOT_MAX_INPUTS)]
    report["requests"] = len(bodies)
    report["inputs"] = len(inputs)
    report["note"] = ("Create the custom contact properties before the first sync. Upsert creates contacts that do not exist yet; filter the input to "
                      "existing marketing contacts if contact tiers or marketing-contact billing matter. With email as idProperty, partial upserts are not supported.")
    return bodies, report


def attribute_sync(platform: str, rows: Iterable[Dict[str, Any]], id_column: str = "email", external_id_column: str = "user_id",
                   braze_instance: str = "iad-01", eu: bool = False) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Person-attribute updates as complete requests (method, url, headers, body). Every column other than the two
    identifier columns is an attribute. Nothing is sent; headers carry ${PLACEHOLDER} names for the secrets."""
    rows = list(rows)
    attrs = [k for k in (rows[0].keys() if rows else []) if k not in (id_column, external_id_column)]
    props = [(a, a) for a in attrs]
    if platform == "klaviyo":
        bodies, report = klaviyo_jobs(rows, id_column, props)
        reqs = [{"method": "POST", "url": "https://a.klaviyo.com/api/profile-bulk-import-jobs",
                 "headers": {"Authorization": "Klaviyo-API-Key ${KLAVIYO_PRIVATE_KEY}", "revision": KLAVIYO_REVISION,
                             "Content-Type": "application/vnd.api+json", "accept": "application/vnd.api+json"}, "body": b} for b in bodies]
        return reqs, report
    if platform == "hubspot":
        bodies, report = hubspot_batches(rows, id_column, props)
        reqs = [{"method": "POST", "url": f"https://api.hubapi.com/crm/objects/{HUBSPOT_VERSION}/contacts/batch/upsert",
                 "headers": {"Authorization": "Bearer ${HUBSPOT_PRIVATE_APP_TOKEN}", "Content-Type": "application/json"}, "body": b} for b in bodies]
        return reqs, report
    report: Dict[str, Any] = {"rows": len(rows), "skipped_no_identifier": 0}
    if platform == "braze":
        objs = []
        for row in rows:
            ext = _ident(row.get(external_id_column))
            if not ext:
                report["skipped_no_identifier"] += 1
                continue
            o: Dict[str, Any] = {"external_id": ext, "_update_existing_only": True}
            o.update({a: _coerce(row.get(a)) for a in attrs})
            objs.append(o)
        reqs = [{"method": "POST", "url": f"https://rest.{braze_instance}.braze.com/users/track",
                 "headers": {"Authorization": "Bearer ${BRAZE_REST_API_KEY}", "Content-Type": "application/json"},
                 "body": {"attributes": objs[i:i + BRAZE_MAX_OBJECTS]}} for i in range(0, len(objs), BRAZE_MAX_OBJECTS)]
        report.update(requests=len(reqs), people=len(objs),
                      note="_update_existing_only: true, so people Braze does not already know are skipped rather than created")
        return reqs, report
    if platform == "customerio":
        host = "cdp-eu.customer.io" if eu else "cdp.customer.io"
        calls, oversize = [], 0
        for row in rows:
            uid = _ident(row.get(external_id_column))
            if not uid:
                report["skipped_no_identifier"] += 1
                continue
            traits = {a: _coerce(row.get(a)) for a in attrs}
            if _ident(row.get(id_column)) and id_column == "email":
                traits["email"] = _ident(row.get(id_column)).lower()
            call = {"type": "identify", "userId": uid, "traits": traits}
            if _bytes(call) > CIO_MAX_CALL_BYTES:
                oversize += 1
                continue
            calls.append(call)
        batches: List[List[Dict[str, Any]]] = []
        size = 0
        for c in calls:
            b = _bytes(c) + 1
            if not batches or size + b + 64 > CIO_MAX_BATCH_BYTES:
                batches.append([])
                size = 0
            batches[-1].append(c)
            size += b
        reqs = [{"method": "POST", "url": f"https://{host}/v1/batch",
                 "headers": {"Authorization": "Basic ${CUSTOMERIO_PIPELINES_API_KEY_BASE64}", "Content-Type": "application/json"},
                 "body": {"batch": b}} for b in batches]
        report.update(requests=len(reqs), people=len(calls), skipped_oversize=oversize,
                      note="identify creates people Customer.io does not know yet; send only people already in the workspace if that matters")
        return reqs, report
    raise ValueError(f"platform must be one of {PLATFORMS}")


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="lcm sync", description="Segment rows to Klaviyo or HubSpot request bodies (nothing is sent)")
    ap.add_argument("target", choices=["klaviyo", "hubspot"], help="the platform to build requests for")
    ap.add_argument("input", help="segment rows, one per person: a .csv, .json or .jsonl file")
    ap.add_argument("--id-column", default="email", help="the input column that identifies each person (default: email)")
    ap.add_argument("--properties", required=True, help="comma list of column or column=destination_property")
    ap.add_argument("--list-id", help="Klaviyo list to add profiles to (optional)")
    ap.add_argument("--id-property", default="email", help="HubSpot unique property used as id")
    ap.add_argument("--out", required=True, help="folder to write one JSON file per batch request into")
    a = ap.parse_args(argv)
    rows = read_rows(Path(a.input))
    props = _mapping(a.properties)
    if a.target == "klaviyo":
        bodies, report = klaviyo_jobs(rows, a.id_column, props, a.list_id)
    else:
        bodies, report = hubspot_batches(rows, a.id_column, props, a.id_property)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for i, b in enumerate(bodies, 1):
        (out / f"{a.target}_batch_{i:04d}.json").write_text(dumps(b), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
