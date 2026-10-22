# Build pack: Win-back: Ridgeview, 14 days since the core action (created a report) on Customer.io

**Status: READY WITH WARNINGS** | mode: native segment | run 11f5fd8ab2ee | slug `ridgeview_winback_14d`

## What is automatic

Customer.io has no create API for this: a person follows the build sheet.

Nothing is created or sent without `lcm build --create --approve` and the platform's key in the environment; without them the command prints what it would send.

## Trigger

- Type: **Attribute or Segment**
- Segment-triggered campaign on 'LCM Win-back: ridgeview_winback_14d' (Every re-match)
- Checked before every step: has not done feature_used since entering
- Exit: conversion goal: performs feature_used; exit when the person converts
- Re-entry: Every re-match (plan: allowed after the person leaves the flow)
- Holdout: split on the person property lcm_holdout_ridgeview_winback_14d (true goes to exit), written once and then weekly by the holdout sync (build/holdout_sync_<dialect>.sql, then lcm send --profile-updates): a segment trigger carries no event to read in_holdout from
- Confidence: LIKELY (the trigger type is verified on the vendor's documentation; this recipe is not)
- Or keep the warehouse job (--warehouse-job): run cohort/job/queue_postgres.sql daily and push core_action_lapsed_14d with lcm send. Use it when feature_used does not reach Customer.io, or when the lapse must be measured from the warehouse's own event history

### Segment to create: LCM Win-back: ridgeview_winback_14d

Customer.io data-driven segment; people enter the campaign when they match (Every re-match). Confidence: LIKELY.

1. has performed onboarding_step_completed at least once
1. has not performed onboarding_step_completed within the past 14 days
1. has not performed feature_used within the past 14 days

Needs: onboarding_step_completed and feature_used are tracked as Customer.io events

## Steps

Waits are relative to the previous step, in the platform's smallest delay unit.

| # | Step | Action | Wait | Channel | Copy | Condition or split |
|---|---|---|---|---|---|---|
| 1 | `holdout_split` | True/False branch | - | - | - | holdout: lcm_holdout_ridgeview_winback_14d is true; yes: exit (holdout: no messages); no: t1_email |
| 2 | `t1_email` | Send email | 0 hours (until 10:00 recipient local time) | email | copy/t1_email_A.html | - |
| 3 | `t2_email` | Send email | 72 hours (3 days) | email | copy/t2_email_A.html | - |
| 4 | `t3_in_app` | Send in-app message | 96 hours (4 days) | in_app | copy/t3_in_app_A.json | - |
| 5 | `t4_email` | Send email | 120 hours (5 days) | email | copy/t4_email_A.html | - |
| 6 | `t5_email` | Send email | 144 hours (6 days) | email | copy/t5_email_A.html | - |
| 7 | `sunset` | True/False branch on the re-permission email click | 336 hours (14 days) | - | - | sunset: clicked the re-permission email since entering; yes: exit (stays subscribed); no: sunset_update |
| 8 | `sunset_update` | Create or update person (LIKELY) | - | - | - | set lcm_sunset = true: suppressed from marketing, record kept, lifted only by a fresh opt-in |

## Goal, exit and settings

- Goal: created a report (feature_used where feature = reports); conversion goal: performs feature_used; exit when the person converts
- Exit: leaves immediately when the goal happens, at any step
- Re-entry: Every re-match
- Status when created: Draft
- Send window: email: 08:00 to 21:00 recipient local (plan); a Time Window delay just before the message: 08:00 to 21:00, in the person's timezone attribute
- Frequency: Message limit: 3 messages across email/sms/push per 7 day(s), with this campaign opted in to message limits

## Capability checks

| Check | Result | Detail | Confidence |
|---|---|---|---|
| `customerio.no_create_api` | pass | The App API cannot create campaigns (only update an existing action): the pack is a build sheet | VERIFIED ([source](https://docs.customer.io/integrations/api/app/tag/automations)) |
| `customerio.message_limits_opt_in` | WARN | Message limits apply only to workflows that opt in: turn the limit on in this campaign's settings, or the plan's cap is not enforced across campaigns | VERIFIED ([source](https://docs.customer.io/messaging/send/message-limits/overview/)) |
| `customerio.latest_liquid` | WARN (hard) | The templates use the default filter, which legacy Liquid lacks: the workspace must be on latest Liquid (accounts from 28 Nov 2023 are). Unknown for this account: set esp.options.liquid | VERIFIED ([source](https://docs.customer.io/messaging/liquid/)) |
| `all.events_in_esp` | WARN (hard) | Every event the flow reads inside the platform (the trigger, the exit, the exclusions) must reach the platform: account option events_in_esp lists them. A native absence trigger whose events are not in the platform needs --warehouse-job. Confirm these reach Customer.io: onboarding_step_completed (as onboarding_step_completed), feature_used (as feature_used) (or list them in esp.options.events_in_esp) | VERIFIED ([source](https://help.klaviyo.com/hc/en-us/articles/115002779051)) |
| `all.content_dialect` | pass | The rendered copy must be in this platform's template dialect: customerio | VERIFIED (engine/models.py PLATFORM_DIALECT) |

## Artifacts

| File | What | Schema | Valid | Unverified fields |
|---|---|---|---|---|
| `build/artifacts/build_sheet.json` | build sheet | build_sheet | yes | none |
| `build/holdout_sync_postgres.sql` | holdout sync sql | - | - | none |
| `build/holdout_sync_snowflake.sql` | holdout sync sql | - | - | none |
| `build/holdout_sync_bigquery.sql` | holdout sync sql | - | - | none |

## Click by click

1. Create Customer.io's campaign named `LCM Win-back: Ridgeview, 14 days since the core action (created a report)` and keep it Draft.
2. Entry: Attribute or Segment. Segment-triggered campaign on 'LCM Win-back: ridgeview_winback_14d' (Every re-match).
3. add True/False branch: lcm_holdout_ridgeview_winback_14d is true goes to exit (holdout: no messages); everyone else to t1_email.
4. after until 10:00 recipient local time, add Send email `t1_email`, content from copy/t1_email_A.html, copy/t1_email_A.mjml, copy/t1_email_A.subject.txt, copy/t1_email_A.txt.
5. after 3 days, add Send email `t2_email`, content from copy/t2_email_A.html, copy/t2_email_A.mjml, copy/t2_email_A.subject.txt, copy/t2_email_A.txt.
6. after 4 days, add Send in-app message `t3_in_app`, content from copy/t3_in_app_A.json.
7. after 5 days, add Send email `t4_email`, content from copy/t4_email_A.html, copy/t4_email_A.mjml, copy/t4_email_A.subject.txt, copy/t4_email_A.txt.
8. after 6 days, add Send email `t5_email`, content from copy/t5_email_A.html, copy/t5_email_A.mjml, copy/t5_email_A.subject.txt, copy/t5_email_A.txt.
9. after 14 days, add True/False branch on the re-permission email click: clicked the re-permission email since entering goes to exit (stays subscribed); everyone else to sunset_update.
10. Create or update person (LIKELY): set lcm_sunset = true: suppressed from marketing, record kept, lifted only by a fresh opt-in.
11. Exit: conversion goal: performs feature_used; exit when the person converts. Re-entry: Every re-match.

## By hand

1. Create the person property lcm_holdout_ridgeview_winback_14d (true or false). Run build/holdout_sync_postgres.sql and send its rows with `lcm send --profile-updates <results.csv> --platform customerio --approve`; repeat weekly so new people carry it before they can qualify
1. Data needed for the segment: onboarding_step_completed and feature_used are tracked as Customer.io events
1. Create the Customer.io data-driven segment 'LCM Win-back: ridgeview_winback_14d': has performed onboarding_step_completed at least once; has not performed onboarding_step_completed within the past 14 days; has not performed feature_used within the past 14 days
1. Make lcm_sunset = true a suppression in every marketing send
1. Entry filters to confirm (checked once at entry): completed onboarding (onboarding_step_completed where step_index = 3) at least once; clicked or replied to any email in the last 90 days (opens do not count: privacy proxies inflate them)
1. Suppressions on every step: unsubscribed from marketing email, or hard bounced; marked any message as spam (complaint), ever; sunset-suppressed (lcm_sunset is true); only a fresh opt-in lifts it; internal, test and seed addresses; currently in a higher-priority flow (by the frequency policy's flow_priority: billing, cart, post-purchase first); received a re-permission ask from any flow in the last 180 days
1. Before going live: send every message to a seed list, run `lcm simulate` on flow_spec.json, and get approval

## Relied on, not verified

Test these in a sandbox account before the first live send.

- LIKELY: the native segment recipe on Customer.io (the trigger type is verified, the recipe is not) ([source](https://docs.customer.io/messaging/send/campaigns/triggers.md))
- LIKELY: step sunset_update: Create or update person (LIKELY) ([source](https://docs.customer.io/messaging/send/campaigns/triggers.md))
