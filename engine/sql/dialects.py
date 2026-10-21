"""The handful of expressions that differ between warehouses, written once per dialect.

The teammate's queries are templates over the canonical tables (users, orders, track_events; see
docs/SQL_SCHEMA_CONVENTIONS.md). Everything that is not portable goes through a Dialect object:
date arithmetic, JSON property access, the current date and the holdout bucket.

The holdout bucket is the important one. The same person must land in the same arm in the
warehouse, in the ESP and in the readout, whichever engine computes it. Every dialect here takes
the first 7 hex digits of md5(user_id || ':' || salt) as an integer and keeps it modulo 100, which
is exactly what holdout_bucket() computes in Python. tests/test_agents.py checks that DuckDB and
Python agree on every fixture user.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Dict

DIALECTS = ("postgres", "snowflake", "bigquery", "duckdb")


def holdout_bucket(user_id: str, salt: str) -> int:
    """Reference assignment: 0 to 99. A person is in the holdout when bucket < holdout percent."""
    return int(hashlib.md5(f"{user_id}:{salt}".encode("utf-8")).hexdigest()[:7], 16) % 100


def _q(value: object) -> str:
    """A SQL string literal. Values come from validated briefs, but quote defensively anyway."""
    return "'" + str(value).replace("'", "''") + "'"


@dataclass(frozen=True)
class Dialect:
    name: str

    @property
    def current_date(self) -> str:
        return {"postgres": "CURRENT_DATE", "duckdb": "CURRENT_DATE", "snowflake": "CURRENT_DATE()", "bigquery": "CURRENT_DATE()"}[self.name]

    @staticmethod
    def date_literal(iso_date: str) -> str:
        return f"DATE {_q(iso_date)}"

    @staticmethod
    def to_date(expr: str) -> str:
        return f"CAST({expr} AS DATE)"

    def add_days(self, date_expr: str, n: int) -> str:
        if self.name in ("postgres", "duckdb"):
            return f"({date_expr} + {int(n)})" if n >= 0 else f"({date_expr} - {abs(int(n))})"
        if self.name == "snowflake":
            return f"DATEADD(day, {int(n)}, {date_expr})"
        return f"DATE_ADD({date_expr}, INTERVAL {int(n)} DAY)"

    def days_between(self, later: str, earlier: str) -> str:
        """Whole days from earlier to later, both DATE expressions."""
        if self.name in ("postgres", "duckdb"):
            return f"({later} - {earlier})"
        if self.name == "snowflake":
            return f"DATEDIFF(day, {earlier}, {later})"
        return f"DATE_DIFF({later}, {earlier}, DAY)"

    @property
    def now(self) -> str:
        return {"snowflake": "CURRENT_TIMESTAMP()", "bigquery": "CURRENT_TIMESTAMP()"}.get(self.name, "CURRENT_TIMESTAMP")

    def ts_literal(self, iso: str) -> str:
        return f"TIMESTAMP {_q(iso)}"

    def add_hours(self, ts_expr: str, n: float) -> str:
        """A timestamp plus n hours (n may be negative or fractional; fractions go to whole minutes)."""
        minutes = int(round(n * 60))
        if self.name in ("postgres", "duckdb"):
            return f"({ts_expr} + INTERVAL '{minutes} minutes')"
        if self.name == "snowflake":
            return f"DATEADD(minute, {minutes}, {ts_expr})"
        return f"TIMESTAMP_ADD({ts_expr}, INTERVAL {minutes} MINUTE)"

    def hours_between(self, later: str, earlier: str) -> str:
        """Hours from earlier to later, both timestamps, as a number."""
        if self.name == "postgres":
            return f"(EXTRACT(EPOCH FROM ({later} - {earlier})) / 3600.0)"
        if self.name == "duckdb":
            return f"(date_diff('minute', {earlier}, {later}) / 60.0)"
        if self.name == "snowflake":
            return f"(DATEDIFF(minute, {earlier}, {later}) / 60.0)"
        return f"(TIMESTAMP_DIFF({later}, {earlier}, MINUTE) / 60.0)"

    def to_ts(self, expr: str) -> str:
        return f"CAST({expr} AS TIMESTAMP)"

    def json_text(self, column: str, key: str) -> str:
        if self.name == "postgres":
            return f"({column} ->> {_q(key)})"
        if self.name == "snowflake":
            return f"{column}:{key}::string"
        if self.name == "bigquery":
            return f"JSON_VALUE({column}, {_q('$.' + key)})"
        return f"json_extract_string({column}, {_q('$.' + key)})"

    def holdout_bucket(self, id_column: str, salt: str) -> str:
        if self.name == "postgres":
            return f"((('x' || substr(md5({id_column} || {_q(':' + salt)}), 1, 7))::bit(28)::int) % 100)"
        if self.name == "snowflake":
            return f"MOD(TO_NUMBER(SUBSTR(MD5({id_column} || {_q(':' + salt)}), 1, 7), 'XXXXXXX'), 100)"
        if self.name == "bigquery":
            return f"MOD(CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5(CONCAT({id_column}, {_q(':' + salt)}))), 1, 7)) AS INT64), 100)"
        return f"(CAST('0x' || substr(md5({id_column} || {_q(':' + salt)}), 1, 7) AS INTEGER) % 100)"

    def event_filter(self, column_prefix: str, event: str, where: Dict[str, object]) -> str:
        parts = [f"{column_prefix}event_name = {_q(event)}"]
        for k, v in where.items():
            text = str(v).lower() if isinstance(v, bool) else str(v)
            parts.append(f"{self.json_text(column_prefix + 'properties', k)} = {_q(text)}")
        return " AND ".join(parts)


def dialect(name: str) -> Dialect:
    if name not in DIALECTS:
        raise ValueError(f"dialect must be one of {DIALECTS}")
    return Dialect(name)
