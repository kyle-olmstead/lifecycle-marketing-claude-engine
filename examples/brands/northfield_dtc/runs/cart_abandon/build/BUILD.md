# Build pack: Abandoned cart: Northfield Coffee Roasters on Klaviyo

**Status: READY WITH WARNINGS** | mode: native event | run 94860bd93458 | slug `northfield_coffee_cart_abandon_1h`

## What is automatic

lcm build --create --approve makes 3 email template(s), the flow, in Draft through Klaviyo's API.

Nothing is created or sent without `lcm build --create --approve` and the platform's key in the environment; without them the command prints what it would send.

## Trigger

- Type: **metric** on `Started Checkout`
- Metric-triggered flow on Started Checkout
- Entry filter: Klaviyo tracks events on known profiles only, so anonymous sessions cannot enter
- Checked before every step: Placed Order zero times since starting this flow
- Exit: flow filter: Placed Order zero times since starting this flow (checked before every action)
- Re-entry: Re-enter after a minimum time period: 1 week(s) (plan: allowed again P7D after entering)
- Holdout: Trigger split on the trigger event's in_holdout property (true goes to exit)

## Steps

Waits are relative to the previous step, in the platform's smallest delay unit.

| # | Step | Action | Wait | Channel | Copy | Condition or split |
|---|---|---|---|---|---|---|
| 1 | `holdout_split` | trigger-split | - | - | - | holdout: the trigger event's in_holdout is true; yes: exit (holdout: no messages); no: t1_email |
| 2 | `t1_email` | send-email | 60 minutes (1 hour) | email | copy/t1_email_A.html | -; window: sends when the delay ends: Klaviyo has no hour window on an hours delay (the plan permits 08:00 to 21:00) |
| 3 | `t2_sms_consent` | conditional-split realized as message filters: send-sms for people with sms consent, then send-email for everyone else (Klaviyo branches never rejoin, so the flow stays one path) | 1380 minutes (23 hours) | - | - | consent: has sms consent; yes: t2_sms; no: t2_email_fallback |
| 4 | `t2_sms` | send-sms | - | sms | copy/t2_sms_A.json | has SMS consent; window: sms_quiet_hours_enabled: true (Klaviyo's SMS quiet hours; the plan permits 09:00 to 20:00) |
| 5 | `t2_email_fallback` | send-email | - | email | copy/t2_email_fallback_A.html | no SMS consent (fallback for t2_sms); window: sends when the delay ends: Klaviyo has no hour window on an hours delay (the plan permits 08:00 to 21:00) |
| 6 | `t3_email` | send-email | 2880 minutes (2 days) | email | copy/t3_email_A.html | -; window: sends when the delay ends: Klaviyo has no hour window on an hours delay (the plan permits 08:00 to 21:00) |

## Goal, exit and settings

- Goal: placed an order (order_completed); flow filter: Placed Order zero times since starting this flow (checked before every action)
- Exit: leaves immediately when the goal happens, at any step
- Re-entry: Re-enter after a minimum time period: 1 week(s)
- Status when created: Draft (the flow and every action in draft; nothing sends until you set it live)
- Send window: email: 08:00 to 21:00 recipient local (plan); sends when the delay ends: Klaviyo has no hour window on an hours delay (the plan permits 08:00 to 21:00)
- Send window: sms: 09:00 to 20:00 recipient local (plan); sms_quiet_hours_enabled: true (Klaviyo's SMS quiet hours; the plan permits 09:00 to 20:00)
- Frequency: plan cap: at most 4 email/sms/push per P7D. Klaviyo: Smart Sending on for every message (smart_sending_enabled: true; email window 16 hours by default): a window, not a count, so the flow's own spacing keeps the cap
- Frequency: plan cap: at most 1 sms per P1D. Klaviyo: Smart Sending on for every message (smart_sending_enabled: true; email window 16 hours by default): a window, not a count, so the flow's own spacing keeps the cap

## Capability checks

| Check | Result | Detail | Confidence |
|---|---|---|---|
| `klaviyo.create_daily_limit` | pass | Create Flow and Create Segment are limited to 1 per second, 15 per minute and 100 per day: this pack makes 1 such create call(s) | VERIFIED ([source](https://raw.githubusercontent.com/klaviyo/openapi/main/openapi/stable.json)) |
| `klaviyo.definition_not_editable` | WARN | A flow definition cannot be changed through the API after it is created (only its status): review the artifact before --create, then edit in the flow builder | VERIFIED ([source](https://developers.klaviyo.com/en/reference/flows_api_overview)) |
| `klaviyo.smart_sending_is_a_window` | WARN | Smart Sending is a per-channel time window (email 16 hours by default), not a count: the plan's weekly cap is approximated by Smart Sending plus the flow's own spacing | VERIFIED ([source](https://help.klaviyo.com/hc/en-us/articles/115002779311)) |
| `klaviyo.no_hour_window_on_delays` | WARN | Klaviyo has no hour-range window on a minutes or hours delay: hour-level touches send when the delay ends (SMS still follows Klaviyo's SMS quiet hours setting) | VERIFIED ([source](https://help.klaviyo.com/hc/en-us/articles/115003885212)) |
| `all.events_in_esp` | WARN (hard) | Every event the flow reads inside the platform (the trigger, the exit, the exclusions) must reach the platform: account option events_in_esp lists them. A native absence trigger whose events are not in the platform needs --warehouse-job. Confirm these reach Klaviyo: checkout_started (as Started Checkout), order_completed (as Placed Order) (or list them in esp.options.events_in_esp) | VERIFIED ([source](https://help.klaviyo.com/hc/en-us/articles/115002779051)) |
| `all.content_dialect` | pass | The rendered copy must be in this platform's template dialect: klaviyo | VERIFIED (engine/models.py PLATFORM_DIALECT) |

## Artifacts

| File | What | Schema | Valid | Unverified fields |
|---|---|---|---|---|
| `build/artifacts/klaviyo_template_t1_email.json` | POST https://a.klaviyo.com/api/templates | klaviyo_template_create | yes | none |
| `build/artifacts/klaviyo_template_t2_email_fallback.json` | POST https://a.klaviyo.com/api/templates | klaviyo_template_create | yes | none |
| `build/artifacts/klaviyo_template_t3_email.json` | POST https://a.klaviyo.com/api/templates | klaviyo_template_create | yes | none |
| `build/artifacts/klaviyo_flow.json` | POST https://a.klaviyo.com/api/flows | klaviyo_flow_create | yes | data.attributes.definition.reentry_criteria (LIKELY): alltime 0 is read as 'Re-enter whenever they re-qualify' from the field description |

## Inputs to supply

Give each with `--id NAME=VALUE` (or esp.options.ids in lmm.yaml); `--create` resolves the rest.

- `<<klaviyo_metric:Placed Order>>`: the Klaviyo metric id of 'Placed Order' (lcm build --create finds it by name with GET /api/metrics)
- `<<klaviyo_metric:Started Checkout>>`: the Klaviyo metric id of 'Started Checkout' (lcm build --create finds it by name with GET /api/metrics)
- `<<klaviyo_template:t1_email>>`: the Klaviyo template id for t1_email (lcm build --create creates it from the rendered copy first)
- `<<klaviyo_template:t2_email_fallback>>`: the Klaviyo template id for t2_email_fallback (lcm build --create creates it from the rendered copy first)
- `<<klaviyo_template:t3_email>>`: the Klaviyo template id for t3_email (lcm build --create creates it from the rendered copy first)

## By hand

1. Entry filters to confirm (checked once at entry): is identified (has an email address on the profile); anonymous checkout_started events cannot enter; has not done order_completed in the 1 hour(s) after checkout_started; not in the program-wide global holdout (bucket of md5(user_id || ':lmm_global') under 5); the trigger is the person's own activity in the last day (a checkout, a product view, an order), which counts as engagement; anyone with no click, reply, order or session in 365 days is still excluded
1. Suppressions on every step: unsubscribed from marketing email, or hard bounced; marked any message as spam (complaint), ever; sunset-suppressed (lcm_sunset is true); only a fresh opt-in lifts it; internal, test and seed addresses; currently in a higher-priority flow (by the frequency policy's flow_priority: billing, cart, post-purchase first)
1. Before going live: send every message to a seed list, run `lcm simulate` on flow_spec.json, and get approval

## Relied on, not verified

Test these in a sandbox account before the first live send.

- LIKELY: klaviyo_flow: data.attributes.definition.reentry_criteria: alltime 0 is read as 'Re-enter whenever they re-qualify' from the field description ([source](https://raw.githubusercontent.com/klaviyo/openapi/main/openapi/stable.json))
