"""Shared logic for the Segment and RudderStack warehouse mappers (segment.py, rudderstack.py).

Both CDPs load the same shapes into a warehouse schema named after the source, and both document them:
  tracks      one row per track call with the standard columns only: id (the messageId), anonymous_id, user_id,
              event (the event name as a slug, which is also the per-event table's name), event_text (the name as
              sent), timestamp, original_timestamp, sent_at, received_at, context_* columns.
  <event>     one table per event with the same columns plus one column per property (nested keys joined with
              underscores, camelCase converted to snake_case). The tracks table holds no properties.
  identifies  one row per identify call: id, user_id, anonymous_id, the timestamps, context_* and one column per trait.
  users       one row per user id holding the latest traits (Segment documents id = user_id; RudderStack has one too).
  Segment: https://www.twilio.com/docs/segment/connections/storage/warehouses/schema
  RudderStack: https://www.rudderstack.com/docs/destinations/warehouse-destinations/warehouse-schema/

The Python mappers read those tables dumped to CSV, one file per table named after it (tracks.csv,
identifies.csv, users.csv, order_completed.csv), in one folder per source. The folder given to map() is searched
recursively for folders holding tracks.csv or identifies.csv, and each mapper takes the folders whose calls came
from its own vendor's libraries, so `lcm import auto exports/` can hold a Segment and a RudderStack dump side by
side. The SQL under adapters/sources/sql/<source>/ does the same in the warehouse;
tests/test_mappers_billing_crm_events.py proves the two agree on the Ridgeview sample.

Rules shared by both mappers and their SQL
  track_events  event_id = tracks.id (duplicates by id: the earliest received_at is kept); user_id and
                anonymous_id with empty as null; event_name = the event slug lower-cased (else event_text in
                snake_case), except application_opened -> session_started (the tracking plan's name, as the webhook
                receiver maps it); event_at = timestamp, else original_timestamp, else received_at (Segment
                recommends timestamp: corrected for device clock skew). properties = the row's columns in its
                per-event table, minus the standard and context_ columns, when that table was dumped; {} otherwise.
                CSV cells become JSON scalars: true/false, integers without leading zeros, decimals, else strings.
  users         per user id, the latest non-empty value of each trait (email lower-cased, plan, country, created_at)
                and of context_timezone, ordered by timestamp then received_at; created_at is the created_at trait,
                else the first identify. country is kept only when it is a two-letter code (upper-cased); names are
                left empty, because the SQL cannot map names portably and the two must agree.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from adapters.sources._mapping import Sheet, TimeParser, csv_files, csv_header, norm, rel, scalar, snake, write_table
from adapters.sources.report import MappingReport

NAMED = ("tracks", "identifies", "users")
OTHER_TABLES = {"pages", "screens", "groups", "aliases"}
STANDARD = {"id", "anonymous_id", "user_id", "event", "event_text", "received_at", "sent_at", "timestamp", "original_timestamp",
            "uuid", "uuid_ts", "loaded_at", "channel", "request_ip", "rudder_id"}
SPEC_TO_PLAN = {"application_opened": "session_started"}
TRAITS = ("email", "plan", "country", "created_at", "context_timezone")
RUDDER_COLUMNS = {"context_source_id", "context_destination_id", "context_request_ip", "context_source_type", "context_destination_type"}


@dataclass
class Dump:
    """The warehouse tables found in one or more source folders."""
    named: Dict[str, List[Sheet]] = field(default_factory=lambda: {n: [] for n in NAMED})
    per_event: List[Sheet] = field(default_factory=list)
    other: List[Path] = field(default_factory=list)


def source_dirs(folder: Path) -> List[Path]:
    """Folders under `folder` (itself included) that hold a tracks.csv or identifies.csv."""
    return sorted({p.parent for p in csv_files(folder) if p.stem.lower() in ("tracks", "identifies")})


def _file(d: Path, stem: str) -> Optional[Path]:
    return next((p for p in sorted(d.iterdir()) if p.is_file() and p.suffix.lower() == ".csv" and p.stem.lower() == stem), None)


def dir_vendor(d: Path) -> Optional[str]:
    """'segment', 'rudderstack' or None for one source folder: which library sent the calls, and vendor-only columns."""
    libs: List[str] = []
    cols: set = set()
    for stem in ("tracks", "identifies"):
        p = _file(d, stem)
        if p is None:
            continue
        cols |= {h.strip().lower() for h in csv_header(p)}
        s = Sheet.read(p)
        libs += [v.lower() for v in (s.get(r, "context_library_name") for r in s.rows[:20]) if v]
    if any("rudder" in lib for lib in libs) or cols & RUDDER_COLUMNS:
        return "rudderstack"
    if any("analytics" in lib or "segment" in lib for lib in libs) or cols & {"uuid", "loaded_at"} or _file(d, "users"):
        return "segment"
    return None


def vendor_dirs(folder: Path, source: str) -> List[Path]:
    return [d for d in source_dirs(Path(folder)) if dir_vendor(d) == source]


def load(dirs: List[Path]) -> Dump:
    dump = Dump()
    for d in dirs:
        for p in sorted(x for x in d.iterdir() if x.is_file() and x.suffix.lower() == ".csv"):
            stem = p.stem.lower()
            if stem in NAMED:
                dump.named[stem].append(Sheet.read(p))
            elif stem in OTHER_TABLES:
                dump.other.append(p)
            elif {"id", "event", "eventtext"} <= {norm(h) for h in csv_header(p)}:
                dump.per_event.append(Sheet.read(p))
            else:
                dump.other.append(p)
    return dump


def _event_name(event: Optional[str], event_text: Optional[str]) -> str:
    slug = (event or "").strip().lower() or (snake(event_text) if event_text else "")
    slug = slug or "unnamed_event"
    return SPEC_TO_PLAN.get(slug, slug)


def _props(sheet: Sheet, row: Dict[str, str]) -> Dict[str, Any]:
    out = {}
    for h in sheet.headers:
        key = h.strip()
        low = key.lower()
        if low in STANDARD or low.startswith("context_"):
            continue
        v = (row.get(h) or "").strip()
        if v:
            out[key] = scalar(v)
    return out


def map_tracks(dump: Dump, tp: TimeParser, report: MappingReport) -> List[Dict[str, Any]]:
    props: Dict[str, Dict[str, Any]] = {}
    for sheet in dump.per_event:
        for row in sheet.rows:
            rid = sheet.get(row, "id")
            if rid and rid not in props:
                props[rid] = _props(sheet, row)
        for h in sheet.headers:
            sheet.use(h)
    kept: Dict[str, Tuple[Tuple[str, int], Dict[str, Any]]] = {}
    duplicates = no_time = 0
    n = 0
    for tracks in dump.named["tracks"]:
        for row in tracks.rows:
            n += 1
            rid = tracks.get(row, "id")
            if not rid:
                continue
            at = tp.parse(tracks.get(row, "timestamp")) or tp.parse(tracks.get(row, "original_timestamp")) \
                or tp.parse(tracks.get(row, "received_at"))
            if at is None:
                no_time += 1
                continue
            received = tp.parse(tracks.get(row, "received_at"))
            rank = (received.isoformat() if received else "", n)
            out = {"event_id": rid, "user_id": tracks.get(row, "user_id"), "anonymous_id": tracks.get(row, "anonymous_id"),
                   "event_name": _event_name(tracks.get(row, "event"), tracks.get(row, "event_text")), "event_at": at,
                   "properties": props.get(rid, {})}
            if rid in kept:
                duplicates += 1
                if rank >= kept[rid][0]:
                    continue
            kept[rid] = (rank, out)
    rows = [v[1] for v in kept.values()]
    if not rows:
        return rows
    with_props = sum(1 for r in rows if r["properties"])
    if dump.per_event:
        report.assumptions.append(f"properties for {with_props} of {len(rows)} event(s) come from {len(dump.per_event)} per-event "
                                  "table(s), matched on id; CSV cells were typed as JSON booleans, integers, decimals or strings.")
    else:
        report.warnings.append("no per-event tables were given, so every event has empty properties: the tracks table holds "
                               "only standard columns. Dump the per-event tables (one CSV per event) to carry properties.")
    if duplicates:
        report.warnings.append(f"{duplicates} track row(s) repeated an id already seen; the earliest received_at was kept.")
    if no_time:
        report.warnings.append(f"{no_time} track row(s) without a timestamp were skipped.")
    return rows


def _seen(sheet: Sheet, row: Dict[str, str], tp: TimeParser) -> Optional[datetime]:
    return tp.parse(sheet.get(row, "timestamp")) or tp.parse(sheet.get(row, "original_timestamp")) or tp.parse(sheet.get(row, "received_at"))


def identify_traits(identifies: List[Sheet], tp: TimeParser) -> Dict[str, Dict[str, Any]]:
    """user_id -> {trait: latest non-empty value, first_seen: earliest identify time}."""
    calls: Dict[str, List[Tuple[datetime, Optional[datetime], int, Sheet, Dict[str, str]]]] = {}
    n = 0
    for sheet in identifies:
        for row in sheet.rows:
            n += 1
            uid = sheet.get(row, "user_id")
            seen = _seen(sheet, row, tp)
            if not uid or seen is None:
                continue
            calls.setdefault(uid, []).append((seen, tp.parse(sheet.get(row, "received_at")), n, sheet, row))
    out: Dict[str, Dict[str, Any]] = {}
    for uid, rows in calls.items():
        latest_first = sorted(rows, key=lambda r: (r[0], r[1] or r[0], r[2]), reverse=True)
        traits: Dict[str, Any] = {"first_seen": min(r[0] for r in rows)}
        for t in TRAITS:
            for _seen_at, _received, _n, sheet, row in latest_first:
                v = sheet.get(row, t)
                if v:
                    traits[t] = v
                    break
        out[uid] = traits
    return out


def _country(v: Optional[str]) -> Optional[str]:
    v = (v or "").strip()
    return v.upper() if len(v) == 2 else None


def users_from_identifies(identifies: List[Sheet], tp: TimeParser, report: MappingReport) -> List[Dict[str, Any]]:
    rows = []
    no_email = 0
    for uid, t in sorted(identify_traits(identifies, tp).items()):
        if not t.get("email"):
            no_email += 1
            continue
        rows.append({"user_id": uid, "email": t["email"].strip().lower(), "created_at": tp.parse(t.get("created_at")) or t["first_seen"],
                     "country": _country(t.get("country")), "plan": t.get("plan"), "timezone": t.get("context_timezone")})
    if no_email:
        report.warnings.append(f"{no_email} identified user(s) never sent an email trait and were left out of users.")
    return rows


def users_from_users_table(users: List[Sheet], identifies: List[Sheet], tp: TimeParser, report: MappingReport) -> List[Dict[str, Any]]:
    traits = identify_traits(identifies, tp)
    rows: Dict[str, Dict[str, Any]] = {}
    no_email = no_created = repeated = 0
    for sheet in users:
        for row in sheet.rows:
            uid = sheet.get(row, "id")
            addr = sheet.get(row, "email")
            if not uid:
                continue
            if not addr:
                no_email += 1
                continue
            t = traits.get(uid, {})
            created = tp.parse(sheet.get(row, "created_at")) or t.get("first_seen") or tp.parse(sheet.get(row, "received_at"))
            if created is None:
                no_created += 1
                continue
            if uid in rows:
                repeated += 1
                continue
            rows[uid] = {"user_id": uid, "email": addr.strip().lower(), "created_at": created,
                         "country": _country(sheet.get(row, "country")), "plan": sheet.get(row, "plan"),
                         "timezone": t.get("context_timezone")}
    if no_email:
        report.warnings.append(f"{no_email} user(s) in the users table have no email trait and were left out.")
    if no_created:
        report.warnings.append(f"{no_created} user(s) have no created_at trait, identify or received_at and were left out.")
    if repeated:
        report.warnings.append(f"{repeated} user id(s) appear more than once in the users table; the first row was kept.")
    return list(rows.values())


def run(folder: Path, out_dir: Path, source: str, use_users_table: bool) -> MappingReport:
    """Map the `source` warehouse dump(s) under `folder`. Folders without a vendor signal are used only when no folder
    carries this vendor's signal (so a mapper named explicitly still works on a bare tracks.csv)."""
    folder, out_dir = Path(folder), Path(out_dir)
    report = MappingReport(source=source)
    tp = TimeParser()
    dirs = vendor_dirs(folder, source) or [d for d in source_dirs(folder) if dir_vendor(d) is None]
    if not dirs:
        report.warnings.append(f"no {source} tracks.csv or identifies.csv under {folder}")
        return report
    dump = load(dirs)
    events = map_tracks(dump, tp, report)
    if use_users_table and dump.named["users"]:
        users = users_from_users_table(dump.named["users"], dump.named["identifies"], tp, report)
        report.assumptions.append("users come from the users table (latest traits per user); created_at is the created_at trait, "
                                  "else the first identify; timezone is the latest identify's context_timezone.")
    else:
        users = users_from_identifies(dump.named["identifies"], tp, report)
        if dump.named["identifies"]:
            report.assumptions.append("users come from identifies: the latest non-empty value of each trait per user; created_at is "
                                      "the created_at trait, else the first identify.")
    if users:
        report.assumptions.append("country is kept only as a two-letter code (country names are left empty, matching the SQL).")
    report.assumptions += tp.notes(source)
    report.warnings += tp.warnings(source)
    for sheets in dump.named.values():
        for sheet in sheets:
            report.unmapped_columns[rel(sheet.path, folder)] = sheet.unused()
    for p in dump.other:
        report.unmapped_columns[rel(p, folder)] = csv_header(p)
        report.warnings.append(f"{rel(p, folder)} is not mapped (only tracks, identifies, users and per-event tables are).")
    if users:
        report.tables["users"] = write_table(out_dir, "users", users, ["user_id"])
    if events:
        report.tables["track_events"] = write_table(out_dir, "track_events", events, ["event_at", "event_id"])
    return report
