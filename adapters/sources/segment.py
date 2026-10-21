"""Segment warehouse tables (tracks, identifies, users and the per-event tables) into track_events and users.

What it reads
  The tables Segment loads into a warehouse schema named after the source, each dumped to a CSV named after the
  table: tracks.csv, identifies.csv, users.csv and one CSV per event table (order_completed.csv, signed_up.csv).
  Segment documents them at https://www.twilio.com/docs/segment/connections/storage/warehouses/schema: tracks
  holds only standard columns (id, anonymous_id, user_id, event = the slug of the name, event_text = the name,
  timestamp, original_timestamp, sent_at, received_at, context_*); each event's properties are columns of its own
  table; identifies and users carry traits as columns; users holds the latest traits, keyed by id = user_id.
  Segment recommends timestamp over original_timestamp (corrected for device clock skew), and BigQuery adds
  loaded_at. Timestamps may be ISO 8601 or "YYYY-MM-DD HH:MM:SS UTC" (BigQuery CSV); both are read as UTC.

  Recognised by Segment's library names in context_library_name (analytics.js, analytics-node, ...), a uuid or
  loaded_at column, or a users.csv, and not RudderStack's (rudderstack.py covers that).

  In the warehouse, run the SQL in adapters/sources/sql/segment/ (track_events and users, BigQuery and Snowflake)
  instead; tests/test_mappers_billing_crm_events.py runs it on the Ridgeview sample and requires the same rows as this mapper.

What it writes
  track_events  event_id = the message id; event_name = the slug (Application Opened -> session_started);
                event_at = timestamp; properties from the matching per-event table, or {} when none was dumped
                (the SQL leaves properties as {} and shows how to join per-event tables).
  users         from users.csv: email, created_at (the created_at trait, else the first identify, else
                received_at), plan, country (two-letter codes only), timezone (latest identify's context.timezone).
                Without users.csv, users are built from identifies (latest non-empty trait per user).

Rules shared with RudderStack, and every assumption: adapters/sources/_cdp_warehouse.py.

    from adapters.sources import segment
    report = segment.map(Path("exports/segment"), Path("data"))
"""
from __future__ import annotations

from pathlib import Path

from adapters.sources import _cdp_warehouse
from adapters.sources.report import MappingReport

NAME = "segment"
DESCRIPTION = "Segment warehouse tables (tracks, identifies, users, per-event tables) as CSV into track_events and users; SQL for BigQuery and Snowflake"


def detect(folder: Path) -> bool:
    """True when the folder holds Segment warehouse tables (tracks.csv or identifies.csv sent by a Segment library)."""
    return bool(_cdp_warehouse.vendor_dirs(Path(folder), "segment"))


def map(folder: Path, out_dir: Path) -> MappingReport:  # noqa: A001
    """Read Segment warehouse CSVs in `folder`; write track_events.csv and users.csv to `out_dir`."""
    return _cdp_warehouse.run(folder, out_dir, NAME, use_users_table=True)
