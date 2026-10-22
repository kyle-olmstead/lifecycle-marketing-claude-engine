# Operational guide

> **In plain English.** How to run the engine week to week: what to put in front of Claude at the
> start of a session, how Claude reasons and checks its own work inside one task, how to run
> improvement loops across weeks without thrashing a working program, and how to get Claude's
> outputs into HubSpot, Klaviyo, Braze, Customer.io, a warehouse or Clay.

## 1. Context loading protocol

Claude works from what is in its context window. Put the right things in, in the right order, and
label how each is known.

**Order** (plugin mode does steps 1 and 3 for you):

1. `prompts/core/principles.md`: the operating principles (syntax rules and diagnostics are read when a job needs them).
2. Your workspace's `lmm.yaml` (written by `lcm init`; older setups: a filled `templates/context/context_loader.xml`).
3. The module prompt for the job (the router in the system prompt names it).
4. The evidence for this job: header samples, query results, exports.

**What goes in the context file, and how to keep it small**

| Supply | Not | Why |
|---|---|---|
| table DDL or column lists with types, plus fill rates for the fields you filter on | full table dumps | the engine needs shape and coverage, not rows |
| `dig` output with the date you ran it | "DKIM is set up" | an observed record is evidence; a sentence is a claim |
| your own benchmarks with their basis (period, bot-excluded or not, attribution model and window) | industry averages | the comparison that matters is against your own history |
| a voice module: register, vocabulary inversion, forbidden phrases, sender identities | a brand book PDF | the copy prompts need rules they can apply, not inspiration |
| live flows with entry volume and holdout share | a flow diagram screenshot | volume decides what can be measured |
| constraints: caps, permitted send windows, approvals, do-not-contact lists | none | these are the rules the engine must not break |

**Labels.** Mark each element LIVE PULL (with `observed_at`), SUPPLIED, or ESTIMATE. The engine
lets a SUPPLIED claim lower a verdict but never raise one: a gate can only read GREEN on observed
authentication.

**When context runs out.** Keep the system prompt and the context file; drop old evidence first. For
a large tracking plan, include only the events the job touches and reference the rest by file path.

## 2. The reasoning loop inside one task

The system prompt makes Claude reason in a fixed order (`<thinking>`), show a five-line summary of
that reasoning (`<analysis>`), and then follow the execution protocol. In practice that is a short
loop, and it is the part of the engine that catches its own mistakes before you see them:

| Pass | What happens | What ends it |
|---|---|---|
| 1. Frame | Name the job and the module, list inputs present and missing, mark each missing input blocking or confidence-lowering | a blocking input is missing: ask (at most three questions, each with a default) and stop |
| 2. Preconditions | Check the gate, the transaction object, data feasibility and the program phase the module requires | a failed precondition: report it and deliver the remediation instead of the asset |
| 3. Draft | Write the deliverable in the user's real values, every number labeled | always continues |
| 4. Check | Run the tools on the draft: `template_lint.py`, `sql_check.py --run`, `stats.py`, schema validation, `sms.py` | everything passes: go to 6 |
| 5. Revise | Fix what the tools flagged, then re-run only the failing check | passes, or a second failure on the same check: deliver with the failure stated, never a third silent attempt |
| 6. Falsify | Ask what would make the answer wrong (a number that looks too good, two claims that conflict, an assumption that carries the result) and put the one that matters in the deliverable | always ends the loop |

Three rules keep the loop honest:

- **The tools are the check, not the reasoning.** A draft Claude believes is right still goes through
  the linter, the SQL runner and the stats tool. Where code cannot run (a plain web chat), the
  artifact is marked "unchecked" rather than described as verified.
- **Two revision passes, then stop.** A query that fails twice usually has a wrong assumption about
  the schema, not a typo. Say which assumption, and ask.
- **The visible summary is short on purpose.** The five `<analysis>` lines tell you what was
  assumed and what was checked. If you want the full reasoning, ask for it; it is not padding the
  deliverable by default.

To run the loop on a draft you already have, paste it with: "Run passes 4 to 6 on this: check it
with the repository tools, fix what fails, and tell me what would make it wrong."

## 3. Execution rules for continuous optimization

The improvement loop across weeks is diagnose, hypothesize, size, build, read, decide, record. It runs at the
cadence the program's phase allows, and the default action in a working program is no action.

**The loop**

1. **Diagnose** with the ladder: measurement, deliverability, list composition, data integrity,
   coverage, then creative and offer last. Stop at the first layer that explains the magnitude.
2. **Hypothesize** in the format in `prompts/jobs/measurement/test_hypothesis_matrix.md`:
   evidence, change, segment, metric, minimum effect, falsifying result.
3. **Size** with `engine/measurement/stats.py`. A test that cannot read inside a quarter is redesigned or dropped.
4. **Build** through the module that owns it, with the holdout specified before launch.
5. **Read** once, at the planned sample, after a sample ratio check.
6. **Decide** with the rule written before launch. Inconclusive is a result.
7. **Record** the decision and the number (labeled) in the context file's `program` section, so the
   next session starts from it.

**Cadence and change budget**

| Cadence | Work | Changes allowed |
|---|---|---|
| Weekly, 15 minutes | vitals: complaint rate, hard bounces, reputation-class bounces, unsubscribes, flows erroring, volume. Verdict CLEAR, WATCH or FLAG | none, unless a FLAG |
| Monthly | trend of the vitals, each send against its own baseline, flow entry volume, tests that reached sample, list health, next month's one test | up to three substantive changes |
| Quarterly | cohort retention, attribution reconciled against the transaction object, flow coverage against the lifecycle map, architecture drift, fill-rate trend, roadmap | structural changes |

A FLAG on any reputation signal pauses volume increases at once, before diagnosis.

**Reasoning discipline across sessions**

- Start every session by re-reading the context file's `program` section; never rely on a number
  remembered from an earlier session. Re-pull it or label it.
- One test per audience at a time.
- Report a quiet month as quiet. Manufacturing significance to justify activity is how a working
  program gets thrashed.

## 4. Integration playbook

Claude produces specifications and code. Getting them into your tools takes one of four routes.
API facts below were read from vendor documentation in October 2026; endpoints and limits change,
so check them before a production run.

### 4a. Connected platforms in Claude (read first, write with approval)

Claude connectors exist for HubSpot, Klaviyo, Braze, Customer.io, BigQuery, Snowflake, Mixpanel,
Amplitude and Clay, among others (`CONNECTORS.md` has the full matrix). With a platform connected,
the engine reads live data before asking you for it and labels it LIVE PULL. It never writes to a
connected platform without your explicit approval of the specific change.

### 4b. Segments and scores into the ESP

The RFM and other segment queries emit API-ready objects and batch numbers. Either aggregate them in
the warehouse (the RFM prompt shows how), or export rows and build request bodies with the size
limits enforced in code:

```text
python3 adapters/destinations/payloads.py hubspot lmm_rfm_scores.csv --id-column email --properties rfm_segment=lmm_rfm_segment --out out/hubspot
python3 adapters/destinations/payloads.py klaviyo lmm_rfm_scores.csv --id-column email --properties rfm_segment=lmm_rfm_segment --out out/klaviyo
```

| Destination | Endpoint | Limit |
|---|---|---|
| HubSpot contacts | `POST /crm/objects/2026-09/contacts/batch/upsert` | 100 inputs per request; create custom properties first |
| Klaviyo profiles | `POST https://a.klaviyo.com/api/profile-bulk-import-jobs` (with a `revision` header) | 10,000 profiles and 5 MB per job, 100 KB per profile |

For recurring syncs, a reverse-ETL tool (or your CDP's warehouse sync) running the same query is
sturdier than scripts. Pause every workflow that triggers on a synced property before the first
backfill, or the backfill will enroll everyone at once.

### 4c. Flows into the platform

The flow spec JSON (`schemas/flow_spec.schema.json`) is written to be built from without a
follow-up question. Build it in the platform's editor, or by API where one exists:

| Platform | Route | Notes |
|---|---|---|
| HubSpot | automation (workflows) API: `POST /automation/2026-09-beta/flows`, `automation` scope. Date-versioned and still beta; newer betas appear (a `2027-03-beta` is listed), so check the current version | `isEnabled` is required on create: set it to `false`, review in the editor, then enable |
| Klaviyo | Flows API: `POST /api/flows/` with an encoded flow definition | flows are created as drafts by default; one trigger per flow |
| Customer.io, Braze, Salesforce Marketing Cloud | build in the editor from the spec | the spec's step IDs map one to one to builder nodes |

Whatever the route, the holdout assignment lives upstream (warehouse or CDP) as a synced property,
so a rebuilt flow keeps the same control group.

### 4d. Event schemas into the CDP and Clay

- **CDP tracking plans.** `custom_event_definitions.json` is a JSON Schema (2020-12) per event, so
  it can seed a tracking plan in a CDP that validates events against JSON Schema. Validate events in
  CI or at the edge with any JSON Schema validator before they reach the messaging platform.
- **Clay.** Clay tables accept rows through a webhook source: each JSON payload POSTed to the
  table's webhook URL becomes a row. Send one object per contact, flat keys matching the table's
  columns, validated against the schema first. Clay's documentation caps a webhook source at
  50,000 submissions, and deleting rows does not reset the count; on Enterprise plans, auto-delete
  (passthrough) tables lift the cap, and on other plans you create a new webhook when it is reached.
  The same page publishes no rate limit, so batch with backoff. Clay also has a Claude connector for
  enrichment lookups.

### 4e. The safety rules that apply to every route

- Credentials live in environment variables or the platform's secret store, never in a prompt, a
  context file or a repository.
- Every write is idempotent (an upsert keyed on a stable identifier, or an idempotency key).
- Retries honor `Retry-After` on 429, back off exponentially with jitter, and stop at a dead-letter
  store someone reads.
- New automations are created disabled and enabled by a person after review.
