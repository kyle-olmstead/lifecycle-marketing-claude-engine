"""RudderStack warehouse tables (tracks, identifies and the per-event tables) into track_events and users.

What it reads
  The tables RudderStack loads into a warehouse schema named after the source (in snake_case), each dumped to a
  CSV named after the table: tracks.csv, identifies.csv and one CSV per event table (add_to_cart.csv). RudderStack
  documents them at https://www.rudderstack.com/docs/destinations/warehouse-destinations/warehouse-schema/:
  tracks has id, anonymous_id, user_id, received_at, sent_at, original_timestamp, timestamp, context_*, event (the
  table name) and event_text (the name as sent), but not the event's properties, which are columns of the
  per-event table (camelCase converted to snake_case, nested keys joined with underscores); identifies carries
  traits as columns. RudderStack also keeps a users table with the latest identify's traits; this mapper builds
  users from identifies instead, which also gives the first-seen time.

  Recognised by RudderStack's library names in context_library_name (RudderLabs JavaScript SDK, rudder-sdk-...)
  or its context_source_id, context_destination_id and context_request_ip columns.

  In the warehouse, run the SQL in adapters/sources/sql/rudderstack/ (track_events and users, BigQuery and
  Snowflake) instead; tests/unit/test_mappers_billing_crm_events.py runs it on the Ridgeview sample and requires the same rows.

What it writes
  track_events  event_id = the message id; event_name = the event slug (application_opened -> session_started);
                event_at = timestamp; properties from the matching per-event table, or {} when none was dumped.
  users         per user id, the latest non-empty email (lower-cased), plan, country (two-letter codes only) and
                context_timezone across identify calls; created_at = the created_at trait, else the first identify.

Not verified: RudderStack's docs list user_id among the standard properties of every table but leave it out of the
tracks column table; it is read when present. The exact rule that turns an event name into its table name is not
documented (the docs show "Add to Cart" -> add_to_cart); the mapper uses the event column as written.

Rules shared with Segment, and every assumption: adapters/sources/_cdp_warehouse.py.

    from adapters.sources import rudderstack
    report = rudderstack.map(Path("exports/rudderstack"), Path("data"))
"""
from __future__ import annotations

from pathlib import Path

from adapters.sources import _cdp_warehouse
from adapters.sources.report import MappingReport

NAME = "rudderstack"
DESCRIPTION = "RudderStack warehouse tables (tracks, identifies, per-event tables) as CSV into track_events and users; SQL for BigQuery and Snowflake"


def detect(folder: Path) -> bool:
    """True when the folder holds RudderStack warehouse tables (tracks.csv or identifies.csv sent by a RudderStack SDK)."""
    return bool(_cdp_warehouse.vendor_dirs(Path(folder), "rudderstack"))


def map(folder: Path, out_dir: Path) -> MappingReport:  # noqa: A001
    """Read RudderStack warehouse CSVs in `folder`; write track_events.csv and users.csv to `out_dir`."""
    return _cdp_warehouse.run(folder, out_dir, NAME, use_users_table=False)
