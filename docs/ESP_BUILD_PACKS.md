# ESP build packs and sending

**In plain English:** a plan says what a flow should do. A build pack says exactly how to build it in your platform:
the trigger to pick, each step and its wait, the splits, the settings, and which copy file goes where. Where the
platform lets software create a flow (Klaviyo, HubSpot, Salesforce Marketing Cloud), the pack also holds the request
that creates it as a draft. Where it does not (Braze, Customer.io, Iterable), the pack is a click-by-click sheet a
person follows. A second command, `lcm send`, pushes the events and profile updates a flow needs, safely.

Nothing reaches your platform unless you add `--approve` and your platform's key is set in the environment. Without
them, both commands print exactly what they would do. The flow `lcm build --create` makes starts switched off: a
Klaviyo flow in Draft, a HubSpot workflow that is off, a Marketing Cloud journey in Draft. The email templates and
segments it creates with the flow are live objects in your account (a segment starts counting members at once), but
none of them sends anything. Your platform sends to customers only after a person turns the flow on.

```text
lcm build --run out/northfield_cart_abandon_1h                          # writes out/northfield_cart_abandon_1h/build/
lcm build --run out/ridgeview_winback_14d --warehouse-job               # keep the daily warehouse job as the trigger
lcm build --run out/northfield_cart_abandon_1h --create                 # dry run: the calls it would make
lcm build --run out/northfield_cart_abandon_1h --create --approve       # creates the draft (needs KLAVIYO_PRIVATE_KEY)
lcm send --run out/ridgeview_winback_14d --rows pending.csv             # dry run of the day's trigger events
lcm send --run out/ridgeview_winback_14d --rows pending.csv --approve   # sends them
```

## What a pack holds

`lcm build` writes `build/` beside the plan:

- `BUILD.md`: the pack in plain English. The trigger, every step with its wait, the splits, the goal and exit, the
  permitted send windows and frequency settings in the platform's own words, the capability checks, the inputs to
  supply, the steps a person does by hand, and every fact the pack relies on that is not verified.
- `artifacts/`: the create requests (Klaviyo, HubSpot, Marketing Cloud) or the build sheet (Braze, Customer.io,
  Iterable), each validated against `config/esp_artifact_schemas.json`.
- `capabilities.json`: the capability report. `pack.json`: everything.
- `holdout_sync_<dialect>.sql` when the holdout split reads a person property (see below).

Every pack has these parts:

- **The native trigger.** The exact trigger type (a Klaviyo metric or segment, a HubSpot event-based or list-based
  enrollment, a Braze action-based or scheduled entry, and so on), its filters, the checks before every step, the exit
  criteria and the re-entry setting.
- **Steps with relative waits** in the platform's smallest delay unit: seconds on Braze; minutes on Klaviyo, HubSpot,
  Iterable and Marketing Cloud; hours on Customer.io (minutes only when a wait is not a whole hour).
- **Explicit splits.** The holdout split comes first, before any message. Each SMS or push step has a consent split
  with its email fallback.
- **Content references**: which rendered copy file each message uses (the approved variant, else A).
- **Capability checks** against your account (below). A failed hard requirement makes the pack BLOCKED with the reason.

## Win-back, replenishment and sunset without a warehouse job

In earlier versions every win-back needed a daily warehouse job an engineer ran. Most platforms can see the absence
themselves, so the pack now triggers these flows natively and says exactly which segment to create:

| Platform | Win-back and sunset | Replenishment |
|---|---|---|
| Klaviyo | segment-triggered flow; the pack creates the segment through the API | Ordered Product metric, then a delay of the supply less the lead days |
| HubSpot | list-based enrollment on the segment's criteria, with re-enrollment | custom event, then a delay |
| Customer.io | segment trigger, Every re-match | event trigger, then a time delay |
| Braze | a daily Scheduled Canvas on a segment that holds people only on the day they cross the line | action-based entry, then a delay |
| Iterable | a daily Schedule entry on a dynamic list, same crossing-day rule | Purchase entry, then a delay |
| Marketing Cloud | a scheduled Data Extension entry filled by a SQL Query Activity | the same |

Keep the warehouse job (`--warehouse-job`) when the core event does not reach your platform, or when the lapse must be
measured from the warehouse's own history. The pack names that alternative every time.

A segment trigger carries no event, so it has no `in_holdout` to read. The holdout then splits on a person property,
`lcm_holdout_<flow>`, written by the same assignment rule as the plan. Run `build/holdout_sync_<dialect>.sql` once and
then weekly, and send its rows with `lcm send --profile-updates <results.csv> --platform <platform> --approve`. HubSpot
always uses this property, because its branches are verified on record properties, not on event data.

The segment recipes are LIKELY, not verified: each platform's trigger type is read on its own documentation, the
recipe is not. Test the segment in a sandbox account before going live.

## Capability checks

The checks live in `config/esp_capabilities.json`, each fact with its source URL and its confidence. Your account comes
from the workspace: `platform` and the `esp` block in `lmm.yaml` (`esp.tier`, `esp.options`), or `--account`, `--tier`
and `--option` on the command line. An unknown tier or option is a warning, never a pass.

```yaml
platform: hubspot
esp:
  tier: professional
  options:
    events_in_esp: [signed_up, checkout_started, order_completed]
    from_email: hello@northfield.example
```

What the checks cover:

- **HubSpot**: custom behavioral event enrollment needs Professional or Enterprise (since March 2026 it is no longer
  Enterprise only); the Send email action needs Marketing Hub or Service Hub Professional or Enterprise; the email
  frequency safeguard (a cap across emails) is Enterprise only, so on lower tiers the plan's cap holds only inside the
  workflow; workflow time windows use the account time zone, not the recipient's.
- **Klaviyo**: creating flows and segments is limited to 100 a day each; a flow definition cannot be changed through
  the API after it is created; Smart Sending is a time window per channel, not a count; there is no hour window on an
  hours delay; there is no in-app action.
- **Braze**: the product block reads Canvas Context, whose availability varies by workspace (`esp.options.canvas_context`);
  global frequency caps count by calendar day, and a capped user moves past a Message step; `/users/track` has no
  idempotency key.
- **Customer.io**: the templates need latest Liquid (`esp.options.liquid: latest`); message limits apply only to
  campaigns that opt in; only events from the last 72 hours trigger a campaign.
- **Iterable**: Frequency Management is off by default; event fields reach a template only in a journey that event
  triggered.
- **Marketing Cloud**: no hard Journey Builder frequency cap was found; Trailhead advises waits of an hour or more.
- **Every platform**: every event the flow reads must reach the platform (`esp.options.events_in_esp`); every wait must
  be a whole number of the platform's unit; the copy must be in the platform's template dialect.

## Per platform

### Klaviyo

- **Created automatically** (`lcm build --create --approve`): one CODE template per email from the rendered copy, the
  segment for a native win-back or sunset, then the flow, in Draft with every action in draft. Metric ids are looked
  up by name; template and segment ids come from the earlier responses. The request shapes are trimmed from Klaviyo's
  official OpenAPI spec (revision 2026-10-15).
- **By hand**: review the flow before creating it (its definition cannot be edited through the API afterwards), set it
  live, and for browse abandonment add the filter that names the abandoned cart flow.
- **Unverified**: how `reentry_criteria` maps to the builder's options; the difference between the update-profile
  operators; Klaviyo's built-in email metric names used for the sunset segment.

### HubSpot

- **Created automatically**: the workflow, off, through the automation v4 API (beta; the pack pins
  `/automation/v4/flows`). Delays, the holdout branch, the emails and the sunset's edit-record action are in it.
- **By hand**: build one automated marketing email per message and give its id with `--id`; set the goal, the
  time-of-day delays and any time window in the workflow editor; add an SMS action where your account has one.
- **Unverified**: the list branch's `type` value (so `--create` also needs `--allow-unverified`), the branch and
  enrollment filter shapes (from the Lists API guide), and the custom event id form.

### Salesforce Marketing Cloud

- **Created automatically**: the journey, in Draft, with its Wait and Email activities. Publishing is a separate call
  the pack never makes.
- **By hand**: create the entry event definition and the emails, set the goal and exit criteria, add SMS or push
  activities, and check the Decision Split before publishing.
- **Unverified**: the Decision Split's type string and Filter XML (the format page could not be read), so `--create`
  needs `--allow-unverified`.

### Braze

- **No create API.** The pack is a build sheet. With `--braze-template-canvas <id>`, it also writes a
  `/canvas/duplicate` request that copies a template Canvas you built; its body is UNVERIFIED and is sent only with
  `--allow-unverified`.
- **By hand**: everything in the sheet, step by step, including the Canvas-level permitted send window.

### Customer.io and Iterable

- **No create API.** The pack is a build sheet; follow `BUILD.md`, Click by click.
- **Unverified**: a Customer.io Time Delay in minutes (hours are verified); Iterable's journey create API is absent by
  evidence, not by statement, and whether re-sending an event with the same id re-triggers a journey is unknown.

Iterable is a full platform: copy renders in Handlebars (`{{defaultIfEmpty firstName "there"}}`, `{{#each items}}`,
`{{unsubscribeUrl}}`, `{{physicalAddress}}`), `lcm lint --dialect handlebars` checks it, and every flow plans on it.

## Sending safely: `lcm send`

`lcm send` takes one input: the day's pending warehouse-job rows (`--rows`, from `cohort/job/pending_<dialect>.sql`),
a folder of request files (`--requests`, from `lcm events` or `lcm sync`), or person-attribute rows
(`--profile-updates`, such as the sunset list or the holdout property).

- **Batching** to each platform's documented limit: HubSpot events 500 per batch and contacts 100, Braze 75 objects per
  request, Klaviyo profile imports 10,000, Customer.io batches by size. An oversized request file is split, never sent
  as it is.
- **Idempotency**: every item carries a key (Klaviyo `unique_id`, HubSpot `uuid`, Customer.io `messageId`, Iterable
  `id`). Braze has no dedupe, so the receipt log is its sent log.
- **Retries**: 429 and 5xx are retried with exponential backoff and jitter, honoring `Retry-After` and Braze's rate-limit
  headers; any other 4xx is never retried. Create calls from `lcm build --create` retry only 429, because a 5xx after a
  create may have made the object.
- **Receipts**: `send/receipts.jsonl` in the run folder. A rerun skips every item already accepted, so nothing is sent
  twice; accepted warehouse rows go to `send/accepted_rows.csv` for the ledger's mark step.
- **Summary**: counts, and every failure by request with its HTTP status and the platform's message.

The keys are read from the environment only at send time and never written: `KLAVIYO_PRIVATE_KEY`,
`HUBSPOT_PRIVATE_APP_TOKEN` (with `HUBSPOT_HUB_ID`), `BRAZE_REST_API_KEY` (with `BRAZE_INSTANCE`),
`CUSTOMERIO_PIPELINES_API_KEY_BASE64`, `ITERABLE_API_KEY`, and for `lcm build --create` on Marketing Cloud
`SFMC_ACCESS_TOKEN` and `SFMC_SUBDOMAIN`. `lcm doctor` lists which are set (names only).

The modules are `adapters/destinations/build/` and `adapters/destinations/send.py`; the research behind every fact is in
`config/esp_capabilities.json` with its source and confidence.
