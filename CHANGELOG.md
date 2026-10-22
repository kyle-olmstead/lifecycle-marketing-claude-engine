# Changelog

## 3.0.0 (2026-10)

**From one sentence to a program you can run.** In plain English: a folder per brand (`lcm init`) holds your platform,
sending gate, one frequency policy, offer policy and brand kit, so a plan never guesses them; the engine plans ten
kinds of flow instead of two and refuses the rest by name; it reads your vendors' own exports; every live flow is
checked together, not one at a time; emails are laid out from your brand kit; measurement handles accounts, peeking
and a program-wide holdout; deliverability gets the daily operations (bounces, complaints, trends against abort rules);
and `lcm` became a real command line with help on every flag. The audit behind this release is the LMM Engine Product
Audit (v2.3); every quick win and systemic enhancement in its roadmap is in here.

**Closed the false greens**
- A bare `GREEN` typed into a brief runs as AMBER: an unobserved claim can lower the gate, never raise it. A gate
  record older than 30 days runs as AMBER too. `lcm deliverability check DOMAIN --write-gate gate.json` writes the
  dated, evidence-labeled record (`schemas/sending_gate.schema.json`) that `lmm.yaml` points to.
- QA passes the sending-gate check only on an effective GREEN, and reports the audience left after the gate.
- Engagement tiers suppress hard bounces only (RFC 3463 class 5.1.x, 5.2.1, mailbox-unknown text, or a bounce with no
  detail); blocks, authentication failures and deferrals are the sender's problem, not the address's.

**Fixed the silent plan-breakers**
- Welcome and other new-entrant flows are exempt from the AMBER engaged-only filter (a new subscriber has no
  engagement history yet); each flow has a gate class (new entrant, behavior, lapsed, transactional, remediation).
- Account-level holdouts hash `account_id` (falling back to `user_id`), so people in one account are never split.
- Caps written as days or months ("2 a day", "6 a month") are parsed, not dropped, and each phrase keeps its own
  channel ("2 texts a day and 6 emails a month" is an SMS cap and an email cap); timestamps are validated; the flow
  spec's branch graph is checked for dangling targets (the shipped example had one).
- Sign-ups are read from `users.created_at` as well as `signed_up` events, so a store whose exports carry no sign-up
  event still has a welcome audience; engagement flows check their events in `message_events`.
- The SMS window is 09:00 to 20:00 recipient local time, the narrowest US state window `config/compliance_rules.json`
  tracks, and the prose says "permitted sending window" everywhere; a send hour outside a channel's window is refused
  when the brief is read.
- The data loader converts timestamps with a UTC offset to UTC instead of keeping the clock time.

**The workspace** (`lmm.yaml`, `schemas/workspace.schema.json`, `engine/workspace.py`)
- `lcm init` writes `lmm.yaml`, `brand.yaml`, `data/`, `briefs/active/` and `runs/`. Commands find the workspace from
  the current folder or `LCM_WORKSPACE`; outside one they use the bundled demo and say so.
- Briefs inherit platform, brand, gate, frequency policy, offer policy and measurement settings from `lmm.yaml`; the
  sentence and explicit overrides still win, and inherited values are not listed as assumptions. `lcm brief` writes
  thin briefs that keep inheriting.
- `lcm doctor` checks Python, packages, the MJML compiler, `lmm.yaml`, the gate's age, the brand kit's contrast, the
  data, the live briefs and which keys are set (names only).

**Typed policies and the program simulator** (`engine/policy/contracts.py`, `engine/policy/program.py`)
- Sending gate, trigger, frequency policy (caps across channels and windows, permitted send windows per channel, flow
  priority, a collision rule), offer policy (offers with real deadlines or none, never in a first touch, exclusions for
  deal hunters) and consent records are typed contracts with JSON Schemas, and tests hold each schema and its model to
  the same answers.
- `lcm simulate --program` walks people who enter several live flows close together, applies the collision rule, and
  checks every cap across flows; when a cap breaks it names the rule that would hold it or the platform's own limit.

**The flow library** (`engine/flows/`, `docs/FLOW_LIBRARY.md`)
- Ten flows: welcome, activation, win-back, abandoned cart, browse abandonment, post-purchase, replenishment, failed
  payment (dunning), trial conversion and sunset, each with its trigger, goal, timing, re-entry, dedupe key, AMBER
  audience, holdout default and native build steps for every platform. Cart and browse flows wait in hours; other
  flows record an hours request as an assumption instead of dropping it. Unsupported flows are refused by name.
- Product blocks show the cart or product from each platform's own event fields (Klaviyo's Shopify events,
  Customer.io and Braze event items, HubSpot and Marketing Cloud contact or entry data), and the button goes back to
  that checkout or product. The offline copy library covers every flow's roles with no bracketed fill-ins, and
  `tests/test_flow_library.py` plans all ten flows on all six platforms with no copy warning.
- Templates read only the event fields the tracking plan gives the trigger event (`engine/render/fields.py`); the
  check found and closed three gaps (item names, item links and images, and the browse event's single product).

**Build packs and sending** (`adapters/destinations/build/`, `adapters/destinations/send.py`, `docs/ESP_BUILD_PACKS.md`)
- `lcm build --run <plan folder>` writes a build pack per platform: the native trigger (exact type, filters, exit
  criteria, re-entry), every step as the platform's own action with its wait in the platform's smallest unit (Braze
  seconds; Klaviyo, HubSpot, Iterable and Marketing Cloud minutes; Customer.io hours), the holdout split first and each
  SMS or push consent split with its email fallback, the goal, permitted send window and frequency settings, the copy
  each message uses, and the steps a person does by hand.
- Win-back, replenishment and sunset trigger natively where the platform can see the absence (Klaviyo and HubSpot
  segments, Customer.io segment triggers, daily scheduled audiences on Braze, Iterable and Marketing Cloud), with the
  exact segment to create; `--warehouse-job` keeps the daily job when the core event does not reach the ESP. A
  segment trigger's holdout reads a person property written by `build/holdout_sync_<dialect>.sql`.
- Capability checks from `config/esp_capabilities.json` (every fact with its source and confidence) against the
  account in `lmm.yaml` (`esp.tier`, `esp.options`): HubSpot custom event enrollment on Professional or Enterprise,
  its frequency safeguard on Enterprise, Braze Canvas Context, Customer.io latest Liquid and opt-in message limits,
  Klaviyo's 100 flow creates a day, delay units, events that must reach the platform. An unknown tier warns; a failed
  hard requirement blocks the pack with the reason.
- Importable artifacts where a create API exists, validated against `config/esp_artifact_schemas.json` with every
  unverified field named: Klaviyo templates, segment and flow (Draft; shapes from Klaviyo's official OpenAPI spec),
  the HubSpot automation v4 workflow (beta, off), the Marketing Cloud journey (Draft). Braze, Customer.io and Iterable
  get a click-by-click build sheet, and Braze an optional `/canvas/duplicate` request (UNVERIFIED).
- `lcm build --create --approve` creates them in order through the safe client: metric ids looked up, template and
  segment ids carried forward, unresolved inputs and UNVERIFIED shapes refused (`--allow-unverified`), 429 retried and
  5xx not, and a receipt log so a rerun never creates the same object twice.
- `lcm send` pushes the warehouse job's pending rows, request files or profile updates: a dry run unless `--approve`
  and the platform's key are given; batched to each platform's limit, idempotency keys on every item (a client-side
  sent log for Braze), retries with backoff and jitter on 429 and 5xx honoring `Retry-After`, never on another 4xx, and
  a JSONL receipt log so a rerun sends nothing twice.
- Iterable is a sixth platform: Handlebars copy (`defaultIfEmpty`, `{{#each items}}`, `{{unsubscribeUrl}}`,
  `{{physicalAddress}}`) with its own linter, product blocks and return links, the field check, its track request with
  an event id, and every flow planning on it.

**Sources and check-data** (`adapters/sources/`, `engine/data/check.py`, `docs/IMPORTING_DATA.md`)
- `lcm import` maps Shopify, Klaviyo, Stripe, HubSpot, Customer.io, GA4 (BigQuery export), Segment and RudderStack
  exports into the canonical tables, merges several sources by email, and keeps the earlier source on a conflict.
  Warehouse SQL models do the same mapping in BigQuery and Snowflake.
- `lcm check-data` reports, column by column, what would break or quietly mislead a plan: missing or late tables,
  empty required columns, values that do not parse, future dates, duplicate keys, orphaned references, bad enums and
  phone numbers, with up to three examples and a fix for each.

**Measurement** (`engine/measurement/`, `docs/MEASUREMENT.md`)
- Cluster-aware sizing and readouts for account-level tests (ICC by ANOVA, design effect); group-sequential
  (Lan-DeMets O'Brien-Fleming) and always-valid (mSPRT) stopping; an experiment registry that catches overlaps,
  overdue reads and unplanned peeking; a global holdout every flow excludes; discount dependency by customer and flow.
  Readouts refuse arms under 30 people or 5 events.

**Deliverability operations** (`engine/deliverability/`, `docs/DELIVERABILITY_OPERATIONS.md`)
- Bounce classification, complaint rates by mailbox provider and flow, provider share, and a daily monitor that
  evaluates the warm-up abort rules (now machine-readable) against message events.

**Brand kit and MJML** (`engine/render/brand.py`, `engine/render/mjml_render.py`, `docs/BRAND_KIT.md`)
- Emails are built in MJML from the brand kit: logo, colors (every text pair checked to WCAG AA in light and dark
  mode), fonts, footer and voice rules (forbidden phrases and vocabulary are flagged in the copy).

**One command line** (`engine/cli/`, `docs/COMMANDS.md`)
- `lcm COMMAND` for every tool, grouped in `lcm --help`, with help text on every flag. `lcm size` and `lcm readout`
  answer in sentences (`--json` for the full result). "make it 4 touches", "add SMS", "use a 20% holdout" revise the
  newest run. `--value-per-conversion` is accepted wherever `--value` is. Every run keeps its own copy of its outputs.
- `lcm workflow run morning_check | new_flow | quarterly_review`: named graphs of commands in `workflows/`.

**Docs that cannot drift** (`engine/docs_gen.py`, `tests/test_drift.py`)
- The flow library, platform, send-window and command tables are generated from the code; CI fails when they are
  stale, when prose names a command or file that does not exist, says "quiet hours", states a single default holdout,
  or gets the SMS window wrong.

**Finished examples**: Ridgeview (SaaS win-back on Customer.io) and Northfield Coffee Roasters (Shopify and Klaviyo,
from raw exports to an abandoned cart plan with two live flows) are complete workspaces, planned from written copy
with no fill-ins, and held to what the engine writes by `tests/test_golden_runs.py`.

### Upgrading from 2.x

- The layout changed: `agents/`, `tools/` and `scripts/` are now `engine/` and `adapters/`; `modules/` is `prompts/`;
  the sample company is `examples/brands/ridgeview_saas`. Run modules as `lcm COMMAND` or `python3 -m engine...`.
- Make a workspace (`lcm init .` in your existing folder) and move platform, gate, caps and offers out of each brief
  into `lmm.yaml`. `quiet_hours` and `frequency_cap_per_week` still load but are deprecated: use `frequency` (caps and
  `send_windows`) in `lmm.yaml`.
- A typed `GREEN` now runs as AMBER: record an observed gate with `lcm deliverability check DOMAIN --write-gate gate.json`.
- If you created the warehouse ledger in 2.x, add the new column once:
  `ALTER TABLE lcm_flow_entries ADD COLUMN detail VARCHAR;` (Snowflake: `VARCHAR`, BigQuery: `STRING`).
- `message_events` may now carry `smtp_status` and `diagnostic`; with them, only hard bounces suppress an address.

## 2.3.0 (2026-10)

**It tells you what to build next.** Ask "what are my top 3 priority builds today and why?", or read the top of the
morning check, which answers it every day.

- `agents/priorities.py`: opportunities found in your data (an onboarding step where new users stall, customers
  lapsing with no flow to catch them, first-time buyers who never order again), each sized as people a month x the
  measured baseline x an assumed lift range x the value of a conversion, and ranked by that value, the data behind it
  and the effort. Fixes that cap every build (a RED domain, a RED list, data that stopped arriving, a live flow's
  trigger that stopped) come first; "top 3" is three builds after them. Win-back and activation builds ship with a
  ready brief and the command to plan it. When `message_events` shows a recurring newsletter, a win-back carries how
  late its real sends reached the people the trigger would catch.
- `agents/tracking.py`: every tracked event's last 7 days (to yesterday) against its usual week, a stale feed,
  required properties going missing, events arriving off the plan, each live flow whose exact trigger or core action
  has stopped, and the webhook receiver's quarantine reasons. The daily check runs it.
- `agents/retention.py`: the weekly retention curve on the core action, with the cliff (a paired week-to-week test on
  the same people, corrected for the number of weeks tested) and the floor, by plan, country or lifecycle stage.
- `blast-vs-trigger` reads your recurring campaign's real sends and recipients from `message_events` by default, and
  labels a comparison without them as simulated.
- The question bank (`config/question_bank.yaml`, `docs/QUESTION_BANK.md`): 25 questions a lifecycle manager asks
  daily, weekly, monthly, quarterly and yearly, each with what you get, the data it needs, and what it cannot
  answer. CI checks that every question reaches its job.
- `lcm` understands those questions: new jobs `priorities`, `daily`, `tracking`, `retention` and `roi`. Asking for
  or naming a flow still plans it, and a question `lcm` has no job for points to Claude instead of starting a plan.
  The daily job opens with the priorities and writes a Priorities tab; the MCP server gains a `priorities` tool.

## 2.2.0 (2026-10)

**An engine that runs itself.** In plain English: plans now survive failures and edits, every plan is
checked against the email and texting laws where your customers live, the engine checks your sending
domain live and stops mailing people who never engage, one plain-English command (`lcm`) runs
everything, and a daily 05:00 UTC check logs to Google Sheets and opens a ticket per problem. The
audit behind this release, finding by finding: `docs/AUDIT.md`.

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
- `docs/ROI_MEMO.md` and `scripts/roi_model.py`: the business case in plain English, every figure from
  one calculator you can rerun with your own numbers (tested against the memo).
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

- Found by the independent review of this release and fixed: compliance with an empty country column,
  RFC 8058 verdict matching, SMS opt-out false positives, stale state on resume, copy reuse across an
  audience change, duplicate DMARC records, Customer.io batch size, remote-load retries, YAML jobs
  missing settings, router priority for flow requests, and smaller items (`docs/AUDIT.md`, section 5).

**Changed behavior**

- A plan whose emails have no postal address is BLOCKED (CAN-SPAM), not passed. Customer.io has no
  address token: add `postal_address` to the brief, or `--set postal_address=...`.
- The plan's SMS send hour must fall inside 9 a.m. to 8 p.m. recipient local (the strictest state window the rules track), and a
  Sunday-texting rule asks you to decide on Sundays with counsel.

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
