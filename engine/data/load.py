"""Load a data folder (or URL prefix) into DuckDB as the canonical tables, matching columns by name.

A file may carry extra columns (ignored) or leave out optional ones (left empty); a missing required column
or required table raises an error that names it. Each table is read from <name>.parquet, else <name>.csv.

Timestamps are stored in UTC. A value with an offset (2026-01-01T10:00:00-05:00) is converted to UTC; a value without
one is read as UTC already, which is what lcm import writes. (A plain cast would keep the wall-clock time and drop
the offset, moving every event by the offset's hours.)
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from engine.data.schema import TABLES

TRANSIENT_LOAD = re.compile(r"HTTP 5\d\d|\(HTTP 429\)|\b(429|500|502|503|504)\b|timed? ?out|connection (refused|reset|aborted)|"
                            r"could not resolve|temporar", re.I)


def _remote(data: str) -> bool:
    return data.startswith(("http://", "https://", "s3://", "gs://"))


def _reader(source: str) -> str:
    return (f"read_parquet('{source}')" if source.endswith(".parquet") else
            f"read_csv('{source}', header=true, all_varchar=true, nullstr='')")


def connect(data: Optional[str], integer_division: bool = False, require: Tuple[str, ...] = ("users", "track_events")):
    """DuckDB over the canonical tables in `data`. Returns (connection, notes); notes list extra or missing optional columns."""
    import duckdb
    if data is None:
        return None, []
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    if integer_division:
        con.execute("SET integer_division = true")
    remote = _remote(data)
    if remote:
        con.execute("INSTALL httpfs; LOAD httpfs;")
    base = data.rstrip("/")
    notes: List[str] = []
    for name, table in TABLES.items():
        con.execute(f"CREATE TABLE {name} ({table.ddl})")
        errors: List[str] = []
        loaded = False
        for source in (f"{base}/{name}.parquet", f"{base}/{name}.csv"):
            if not remote and not Path(source).exists():
                continue
            try:
                cols = [d[0] for d in con.execute(f"SELECT * FROM {_reader(source)} LIMIT 0").description]
            except Exception as e:  # noqa: BLE001
                errors.append(f"{source}: {str(e).splitlines()[0]}")
                continue
            by_lower: Dict[str, str] = {c.lower(): c for c in cols}
            missing = [c.name for c in table.columns if c.required and c.name not in by_lower]
            if missing:
                raise ValueError(f"{source}: required column(s) {', '.join(missing)} missing from {name} "
                                 f"(found: {', '.join(cols)}). Run lcm check-data for the full list")
            use = [c.name for c in table.columns if c.name in by_lower]
            extra = [c for c in cols if c.lower() not in set(table.column_names)]
            if extra:
                notes.append(f"{name}: ignored column(s) {', '.join(extra)}")
            types = {c.name: c.type.upper() for c in table.columns}

            def col(c: str) -> str:
                q = f'"{by_lower[c]}"'
                if types[c].startswith("TIMESTAMP"):
                    return f"COALESCE(CAST(TRY_CAST({q} AS TIMESTAMPTZ) AS TIMESTAMP), CAST({q} AS TIMESTAMP))"
                return q
            select = ", ".join(col(c) for c in use)
            try:
                con.execute(f"INSERT INTO {name} ({', '.join(use)}) SELECT {select} FROM {_reader(source)}")
            except Exception as e:  # noqa: BLE001
                errors.append(f"{source}: {str(e).splitlines()[0]}")
                continue
            loaded = True
            break
        if not loaded and name in require:
            msg = f"{data}: could not load {name} from .parquet or .csv ({'; '.join(errors) or 'no file'})"
            if remote and any(TRANSIENT_LOAD.search(e) for e in errors):
                raise ConnectionError(msg)   # a server error can pass on retry; a missing file cannot
            raise FileNotFoundError(msg)
        if not loaded and errors:
            raise ValueError(f"{name}: " + "; ".join(errors))
    return con, notes
