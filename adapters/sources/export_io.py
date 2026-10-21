"""Shared plumbing for the source mappers: reading vendor files, matching their columns, parsing their dates and money,
and writing canonical tables in one deterministic shape.

Why one module: every vendor writes dates, money, yes/no flags and column names a little differently, and the rules for
reading them (what counts as UTC, what a blank means, which consent wins) must be the same in every mapper, or two
sources imported together would disagree about the same person.

    from adapters.sources.export_io import Columns, read_rows, parse_ts, write_tables
    headers, rows = read_rows(Path("exports/shopify/orders_export.csv"))
    cols = Columns(headers)
    created = parse_ts(cols.get(rows[0], "Created at", "created_at"))
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from engine.data.schema import TABLES

SKIP_PARTS = {"__pycache__", ".git", "node_modules"}
TABULAR = (".csv", ".csv.gz", ".tsv", ".txt")

# How each canonical table is sorted when written, so the same input always gives byte-identical files.
SORT_KEYS: Dict[str, Tuple[str, ...]] = {
    "users": ("user_id",), "track_events": ("event_at", "event_id"), "orders": ("order_at", "order_id"),
    "line_items": ("order_id", "line_id", "product_id"), "products": ("product_id",), "discounts": ("order_id", "code"),
    "refunds": ("refunded_at", "refund_id"), "accounts": ("account_id",), "subscriptions": ("subscription_id",),
    "invoices": ("created_at", "invoice_id"), "consent_events": ("user_id", "channel", "occurred_at", "status", "source"),
    "message_events": ("event_at", "event_id"), "lcm_flow_entries": ("flow", "user_id", "anchor_date"),
}
UNDATED_SOURCE = "import_undated"   # consent_events.source for a record whose time the export did not carry (ASSUMED time)


# ---------------------------------------------------------------------------------------------------------------------
# files and columns


def norm(name: Any) -> str:
    """Column name reduced to lower-case letters and digits: 'Lineitem compare-at price' == 'lineitem compare at price'."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def iter_files(folder: Path, suffixes: Sequence[str] = TABULAR) -> List[Path]:
    """Every file under `folder` (recursively) whose name ends with one of `suffixes`, in a stable order."""
    out = []
    for p in sorted(Path(folder).rglob("*")):
        rel = p.relative_to(folder).parts
        if p.is_file() and not any(part in SKIP_PARTS or part.startswith(".") for part in rel) \
                and p.name.lower().endswith(tuple(suffixes)):
            out.append(p)
    return out


def _open_text(path: Path) -> io.TextIOBase:
    if path.name.lower().endswith(".gz"):
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8-sig", newline="")
    return open(path, encoding="utf-8-sig", newline="")


def _delimiter(path: Path, first_line: str) -> str:
    if path.name.lower().endswith((".tsv", ".tsv.gz")):
        return "\t"
    return max((",", ";", "\t"), key=first_line.count) if first_line.count(",") == 0 else ","


def read_headers(path: Path) -> List[str]:
    """The header row of a CSV (plain or .gz), or [] when the file cannot be read as one."""
    try:
        with _open_text(path) as fh:
            first = fh.readline()
        return next(csv.reader([first], delimiter=_delimiter(path, first))) if first.strip() else []
    except Exception:  # noqa: BLE001
        return []


def read_rows(path: Path) -> Tuple[List[str], List[List[str]]]:
    """(headers, rows) of a CSV, plain or gzip-compressed. Rows shorter than the header are padded with blanks."""
    with _open_text(path) as fh:
        text = fh.read()
    first = text.split("\n", 1)[0]
    rows = list(csv.reader(io.StringIO(text), delimiter=_delimiter(path, first)))
    if not rows:
        return [], []
    headers = [h.strip() for h in rows[0]]
    width = len(headers)
    body = [r + [""] * (width - len(r)) if len(r) < width else r for r in rows[1:] if any(c.strip() for c in r)]
    return headers, body


def read_jsonl(path: Path) -> Iterator[Dict[str, Any]]:
    """Each JSON object in a .jsonl or .jsonl.gz file."""
    with _open_text(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    yield obj


def relative(path: Path, folder: Path) -> str:
    try:
        return path.relative_to(folder).as_posix()
    except ValueError:
        return path.name


class Columns:
    """Finds a vendor column by any of its known names, case- and punctuation-insensitively, and remembers which
    columns were used so the mapping report can list the rest. A header that appears twice (Shopify's orders export
    has two Phone columns) is read as the first non-blank value among its copies."""

    def __init__(self, headers: Sequence[str]):
        self.headers = list(headers)
        self.index: Dict[str, List[int]] = {}
        for i, h in enumerate(self.headers):
            self.index.setdefault(norm(h), []).append(i)
        self.used: set = set()

    def has(self, *aliases: str) -> bool:
        return any(norm(a) in self.index for a in aliases)

    def name(self, *aliases: str) -> Optional[str]:
        for a in aliases:
            idx = self.index.get(norm(a))
            if idx:
                return self.headers[idx[0]]
        return None

    def get(self, row: Sequence[str], *aliases: str) -> str:
        for a in aliases:
            idx = self.index.get(norm(a))
            if not idx:
                continue
            self.used.update(idx)
            for i in idx:
                if i < len(row) and row[i] is not None and str(row[i]).strip():
                    return str(row[i]).strip()
        return ""

    def mark(self, *aliases: str) -> None:
        """Count columns as used without reading them (read for validation only, or deliberately ignored)."""
        for a in aliases:
            self.used.update(self.index.get(norm(a), []))

    def unmapped(self) -> List[str]:
        return [h for i, h in enumerate(self.headers) if i not in self.used and h]


# ---------------------------------------------------------------------------------------------------------------------
# values


_OFFSET_TS = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)\s*(?:(Z|UTC|GMT)|([+-])(\d{2}):?(\d{2})?)$", re.I)


class TimeParser:
    """Parses vendor timestamps into aware UTC datetimes and counts what it had to assume.

    Understood: ISO 8601 with or without T, with Z or an offset ('2026-03-15T14:22:10-05:00'), the export style
    '2026-03-15 14:22:10 -0500' (Shopify), plain dates, and Unix epoch seconds or milliseconds. A timestamp with no
    offset is read as UTC and counted in `naive`; anything else (slashed dates such as 03/04/2026, which could be
    either month or day first) is refused and counted in `failed`, never guessed."""

    def __init__(self) -> None:
        self.naive = 0
        self.failed = 0
        self.failed_examples: List[str] = []

    def __call__(self, value: Any) -> Optional[datetime]:
        if value is None:
            return None
        s = str(value).strip()
        if not s:
            return None
        dt = self._parse(s)
        if dt is None:
            self.failed += 1
            if len(self.failed_examples) < 3:
                self.failed_examples.append(s[:40])
        return dt

    def _parse(self, s: str) -> Optional[datetime]:
        if re.fullmatch(r"\d{9,13}(\.\d+)?", s):
            n = float(s)
            return datetime.fromtimestamp(n / 1000 if n > 1e11 else n, tz=timezone.utc)
        m = _OFFSET_TS.match(s)
        if m:
            day, clock, z, sign, hh, mm = m.groups()
            offset = "+00:00" if z else f"{sign}{hh}:{mm or '00'}"
            try:
                return datetime.fromisoformat(f"{day}T{clock}{offset}").astimezone(timezone.utc)
            except ValueError:
                return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            self.naive += 1
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)


def parse_ts(value: Any) -> Optional[datetime]:
    """One-off parse with the TimeParser rules (naive means UTC)."""
    return TimeParser()(value)


def fmt_ts(dt: Optional[datetime]) -> str:
    """UTC 'YYYY-MM-DD HH:MM:SS', the form the canonical tables and the loader expect; '' for None."""
    if dt is None:
        return ""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def money(value: Any) -> Optional[Decimal]:
    """'$1,234.50', '1234.5', '(12.00)' and '1.234,50' as a Decimal with two places; None for blank or unreadable."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    negative = s.startswith("(") and s.endswith(")") or s.startswith("-")
    s = re.sub(r"[^0-9.,]", "", s)
    if re.fullmatch(r"\d{1,3}(\.\d{3})*,\d{1,2}", s):
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        d = Decimal(s).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    return -d if negative else d


def fmt_money(d: Optional[Decimal]) -> str:
    return "" if d is None else f"{d:.2f}"


def num(d: Optional[Decimal]) -> Optional[float]:
    """A Decimal as a JSON number (float), or None."""
    return None if d is None else float(d)


def yes(value: Any) -> Optional[bool]:
    """'yes', 'true', '1', 'subscribed' -> True; 'no', 'false', '0' -> False; blank or anything else -> None."""
    s = norm(value)
    if s in ("yes", "y", "true", "t", "1", "subscribed", "optedin"):
        return True
    if s in ("no", "n", "false", "f", "0", "unsubscribed", "optedout"):
        return False
    return None


def email_norm(value: Any) -> str:
    return str(value or "").strip().lower()


def email_ok(value: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value or ""))


def email_user_id(email: str) -> str:
    """A stable user id made from the email address: the same in every mapper, so two exports of the same person join
    without a shared id, and the address itself is never used as the key."""
    return "em_" + hashlib.sha256(email_norm(email).encode("utf-8")).hexdigest()[:16]


def phone_e164(value: Any, country: Optional[str] = None) -> str:
    """'+1 (555) 555-0123' -> '+15555550123'; a ten-digit North American number gets +1. Anything else is returned with
    only its digits and leading plus kept, for check-data to flag."""
    s = str(value or "").strip().lstrip("'")
    if not s:
        return ""
    plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if plus:
        return "+" + digits
    if len(digits) == 10 and (country or "US") in ("US", "CA"):
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1") and (country or "US") in ("US", "CA"):
        return "+" + digits
    return digits


def strip_id(value: Any) -> str:
    """An id as exported: spreadsheet guards (a leading apostrophe) and surrounding spaces removed."""
    return str(value or "").strip().lstrip("'").strip()


def slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")


def jdump(obj: Any) -> str:
    """Compact JSON with sorted keys: deterministic, and what properties columns hold."""
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------------------------------------------------------------
# consent


def consent_rollup(consent_rows: Iterable[Dict[str, str]]) -> Dict[Tuple[str, str], str]:
    """(user_id, channel) -> when the consent now in force was given ('' when the newest record is a revoke).

    Newest wins: records are read in time order; a revoke clears consent and the next grant starts it again. Two records
    at the same second are read revoke last, so a tie never grants."""
    by_key: Dict[Tuple[str, str], List[Tuple[str, int, str]]] = {}
    for r in consent_rows:
        if (r.get("purpose") or "marketing") != "marketing":
            continue
        by_key.setdefault((r["user_id"], r["channel"]), []).append(
            (r["occurred_at"], 1 if r["status"] == "revoked" else 0, r["occurred_at"] if r["status"] == "granted" else ""))
    out: Dict[Tuple[str, str], str] = {}
    for key, recs in by_key.items():
        current = ""
        for _at, revoked, granted_at in sorted(recs):
            if revoked:
                current = ""
            elif not current:
                current = granted_at
        out[key] = current
    return out


def apply_consent(users: List[Dict[str, str]], consent_rows: List[Dict[str, str]]) -> None:
    """Set users.email_consent_at and users.sms_consent_at from consent_events (newest wins)."""
    state = consent_rollup(consent_rows)
    for u in users:
        u["email_consent_at"] = state.get((u["user_id"], "email"), "")
        u["sms_consent_at"] = state.get((u["user_id"], "sms"), "")


# ---------------------------------------------------------------------------------------------------------------------
# writing


def write_tables(out_dir: Path, tables: Dict[str, List[Dict[str, Any]]]) -> Dict[str, int]:
    """Write each non-empty table as <table>.csv in schema column order (required columns always, optional columns only
    when some row has a value), sorted by SORT_KEYS. Returns table -> rows written."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: Dict[str, int] = {}
    for name in sorted(tables, key=list(TABLES).index):
        rows = tables[name]
        if not rows:
            continue
        table = TABLES[name]
        cols = [c.name for c in table.columns if c.required or any(str(r.get(c.name) or "") for r in rows)]
        keys = SORT_KEYS.get(name, tuple(cols[:1]))
        ordered = sorted(rows, key=lambda r: tuple(str(r.get(k) or "") for k in keys))
        with open(out_dir / f"{name}.csv", "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(cols)
            for r in ordered:
                w.writerow(["" if r.get(c) is None else str(r.get(c)) for c in cols])
        written[name] = len(ordered)
    return written


def read_canonical(folder: Path) -> Dict[str, List[Dict[str, str]]]:
    """Canonical <table>.csv files in a folder as lists of dictionaries (only tables that exist)."""
    out: Dict[str, List[Dict[str, str]]] = {}
    for name in TABLES:
        p = Path(folder) / f"{name}.csv"
        if p.exists():
            with open(p, encoding="utf-8", newline="") as fh:
                out[name] = [dict(r) for r in csv.DictReader(fh)]
    return out


def window(datetimes: Iterable[Optional[datetime]]) -> Tuple[Optional[datetime], Optional[datetime]]:
    """(earliest, latest) of the datetimes that are not None."""
    vals = [d for d in datetimes if d is not None]
    return (min(vals), max(vals)) if vals else (None, None)
