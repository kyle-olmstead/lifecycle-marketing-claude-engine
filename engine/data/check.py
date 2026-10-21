"""Check a data folder against the canonical tables before the engine reads it.

What it does: reads every <table>.csv or <table>.parquet in a folder (the same files engine/data/load.py loads), compares
them with engine/data/schema.py and reports, column by column, what would break or quietly mislead the engine:

  structure     required tables and columns present; columns the engine does not know (ignored, listed as notes)
  types         values that do not parse as the column's type, counted with DuckDB TRY_CAST: TIMESTAMP (ISO 8601 with
                Z works; a non-zero UTC offset is read as clock time with the offset dropped, so it is flagged), DECIMAL,
                INTEGER, BOOLEAN, DATE, and JSON objects in track_events.properties
  keys          primary keys unique (users.user_id, track_events.event_id, orders.order_id, invoices.invoice_id,
                subscriptions.subscription_id, refunds.refund_id, and the other id columns the schema calls unique)
  references    track_events.user_id and orders.user_id found in users, line_items.order_id in orders, invoices in
                subscriptions, and the other joins the engine makes
  values        event_name in snake_case object_action (order_completed), emails well formed and lower case,
                users.timezone a real IANA zone, country ISO 3166-1 alpha-2, currency three capital letters, status and
                channel values the engine recognizes
  time          consent timestamps not in the future (a future consent would outrank every later unsubscribe),
                events dated in the future, and freshness: the age of the newest event

Why: a real export rarely matches the canonical shape on the first try, and the engine's own errors name the first
problem only. This names every problem at once, with the table.column, how many rows, two or three bad values, and the
fix in one line, so an hour is enough to get from a vendor export to a folder the engine trusts.

Exit code 1 when there are errors (the engine cannot load the folder or would read it wrongly), 0 when there are only
warnings and notes. Markdown by default, JSON with --json.

    python3 engine/data/check.py examples/brands/northfield_dtc/data
    python3 engine/data/check.py exports/ --json
    python3 engine/data/check.py exports/ --as-of 2026-10-19      # judge "future" and freshness against a fixed date
    lcm check-data exports/                                         # the same, once wired into the lcm command

From Python: `check_folder(path)` returns the report as a dictionary; `render_markdown(report)` formats it.
"""
from __future__ import annotations

if __package__ in (None, ""):  # run as a file (python3 <path>): make the repository importable
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse
import difflib
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from engine.data.iso import COUNTRY_CODES, country_code
from engine.data.schema import TABLES, Table

SEVERITIES = ("error", "warning", "info")

# Unique id columns: the six the engine joins on most, and the others the schema describes as unique.
PRIMARY_KEYS: Tuple[Tuple[str, str], ...] = (
    ("users", "user_id"), ("track_events", "event_id"), ("orders", "order_id"), ("invoices", "invoice_id"),
    ("subscriptions", "subscription_id"), ("refunds", "refund_id"), ("message_events", "event_id"),
    ("products", "product_id"), ("accounts", "account_id"), ("line_items", "line_id"),
)

# (child table, child column, parent table, parent column, core). A core join loses revenue or events silently when it
# misses: 5 percent or more orphans is an error, less is a warning. A non-core join is always a warning.
REFERENCES: Tuple[Tuple[str, str, str, str, bool], ...] = (
    ("track_events", "user_id", "users", "user_id", True),
    ("orders", "user_id", "users", "user_id", True),
    ("line_items", "order_id", "orders", "order_id", True),
    ("invoices", "subscription_id", "subscriptions", "subscription_id", True),
    ("discounts", "order_id", "orders", "order_id", True),
    ("refunds", "order_id", "orders", "order_id", True),
    ("message_events", "user_id", "users", "user_id", True),
    ("consent_events", "user_id", "users", "user_id", False),
    ("subscriptions", "user_id", "users", "user_id", False),
    ("line_items", "product_id", "products", "product_id", False),
)
ORPHAN_ERROR_SHARE = 0.05

# Values the engine branches on. Anything else is read as "not one of these" (an order with status 'paid' is not revenue).
ENUMS: Dict[Tuple[str, str], Tuple[str, ...]] = {
    ("orders", "status"): ("completed", "refunded", "canceled", "cancelled", "pending"),
    ("message_events", "event_name"): ("delivered", "opened", "clicked", "bounced", "deferred", "complained", "unsubscribed"),
    ("message_events", "channel"): ("email", "sms", "push"),
    ("consent_events", "channel"): ("email", "sms", "push"),
    ("consent_events", "status"): ("granted", "revoked"),
    ("consent_events", "purpose"): ("marketing", "transactional"),
    ("subscriptions", "status"): ("trialing", "active", "past_due", "unpaid", "canceled", "cancelled", "paused",
                                  "incomplete", "incomplete_expired"),
    ("invoices", "status"): ("draft", "open", "paid", "uncollectible", "void"),
    ("discounts", "discount_type"): ("percentage", "fixed_amount", "free_shipping"),
}

CONSENT_TIMES: Tuple[Tuple[str, str], ...] = (("users", "email_consent_at"), ("users", "sms_consent_at"),
                                              ("consent_events", "occurred_at"))
EVENT_TIMES: Tuple[Tuple[str, str], ...] = (("track_events", "event_at"), ("message_events", "event_at"),
                                            ("orders", "order_at"), ("refunds", "refunded_at"))
CURRENCY_COLUMNS: Tuple[Tuple[str, str], ...] = (("orders", "currency"), ("subscriptions", "currency"), ("invoices", "currency"))

FUTURE_TOLERANCE = timedelta(minutes=5)
STALE_AFTER_DAYS = 7
EVENT_NAME = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)+$")
EMAIL_SQL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
OFFSET_SQL = r"\d{2}:\d{2}(:\d{2}(\.\d+)?)?\s*[+-]\d{2}(:?\d{2})?$"
ZERO_OFFSET_SQL = r"[+-]00(:?00)?$"
E164 = re.compile(r"^\+[1-9]\d{6,14}$")
MAX_DISTINCT = 5000
KNOWN_SIDE_FILES = {"import_report.json", "readme.md", ".gitkeep"}


@dataclass
class Finding:
    severity: str
    check: str
    table: str
    column: Optional[str]
    rows: int
    message: str
    fix: str
    examples: List[str] = field(default_factory=list)

    @property
    def where(self) -> str:
        return f"{self.table}.{self.column}" if self.column else self.table


# ---------------------------------------------------------------------------------------------------------------------
# helpers


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _short(value: Any, limit: int = 60) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\n", " ").replace("\r", " ")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def snake_case(name: str) -> str:
    """'Order Completed', 'orderCompleted' and 'order-completed' all become 'order_completed'."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(name).strip())
    s = re.sub(r"[^A-Za-z0-9]+", "_", s)
    return re.sub(r"_+", "_", s).strip("_").lower()


def _timestamp_fix(examples: Sequence[str]) -> str:
    joined = " ".join(examples)
    if examples and all(re.fullmatch(r"\d{9,13}(\.\d+)?", e.strip()) for e in examples):
        return "these look like Unix epoch numbers: convert them (to_timestamp(seconds), or milliseconds / 1000) and write UTC 'YYYY-MM-DD HH:MM:SS'"
    if re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", joined):
        return "write dates as ISO 8601 'YYYY-MM-DD HH:MM:SS' in UTC; slashed dates are ambiguous (month or day first)"
    if re.search(r"\d{2}:\d{2}(:\d{2})?\s+[+-]\d{4}\b", joined):
        return "convert to UTC and drop the offset ('2026-03-15 14:22:10 -0500' becomes '2026-03-15 19:22:10'); lcm import does this for vendor exports"
    return "write timestamps as ISO 8601 in UTC: 'YYYY-MM-DD HH:MM:SS' (a trailing Z is fine)"


def _tz_names() -> Optional[set]:
    try:
        import zoneinfo
        names = zoneinfo.available_timezones()
        return set(names) if names else None
    except Exception:  # noqa: BLE001
        return None


def _valid_timezone(value: str, names: Optional[set]) -> Optional[bool]:
    """True or False, or None when this machine has no time zone database to check against."""
    if names is not None:
        return value in names
    try:
        import zoneinfo
        zoneinfo.ZoneInfo(value)
        return True
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ == "ZoneInfoNotFoundError":
            return False
        return None


# ---------------------------------------------------------------------------------------------------------------------
# loading


@dataclass
class _Loaded:
    table: Table
    file: Path
    sql_name: str
    rows: int
    columns: List[str]                 # canonical columns present (lower case)
    extra: List[str]                   # other columns in the file


def _find_file(folder: Path, name: str) -> Optional[Path]:
    for suffix in (".parquet", ".csv"):
        p = folder / f"{name}{suffix}"
        if p.exists():
            return p
    by_lower = {p.name.lower(): p for p in folder.iterdir() if p.is_file()}
    for suffix in (".parquet", ".csv"):
        if f"{name}{suffix}" in by_lower:
            return by_lower[f"{name}{suffix}"]
    return None


def _load(con, folder: Path, findings: List[Finding]) -> Dict[str, _Loaded]:
    loaded: Dict[str, _Loaded] = {}
    for name, table in TABLES.items():
        path = _find_file(folder, name)
        if path is None:
            if table.required:
                findings.append(Finding("error", "missing_table", name, None, 0,
                                        f"{name}.csv (or {name}.parquet) is not in the folder; the engine cannot run without it",
                                        f"add {name}.csv with at least: " + ", ".join(c.name for c in table.columns if c.required)
                                        + " (or build it from a vendor export with lcm import)"))
            continue
        reader = (f"read_parquet({_lit(str(path))})" if path.suffix.lower() == ".parquet" else
                  f"read_csv({_lit(str(path))}, header=true, all_varchar=true, nullstr='')")
        try:
            cols = [d[0] for d in con.execute(f"SELECT * FROM {reader} LIMIT 0").description]
        except Exception as e:  # noqa: BLE001
            findings.append(Finding("error", "unreadable_file", name, None, 0,
                                    f"{path.name} could not be read: {str(e).splitlines()[0]}",
                                    "save it as UTF-8 CSV with a header row and a comma delimiter, or as Parquet"))
            continue
        canonical = set(table.column_names)
        by_lower: Dict[str, str] = {}
        for c in cols:
            if c.lower() in canonical:
                by_lower[c.lower()] = c          # the last one wins, as in the loader
        extra = [c for c in cols if c.lower() not in canonical]
        select = [f"CAST({_q(src)} AS VARCHAR) AS {_q(dst)}" for dst, src in by_lower.items()]
        select += [f"CAST({_q(c)} AS VARCHAR) AS {_q('extra:' + c)}" for c in extra]
        sql_name = f"chk_{name}"
        try:
            con.execute(f"CREATE TABLE {sql_name} AS SELECT {', '.join(select) or '1 AS _empty'} FROM {reader}")
        except Exception as e:  # noqa: BLE001
            findings.append(Finding("error", "unreadable_file", name, None, 0,
                                    f"{path.name} could not be read: {str(e).splitlines()[0]}",
                                    "check the file for unbalanced quotes or rows with too many fields"))
            continue
        rows = con.execute(f"SELECT count(*) FROM {sql_name}").fetchone()[0]
        loaded[name] = _Loaded(table, path, sql_name, rows, sorted(by_lower), extra)
        for col in table.columns:
            if col.required and col.name not in by_lower:
                close = difflib.get_close_matches(col.name, [c.lower() for c in extra], n=1, cutoff=0.6)
                hint = f" (is '{[c for c in extra if c.lower() == close[0]][0]}' it? rename it)" if close else ""
                findings.append(Finding("error", "missing_column", name, col.name, rows,
                                        f"required column {col.name} ({col.meaning}) is missing from {path.name}{hint}",
                                        f"add {col.name} as a column; names match case-insensitively"))
        if extra:
            for c in extra:
                close = difflib.get_close_matches(c.lower(), table.column_names, n=1, cutoff=0.75)
                hint = f"; did you mean {close[0]}?" if close and close[0] not in by_lower else ""
                findings.append(Finding("info", "unknown_column", name, c, rows,
                                        f"column {c} is not part of {name} and is ignored{hint}",
                                        f"rename it to {close[0]} if that is what it holds" if hint else "nothing to do unless it holds a canonical field"))
        if rows == 0 and table.required:
            findings.append(Finding("error", "empty_table", name, None, 0, f"{path.name} has a header but no rows",
                                    "export the data again; the engine needs at least one row"))
    known = {f"{n}{s}" for n in TABLES for s in (".csv", ".parquet")}
    others = sorted(p.name for p in folder.iterdir() if p.is_file() and p.name.lower() not in known
                    and p.name.lower() not in KNOWN_SIDE_FILES and not p.name.startswith("."))
    if others:
        findings.append(Finding("info", "extra_file", "(folder)", None, len(others),
                                "files that are not canonical tables are ignored: " + ", ".join(others[:8]) + (" ..." if len(others) > 8 else ""),
                                "run lcm import on vendor exports to turn them into canonical tables", others[:3]))
    return loaded


# ---------------------------------------------------------------------------------------------------------------------
# checks


def _column_checks(t: _Loaded, as_of: datetime) -> List[Tuple[str, str, str, str, str, str]]:
    """(severity, check, column, SQL condition for a bad row, message, fix) for each canonical column present."""
    out = []
    for col in t.table.columns:
        if col.name not in t.columns:
            continue
        c = _q(col.name)
        nonnull = f"{c} IS NOT NULL"
        if col.required:
            severity = "warning" if (t.table.name, col.name) == ("users", "email") else "error"
            out.append((severity, "required_empty", col.name, f"{c} IS NULL OR trim({c}) = ''",
                        f"rows with no {col.name}" + (" (email features skip them)" if severity == "warning" else ""),
                        f"fill {col.name} on every row, or drop rows that cannot have one"))
        typ = col.type.upper()
        if typ == "TIMESTAMP":
            out.append(("error", "type", col.name, f"{nonnull} AND TRY_CAST({c} AS TIMESTAMP) IS NULL",
                        "values that do not parse as a timestamp (the loader stops on the first one)", "TIMESTAMP"))
            out.append(("warning", "utc_offset", col.name,
                        f"{nonnull} AND TRY_CAST({c} AS TIMESTAMP) IS NOT NULL AND regexp_matches({c}, {_lit(OFFSET_SQL)}) "
                        f"AND NOT regexp_matches({c}, {_lit(ZERO_OFFSET_SQL)})",
                        "timestamps with a UTC offset: the loader keeps the clock time and drops the offset, so they are off by that many hours",
                        "convert to UTC before loading ('2026-03-15 14:22:10+02:00' becomes '2026-03-15 12:22:10')"))
        elif typ.startswith("DECIMAL"):
            out.append(("error", "type", col.name, f"{nonnull} AND TRY_CAST({c} AS {col.type}) IS NULL",
                        "values that do not parse as a number",
                        "remove currency symbols and thousands separators and use a dot for decimals (1234.50)"))
        elif typ == "INTEGER":
            out.append(("error", "type", col.name,
                        f"{nonnull} AND (TRY_CAST({c} AS INTEGER) IS NULL OR NOT regexp_matches(trim({c}), '^[+-]?[0-9]+(\\.0+)?$'))",
                        "values that are not whole numbers (a decimal would be rounded)", "write whole numbers only (3, not 2.5 or 'three')"))
        elif typ == "BOOLEAN":
            out.append(("error", "type", col.name, f"{nonnull} AND TRY_CAST({c} AS BOOLEAN) IS NULL",
                        "values that are not true or false", "use true or false (1/0, t/f, yes/no are also read)"))
        elif typ == "DATE":
            out.append(("error", "type", col.name, f"{nonnull} AND TRY_CAST({c} AS DATE) IS NULL",
                        "values that do not parse as a date", "write dates as YYYY-MM-DD"))
        elif typ == "JSON":
            out.append(("error", "type", col.name,
                        f"{nonnull} AND trim({c}) <> '' AND (CASE WHEN json_valid({c}) THEN json_type({c}) <> 'OBJECT' ELSE TRUE END)",
                        "values that are not a JSON object", "write each value as a JSON object, for example {\"order_id\":\"1001\"} (an empty object {} is fine)"))
    name = t.table.name
    if name == "users" and "email" in t.columns:
        out.append(("warning", "email_shape", "email", f"email IS NOT NULL AND NOT regexp_matches(email, {_lit(EMAIL_SQL)})",
                    "emails that are not a single well-formed address", "trim spaces and keep one address per row (name@domain.tld)"))
        out.append(("warning", "email_case", "email",
                    f"email IS NOT NULL AND regexp_matches(email, {_lit(EMAIL_SQL)}) AND email <> lower(email)",
                    "emails with capital letters: joins on email and suppression matching are case-sensitive in SQL",
                    "lower-case every email (lower(trim(email)))"))
    cutoff = (as_of + FUTURE_TOLERANCE).strftime("%Y-%m-%d %H:%M:%S")
    for tbl, col in CONSENT_TIMES:
        if tbl == name and col in t.columns:
            out.append(("error", "consent_future", col, f"TRY_CAST({_q(col)} AS TIMESTAMP) > TIMESTAMP {_lit(cutoff)}",
                        f"consent timestamps after {as_of:%Y-%m-%d %H:%M} UTC: newest-wins consent logic would let them outrank a real later unsubscribe",
                        "fix the source time zone or date format; a consent cannot be captured in the future"))
    for tbl, col in EVENT_TIMES:
        if tbl == name and col in t.columns:
            out.append(("warning", "future_event", col, f"TRY_CAST({_q(col)} AS TIMESTAMP) > TIMESTAMP {_lit(cutoff)}",
                        f"rows dated after {as_of:%Y-%m-%d %H:%M} UTC",
                        "check the time zone conversion (local time written as UTC runs ahead by the offset) and test events with fake dates"))
    return out


def _examples(con, t: _Loaded, column: str, condition: str) -> List[str]:
    rows = con.execute(f"SELECT {_q(column)} AS v, count(*) AS n FROM {t.sql_name} WHERE {condition} "
                       f"GROUP BY 1 ORDER BY n DESC, v NULLS FIRST LIMIT 3").fetchall()
    return [("(empty)" if v is None else _short(v)) for v, _n in rows]


def _run_column_checks(con, t: _Loaded, as_of: datetime, findings: List[Finding]) -> None:
    checks = _column_checks(t, as_of)
    if not checks or t.rows == 0:
        return
    sql = "SELECT " + ", ".join(f"count(*) FILTER (WHERE {cond}) AS c{i}" for i, (_s, _k, _c, cond, _m, _f) in enumerate(checks))
    counts = con.execute(f"{sql} FROM {t.sql_name}").fetchone()
    for (severity, check, column, cond, message, fix), n in zip(checks, counts, strict=True):
        if not n:
            continue
        examples = _examples(con, t, column, cond)
        if check == "type" and fix == "TIMESTAMP":
            fix = _timestamp_fix(examples)
        findings.append(Finding(severity, check, t.table.name, column, int(n), f"{n:,} {message}", fix, examples))


def _distinct(con, t: _Loaded, column: str) -> List[Tuple[str, int]]:
    return con.execute(f"SELECT {_q(column)}, count(*) FROM {t.sql_name} WHERE {_q(column)} IS NOT NULL "
                       f"GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {MAX_DISTINCT}").fetchall()


def _value_checks(con, loaded: Dict[str, _Loaded], findings: List[Finding]) -> None:
    def add(severity, check, t, column, bad: List[Tuple[str, int]], message, fix):
        if bad:
            n = sum(c for _v, c in bad)
            findings.append(Finding(severity, check, t.table.name, column, n, f"{n:,} {message}", fix,
                                    [_short(v) for v, _c in bad[:3]]))

    users = loaded.get("users")
    if users and "timezone" in users.columns:
        names = _tz_names()
        values = _distinct(con, users, "timezone")
        verdicts = [(v, c, _valid_timezone(v.strip(), names)) for v, c in values]
        if any(ok is None for _v, _c, ok in verdicts):
            findings.append(Finding("info", "timezone", "users", "timezone", 0,
                                    "time zones were not checked: this machine has no time zone database",
                                    "pip install tzdata, then run the check again"))
        else:
            add("warning", "timezone", users, "timezone", [(v, c) for v, c, ok in verdicts if not ok],
                "rows whose timezone is not an IANA zone name; send windows fall back to the account default for them",
                "use IANA names such as America/Chicago or Europe/London, not offsets or abbreviations (EST, GMT-5)")
    if users and "country" in users.columns:
        bad = [(v, c) for v, c in _distinct(con, users, "country") if v.strip() not in COUNTRY_CODES]
        if bad:
            mapped = [f"{v} -> {country_code(v)}" for v, _c in bad if country_code(v)]
            fix = ("write ISO 3166-1 alpha-2 codes in capitals" + (f" ({'; '.join(mapped[:2])})" if mapped else " (US, CA, GB)"))
            add("warning", "country", users, "country", bad,
                "rows whose country is not an ISO 3166-1 alpha-2 code; the law that applies to them cannot be decided", fix)
    if users and "phone" in users.columns:
        add("warning", "phone", users, "phone", [(v, c) for v, c in _distinct(con, users, "phone") if not E164.match(v.strip())],
            "phone numbers not in E.164 form; SMS tools reject them", "write +<country code><number> with no spaces or dashes (+15555550123)")
    for tbl, col in CURRENCY_COLUMNS:
        t = loaded.get(tbl)
        if t and col in t.columns:
            add("warning", "currency", t, col, [(v, c) for v, c in _distinct(con, t, col) if not re.fullmatch(r"[A-Z]{3}", v.strip())],
                "rows whose currency is not a three-letter ISO 4217 code in capitals", "write USD, CAD, GBP (upper-case three letters)")
    for (tbl, col), allowed in ENUMS.items():
        t = loaded.get(tbl)
        if t and col in t.columns:
            bad = [(v, c) for v, c in _distinct(con, t, col) if v not in allowed]
            if bad:
                lowered = [v for v, _c in bad if v.strip().lower() in allowed]
                fix = ("lower-case the values" if lowered and len(lowered) == len(bad) else
                       f"map them to one of: {', '.join(allowed)}")
                add("warning", "enum", t, col, bad, f"rows whose {col} is not one the engine recognizes", fix)
    events = loaded.get("track_events")
    if events and "event_name" in events.columns:
        bad = [(v, c) for v, c in _distinct(con, events, "event_name") if not EVENT_NAME.match(v)]
        if bad:
            suggestions = [f"{v} -> {snake_case(v)}" for v, _c in bad[:2] if snake_case(v) != v and "_" in snake_case(v)]
            fix = ("rename to snake_case object_action" + (f" ({'; '.join(suggestions)})" if suggestions else
                                                          " (order_completed, product_viewed, checkout_started)"))
            add("warning", "event_name_format", events, "event_name", bad,
                "rows whose event_name is not snake_case object_action; triggers and the tracking plan match names exactly", fix)


def _key_checks(con, loaded: Dict[str, _Loaded], findings: List[Finding]) -> None:
    for tbl, col in PRIMARY_KEYS:
        t = loaded.get(tbl)
        if not t or col not in t.columns:
            continue
        dupes = con.execute(f"SELECT {_q(col)}, count(*) AS n FROM {t.sql_name} WHERE {_q(col)} IS NOT NULL "
                            f"GROUP BY 1 HAVING count(*) > 1 ORDER BY n DESC, 1").fetchall()
        if dupes:
            rows = sum(n for _v, n in dupes)
            findings.append(Finding("error", "primary_key", tbl, col, rows,
                                    f"{rows:,} rows share {len(dupes):,} {col} value(s) that should be unique",
                                    f"deduplicate on {col}, keeping the newest row" if tbl != "track_events" else
                                    "deduplicate on event_id (a retried webhook or a double export writes the same event twice)",
                                    [_short(v) for v, _n in dupes[:3]]))


def _reference_checks(con, loaded: Dict[str, _Loaded], findings: List[Finding]) -> None:
    for child, ccol, parent, pcol, core in REFERENCES:
        c, p = loaded.get(child), loaded.get(parent)
        if not c or ccol not in c.columns:
            continue
        if not p or pcol not in p.columns:
            if core and c.rows:
                findings.append(Finding("info", "reference", child, ccol, 0,
                                        f"{child}.{ccol} could not be checked: {parent} is not in the folder",
                                        f"add {parent}.csv to check that every {ccol} exists"))
            continue
        cond = (f"{_q(ccol)} IS NOT NULL AND trim({_q(ccol)}) <> '' AND NOT EXISTS "
                f"(SELECT 1 FROM {p.sql_name} p WHERE p.{_q(pcol)} = {c.sql_name}.{_q(ccol)})")
        total, bad = con.execute(f"SELECT count(*) FILTER (WHERE {_q(ccol)} IS NOT NULL AND trim({_q(ccol)}) <> ''), "
                                 f"count(*) FILTER (WHERE {cond}) FROM {c.sql_name}").fetchone()
        if not bad:
            continue
        share = bad / total if total else 0.0
        severity = "error" if core and share >= ORPHAN_ERROR_SHARE else "warning"
        findings.append(Finding(severity, "reference", child, ccol, int(bad),
                                f"{bad:,} of {total:,} rows ({share:.1%}) have a {ccol} that is not in {parent}.{pcol}; joins drop them",
                                f"export {parent} in full (every {pcol} that {child} mentions), or map ids to the same key as {parent}",
                                _examples(con, c, ccol, cond)))


def _freshness(con, loaded: Dict[str, _Loaded], as_of: datetime, findings: List[Finding]) -> Optional[str]:
    t = loaded.get("track_events")
    if not t or "event_at" not in t.columns or not t.rows:
        return None
    cutoff = (as_of + FUTURE_TOLERANCE).strftime("%Y-%m-%d %H:%M:%S")
    newest = con.execute(f"SELECT max(TRY_CAST(event_at AS TIMESTAMP)) FROM {t.sql_name} "
                         f"WHERE TRY_CAST(event_at AS TIMESTAMP) <= TIMESTAMP {_lit(cutoff)}").fetchone()[0]
    if newest is None:
        return None
    age = as_of - newest
    days = age.total_seconds() / 86400
    stamp = newest.strftime("%Y-%m-%d %H:%M")
    if days > STALE_AFTER_DAYS:
        findings.append(Finding("warning", "freshness", "track_events", "event_at", 0,
                                f"the newest event is {stamp} UTC, {days:.1f} days before {as_of:%Y-%m-%d}: daily jobs would read stale data",
                                "export again, or check that the event pipeline has not stopped", [stamp]))
    else:
        findings.append(Finding("info", "freshness", "track_events", "event_at", 0,
                                f"the newest event is {stamp} UTC, {days:.1f} days before {as_of:%Y-%m-%d %H:%M}",
                                "nothing to do", [stamp]))
    return stamp


# ---------------------------------------------------------------------------------------------------------------------
# public API


def check_folder(folder: Path | str, as_of: Optional[datetime] = None) -> Dict[str, Any]:
    """Check every canonical table in `folder`. Returns {ok, counts, tables, findings, ...}; ok is False on any error."""
    import duckdb
    folder = Path(folder)
    as_of = (as_of or datetime.now(timezone.utc))
    if as_of.tzinfo is not None:
        as_of = as_of.astimezone(timezone.utc).replace(tzinfo=None)
    findings: List[Finding] = []
    if not folder.is_dir():
        findings.append(Finding("error", "missing_folder", "(folder)", None, 0, f"{folder} is not a folder",
                                "pass the folder that holds users.csv, track_events.csv and the other tables"))
        return _report(folder, as_of, {}, findings, None)
    con = duckdb.connect()
    loaded = _load(con, folder, findings)
    for t in loaded.values():
        _run_column_checks(con, t, as_of, findings)
    _value_checks(con, loaded, findings)
    _key_checks(con, loaded, findings)
    _reference_checks(con, loaded, findings)
    newest = _freshness(con, loaded, as_of, findings)
    con.close()
    return _report(folder, as_of, loaded, findings, newest)


def _report(folder: Path, as_of: datetime, loaded: Dict[str, _Loaded], findings: List[Finding], newest: Optional[str]) -> Dict[str, Any]:
    order = {name: i for i, name in enumerate(TABLES)}
    findings.sort(key=lambda f: (SEVERITIES.index(f.severity), order.get(f.table, -1), f.column or "", f.check))
    counts = {s: sum(1 for f in findings if f.severity == s) for s in SEVERITIES}
    return {
        "folder": str(folder),
        "as_of": as_of.strftime("%Y-%m-%d %H:%M:%S"),
        "ok": counts["error"] == 0,
        "counts": counts,
        "newest_event_at": newest,
        "tables": [{"table": n, "file": t.file.name, "rows": t.rows, "columns": t.columns, "extra_columns": t.extra}
                   for n, t in loaded.items()],
        "missing_optional_tables": [n for n, t in TABLES.items() if not t.required and n not in loaded],
        "findings": [dict(asdict(f), where=f.where) for f in findings],
    }


def render_markdown(report: Dict[str, Any]) -> str:
    c = report["counts"]
    verdict = "PASS" if report["ok"] else "FAIL"
    parts = [f"{c['error']} error(s)"] if c["error"] else []
    parts += [f"{c['warning']} warning(s)", f"{c['info']} note(s)"]
    L = [f"# Data check: {report['folder']}", "",
         f"**{verdict}** with {', '.join(parts)}, checked as of {report['as_of'][:16]} UTC.", ""]
    if report["tables"]:
        L += ["| Table | File | Rows |", "|---|---|---:|"]
        L += [f"| {t['table']} | {t['file']} | {t['rows']:,} |" for t in report["tables"]]
        L.append("")
    if report.get("missing_optional_tables"):
        L += ["Optional tables not present (features that need them say so): " + ", ".join(report["missing_optional_tables"]) + ".", ""]
    titles = {"error": "Errors: fix these before the engine reads the folder",
              "warning": "Warnings: the engine runs, but these rows are skipped or misread",
              "info": "Notes"}
    for sev in SEVERITIES:
        items = [f for f in report["findings"] if f["severity"] == sev]
        if not items:
            continue
        L += [f"## {titles[sev]}", ""]
        for f in items:
            ex = (" Examples: " + ", ".join(f"`{e}`" for e in f["examples"]) + ".") if f["examples"] and f["check"] not in ("freshness",) else ""
            fix = "" if f["fix"] == "nothing to do" else f" Fix: {f['fix']}."
            L.append(f"- **{f['where']}**: {f['message']}.{ex}{fix}")
        L.append("")
    if not report["findings"]:
        L += ["Nothing to report: every table and column matches the canonical schema.", ""]
    return "\n".join(L)


def _parse_as_of(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="lcm check-data",
                                 description="Check a data folder against the canonical tables (engine/data/schema.py)")
    ap.add_argument("folder", help="folder holding users.csv, track_events.csv and the other canonical tables")
    ap.add_argument("--json", action="store_true", help="print the report as JSON instead of Markdown")
    ap.add_argument("--as-of", help="judge future dates and freshness against this UTC time (default: now), e.g. 2026-10-19")
    a = ap.parse_args(argv)
    report = check_folder(Path(a.folder), _parse_as_of(a.as_of))
    print(json.dumps(report, indent=2) if a.json else render_markdown(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(_cli())
