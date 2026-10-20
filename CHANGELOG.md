# Changelog

## 2.1.0 (2026-10)

**The teammate.** One campaign brief in, a build-ready plan out.

- `agents/`: an orchestrator and four agents (cohort analyst, campaign architect, copywriter,
  experimentation), with typed contracts (Pydantic) between them and a QA pass that rolls every check
  into READY, READY WITH ASSUMPTIONS or BLOCKED.
- Briefs as YAML (`briefs/brief_template.yaml`) or one sentence; every unstated value is listed for the
  marketer to confirm.
- Cohort SQL for PostgreSQL, Snowflake, BigQuery and DuckDB, including a holdout bucket that is
  identical in every dialect and in Python.
- `scripts/campaign_simulator.py`: structure checks, synthetic customer journeys and payload
  validation for any flow spec, including the hand-written examples.
- `tools/event_payloads.py` and `config/esp_payload_schemas.json`: trigger-event requests for Klaviyo,
  HubSpot (2026-09), Braze and Customer.io Pipelines.
- `scripts/webhook_parser.py`: Segment and GA4 ingest into canonical events, signed, validated against
  the tracking plan, deduplicated, with a quarantine.
- `.github/workflows/daily_cohort_audit.yml` and `scripts/sheets_sync.py`: a 05:00 UTC cohort health
  audit with robust anomaly detection, logged to Google Sheets and raised as an issue.
- `scripts/mcp_server.py` and `config/mcp_server_config.json`: the teammate as MCP tools, and the
  vendor MCP servers for live data.
- Skill `lifecycle-teammate`; docs `quickstart_for_marketers.md` and `architectural_overview.md`.
- Win-back entry is keyed on the lapse and runs through an outbox table (`lcm_flow_entries`): queue,
  push, mark. Each lapse enters once, a missed daily run catches up for three days, and a failed push
  is retried on the next run.
- CI: a PostgreSQL 16 job runs the PostgreSQL queries on a real database.

**Fixes**

- The tracking plan (`custom_event_definitions.json`) used `multipleOf: 0.01` on money fields, which
  rejects ordinary amounts such as 19.99 under binary floating point. The constraint is removed and a
  regression test added.
- `tools/sql_check.py` runs PostgreSQL's hex-to-integer idiom on the DuckDB fixtures.

## 2.0.0 (2026-10)

First public release: deliverability gate, behavioral flows, multi-dialect copy, warehouse SQL,
experiment sizing and readouts, deterministic tools, plugin packaging and CI.
