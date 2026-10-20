# LMM Engine (lifecycle-marketing-claude-engine) v2.1

**An operating layer for enterprise lifecycle marketing on Claude.** Claude works as two senior
people at once: a principal lifecycle growth strategist and a senior CRM systems and data engineer.
Small deterministic tools do the parts that must never be guessed: DNS and header analysis, SMS
segment math, experiment statistics, template and SQL syntax.

**If you read nothing else:**
- It writes flows, copy, SQL and deliverability fixes for HubSpot, Klaviyo, Braze, Customer.io and
  Salesforce Marketing Cloud, in each platform's real template language (only two of them use
  Liquid), with a fallback on every personalization token.
- It checks the sending gate before it designs anything that sends, verifies data before it writes
  a segment, and sizes the holdout before a flow ships. It reports what a flow actually drove
  (incremental), not only what the platform credits it with (attributed).
- **New in 2.1, the teammate:** describe a campaign in one sentence ("a 5-touch win-back for users
  who completed onboarding but haven't created a report in 14 days") and get a build-ready plan: the
  cohort SQL, a multi-channel flow that has been simulated, linted copy, and a holdout with its read
  date. Every assumption it made is listed for you to confirm. [Quickstart](docs/quickstart_for_marketers.md).
- Every SQL, template and JSON example in this repository is checked in CI against the rules the
  engine imposes on Claude. Free and open source (Apache 2.0).

| If you are | Start with | Time |
|---|---|---|
| a lifecycle or CRM manager | [Install](#4-install), then say *"set up the lifecycle engine"*, then *"check our deliverability"* | 15 minutes |
| a marketer with a campaign to plan | [Quickstart for marketers](docs/quickstart_for_marketers.md), then type your brief | 10 minutes |
| a marketing leader | [What it does](#1-what-it-does-in-plain-english) and [What is verified](#8-what-is-verified-and-what-is-not) | 5 minutes |
| a data or marketing engineer | [Repository architecture](#5-repository-architecture), then `docs/ARCHITECTURE.md`, `docs/architectural_overview.md` and `tools/` | as long as you like |

---

## 1. What it does, in plain English

1. **Decides whether you can send.** It reads your email headers and DNS records, works out whether
   your mail is authenticated and aligned, checks Gmail, Yahoo and Microsoft bulk-sender rules, and
   issues a GREEN, AMBER or RED sending gate. Then it writes the exact DNS records and the warming
   plan the gate allows.
2. **Designs flows that know when to stop.** Onboarding, activation and win-back flows with entry
   and exit rules, suppression, channel choice by consent and context, send timing, and a holdout
   sized so the result can actually be read.
3. **Writes copy in the right code.** The same email in Braze Liquid, Customer.io Liquid, Klaviyo's
   template language, HubSpot HubL and Salesforce AMPscript, linted for broken tags and missing
   fallbacks. SMS counted in segments on the longest possible rendering.
4. **Writes warehouse SQL that runs.** RFM scoring and segments, cohort LTV curves and retention, in
   Snowflake, BigQuery or PostgreSQL, with export payloads for Klaviyo and HubSpot.
5. **Tells you what worked.** Test sizing before launch, and readouts that report attributed revenue,
   incremental revenue and the gap between them, with honest labels when a sample is too small.
6. **Plans a whole campaign from one brief (new in 2.1).** Four agents in sequence: a cohort analyst
   (who enters, on which events, with the SQL and the data checked), a campaign architect (the
   multi-channel logic tree with waits, consent, exits and a sunset), a copywriter (variants per
   message, rendered in your platform's template language and linted), and an experimentation agent
   (a persistent holdout, 15% by default, sized with its read date). A QA pass simulates the flow
   with synthetic customers before anyone builds it. A daily audit at 05:00 UTC checks the cohort's
   health and logs it to Google Sheets.

**What it will never do:** recommend more volume on a damaged sender, warm a domain with people who
never engage, write fake urgency or invented personalization, report attributed revenue as if it were
incremental, or change a connected system without your approval.

## 2. Why it is built this way

Most lifecycle tooling fails in the same five places. The engine is designed around each:

| Failure | What the engine does |
|---|---|
| Copy written in "Liquid" for a platform that does not use Liquid | names the dialect on every block and lints it with a real parser (`tools/template_lint.py`) |
| Volume pushed onto a sender whose reputation is already slipping | issues the gate first and refuses growth on AMBER or RED (`tools/deliverability.py`, `tools/warmup.py`) |
| Segments built on fields that are 11% filled, or on deals in a services business | verifies fill rates and resolves the transaction object before writing a filter |
| Flows that cannot prove they worked | sizes a persistent holdout at spec time (`tools/stats.py feasible`) |
| Win-back credited with revenue that was coming back anyway | reports attributed, incremental and the gap, with the read labeled (`tools/stats.py incrementality`) |

The method comes from a working lifecycle and deliverability practice: verification before
assertion, evidence labels on every number (LIVE PULL, SUPPLIED, ESTIMATE, UNKNOWN), exit and
suppression before content, and low-volume honesty.

## 3. A taste

```text
$ python3 tools/deliverability.py analyze examples/deliverability/incident.json --markdown
## Sending gate: RED

**Permitted:** Stop bulk sending. Remediation and re-permission only.
...
**Start triage with:** identity. Basis: the cause class with the most critical findings (identity 1, reputation 1). Ties go to identity, then reputation, consent, content and infrastructure. A triage starting point, not a diagnosis.
...
- **CRITICAL** [identity] Signing domain(s): esp-mail.example.net. From domain: example.com. A signature from the ESP's
  shared domain passes DKIM but does not align, so it cannot carry DMARC and does not survive forwarding. ...
```

```text
$ python3 tools/stats.py incrementality examples/stats/winback_readout.json --markdown
  Conversion:     5.05% vs 4.02%; difference +1.03 pts, 95% CI -0.02 to +1.95 pts; not distinguishable from zero at 95%
  Attributed:     $41,900   (Klaviyo flow attribution, 5-day click window)
  Incremental:    $15,880   (point estimate: revenue per entrant, treatment minus holdout, times treatment entrants; ...)
  Gap:            $26,020
```

```text
$ python3 -m agents.orchestrator plan --brief briefs/examples/winback_14d.yaml --data tests/fixtures
READY WITH ASSUMPTIONS: Win-back: Ridgeview, 14 days since the core action (created a report)
  WARN  copy: t2_email: 2 variant(s) pass lint and guardrails (offline); contains [bracketed] fill-ins: ...
  WARN  holdout read: directional only: not inside 12 months at this volume (it would take about 187 months): ...
plan: out/ridgeview_winback_14d/plan.md
```

The full output for that brief is in [`examples/teammate/ridgeview_winback_14d/`](examples/teammate/ridgeview_winback_14d/plan.md).
The fixture company is small, and the plan says plainly that its holdout cannot read inside a year.

## 4. Install

You need a paid Claude plan. It works best in **Claude Code** or the **Claude desktop app with a
working folder**, where it can save files and run its tools. In a plain web chat it still works; it
labels anything it could not run as unchecked.

**In the Claude app:** open **Customize → Plugins → Add marketplace**, enter
`kyle-olmstead/lifecycle-marketing-claude-engine`, then add the **lifecycle-engine** plugin.

**In Claude Code:**

```text
/plugin marketplace add kyle-olmstead/lifecycle-marketing-claude-engine
/plugin install lifecycle-engine@lifecycle-engine
```

Then say **"set up the lifecycle engine."** Setup inventories your connected platforms, records each
one's template and SQL dialect, and writes the context file every other skill reads.

**Without the plugin:** paste `core/system_prompt.md`, then your filled `core/context_loader.xml`,
then the module prompt for the job, into any Claude conversation.

**Run the tools locally** (Python 3.12 or later):

```text
pip install -r requirements.txt
pytest -q && python3 tools/repo_check.py
```

**Run the teammate** (same install):

```text
python3 -m agents.orchestrator plan --text "Build a 5-touch win-back flow for users who completed onboarding but haven't created a report in 14 days" --product Acme
python3 -m agents.orchestrator plan --brief briefs/brief_template.yaml --data <folder with users, orders, track_events exports>
python3 -m agents.cohort_analyst audit --brief briefs/examples/winback_14d.yaml --data tests/fixtures --out out/audit
```

With the plugin, just describe the campaign; the `lifecycle-teammate` skill runs these for you.
For Claude Desktop without the plugin, add the local MCP server in `config/mcp_server_config.json`.

## 5. Repository architecture

```text
lifecycle-marketing-claude-engine/
├── .github/
│   ├── ISSUE_TEMPLATE/
│   │   ├── flow_bug.md                      checklist for a flow that misfired
│   │   └── deliverability_incident.md       the evidence a deliverability report needs
│   └── workflows/ci.yml                     tests, repository check, worked examples       [added]
├── docs/
│   ├── ARCHITECTURE.md                      layers, module dependencies, what is verified
│   ├── LIQUID_CHEATSHEET.md                 every dialect: Customer.io, Braze, Klaviyo, HubL, AMPscript
│   ├── SQL_SCHEMA_CONVENTIONS.md            canonical tables, naming, dialect differences
│   └── OPERATIONAL_GUIDE.md                 context loading, optimization loops, integrations
├── core/
│   ├── system_prompt.md                     dual persona, principles, syntax rules, diagnostics, router
│   └── context_loader.xml                   one file per brand: stack, data, consent, gate, voice
├── modules/
│   ├── 01_deliverability_and_infrastructure/
│   │   ├── prompts/
│   │   │   ├── header_analyzer.md           Workflow B: diagnosis, remediation, warming
│   │   │   └── warmup_schedule_generator.md
│   │   └── checklists/
│   │       └── dmarc_alignment_audit.md     RFC 9989-era rollout from p=none to p=reject
│   ├── 02_behavioral_triggers_and_flows/
│   │   ├── prompts/
│   │   │   ├── activation_flow_architect.md Workflow A: onboarding and activation
│   │   │   └── winback_branching_logic.md
│   │   └── schemas/
│   │       └── custom_event_definitions.json  the tracking plan as JSON Schema 2020-12
│   ├── 03_copywriting_and_multichannel/
│   │   ├── prompts/
│   │   │   ├── dynamic_liquid_email_writer.md one email in five dialects
│   │   │   └── sms_push_modal_orchestrator.md
│   │   └── templates/
│   │       └── modular_email_components.html  table layout, VML button, dark mode
│   ├── 04_data_schemas_and_sql/
│   │   ├── prompts/
│   │   │   ├── rfm_segmentation_builder.md  Workflow C: RFM in Snowflake, BigQuery, PostgreSQL
│   │   │   └── ltv_cohort_query_generator.md
│   │   └── queries/
│   │       └── rolling_d30_retention.sql
│   └── 05_experimentation_and_analytics/
│       ├── prompts/
│       │   ├── ab_power_calculator.md
│       │   └── holdout_incrementality_analyzer.md
│       └── frameworks/
│           └── test_hypothesis_matrix.md
├── .claude-plugin/                          plugin and marketplace manifests                [added]
├── skills/                                  seven plugin skills that route to the modules   [added]
├── tools/                                   deterministic tools (below)                     [added]
├── agents/                                  the teammate: orchestrator and four agents      [2.1]
│   ├── orchestrator.py                      brief to plan: Analyst, Architect, Copywriter, Experiment, QA
│   ├── cohort_analyst.py                    entry cohort, SQL in four dialects, daily health audit
│   ├── campaign_architect.py                multi-channel logic tree as a flow spec
│   ├── copywriter.py                        variants, rendered per dialect and linted
│   ├── experimentation_agent.py             persistent holdout, sizing, read date
│   └── models.py, brief.py, guardrails.py, sql_dialects.py
├── briefs/brief_template.yaml               the campaign brief a marketer fills in          [2.1]
├── config/
│   ├── mcp_server_config.json               local and remote MCP servers for live data      [2.1]
│   └── esp_payload_schemas.json             trigger-event request bodies per ESP            [2.1]
├── scripts/
│   ├── campaign_simulator.py                dry-run: structure, synthetic journeys, payloads [2.1]
│   ├── webhook_parser.py                    Segment and GA4 ingest to canonical events      [2.1]
│   ├── sheets_sync.py                       plans and audits to Google Sheets               [2.1]
│   └── mcp_server.py                        the teammate as MCP tools                       [2.1]
├── .github/workflows/daily_cohort_audit.yml 05:00 UTC cohort health check                   [2.1]
├── schemas/flow_spec.schema.json            machine-readable flow specification             [added]
├── examples/                                fictional worked-example inputs                 [added]
├── tests/                                   pytest suite and DuckDB fixtures                [added]
├── CONNECTORS.md                            which tools have a Claude connector today       [added]
├── requirements.txt                         pinned Python dependencies for the tools        [added]
├── .gitignore                                                                               [added]
├── README.md                                this file
└── LICENSE                                  Apache 2.0
```

Every path in the specification's tree is present with the same name; entries marked *added* go
beyond it, to make the engine installable as a Claude plugin and testable in CI.

**Tools**

| Tool | Computes |
|---|---|
| `tools/deliverability.py` | SPF (lookup counts through includes), DKIM key size, DMARC (RFC 7489 and RFC 9989 tags), MTA-STS, TLS-RPT, BIMI eligibility, Authentication-Results alignment, RFC 8058 one-click unsubscribe, bulk-sender checks, the sending gate, copy-paste DNS fixes |
| `tools/warmup.py` | a warming ladder that refuses on RED, holds on AMBER, never uses the unengaged tier, and caps at engaged capacity |
| `tools/stats.py` | sample size for any allocation, MDE, holdout feasibility, Newcombe intervals, sample ratio checks, attributed versus incremental readouts |
| `tools/template_lint.py` | syntax and fallback rules for Braze, Customer.io (latest and legacy), Klaviyo, HubL and AMPscript; email checks (unsubscribe, address, alt text, size, dark mode) |
| `tools/sql_check.py` | parse in a named dialect; run PostgreSQL on the fixtures |
| `tools/sms.py` | GSM-7 versus UCS-2 encoding and segment counts, with the characters that forced the switch |
| `tools/payloads.py` | Klaviyo bulk-import and HubSpot batch-upsert bodies within vendor limits |
| `tools/event_payloads.py` | trigger-event requests for Klaviyo, HubSpot, Braze and Customer.io, within vendor limits (2.1) |
| `tools/repo_check.py` | runs every check above on every example in the repository |

## 6. The modules

### 01 Deliverability and infrastructure

| | |
|---|---|
| **Core capability scope** | The sending gate (GREEN, AMBER, RED) from authentication, reputation, consent and placement signals; root-cause diagnosis in one of four classes (identity, reputation, consent, content and infrastructure); SPF, DKIM, DMARC, MTA-STS, TLS-RPT and BIMI analysis; Gmail, Yahoo and Microsoft bulk-sender compliance; DMARC rollout; warming ladders |
| **Key inputs and context artifacts** | raw headers from affected messages; `dig` output with the date run; Postmaster spam rate and compliance status; bounce classes; complaint and hard-bounce rates; engagement tiers; daily volume by provider; the full sending inventory; recipient geography |
| **Primary output artifacts** | gate verdict with the signals that set it; alignment report; bulk-sender matrix; remediation protocol with owners, copy-paste DNS records (host, type, TTL, value) and verification lookups; warming schedule; verification plan |

### 02 Behavioral triggers and flows

| | |
|---|---|
| **Core capability scope** | Onboarding and activation flows built around the largest measured leak; win-back, reactivation and sunset flows triggered on absence from each customer's own cycle; channel orchestration by consent and context; collision prevention; holdout design |
| **Key inputs and context artifacts** | target persona; aha definition; the tracking plan (`custom_event_definitions.json`) with fill rates; platform stack; signup or lapse volume; baseline rates; channel consent; the gate verdict |
| **Primary output artifacts** | leak map; data feasibility with BLOCKED steps named; the FLOW block; flow JSON valid against `schemas/flow_spec.schema.json`; event payload schemas; branching rules in builder terms and template dialect; send cadence; copy; measurement plan |

### 03 Copywriting and multichannel

| | |
|---|---|
| **Core capability scope** | Direct-response lifecycle copy for a specific position in a specific flow; the same message in five template dialects; SMS, push and in-app copy within each channel's real limits; modular, dark-mode-safe HTML |
| **Key inputs and context artifacts** | flow position, segment, trigger and the single conversion goal; voice module (binding); sender identity; platform and dialect; verified fields with fill rates; consent per channel; quiet hours |
| **Primary output artifacts** | message header block; subject and preview variants each with a falsifiable hypothesis; body in the platform dialect with fallbacks; plain-text part; SMS encoding report; lint results |

### 04 Data schemas and SQL

| | |
|---|---|
| **Core capability scope** | RFM scoring (NTILE for recency and monetary, banded frequency, LAG and LEAD for purchase cycles) and eleven mutually exclusive segments; cohort LTV with right-censoring; windowed D7 to D90 retention with maturity rules; CDP and ESP export payloads |
| **Key inputs and context artifacts** | SQL dialect flag (Snowflake, BigQuery, PostgreSQL); schemas for `users`, `orders`, `track_events`; the resolved transaction object; as-of date and windows; destination (Klaviyo, HubSpot); consent rule |
| **Primary output artifacts** | preconditions report; the query in the named dialect; segment dictionary mapped to the flow each segment serves; validation queries; export plan and request bodies |

### 05 Experimentation and analytics

| | |
|---|---|
| **Core capability scope** | Test and holdout sizing for any allocation; feasibility of a holdout at a flow's entry volume; readouts with intervals and sample ratio checks; attributed versus incremental reporting; test backlog prioritization under a change budget |
| **Key inputs and context artifacts** | primary metric (never opens); baseline from your own history; the minimum effect worth acting on; eligible volume; arm sizes, conversions and revenue; attribution model and window; inter-purchase interval |
| **Primary output artifacts** | sample size and duration; decision rule and peeking policy; the three-number readout with its read label; caveats; a prioritized hypothesis matrix |

## 7. What it works with

The engine reads platforms that have a Claude connector in your workspace, and works from pasted
evidence for the rest. Checked against Claude's connector directory on 2026-10-19:

| Category | Connector available today | No connector found (paste or export instead) |
|---|---|---|
| Messaging and ESP | HubSpot, Klaviyo, Braze, Customer.io (also Mailchimp, Brevo, Kit, OneSignal) | Salesforce Marketing Cloud |
| Data and CDP | BigQuery, Snowflake, Neon (PostgreSQL), Clay | Segment, RudderStack |
| Analytics | Mixpanel, Amplitude | Google Analytics 4, Looker Studio |

Details, and the paths for tools without a connector: [`CONNECTORS.md`](CONNECTORS.md).

## 8. What is verified, and what is not

| Verified in CI | Not verified |
|---|---|
| every SQL example parses in its dialect (sqlglot); PostgreSQL examples run on DuckDB fixtures; the Snowflake and BigQuery RFM and LTV queries reproduce the PostgreSQL output's key columns when transpiled to DuckDB: segment and R, F, M scores for every customer (RFM), observed customers and cumulative LTV for every cohort and period (LTV) | execution on a live Snowflake, BigQuery or PostgreSQL account |
| every Liquid, Klaviyo and HubL example parses (python-liquid, Django, Jinja2); AMPscript examples pass structural checks | rendering inside each ESP |
| sample sizes match the standard two-proportion formula; Newcombe intervals and chi-square tails match reference values checked against statsmodels and scipy when written | the suite pins those values; it does not import either library |
| every flow spec validates against the flow schema; every example event validates (and every invalid one is rejected) against the tracking plan | your platform's own validation |
| the teammate's entry query selects the same people as an independent Python computation on 40 dates; its Snowflake and BigQuery versions return the same rows when transpiled to DuckDB; its PostgreSQL version runs on a real PostgreSQL 16 in CI | a live Snowflake or BigQuery run |
| every generated flow passes the simulator: holdout gets nothing, no send without consent, nothing after the core action, frequency cap, send hour, quiet hours; the simulator catches each of those defects when they are injected | your platform's own flow engine |
| every rendered template lints in its dialect for all five platforms; trigger payloads validate against schemas written from vendor documentation (2026-10-20) | a live send or API call |
| the Claude copy path (structured output, repair round, fallback) against a stub client | a live Claude API call in CI |
| the repository contains no em dashes (house style) | taste |

Platform endpoints, limits and rules were read from vendor documentation in October 2026. They
change; check before a production run. Examples use fictional companies (Ridgeview, Northfield) on names RFC 2606 reserves for
documentation and testing: `example.com`, `example.net` and `example.org`, the `.example` top-level
domain (`ridgeview.example`, `northfield.example`), and `.test` for fixture addresses. None of them
belong to a real sender.

## 9. License

Apache 2.0. Improvements to the module prompts and new platform dialects are the most valuable pull
requests.
