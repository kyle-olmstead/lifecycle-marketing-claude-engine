---
name: lifecycle-sql
description: >
  Use when the user says "RFM", "segment my customers", "champions and at risk", "write the SQL
  for", "cohort retention", "D30 retention", "LTV curve", "sync segments to Klaviyo" or "to
  HubSpot", or names Snowflake, BigQuery or PostgreSQL with a segmentation or cohort question.
  Writes warehouse SQL in the named dialect against verified columns, with window functions,
  censoring, readability labels and API-ready export payloads.
metadata:
  version: "2.0.0"
---

# Lifecycle SQL

## Step 0: Load context and rules

Read `${CLAUDE_PLUGIN_ROOT}/prompts/core/system_prompt.md`, `lmm-engine/context.xml` (warehouse dialect and
transaction object) and `${CLAUDE_PLUGIN_ROOT}/docs/SQL_SCHEMA_CONVENTIONS.md`.

**Before running a tool:** the checks need Python 3.12 or later and the packages in
`${CLAUDE_PLUGIN_ROOT}/requirements.txt`. If an import fails, install them once with
`python3 -m pip install -r ${CLAUDE_PLUGIN_ROOT}/requirements.txt`. If code cannot run here (no
shell, or the install is blocked), still deliver, and write "unchecked" next to every artifact a
tool would have checked; never describe an unchecked artifact as verified.

## Step 1: Pick the module

- RFM scores, segments and CDP sync: `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/sql/rfm_segmentation.md`
- Cohort LTV curves: `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/sql/ltv_cohort.md`
- Retention: `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/sql/rolling_d30_retention.sql`

## Step 2: Verify before writing

With a warehouse connector (BigQuery, Snowflake, Neon, Blaze SQL), read the schema and run the
module's fill-rate and frequency queries first, labeled LIVE PULL. Otherwise ask for DDL and fill
rates. Never filter on an unverified column.

## Step 3: Check the query

```text
python3 ${CLAUDE_PLUGIN_ROOT}/engine/sql/check.py query.sql --dialect <dialect>
python3 ${CLAUDE_PLUGIN_ROOT}/engine/sql/check.py query.sql --run        # PostgreSQL only, on the bundled fixtures
```

## Step 4: Export

Build request bodies within vendor limits with `python3 ${CLAUDE_PLUGIN_ROOT}/adapters/destinations/payloads.py`.
Run queries that read data only; never run statements that write to the user's warehouse or push to
an ESP without explicit approval of that specific action.
