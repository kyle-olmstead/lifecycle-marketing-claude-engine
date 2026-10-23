# Under the hood

**In plain English:** the README says what the LMM Engine does for a marketer. This page is for the engineer or the
technical marketer: how the pieces fit, how the daily check runs as a scheduled job, where everything lives in the
repository, and exactly what the test suite proves and what it does not.

## How it works, in one picture

```
  YOUR TOOLS                       THE ENGINE                                          YOU
  ──────────                       ──────────                                          ───
  Email platform     ┐
  (Klaviyo, HubSpot, │      1. Finds the audience in your data, checks the data
   Braze, Customer.io│      2. Decides the shape of the campaign, and says why
   Iterable, SFMC)   │      3. Builds the flow and simulates it with test customers
  Data warehouse     ├──►   4. Writes every message in your platform's code     ──►   approve · edit · reject
  Event stream       │      5. Sizes the holdout and sets the read date                     │
  (Segment, GA4)     │      6. Checks the law and the inbox rules                           ▼
  Your domain's DNS  ┘                                                         7. Only then: you build it,
                                                                                  or it hands you the files
                            Every morning: audience health · engagement · inbox checks ──► Google Sheet + tickets
```

Each step saves its work. If a step fails (a data export is missing, an API times out) the plan picks up where it stopped. Change one thing ("make it 4 touches") and only what that change affects is redone; approved copy stays approved.

## The daily check, as a scheduled job

The same engine runs as a scheduled job (GitHub Actions, 05:00 UTC every day) against your warehouse or exported files: every flow listed in `briefs/active/`, the engagement tiers, every sending domain. It writes seven tabs to a Google Sheet (priorities, campaign plans, cohort health, benchmarks, anomalies, engagement, deliverability), opens one GitHub issue per problem, and comments on it while it stays open. With nothing configured it runs on the sample data, so you can see the output first. Setup needs an engineer for about a day: [`docs/quickstart_for_marketers.md`](docs/quickstart_for_marketers.md), section 7, and the top of [`.github/workflows/daily_cohort_audit.yml`](.github/workflows/daily_cohort_audit.yml).

## The repository

```text
lifecycle-marketing-claude-engine/
├── engine/          the engine: the resumable planning pipeline (engine/pipeline), the agents (analyst, strategist,
│                    architect, copywriter, experiment, QA, compliance, priorities, engagement, tracking, retention),
│                    the flow library (engine/flows), typed policies and the simulators (engine/policy), deliverability,
│                    measurement, rendering (MJML, brand kit, linting, SMS), the data loader and check, the workspace,
│                    workflows, and lcm itself (engine/cli)
├── adapters/        sources: vendor exports to the canonical tables (Shopify, Klaviyo, Stripe, HubSpot, Customer.io,
│                    GA4, Segment, RudderStack) and the webhook receiver; destinations: ESP request bodies, Sheets
├── prompts/         the method as prompts: core principles, template syntax, diagnostics, one prompt per job, examples
├── skills/          the Claude plugin skills; each calls lcm for the deterministic work
├── schemas/         machine-readable contracts: brief, flow spec, trigger, sending gate, frequency and offer policy, consent,
│                    event envelope, workspace, brand kit, experiment, and every tool input
├── templates/       the workspace (lmm.yaml), brief and brand kit templates, MJML email components
├── workflows/       named job graphs: morning_check, new_flow, quarterly_review
├── config/          compliance rules (each with its source), the tracking plan, ESP request schemas, the question bank
├── examples/        two complete workspaces (examples/brands/ridgeview_saas, examples/brands/northfield_dtc) and sample inputs
├── docs/            commands, the flow library, importing data, measurement, deliverability operations, the brand kit,
│                    the question bank, the business case (ROI memo), architecture, SQL conventions
└── tests/           about 1,500 checks CI runs on every change, by kind: unit/, contracts/ (each schema against its model),
                     drift/ (prose against code), golden/ (the finished runs), sql/ (DuckDB and a real PostgreSQL 16),
                     planted_defects/ (one broken artifact per checker, each must fail)
```

| Tool | What it computes |
|---|---|
| `engine/flows/library.py` | the ten flows the engine plans: trigger, goal, touches and timing, AMBER-gate audience, re-entry, holdout default ([docs/FLOW_LIBRARY.md](docs/FLOW_LIBRARY.md)) |
| `engine/policy/contracts.py`, `engine/policy/program.py` | the typed sending gate, trigger, frequency, offer and consent policies; every live flow simulated together against one frequency policy |
| `engine/data/check.py`, `adapters/sources/` | check-data (missing, late, empty, malformed, future-dated, duplicate, orphaned) and the vendor-export mappers behind `lcm import` |
| `engine/deliverability/analyze.py` | SPF (lookup counts, loops), DKIM key size, DMARC (including subdomain policy), MTA-STS, TLS-RPT, BIMI, header alignment, one-click unsubscribe, bulk-sender rules, the sending gate, copy-paste DNS fixes |
| `engine/deliverability/dns_collect.py`, `monitor.py`, `bounces.py`, `complaints.py`, `warmup.py` | the live DNS lookup and the dated gate record; the daily trend check against the abort rules; bounce and complaint classification; a warming ladder that refuses on RED, holds on AMBER, and never warms with unengaged people |
| `engine/measurement/` | sample size, minimum detectable effect, holdout feasibility, credited versus true lift; cluster-aware sizing (ICC, design effect); group-sequential and always-valid stopping; the experiment registry; the global holdout; discount dependency; ROI |
| `engine/render/` | MJML email built from the brand kit (contrast checked to WCAG AA), product blocks per platform, syntax and fallback rules for Braze, Customer.io, Klaviyo, HubL, AMPscript and Iterable's Handlebars, SMS encoding and segments |
| `engine/sql/check.py`, `adapters/destinations/payloads.py`, `adapters/destinations/event_payloads.py` | SQL parsing per dialect; ESP request bodies within each vendor's limits |
| `adapters/destinations/build/`, `adapters/destinations/send.py` | the build pack per platform behind `lcm build` (native trigger, steps with waits in the platform's unit, holdout and consent splits, capability checks from `config/esp_capabilities.json`, create-API artifacts validated against `config/esp_artifact_schemas.json`), and `lcm send` (dry run unless approved; batched, idempotent, retried, receipted): [docs/ESP_BUILD_PACKS.md](docs/ESP_BUILD_PACKS.md) |

**What is verified, and what is not**

| Verified in CI | Not verified |
|---|---|
| every SQL example parses in its dialect; PostgreSQL examples run on fixtures and on a real PostgreSQL 16; Snowflake and BigQuery versions reproduce the PostgreSQL results | execution on a live Snowflake or BigQuery account |
| every template parses in its dialect; every rendered email passes the email checks | rendering inside each email platform |
| statistics match the standard formulas and reference values | |
| every generated flow passes the simulator: the holdout gets nothing, no send without consent, nothing after the goal, frequency caps, permitted send windows; injected defects are caught | your platform's own flow engine |
| a run that fails at any stage resumes there; a revision keeps only copy whose job, audience and voice are unchanged, and an approval only while its words are | a long-running production deployment |
| every compliance rule names a check and a source; the SMS send hour is held to the strictest state window the rules track | legal advice: it tracks some states and not all, and laws change |
| the DNS collector and the daily job, end to end, against a fake resolver and a transpiled warehouse | live DNS and a live BigQuery run in CI |
| request bodies validate against schemas written from vendor documentation (October 2026) | a live API call to each vendor |
| every build-pack artifact validates against a schema written from the vendor's documented shape (Klaviyo's official OpenAPI spec, revision 2026-10-15), and fields the docs do not confirm are listed as UNVERIFIED; `lcm send` and `lcm build --create` retry, batch and dedupe against a mocked API | creating a flow in a live account; the UNVERIFIED fields named in each build pack |
| every JSON Schema and its Pydantic twin accept and reject the same documents; the tables in the docs are generated from the code and fail CI when stale | |
| the source mappers reproduce the committed sample data byte for byte; each planted data defect is caught with its column | every vendor export format variation |
| the repository contains no em dashes (house style) | taste |

Every technical document opens with a plain-English summary. Every change with the reason for it is in [`CHANGELOG.md`](CHANGELOG.md); the v2.2 method audit is [`docs/AUDIT.md`](docs/AUDIT.md).
