# Changelog

## 2.2.0 (2026-10)

**An engine that runs itself.** The audit behind this release, finding by finding: `docs/AUDIT.md`.

- `lcm`: one command for the whole engine, in plain English or a YAML job file
  (`pip install -e . --no-deps` installs it). Rules pick the job; anything missing is asked for.
- A stateful pipeline: intake, Analyst, Strategist, Architect, Copywriter, Experiment, QA,
  Compliance. Every stage is saved with an input hash; `resume` restarts at a failed stage,
  `revise --set` re-runs only what a change affects and keeps unchanged and approved copy,
  `approve` marks the variant to ship, `status` shows the run.
- `agents/strategist.py`: the campaign's shape decided and explained (touch jobs, channels, days,
  offer stance), dropping channels the data cannot support.
- `agents/compliance.py` and `config/compliance_rules.json`: CAN-SPAM, TCPA and state texting laws,
  CASL, PECR, ePrivacy, Germany's double opt-in case law, Australia's Spam Act and the mailbox
  providers' bulk-sender rules, for the countries in the audience, each rule with its source.
- `agents/engagement.py`: engagement tiers from message events (clicks, never opens), the unengaged
  share as a sending-gate signal, and a reversible sunset attribute synced to all four ESPs
  (`tools/payloads.py attribute_sync`).
- `tools/dns_collect.py`: live SPF, DKIM, DMARC, MTA-STS, TLS-RPT and BIMI lookups for the analyzer.
- `tools/contracts.py` and `schemas/*_input.schema.json`: Pydantic contracts for every tool input.
- Text-to-image ratio in every email lint.
- `blast-vs-trigger`: what a calendar blast reaches compared with the behavioral trigger.
- `scripts/daily.py`: the 05:00 UTC job now audits every brief in `briefs/active/`, rebuilds the
  tiers, checks each sending domain, logs six Sheets tabs and opens one issue per finding.
- Opt-in real-time forwarding of activation triggers from the webhook parser, with the holdout
  assignment added.
- `--set FIELD=VALUE` on `plan` and `brief`.
- `tests/fixtures/message_events.csv`; GitHub Actions moved to the Node 24 majors.

**Fixes** (each with a regression test; details in `docs/AUDIT.md`)

- Deliverability: Microsoft Authentication-Results without an authserv-id; the receiver's DMARC
  verdict preferred; subdomains judged by `sp=`; RFC 8058 needs a verified covering signature; SPF
  include loops, unknown modifiers and redirect with `all`; `v=DMARC1` case; MTA-STS `mode: none`.
- Payloads: no coercion of leading zeros or long ids; HubSpot values as strings; compact files sized
  as written; CSV byte-order marks and quoted newlines.
- Templates: Braze `${` before Liquid; HubL `unless`; AMPscript variables named like keywords.
- SMS: segments packed the way carriers pack them; the opt-out check wants an instruction.
- SQL: DuckDB divides integers like PostgreSQL; the LTV query's always-1 share replaced by
  `repeat_share`.
- Stats and warm-up: interval labels follow alpha; no 25% suggestion at 25%; `days=0` and zero
  engaged capacity.

**Changed behavior**

- A plan whose emails have no postal address is BLOCKED (CAN-SPAM), not passed. Customer.io has no
  address token: add `postal_address` to the brief, or `--set postal_address=...`.
- The plan's SMS window is 9 a.m. to 8 p.m. recipient local (the strictest state rule).

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
