---
id: lmm.flows.activation
version: 3.0.0
inputs: [target_persona, aha_definition, core_product_events, platform_stack, signup_volume, baseline_activation, channel_consent, voice_module, gate_verdict]
tools: [lcm plan, lcm size, lcm lint, lcm simulate]
---

# Activation flow

Carry a new user from signup to the moment the product proves its value. Exits and suppressions define the flow as
much as content; without a holdout it cannot prove it worked.

**The deterministic plan comes from the engine**: `lcm plan --text "<the request>" --data <exports>` (trigger
`signed_up`, goal `activation_reached`, the touch roles first_step, unblock, show_outcome, last_nudge and more up to 8,
the schedule, re-entry never, the 15% holdout sized, the flow spec, the SQL and copy briefs). Use this prompt for the
judgment the plan cannot make. A model answer: `prompts/examples/activation_ridgeview.md`; read it only when one would
help.

## Protocol

1. **Gate.** RED: suppression and remediation design only; say so and stop. AMBER: email to new opt-ins and the
   engaged (the plan applies it); in-app is unaffected. No verdict: run deliverability-gate first.
2. **Resolve the aha event**: it exists, fires reliably and means what the team thinks. Set the window from data:
   the 75th percentile of time-to-aha among users who reach it, rounded up to whole days, capped at 30. Query: `prompts/jobs/flows/activation/activation_window_postgres.sql`.
3. **Model and transition**: the CLV lever (usually lifespan). In team products the unit is the account: the first
   invited teammate beats three more nudges to the admin.
4. **Leak map** on mature cohorts only (signed up at least one window ago), ranked by users recoverable; spend most
   of the flow on the largest leak. With data the plan reports it; with a warehouse connector adapt
   `prompts/jobs/flows/activation/activation_leak_map_postgres.sql`.
5. **Data feasibility** for every trigger, branch, filter and token: exists, type, fill rate (60% for segmentation,
   90% for branching). A step that needs a missing event is **BLOCKED**, ships switched off, and names the
   instrumentation (`prompts/core/diagnostics/missing_cdp_attribute.md`).
6. **States to channels.** Model states (signed up, each step done, activated, stalled N days); attach each touch to a
   state change or a timeout. Email when the step can wait (never the only channel for an in-session action). In-app modal when the user is in session and can act now (never in the first minute of the
   first session; one per session). Push when a state changed that the user is waiting on, with the SDK, a token and
   permission, inside the permitted push window. SMS only with explicit SMS consent, time-sensitive and expected,
   inside the permitted SMS window; never for B2B onboarding nudges. Push and SMS consent are separate.
7. **Cadence and collisions**: the workspace's one frequency policy (caps, permitted send windows in recipient local
   time, flow priority); mutual suppression with flows that must never overlap (trial expiry, sales sequences).
   The classic failures: double sends, mailing someone who already did it, sending an invitee the admin's onboarding.
8. **Holdout**: persistent, randomized and assigned upstream (warehouse or CDP) so it survives a rebuild, at the
   account level where the buying unit is an account; then size with the design effect and analyze at that level:
   `lcm size --baseline <rate> --mde-abs <lift> --holdout 0.15 --entries-per-month <signups> --outcome-window-days
   <window> --design-effect <1 + (signups per account - 1) x ICC>`. Too little volume to read: say so at spec time
   and use a labeled pre/post baseline.
9. **Check**: lint every template (`lcm lint --dialect <dialect> FILE`), validate the flow JSON against
   `schemas/flow_spec.schema.json`, `lcm simulate <flow.json>`; unlinted copy is labeled unlinted.

## Branching by platform

Flow logic belongs in the builder, in-message logic in the template dialect.

| Platform | Builder branching | Entry and holdout |
|---|---|---|
| Customer.io | True/False, Multi-split, Random Cohort | event-triggered campaigns read `event.x`; segment-triggered with re-entry is a runaway-send risk |
| Braze | Decision Split, Audience, Action and Experiment Paths | Canvas entry properties; Experiment Paths can hold out a control |
| Klaviyo | Conditional and Trigger splits | trigger filters apply at entry, flow filters at every step; Smart Sending is a per-channel cap |
| HubSpot | If/then branch, delay until event | re-enrollment off by default; goal criteria unenroll |
| Salesforce Marketing Cloud | Decision, Engagement and Random Split (the native holdout) | re-entry set at build time; a running journey can only be versioned |

## Output format

1. Gate check. 2. Aha and window (event, fill rate, how the window was chosen). 3. Leak map (ranked; the one the flow
is built around). 4. Data feasibility (verified / blocked / unknown, fill rates, instrumentation). 5. FLOW block.
6. Flow spec JSON (one `json flow_spec` block). 7. Event payload schemas. 8. Branching rules (builder, then template
blocks naming the dialect). 9. Send cadence table (step, timing, channel, condition, goal). 10. Copy blocks
(email_writer format; fallbacks per `prompts/core/syntax_rules.md`; every variant with a falsifiable hypothesis).
11. Measurement plan (metric, holdout, read date, guardrails, what would change the plan). 12. Checks run.

## Edge cases

- The aha fires for under 5% of signups: propose an intermediate milestone that predicts retention (with evidence);
  keep the original as the north star.
- Invitees arrive with context and a workspace: their own path. No session data: in-app steps are BLOCKED, not email.
- A few hundred signups a month: no readable holdout inside a quarter; labeled baseline, three messages at most.

## Guardrails

- No sending flow on a RED gate; no trigger, branch or token on an unverified field; suppression before copy.
