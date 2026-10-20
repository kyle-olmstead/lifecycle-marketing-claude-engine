"""
Turn segment rows into API request bodies for Klaviyo and HubSpot (modules/04 export step).

Input is the CSV or JSON Lines output of a segmentation query: one row per person, an identifier
column, and the properties to write. Output is one request body per batch, written to a folder,
ready to POST. Nothing is sent from here.

  Klaviyo  POST https://a.klaviyo.com/api/profile-bulk-import-jobs   (header: revision: <date>)
           up to 10,000 profiles and 5 MB per job, 100 KB per profile; null values cannot clear a
           property through this endpoint, so nulls are dropped and reported
  HubSpot  POST /crm/objects/<version>/contacts/batch/upsert          (date-versioned paths since 2026)
           up to 100 inputs per request; property internal names must already exist on the
           contact object (lowercase, letters, digits, underscores)

API versions change. The limits above were read from the vendors' documentation in October 2026;
check them before a production sync.

    python3 tools/payloads.py klaviyo out/rfm.csv --id-column email --properties rfm_segment,r_score,f_score,m_score --out out/klaviyo
    python3 tools/payloads.py hubspot out/rfm.csv --id-column email --properties rfm_segment=lmm_rfm_segment,r_score=lmm_rfm_r --out out/hubspot
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

KLAVIYO_MAX_PROFILES = 10_000
KLAVIYO_MAX_BYTES = 5 * 1024 * 1024
KLAVIYO_MAX_PROFILE_BYTES = 100 * 1024
HUBSPOT_MAX_INPUTS = 100
HUBSPOT_PROPERTY = re.compile(r"^[a-z][a-z0-9_]*$")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def read_rows(path: Path) -> List[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".jsonl", ".ndjson"):
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    return list(csv.DictReader(text.splitlines()))


def _coerce(v: Any) -> Any:
    if isinstance(v, str):
        s = v.strip()
        if s == "":
            return None
        if re.fullmatch(r"-?\d+", s):
            return int(s)
        if re.fullmatch(r"-?\d+\.\d+", s):
            return float(s)
        if s.lower() in ("true", "false"):
            return s.lower() == "true"
        return s
    return v


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
        ident = (row.get(id_column) or "").strip() if isinstance(row.get(id_column), str) else row.get(id_column)
        if not ident or (id_key == "email" and not EMAIL.match(str(ident))):
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
        if len(json.dumps(prof).encode()) > KLAVIYO_MAX_PROFILE_BYTES:
            report["skipped_oversize"] += 1
            continue
        profiles.append(prof)
    jobs, batch, size = [], [], 0
    overhead = 300
    for p in profiles:
        b = len(json.dumps(p).encode()) + 1
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
        ident = row.get(id_column)
        if not ident or (id_property == "email" and not EMAIL.match(str(ident))):
            report["skipped_no_identifier"] += 1
            continue
        properties = {dst: ("" if _coerce(row.get(src)) is None else str(_coerce(row.get(src)))) for src, dst in props}
        inputs.append({"idProperty": id_property, "id": str(ident).lower() if id_property == "email" else str(ident), "properties": properties})
    bodies = [{"inputs": inputs[i:i + HUBSPOT_MAX_INPUTS]} for i in range(0, len(inputs), HUBSPOT_MAX_INPUTS)]
    report["requests"] = len(bodies)
    report["inputs"] = len(inputs)
    report["note"] = ("Create the custom contact properties before the first sync. Upsert creates contacts that do not exist yet; filter the input to "
                      "existing marketing contacts if contact tiers or marketing-contact billing matter. With email as idProperty, partial upserts are not supported.")
    return bodies, report


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Segment rows to Klaviyo or HubSpot request bodies")
    ap.add_argument("target", choices=["klaviyo", "hubspot"])
    ap.add_argument("input")
    ap.add_argument("--id-column", default="email")
    ap.add_argument("--properties", required=True, help="comma list of column or column=destination_property")
    ap.add_argument("--list-id", help="Klaviyo list to add profiles to (optional)")
    ap.add_argument("--id-property", default="email", help="HubSpot unique property used as id")
    ap.add_argument("--out", required=True)
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
        (out / f"{a.target}_batch_{i:04d}.json").write_text(json.dumps(b, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
