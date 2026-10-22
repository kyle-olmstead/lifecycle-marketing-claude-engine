---
name: lifecycle-sql
description: >
  Use when the user says "RFM", "segment my customers", "champions and at risk", "write the SQL
  for", "cohort retention", "D30 retention", "LTV curve", "sync segments to Klaviyo" or "to
  HubSpot", or names Snowflake, BigQuery or PostgreSQL with a segmentation or cohort question.
  Writes warehouse SQL in the named dialect against verified columns, with window functions,
  censoring, readability labels and API-ready export payloads.
metadata:
  version: "3.0.0"
---

# Lifecycle SQL

Always read (once per conversation; skip any you already read in this conversation):
- `${CLAUDE_PLUGIN_ROOT}/prompts/core/principles.md`
- `${CLAUDE_PLUGIN_ROOT}/prompts/core/syntax_rules.md` (its SQL section binds every query)

Workspace: `lmm.yaml` `warehouse_dialect` (postgres, snowflake or bigquery) and the transaction object; older setups
keep both in `lmm-engine/context.xml`. Never guess the dialect.

`lcm` below is the installed command, or `python3 ${CLAUDE_PLUGIN_ROOT}/engine/cli/main.py` (Python 3.12 or later and
`${CLAUDE_PLUGIN_ROOT}/requirements.txt`; `lcm doctor` checks the install). If code cannot run, deliver the query
labeled unexecuted.

## 1. Pick the job

Route (read the one job prompt that matches; the RFM and LTV prompts name the one reference file to read for the
workspace's dialect):
- RFM scores, segments and CDP sync: `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/sql/rfm_segmentation.md`
- Cohort LTV curves: `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/sql/ltv_cohort.md`
- Retention by signup cohort: `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/sql/rolling_d30_retention.sql`

The engine writes some SQL itself: a flow plan's entry and history queries per dialect (`lcm plan`), engagement tiers
(`lcm engagement --sql-only --dialect <dialect> --out <dir>`), and retention curves on exports (`lcm retention`).
When the user's tables differ from the canonical ones the reference SQL reads (users, orders, track_events,
message_events), map them with `${CLAUDE_PLUGIN_ROOT}/docs/SQL_SCHEMA_CONVENTIONS.md` or `lcm import`.

## 2. Verify before writing

With a warehouse connector (BigQuery, Snowflake, Neon, Blaze SQL), read the schema and run the job's fill-rate and
frequency queries first, labeled LIVE PULL. Otherwise ask for DDL and fill rates. Never filter on an unverified
column.

## 3. Check and export

```text
lcm sql-check query.sql --dialect <dialect>
lcm sql-check query.sql --run              # PostgreSQL only, on the bundled fixtures
lcm sync klaviyo|hubspot rows.csv --id-column email --properties <col=property,...> --out <dir>
```

`lcm sync` builds request bodies within the vendor's limits and sends nothing. Run queries that read data only; never
run statements that write to the user's warehouse, or push to an ESP, without explicit approval of that action.
