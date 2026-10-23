# Worked example: activation flow for Ridgeview (fictional)

A model answer for `prompts/jobs/flows/activation.md`, every section of its output format, on the small Ridgeview world
in `tests/fixtures/ridgeview_small/data` (400 people; the demo workspace's own data is the same company at full size).
Read it only when a model answer would help; the method and rules are in the job prompt.

**Inputs.** Ridgeview (fictional), a reporting product for operations teams. Persona: an
operations analyst at a 50 to 500 person company who rebuilds the same Monday numbers by hand for a
manager. Aha: `activation_reached` (first report shared). Events: the fixture tracking plan in
`tests/fixtures/ridgeview_small/data`. Stack: Customer.io (latest Liquid) for messaging, Mixpanel for analysis, a
PostgreSQL warehouse behind the CDP. About 3,000 signups a month. Email consent on 86% of users;
SMS consent on 22%; push permission unknown. No voice module supplied: the voice below is derived
from the product's in-app copy and labeled as derived. Gate: GREEN (from the deliverability gate).

**1. Gate check.** GREEN: email, in-app and push steps are permitted; volume is set by signups,
not by the flow.

**2. Aha and window.** `activation_reached` is in the plan and fires for 125 of the fixture
users. The window comes from the data, not from a round number:

Query: `prompts/jobs/flows/activation/activation_window_postgres.sql` (PostgreSQL; it runs on the fixtures).
Result on the fixtures: 118 of 375 mature signups activated inside the cap; median 7.0 days, 75th
percentile 8.3 days, so the window is **9 days**.
The team had been reporting a 14-day number; that number credits the flow with users who were
going to activate on their own schedule.

**3. Leak map** (mature cohorts: signed up at least 9 days before the as-of date):

Query: `prompts/jobs/flows/activation/activation_leak_map_postgres.sql` (PostgreSQL; it runs on the fixtures).
| Step | Reached (ever) | Drop from previous |
|---|---|---|
| Signed up | 397 (100%) | |
| Connected a data source | 81.4% | 18.6 points |
| Invited a teammate | 56.9% | **24.5 points (largest leak)** |
| Created a first report | 39.8% | 17.1 points |
| Activated within 9 days | 28.0% | |

Median time to the first step is 45 hours; median time to activation is 7 days. The flow is built
around the invite step: one teammate turns a solo trial into a team workspace, and the aha (a report
*shared*) needs someone to share it with.

**4. Data feasibility.**

| Dependency | Kind | Status | Fill rate | Note |
|---|---|---|---|---|
| `signed_up` | event | verified | 100% of users | trigger |
| `onboarding_step_completed.step` | event | verified | 100% of step events | branch conditions |
| `activation_reached` | event | verified | 28% within 9 days | goal and exit |
| `session_started` | event | verified | n/a | in-app eligibility |
| `workspace_id` | attribute | unknown | unknown | needed for account-level holdout; confirm it is on the profile |
| `first_data_source` | attribute | unknown | unknown | personalization in the invite email, with a fallback |
| `data_sync_completed` | event | **BLOCKED** | n/a | not in the plan; the push step ships off until engineering emits it server-side |

**5. FLOW block.**

```text
FLOW: Activation: first shared report
Lifecycle stage: signed up -> activated
Objective: share a first report within 9 days of signup
CLV lever: lifespan

TRIGGER: Customer.io custom event signed_up
  Source object: track_events, forwarded from the CDP
  Trigger type: event
  Re-entry: off

ENTRY FILTERS (evaluated at entry only):
  - customer.lmm_holdout_activation is not true
  - customer.created_at within the last 1 day (keeps backfilled signups out)

ONGOING FILTERS (evaluated at every step):
  - has not performed activation_reached
  - email_consent_at exists (email steps only)

SUPPRESSIONS:
  - Unsubscribed, hard bounced
  - Already activated
  - Invitees whose workspace is already activated (invitee path instead)
  - Active in the trial-expiring flow

HOLDOUT: 15% persistent (the activation default), randomized by workspace in the warehouse,
  synced as an attribute. Signups in one workspace move together, so the sample carries a design
  effect. At an assumed 1.3 entrants per workspace and ICC 0.5 (ESTIMATE; measure both once
  workspace_id is confirmed), design effect 1.15: 911 holdout signups for a 5-point lift on a 28%
  baseline, 2.0 months of entries plus the 9-day window, so the read lands at about 2.3 months.

STEPS:
  1. at signup -> email -> welcome -> connected_data_source
     BRANCH: connected_data_source within 1 day
       YES -> step 4
       NO  -> step 2 if in session, else step 3
  2. next session within 1 day -> in-app modal -> connect assist -> connected_data_source
  3. next 10:00 local -> email -> connect reminder -> connected_data_source
  4. on connected_data_source -> email -> invite a teammate -> invited_teammate (wait up to 2 days)
  5. next session after step 4 -> in-app modal -> weekly summary template -> created_first_report
  6. on data_sync_completed -> push -> data is ready -> session  [BLOCKED: event not instrumented]
  7. day 7, not activated -> plain-text email from a named person -> reply or activation

EXIT / GOAL: activation_reached at any point unenrolls immediately and starts the post-activation flow.

MEASUREMENT:
  Primary metric: activated within 9 days of signup
  Read after: 911 holdout signups past their 9-day window (about 2.3 months); no early read
  Compared against: persistent holdout
```

**6. Flow spec JSON** (validates against `schemas/flow_spec.schema.json`):

```json flow_spec
{
  "flow": "Activation: first shared report",
  "platform": "customerio",
  "analytics": "mixpanel",
  "template_dialect": "customerio",
  "lifecycle_stage": "signed_up to activated",
  "objective": "share a first report within 9 days of signup (the data-derived activation window)",
  "clv_lever": "lifespan",
  "gate_required": "GREEN",
  "trigger": {
    "type": "event",
    "definition": "signed_up",
    "source_object": "track_events (Customer.io custom event signed_up, forwarded from the CDP)",
    "re_entry": "off: one activation journey per person"
  },
  "entry_filters": [
    {
      "condition": "customer.lmm_holdout_activation is not true",
      "evaluated": "entry_only"
    },
    {
      "condition": "customer.created_at within the last 1 day (excludes backfilled signups)",
      "evaluated": "entry_only"
    }
  ],
  "ongoing_filters": [
    {
      "condition": "has not performed activation_reached",
      "evaluated": "every_step"
    },
    {
      "condition": "email_consent_at exists (email steps only)",
      "evaluated": "every_step"
    }
  ],
  "suppressions": [
    "unsubscribed or hard bounced",
    "already activated (activation_reached at any time)",
    "invited by a teammate whose workspace is already activated (they get the invitee path)",
    "active in the trial-expiring flow"
  ],
  "holdout": {
    "percent": 15,
    "persistent": true,
    "unit": "account",
    "assignment": "warehouse: lmm_holdout_activation = (abs(hash(workspace_id)) % 100 < 15), synced as a customer attribute before entry",
    "sizing": "3,000 signups a month, 28% baseline activation within 9 days, 5-point lift, 15% holdout. Randomized by workspace, so signups in one workspace are not independent: design effect 1.15 (ESTIMATE: 1.3 entrants per workspace and ICC 0.5, both unmeasured until workspace_id is confirmed; re-size when they are). 911 holdout signups, 2.0 months of entries plus the 9-day window: read at about 2.3 months (lcm size --baseline 0.28 --mde-abs 0.05 --holdout 0.15 --entries-per-month 3000 --outcome-window-days 9 --design-effect 1.15)",
    "read": "causal, slow"
  },
  "frequency": {
    "cap": "at most 1 lifecycle email per day and 3 per week across all flows; at most 1 in-app modal per session",
    "send_windows": [
      {"channel": "email", "allowed_from": "08:00", "allowed_until": "21:00", "basis": "recipient_local", "source": "engine default"},
      {"channel": "push", "allowed_from": "09:00", "allowed_until": "21:00", "basis": "recipient_local", "source": "engine default"}
    ],
    "send_hour_local": "10:00",
    "priority": 2
  },
  "steps": [
    {
      "id": "welcome",
      "wait": {
        "type": "none"
      },
      "channel": "email",
      "message": "Welcome: connect your first data source",
      "goal": "onboarding_step_completed step=connected_data_source",
      "requires": [
        "email_consent"
      ]
    },
    {
      "id": "wait_connect",
      "wait": {
        "type": "until_event",
        "event": "onboarding_step_completed (step = connected_data_source)",
        "timeout": "P1D"
      },
      "channel": "none",
      "message": "-",
      "goal": "-",
      "branch": {
        "condition": "connected_data_source done",
        "yes": "invite_teammate",
        "no": "in_session"
      }
    },
    {
      "id": "in_session",
      "wait": {
        "type": "none"
      },
      "channel": "none",
      "message": "-",
      "goal": "-",
      "branch": {
        "condition": "session_started in the last 30 minutes",
        "yes": "connect_modal",
        "no": "connect_reminder"
      }
    },
    {
      "id": "connect_modal",
      "wait": {
        "type": "none"
      },
      "channel": "in_app",
      "message": "Modal: connect a source in two clicks",
      "goal": "connected_data_source",
      "branch": {
        "condition": "modal shown or dismissed",
        "yes": "invite_teammate",
        "no": "invite_teammate"
      }
    },
    {
      "id": "connect_reminder",
      "wait": {
        "type": "until_time_local",
        "local_time": "10:00"
      },
      "channel": "email",
      "message": "Reminder: the one step before your first report",
      "goal": "connected_data_source",
      "requires": [
        "email_consent"
      ]
    },
    {
      "id": "invite_teammate",
      "wait": {
        "type": "until_event",
        "event": "onboarding_step_completed (step = invited_teammate)",
        "timeout": "P2D"
      },
      "channel": "email",
      "message": "Invite the person who asks you for the numbers",
      "goal": "onboarding_step_completed step=invited_teammate",
      "requires": [
        "email_consent"
      ],
      "branch": {
        "condition": "invited_teammate done within timeout",
        "yes": "first_report",
        "no": "first_report"
      }
    },
    {
      "id": "first_report",
      "wait": {
        "type": "until_event",
        "event": "onboarding_step_completed (step = created_first_report)",
        "timeout": "P2D"
      },
      "condition": "session_started in the last 30 minutes",
      "channel": "in_app",
      "message": "Modal: start from the weekly summary template",
      "goal": "created_first_report"
    },
    {
      "id": "data_ready_push",
      "wait": {
        "type": "until_event",
        "event": "data_sync_completed",
        "timeout": "P1D"
      },
      "condition": "mobile app installed",
      "channel": "push",
      "message": "Your data finished syncing",
      "goal": "session_started",
      "requires": [
        "push_permission"
      ]
    },
    {
      "id": "human_help",
      "wait": {
        "type": "fixed",
        "duration": "P7D"
      },
      "condition": "not activated 7 days after signup",
      "channel": "email",
      "message": "Plain text from a named onboarding lead: reply and I will set it up with you",
      "goal": "reply, or activation_reached",
      "requires": [
        "email_consent"
      ]
    }
  ],
  "exit": {
    "goal": "activation_reached",
    "unenroll_immediately": true,
    "then": "enter the post-activation flow (first week of value)"
  },
  "measurement": {
    "primary_metric": "activated within 9 days of signup (Mixpanel funnel signed_up then activation_reached, 9-day conversion window, broken down by lmm_holdout_activation)",
    "read_after": "911 holdout signups past their 9-day window (about 2.3 months at current volume; re-size once workspace size is measured); no read before",
    "compared_against": "holdout",
    "guardrails": [
      "unsubscribe rate per send under 0.3%",
      "spam complaints under 0.1%",
      "support tickets per signup not rising"
    ]
  },
  "data_dependencies": [
    {
      "name": "signed_up",
      "kind": "event",
      "status": "verified",
      "fill_rate": 1.0
    },
    {
      "name": "onboarding_step_completed.step",
      "kind": "event",
      "status": "verified",
      "fill_rate": 1.0
    },
    {
      "name": "activation_reached",
      "kind": "event",
      "status": "verified",
      "fill_rate": null,
      "note": "fires for 28% of mature signups within 9 days; 31% ever"
    },
    {
      "name": "session_started",
      "kind": "event",
      "status": "verified",
      "fill_rate": null
    },
    {
      "name": "workspace_id",
      "kind": "attribute",
      "status": "unknown",
      "fill_rate": null,
      "note": "needed for account-level holdout assignment; confirm it is on the customer profile"
    },
    {
      "name": "first_data_source",
      "kind": "attribute",
      "status": "unknown",
      "fill_rate": null,
      "note": "used in the invite email with a fallback; set server-side when connected_data_source fires"
    },
    {
      "name": "data_sync_completed",
      "kind": "event",
      "status": "blocked",
      "fill_rate": null,
      "note": "not in the tracking plan; the data_ready_push step stays off until engineering emits it server-side when the first sync finishes"
    }
  ]
}
```

**7. Event payload schema** for the blocked event, as an addition to the tracking plan
(`config/tracking_plan.json`: the `$defs` entry and its tracking-plan line):

```json
{
  "$defs": {
    "data_sync_completed": {
      "type": "object",
      "additionalProperties": false,
      "required": ["source", "rows_synced", "duration_seconds", "first_sync"],
      "properties": {
        "source": {"type": "string", "minLength": 1},
        "rows_synced": {"type": "integer", "minimum": 0},
        "duration_seconds": {"type": "integer", "minimum": 0},
        "first_sync": {"type": "boolean"}
      }
    }
  },
  "x-tracking-plan": [
    {"event": "data_sync_completed", "layer": "behavior", "sources": ["server"], "unlocks": ["data-ready push in the activation flow", "time to first sync"]}
  ]
}
```

**8. Branching rules.** In the Customer.io workflow builder: a True/False branch after the welcome
email on "performed `onboarding_step_completed` where `step` = `connected_data_source`" with a
1-day wait; a True/False branch on "performed `session_started` in the last 30 minutes" to choose
between the in-app modal and the reminder email. The holdout is an entry filter on the synced
attribute, not a Random Cohort branch, so the assignment survives a rebuild of the workflow and
stays fixed per workspace. In-message logic is in the templates below.

**9. Send cadence.**

| Step | Timing | Channel | Condition | Goal |
|---|---|---|---|---|
| welcome | at signup | email | email consent | connect a source |
| connect_assist | next session, within 1 day | in-app | source not connected, in session | connect a source |
| connect_reminder | next 10:00 local after 1 day | email | source not connected | connect a source |
| invite_teammate | when the source connects | email | no teammate invited | invite a teammate |
| first_report | next session after the invite email | in-app | no report yet | create a report |
| data_ready_push | when the first sync completes | push | push permission (BLOCKED) | open the app |
| human_help | day 7 | email | not activated | reply, or activate |

No SMS: this is B2B onboarding, the nudges are not time-sensitive, and SMS consent covers 22% of
users. An activation nudge by text costs more in opt-outs than it earns.

**10. Copy blocks.** Voice derived from the product's in-app copy (plain, specific, second person);
derived, not supplied, so treat it as a draft voice.

### Activation: first shared report, message 1

```text
Segment:            new signups, not in the holdout
Lifecycle stage:    signed up
Position in flow:   first message; nothing precedes it
Conversion goal:    connect a data source
Timing:             at signup
Channel:            email
From-name:          Dana Ortiz, Ridgeview
```

| Variant | Subject | Preview text | Hypothesis |
|---|---|---|---|
| A | Connect your first source (about two minutes) | Then the weekly summary fills itself in. | Stating the time cost raises connect rate within 24 hours among self-serve signups, because effort is the usual reason setup gets deferred. Falsified if B's 24-hour connect rate is equal or higher. |
| B | Your first report starts with one connection | Pick the tool your numbers already live in. | Control: names the outcome without the effort. |

```liquid customerio
Hi {{ customer.first_name | default: "there" }},

{% if event.method == "invite" %}
Your team already has a Ridgeview workspace, so there is nothing to set up. Open the reports they share with you and add your own when you are ready.
{% else %}
Ridgeview builds your first report as soon as it can see your numbers. Connect the tool they already live in and the weekly summary fills itself in.
{% endif %}

Connect a source: {% cio_link url:https://app.ridgeview.example/connect %}

Dana Ortiz
Onboarding, Ridgeview

Not useful? {% unsubscribe_url %}
```

Token fallbacks: `customer.first_name` falls back to "there"; `event.method` is required on
`signed_up` by the schema. Technical notes: plain-text part identical to the HTML; one link; no
images. Measurement: 24-hour connect rate by variant, read after 1,000 sends per arm.

### Activation: first shared report, message 4

```text
Segment:            connected a source, no teammate invited
Lifecycle stage:    signed up, step 1 done
Position in flow:   after the welcome email and the source connection
Conversion goal:    invite a teammate
Timing:             when connected_data_source fires
Channel:            email
From-name:          Dana Ortiz, Ridgeview
```

| Variant | Subject | Preview text | Hypothesis |
|---|---|---|---|
| A | Who asks you for these numbers? | Invite them and your first report goes straight to them. | Naming the person who asks for the numbers raises invites within 48 hours, because a report built for a known reader gets finished. Falsified if B's 48-hour invite rate is equal or higher. |
| B | Invite one teammate to Ridgeview | Reports get read when someone is waiting for them. | Control: the plain ask. |

```liquid customerio
{{ customer.first_name | default: "Hi" }}, your {{ customer.first_data_source | default: "data source" }} is connected.

Most first reports in Ridgeview get built for one person: whoever asks you for the numbers every Monday. Invite them now, and the first report you make lands in their inbox already shared.

Invite a teammate: {% cio_link url:https://app.ridgeview.example/team/invite %}

Dana

Not useful? {% unsubscribe_url %}
```

Token fallbacks: `customer.first_name` falls back to "Hi" (so the line reads "Hi, your data source
is connected."); `customer.first_data_source` falls back to "data source" until the attribute is
verified.

### Activation: first shared report, message 7

```text
Segment:            not activated 7 days after signup
Lifecycle stage:    signed up, stalled
Position in flow:   last touch; after up to four nudges
Conversion goal:    a reply (or activation)
Timing:             day 7
Channel:            email, plain text only
From-name:          Dana Ortiz
```

No A/B test on this message: the flow's holdout is the test, and the primary metric for this step is
reply rate.

```liquid customerio
Hi {{ customer.first_name | default: "there" }},

I run onboarding at Ridgeview. Your workspace is set up but there is no report in it yet, which usually means one connection didn't behave.

Reply with the tool your numbers live in and I will build the first report with you, or send you a ten-minute slot this week.

Dana Ortiz

Not useful? {% unsubscribe_url %}
```

In-app modals (Customer.io in-app; at most one per session; never in the first minute):

| Step | Title | Body | Button |
|---|---|---|---|
| connect_assist | Connect a source in two clicks | Pick where your numbers live. Ridgeview does the rest. | Connect |
| first_report | Start from the weekly summary | It's the report most teams share first. You can change everything later. | Use template |

**11. Measurement plan.** Mixpanel Funnels report: step 1 `signed_up`, step 2
`activation_reached`, conversion window 9 days, broken down by the user property
`lmm_holdout_activation` (set on both arms by the warehouse job). Read only when the holdout arm has
911 signups past their 9-day window (about 2.3 months; re-size when the design effect is measured).
Analyze at the workspace level, or with standard errors clustered by workspace, to match the
randomization; report attributed (activations after a flow touch),
incremental (treatment rate minus holdout rate, scaled) and the gap, using
`prompts/jobs/measurement/holdout_incrementality.md`. Guardrails:
unsubscribes under 0.3% per send, complaints under 0.1%, support tickets per signup flat. What would
change the plan: a holdout activation rate within 1 point of treatment at the read date means the
flow is not the lever; rebuild around the invite step in-product instead.

**12. Checks run.** `lcm lint --dialect customerio` on the three templates: no errors. The flow JSON
validates against `schemas/flow_spec.schema.json`. Both queries run on the fixtures (CI).
