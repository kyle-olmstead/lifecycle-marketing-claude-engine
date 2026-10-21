"""The GA4 BigQuery export (events_YYYYMMDD rows) flattened into the canonical track_events table.

What it reads
  An offline dump of export rows in a folder (subfolders are searched): newline-delimited JSON (*.jsonl, *.ndjson, or *.json written by
  `bq extract --destination_format NEWLINE_DELIMITED_JSON`), a JSON array, or Parquet (*.parquet, read with DuckDB).
  Each row has the export schema documented at https://support.google.com/analytics/answer/7029846: event_date,
  event_timestamp (UTC microseconds), event_name, event_params (repeated key / value.string_value, int_value,
  float_value, double_value), user_id, user_pseudo_id, event_bundle_sequence_id, batch_event_index, items, geo,
  device, ecommerce. BigQuery's JSON export writes INT64 values as strings to keep 64-bit precision
  (https://docs.cloud.google.com/bigquery/docs/exporting-data); both strings and numbers are read.

  In the warehouse, run adapters/sources/sql/ga4/track_events_bigquery.sql instead: it applies the same rules in
  BigQuery SQL, and tests/test_mappers_billing_crm_events.py proves the two produce identical rows on the same sample.

What it writes (track_events)
  event_id      purchase:<transaction_id> for a purchase carrying a transaction id (GA4 logs a purchase again when
                a confirmation page reloads; one transaction is one order), else ga4:<md5 hex> of
                user_pseudo_id|event_timestamp|event_name|event_bundle_sequence_id|batch_event_index, since GA4 has
                no event id. Rows sharing an id are duplicates: the earliest is kept and the report counts the rest.
  user_id       user_id; empty before sign-in
  anonymous_id  user_pseudo_id
  event_name    purchase -> order_completed, begin_checkout -> checkout_started, view_item -> product_viewed,
                add_to_cart -> product_added, sign_up -> signed_up, login -> session_started; any other name is
                kept in snake_case (page_view stays page_view, session_start stays session_start, a custom
                reportShared becomes report_shared).
  event_at      event_timestamp as UTC (microseconds kept)
  properties    every event parameter by key, valued by the first non-null of string_value, int_value,
                double_value, float_value; items (item_id, item_name, item_brand, item_variant, item_category, price,
                quantity, item_revenue, coupon) when the event has items; geo_country (geo.country, the country
                name GA4 derives from the IP address); device_category (device.category). GA4 parameter names are
                kept as GA4 sends them (transaction_id, value, currency), not renamed to the tracking plan's.

What it assumes
  * event_timestamp is UTC by definition; event_date (the property's time zone) is not used.
  * The md5 key assumes the combination above identifies one event; GA4 documents that events in one request
    can share a timestamp, and batch_event_index orders them, so on exports from before batch_event_index
    existed two identical events in one request collapse into one (counted as duplicates).

    from adapters.sources import ga4_bigquery
    report = ga4_bigquery.map(Path("exports/ga4"), Path("data"))
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from adapters.sources._mapping import epoch, files_under, rel, snake, write_table
from adapters.sources.report import MappingReport

NAME = "ga4"
DESCRIPTION = "GA4 BigQuery export rows (JSONL, JSON or Parquet dump) into track_events, with warehouse SQL for BigQuery"

RENAME = {"purchase": "order_completed", "begin_checkout": "checkout_started", "view_item": "product_viewed",
          "add_to_cart": "product_added", "sign_up": "signed_up", "login": "session_started"}
ITEM_FIELDS = ("item_id", "item_name", "item_brand", "item_variant", "item_category", "price", "quantity", "item_revenue", "coupon")
ITEM_FLOATS = {"price", "item_revenue"}
ITEM_INTS = {"quantity"}
EXTRA_KEYS = ("items", "geo_country", "device_category")
SUFFIXES = (".jsonl", ".ndjson", ".json", ".parquet")
USED = {"event_date", "event_timestamp", "event_name", "event_params", "user_id", "user_pseudo_id", "event_bundle_sequence_id",
        "batch_event_index", "items", "geo", "device", "ecommerce"}


def _first(path: Path) -> Optional[Dict[str, Any]]:
    """The first row of a dump, or None when the file is not readable as rows (another vendor's JSON, say)."""
    try:
        return next(_rows(path), None)
    except Exception:  # noqa: BLE001  (a file that is not GA4 rows is simply not GA4)
        return None


def _files(folder: Path) -> List[Path]:
    """GA4 dump files under `folder` (searched recursively): JSON or Parquet whose first row has GA4's fields."""
    out = []
    for p in files_under(folder, SUFFIXES):
        first = _first(p)
        if first is not None and _looks_like_ga4(first):
            out.append(p)
    return out


def _rows(path: Path) -> Iterator[Dict[str, Any]]:
    if path.suffix.lower() == ".parquet":
        import duckdb
        con = duckdb.connect()
        cur = con.execute("SELECT * FROM read_parquet(?)", [str(path)])
        cols = [d[0] for d in cur.description]
        for rec in cur.fetchall():
            yield dict(zip(cols, rec))
        return
    text = path.read_text(encoding="utf-8-sig")
    stripped = text.lstrip()
    if stripped.startswith("["):
        for rec in json.loads(stripped):
            if isinstance(rec, dict):
                yield rec
        return
    for line in text.splitlines():
        line = line.strip()
        if line:
            rec = json.loads(line)
            if isinstance(rec, dict):
                yield rec


def _looks_like_ga4(rec: Dict[str, Any]) -> bool:
    return "event_name" in rec and "event_timestamp" in rec and ("event_params" in rec or "user_pseudo_id" in rec)


def detect(folder: Path) -> bool:
    """True when the folder (or a subfolder) holds JSON or Parquet rows with GA4's event_name, event_timestamp and event_params."""
    return bool(_files(Path(folder)))


def _int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return int(float(v))


def _float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    return float(v)


def _param_value(value: Optional[Dict[str, Any]]) -> Any:
    value = value or {}
    if value.get("string_value") is not None:
        return str(value["string_value"])
    if value.get("int_value") is not None:
        return _int(value["int_value"])
    if value.get("double_value") is not None:
        return _float(value["double_value"])
    if value.get("float_value") is not None:
        return _float(value["float_value"])
    return None


def _str(v: Any) -> Optional[str]:
    return None if v is None or v == "" else str(v)


def _item(it: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k in ITEM_FIELDS:
        v = it.get(k)
        out[k] = _float(v) if k in ITEM_FLOATS else _int(v) if k in ITEM_INTS else _str(v)
    return out


def _rank(v: Any) -> int:
    """Sort position of a nullable integer, missing first (as ORDER BY ... NULLS FIRST in the SQL)."""
    n = _int(v)
    return -1 if n is None else n


def event_name(raw: str) -> str:
    return RENAME.get(raw, snake(raw))


def flatten(rec: Dict[str, Any]) -> Tuple[str, Dict[str, Any], int]:
    """One export row to (event_id, canonical row, event_timestamp)."""
    ts = _int(rec.get("event_timestamp"))
    name = str(rec.get("event_name") or "")
    params: Dict[str, Any] = {}
    for p in rec.get("event_params") or []:
        if isinstance(p, dict) and p.get("key") is not None:
            params[str(p["key"])] = _param_value(p.get("value"))
    txn = params.get("transaction_id")
    txn = str(txn) if txn not in (None, "") else _str((rec.get("ecommerce") or {}).get("transaction_id"))
    if txn in ("", "(not set)"):
        txn = None
    pseudo = _str(rec.get("user_pseudo_id"))
    if name == "purchase" and txn:
        event_id = f"purchase:{txn}"
    else:
        key = "|".join([pseudo or "", str(ts), name, _str(_int(rec.get("event_bundle_sequence_id"))) or "",
                        _str(_int(rec.get("batch_event_index"))) or ""])
        event_id = "ga4:" + hashlib.md5(key.encode("utf-8")).hexdigest()
    props = {k: v for k, v in params.items() if k not in EXTRA_KEYS}
    items = [_item(i) for i in (rec.get("items") or []) if isinstance(i, dict)]
    if items:
        props["items"] = items
    country = _str((rec.get("geo") or {}).get("country"))
    if country:
        props["geo_country"] = country
    category = _str((rec.get("device") or {}).get("category"))
    if category:
        props["device_category"] = category
    at: Optional[datetime] = epoch(ts) if ts else None
    row = {"event_id": event_id, "user_id": _str(rec.get("user_id")), "anonymous_id": pseudo, "event_name": event_name(name),
           "event_at": at, "properties": props}
    return event_id, row, ts or 0


def map(folder: Path, out_dir: Path) -> MappingReport:  # noqa: A001
    """Read a GA4 export dump in `folder`; write track_events.csv to `out_dir`."""
    folder, out_dir = Path(folder), Path(out_dir)
    report = MappingReport(source=NAME)
    kept: Dict[str, Tuple[Tuple[int, int, int, int], Dict[str, Any]]] = {}
    order = duplicates = no_time = no_name = 0
    renamed: Dict[str, int] = {}
    files = _files(folder)
    if not files:
        report.warnings.append(f"no GA4 export rows (JSONL, JSON or Parquet with event_name and event_params) in {folder}")
        return report
    for path in files:
        unused: set = set()
        try:
            recs = list(_rows(path))
        except ValueError as e:
            report.warnings.append(f"{rel(path, folder)} could not be read past its first row ({str(e).splitlines()[0]}); skipped.")
            continue
        for rec in recs:
            unused |= set(rec) - USED
            if not rec.get("event_name"):
                no_name += 1
                continue
            event_id, row, ts = flatten(rec)
            if row["event_at"] is None:
                no_time += 1
                continue
            raw = str(rec["event_name"])
            if raw in RENAME:
                renamed[f"{raw} -> {RENAME[raw]}"] = renamed.get(f"{raw} -> {RENAME[raw]}", 0) + 1
            order += 1
            # the earliest copy wins: event_timestamp, then bundle and batch position (missing first), then file order
            rank = (ts, _rank(rec.get("event_bundle_sequence_id")), _rank(rec.get("batch_event_index")), order)
            prev = kept.get(event_id)
            if prev is not None:
                duplicates += 1
                if rank >= prev[0]:
                    continue
            kept[event_id] = (rank, row)
        report.unmapped_columns[rel(path, folder)] = sorted(unused)
    if renamed:
        report.assumptions.append("GA4 recommended events renamed to the tracking plan: "
                                  + ", ".join(f"{k} ({v})" for k, v in sorted(renamed.items())) + "; other names kept in snake_case.")
    report.assumptions.append("event_id: purchase:<transaction_id> for purchases, else an md5 of user_pseudo_id, event_timestamp, "
                              "event_name, event_bundle_sequence_id and batch_event_index (GA4 has no event id).")
    if duplicates:
        report.warnings.append(f"{duplicates} GA4 row(s) shared an event id with an earlier row (a repeated purchase "
                               "transaction or an identical event in one request) and were dropped as duplicates.")
    if no_time:
        report.warnings.append(f"{no_time} GA4 row(s) without an event_timestamp were skipped.")
    if no_name:
        report.warnings.append(f"{no_name} GA4 row(s) without an event_name were skipped.")
    rows = [v[1] for v in kept.values()]
    if rows:
        report.tables["track_events"] = write_table(out_dir, "track_events", rows, ["event_at", "event_id"])
    return report
