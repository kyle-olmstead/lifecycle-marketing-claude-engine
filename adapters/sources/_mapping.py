"""Shared plumbing for the source mappers in this folder: tolerant column lookup, timestamps, money, canonical CSVs.

What it does
  Every mapper (stripe.py, hubspot_export.py, customerio_export.py, ga4_bigquery.py, rudderstack.py, segment.py)
  reads a vendor export and writes the canonical tables of engine/data/schema.py. The same small jobs repeat in
  each of them, so they live here once:

  * Sheet        a CSV file with case-insensitive, punctuation-blind column lookup ("Created (UTC)", "created",
                 "CREATED_UTC" all match the alias "created"), which also remembers which columns were used so
                 the mapping report can list the ones that were not.
  * TimeParser   ISO 8601 strings, "YYYY-MM-DD HH:MM" strings and Unix epochs (seconds, milliseconds or
                 microseconds) into UTC datetimes. A value without a zone is read in a stated zone (UTC unless
                 the mapper says otherwise) and the parser counts how often it had to, so the mapper can record
                 that as an assumption instead of hiding it.
  * money        "1,234.50", "$49.00" or Stripe minor units into Decimal, never float.
  * write_table  a canonical CSV: every column of the table in schema order, ISO 8601 UTC timestamps
                 (2026-03-01T14:22:05Z), JSON properties as a compact JSON object string, empty for unknown.

Why
  Deterministic output: the same export always produces byte-identical CSVs (rows sorted by key, JSON keys
  sorted), so a mapping can be reviewed with a diff and tested exactly.

Usage
    from adapters.sources._mapping import Sheet, TimeParser, write_table
    sheet = Sheet.read(Path("export/contacts.csv"))
    tp = TimeParser()
    for row in sheet.rows:
        created = tp.parse(sheet.get(row, "create date", "createdate"))
    write_table(out_dir, "users", rows)

The leading underscore marks this as a helper, not a mapper: it has no NAME, detect() or map().
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from engine.data.schema import TABLES

UTC = timezone.utc
CENT = Decimal("0.01")


# ----------------------------------------------------------------------------------------------
# column names
# ----------------------------------------------------------------------------------------------

def norm(name: str) -> str:
    """A column name reduced to lower-case letters and digits, with a trailing or bracketed "UTC" dropped.

    "Created (UTC)" -> "created", "Current Period End UTC" -> "currentperiodend", "created_at" -> "createdat".
    """
    s = str(name).strip().lower()
    s = re.sub(r"\(\s*utc\s*\)", "", s)
    s = re.sub(r"[\s_]utc$", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


@dataclass
class Sheet:
    """One CSV export: its header, its rows (dicts keyed by the original header) and which columns a mapper used."""

    path: Path
    headers: List[str]
    rows: List[Dict[str, str]]
    used: set = field(default_factory=set)

    @classmethod
    def read(cls, path: Path) -> "Sheet":
        with Path(path).open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            headers = [h for h in (reader.fieldnames or []) if h is not None]
            rows = [{k: (v if v is not None else "") for k, v in r.items() if k is not None} for r in reader]
        return cls(Path(path), headers, rows)

    @property
    def index(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for h in self.headers:
            out.setdefault(norm(h), h)
        return out

    def column(self, *aliases: str) -> Optional[str]:
        """The first header matching one of the aliases (compared with norm()), marked as used."""
        idx = self.index
        for a in aliases:
            h = idx.get(norm(a))
            if h is not None:
                self.used.add(h)
                return h
        return None

    def has(self, *aliases: str) -> bool:
        idx = self.index
        return any(norm(a) in idx for a in aliases)

    def get(self, row: Dict[str, str], *aliases: str) -> Optional[str]:
        """The first non-empty value among the aliased columns, stripped; None when all are empty or absent."""
        idx = self.index
        for a in aliases:
            h = idx.get(norm(a))
            if h is None:
                continue
            self.used.add(h)
            v = (row.get(h) or "").strip()
            if v and v.lower() not in ("null", "none", "nan"):
                return v
        return None

    def use(self, header: str) -> None:
        self.used.add(header)

    def unused(self) -> List[str]:
        return [h for h in self.headers if h not in self.used]


def csv_header(path: Path) -> List[str]:
    """The header row of a CSV file, or [] when it cannot be read."""
    try:
        with Path(path).open(newline="", encoding="utf-8-sig") as fh:
            return next(csv.reader(fh), [])
    except (OSError, UnicodeDecodeError, csv.Error):
        return []


SKIP_PARTS = {"__pycache__", ".git", "node_modules"}


def files_under(folder: Path, suffixes: Sequence[str]) -> List[Path]:
    """Every file under `folder`, searched recursively, whose name ends with one of `suffixes`, in a stable order.

    Recursive so that `lcm import auto exports/` finds each vendor's files in its own subfolder; hidden folders,
    __pycache__ and node_modules are skipped.
    """
    folder = Path(folder)
    if not folder.is_dir():
        return []
    out = []
    for p in sorted(folder.rglob("*")):
        parts = p.relative_to(folder).parts
        if p.is_file() and not any(x in SKIP_PARTS or x.startswith(".") for x in parts) \
                and p.name.lower().endswith(tuple(s.lower() for s in suffixes)):
            out.append(p)
    return out


def csv_files(folder: Path) -> List[Path]:
    return files_under(folder, (".csv",))


def rel(path: Path, folder: Path) -> str:
    """A file's path relative to the folder given to map(), for the report (posix separators)."""
    try:
        return Path(path).relative_to(Path(folder)).as_posix()
    except ValueError:
        return Path(path).name


# ----------------------------------------------------------------------------------------------
# time
# ----------------------------------------------------------------------------------------------

_SLASH = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AaPp][Mm])?)?$")


class TimeParser:
    """Turns vendor timestamps into aware UTC datetimes and counts every judgment it made.

    naive_tz   the zone a timestamp without an offset is read in (UTC unless the vendor documents another)
    counts     how many values were read without a zone ("naive"), or as month-first slash dates ("slash")
    """

    def __init__(self, naive_tz: tzinfo = UTC):
        self.naive_tz = naive_tz
        self.counts: Dict[str, int] = {"naive": 0, "slash": 0, "unparsed": 0}

    def parse(self, value: Any) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return self._aware(value)
        if isinstance(value, date):
            return self._aware(datetime(value.year, value.month, value.day))
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float, Decimal)):
            return epoch(value)
        s = str(value).strip()
        if not s or s.lower() in ("null", "none", "nan"):
            return None
        if re.fullmatch(r"-?\d+(\.\d+)?", s):
            return epoch(Decimal(s))
        t = re.sub(r"\s+(UTC|GMT)$", "+00:00", s, flags=re.I)        # BigQuery CSV: "2026-03-01 14:22:05 UTC"
        t = re.sub(r"\s+([+-]\d{2}:?\d{2})$", r"\1", t)                # Snowflake CSV: "2026-03-01 14:22:05.000 +0000"
        try:
            return self._aware(datetime.fromisoformat(t.replace("Z", "+00:00").replace("z", "+00:00")))
        except ValueError:
            pass
        m = _SLASH.match(s)
        if m:
            month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
            hour, minute, sec = int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0)
            ampm = (m.group(7) or "").lower()
            if ampm == "pm" and hour < 12:
                hour += 12
            if ampm == "am" and hour == 12:
                hour = 0
            if month > 12 and day <= 12:   # unambiguous day-first
                month, day = day, month
            self.counts["slash"] += 1
            try:
                return self._aware(datetime(year, month, day, hour, minute, sec))
            except ValueError:
                pass
        self.counts["unparsed"] += 1
        return None

    def _aware(self, dt: datetime) -> datetime:
        if dt.tzinfo is None:
            self.counts["naive"] += 1
            dt = dt.replace(tzinfo=self.naive_tz)
        return dt.astimezone(UTC)

    def notes(self, source: str, zone_name: str = "UTC") -> List[str]:
        """Plain-words assumptions for the mapping report."""
        out = []
        if self.counts["naive"]:
            out.append(f"ASSUMED: {self.counts['naive']} {source} timestamp value(s) carried no time zone; read as {zone_name}.")
        if self.counts["slash"]:
            out.append(f"ASSUMED: {self.counts['slash']} {source} date value(s) were slash dates (03/04/2026); read month first "
                       "unless the first number was above 12.")
        return out

    def warnings(self, source: str) -> List[str]:
        if self.counts["unparsed"]:
            return [f"{self.counts['unparsed']} {source} timestamp value(s) could not be read and were left empty."]
        return []


def epoch(value: Any) -> Optional[datetime]:
    """Unix epoch to UTC datetime. The unit is read from the magnitude: microseconds, milliseconds or seconds."""
    try:
        n = Decimal(str(value))
    except InvalidOperation:
        return None
    if n == 0:
        return None
    a = abs(n)
    if a >= Decimal(10) ** 14:
        micros = int(n)
    elif a >= Decimal(10) ** 11:
        micros = int(n * 1000)
    else:
        micros = int(n * 1_000_000)
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=micros)


def iso(dt: Optional[datetime]) -> str:
    """ISO 8601 UTC with a Z; microseconds only when present. Empty string for None."""
    if dt is None:
        return ""
    dt = dt.astimezone(UTC)
    base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{dt.microsecond:06d}Z" if dt.microsecond else f"{base}Z"


# ----------------------------------------------------------------------------------------------
# values
# ----------------------------------------------------------------------------------------------

def money(value: Any) -> Optional[Decimal]:
    """A decimal amount from "1,234.50", "$49.00", "49" or a number; None when it is not a number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    s = str(value).strip()
    negative = s.startswith("(") and s.endswith(")")
    s = re.sub(r"[^0-9.\-]", "", s)
    if not s or s in ("-", ".", "-."):
        return None
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return -d if negative else d


def cents(d: Optional[Decimal]) -> str:
    """Decimal to a 2-place string ("49.00"); empty for None."""
    if d is None:
        return ""
    return str(d.quantize(CENT, rounding=ROUND_HALF_UP))


def as_int(value: Any) -> Optional[int]:
    d = money(value)
    if d is None:
        return None
    try:
        return int(d)
    except (ValueError, OverflowError):
        return None


def truthy(value: Any) -> Optional[bool]:
    """Yes/no columns as exported by vendors: true/false, yes/no, 1/0, t/f. None when empty or unknown."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("true", "t", "yes", "y", "1", "checked"):
        return True
    if s in ("false", "f", "no", "n", "0", "unchecked"):
        return False
    return None


def email(value: Any) -> Optional[str]:
    """Lower-cased, trimmed email; None when it does not look like an address."""
    if not value:
        return None
    s = str(value).strip().lower()
    return s if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", s) else None


def phone_e164(value: Any) -> Optional[str]:
    """An E.164 number (+15550100) when the value already carries a country code; None otherwise (never guessed)."""
    if not value:
        return None
    s = re.sub(r"[\s().\-]", "", str(value))
    return s if re.fullmatch(r"\+[1-9]\d{6,14}", s) else None


def snake(name: str) -> str:
    """object_action names: "Order Completed" -> order_completed, "reportShared" -> report_shared."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(name).strip())
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    return s or "unnamed_event"


def scalar(text: str) -> Any:
    """A CSV cell as a JSON scalar: true/false, an integer without leading zeros, a decimal, else the string."""
    s = text.strip()
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    if re.fullmatch(r"-?(0|[1-9]\d{0,17})", s):
        return int(s)
    if re.fullmatch(r"-?(0|[1-9]\d*)\.\d+", s):
        return float(s)
    return s


def dumps(obj: Any) -> str:
    """Compact, key-sorted JSON (deterministic)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_json_default)


def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, datetime):
        return iso(o)
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


# ----------------------------------------------------------------------------------------------
# countries
# ----------------------------------------------------------------------------------------------

ISO2 = set("""AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ
CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD
GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM
KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA
NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ
SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU
WF WS YE YT ZA ZM ZW""".split())

COUNTRY_NAMES = {
    "united states": "US", "united states of america": "US", "usa": "US", "us": "US", "america": "US",
    "canada": "CA", "mexico": "MX", "brazil": "BR", "argentina": "AR", "chile": "CL", "colombia": "CO", "peru": "PE",
    "uruguay": "UY", "puerto rico": "PR",
    "united kingdom": "GB", "uk": "GB", "great britain": "GB", "britain": "GB", "england": "GB", "scotland": "GB",
    "wales": "GB", "northern ireland": "GB", "ireland": "IE", "germany": "DE", "deutschland": "DE", "france": "FR",
    "spain": "ES", "italy": "IT", "portugal": "PT", "netherlands": "NL", "the netherlands": "NL", "holland": "NL",
    "belgium": "BE", "luxembourg": "LU", "switzerland": "CH", "austria": "AT", "sweden": "SE", "norway": "NO",
    "denmark": "DK", "finland": "FI", "iceland": "IS", "poland": "PL", "czech republic": "CZ", "czechia": "CZ",
    "slovakia": "SK", "hungary": "HU", "romania": "RO", "bulgaria": "BG", "greece": "GR", "croatia": "HR",
    "slovenia": "SI", "estonia": "EE", "latvia": "LV", "lithuania": "LT", "malta": "MT", "cyprus": "CY",
    "ukraine": "UA", "serbia": "RS", "russia": "RU", "russian federation": "RU", "turkey": "TR", "turkiye": "TR",
    "australia": "AU", "new zealand": "NZ", "japan": "JP", "south korea": "KR", "republic of korea": "KR",
    "korea, republic of": "KR", "china": "CN", "hong kong": "HK", "taiwan": "TW", "singapore": "SG", "india": "IN",
    "indonesia": "ID", "malaysia": "MY", "philippines": "PH", "thailand": "TH", "vietnam": "VN", "viet nam": "VN",
    "pakistan": "PK", "bangladesh": "BD", "sri lanka": "LK", "united arab emirates": "AE", "uae": "AE",
    "saudi arabia": "SA", "qatar": "QA", "kuwait": "KW", "israel": "IL", "egypt": "EG", "south africa": "ZA",
    "nigeria": "NG", "kenya": "KE", "morocco": "MA", "ghana": "GH",
}


def country_iso2(value: Any, names: bool = True) -> Optional[str]:
    """ISO 3166-1 alpha-2 from a code or (when names=True) a common English name. None when unknown: never guessed."""
    if not value:
        return None
    s = str(value).strip()
    if len(s) == 2 and s.upper() in ISO2:
        return s.upper()
    if s.upper() == "UK":
        return "GB"
    if names:
        key = re.sub(r"\s+", " ", re.sub(r"[.]", "", s.lower())).strip()
        return COUNTRY_NAMES.get(key)
    return None


# ----------------------------------------------------------------------------------------------
# writing
# ----------------------------------------------------------------------------------------------

def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, datetime):
        return iso(v)
    if isinstance(v, Decimal):
        return cents(v)
    if isinstance(v, (dict, list)):
        return dumps(v)
    return str(v)


def write_table(out_dir: Path, table: str, rows: Sequence[Dict[str, Any]], sort_by: Iterable[str] = ()) -> int:
    """Write <table>.csv with every canonical column in schema order. Returns the number of rows written."""
    cols = TABLES[table].column_names
    keys = list(sort_by)
    data = [{c: _cell(r.get(c)) for c in cols} for r in rows]
    if keys:
        data.sort(key=lambda r: tuple(r[k] for k in keys))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with (out / f"{table}.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        w.writerows(data)
    return len(data)


def dedupe(rows: List[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    """Keep the first row for each key value (callers order rows so that "first" is the one to keep)."""
    seen: set = set()
    out = []
    for r in rows:
        k = r.get(key)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out
