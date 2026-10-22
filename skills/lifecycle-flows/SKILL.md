---
name: lifecycle-flows
description: >
  Use when the user says "build an onboarding flow", "activation flow", "design a welcome
  journey", "win-back flow", "reactivation", "abandoned cart", "browse abandonment", "post-purchase",
  "replenishment", "failed payment" or "dunning", "trial conversion", "sunset flow", "re-permission",
  "lapsed customers", "how should users move through automation", or asks for a flow spec in
  Customer.io, Braze, Klaviyo, HubSpot or Salesforce Marketing Cloud. Produces a build-ready flow:
  gate check, data feasibility, the FLOW block, a schema-valid flow JSON, branching rules, cadence,
  copy and a holdout sized to read.
metadata:
  version: "3.0.0"
---

# Behavioral flows

Always read (once per conversation; skip any you already read in this conversation):
- `${CLAUDE_PLUGIN_ROOT}/prompts/core/principles.md`
- `${CLAUDE_PLUGIN_ROOT}/prompts/core/syntax_rules.md` (every flow carries a flow spec JSON and templates)

Workspace: `lmm.yaml`; every brief inherits its platform, sending gate, frequency policy, offer policy and brand
(older setups: `lmm-engine/context.xml`). No sending gate: run `deliverability-gate` first, or design suppression and
remediation only.

`lcm` below is the installed command, or `python3 ${CLAUDE_PLUGIN_ROOT}/engine/cli/main.py` (Python 3.12 or later and
`${CLAUDE_PLUGIN_ROOT}/requirements.txt`; `lcm doctor` checks the install). If code cannot run, write the plan from
the job prompt by hand and label every artifact "unchecked".

## 1. Plan with the engine

The engine plans ten flows, one definition each in `engine/flows/library.py` (the table: `docs/FLOW_LIBRARY.md`), and
refuses anything else by name: say so, and never plan the nearest flow by hand instead.

```text
lcm plan --text "<the user's request>" [--data <exports>] --copy-mode handoff
lcm workflow run new_flow --set sentence="<the request>" --set data=<exports>    # also checks collisions with live flows
```

The plan writes the trigger and exit, the touch schedule, the holdout at the flow type's default share sized against
the data (`lcm size` resizes it), the flow spec JSON, the warehouse SQL per dialect, the copy briefs and the
compliance review, under the workspace's `runs/`. Check it: `lcm simulate <flow.json>` for one flow, `lcm simulate
--program` for every live flow against the one frequency policy, `lcm lint` for each template. Verify the data first:
`lcm check-data <folder>` and `lcm tracking --data <folder>`; a step that needs a missing event is BLOCKED and ships
off (`${CLAUDE_PLUGIN_ROOT}/prompts/core/diagnostics/missing_cdp_attribute.md`).

## 2. Judgment

Route (read the job prompt for the flow type; it holds what the plan cannot decide: who should not get it, what the
copy must not claim, what to check in the data, how to read the result):
- Welcome: `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/flows/welcome.md`
- Onboarding and activation: `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/flows/activation.md`
- Win-back and reactivation: `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/flows/winback.md`
- Abandoned cart: `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/flows/cart_abandon.md`
- Browse abandonment: `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/flows/browse_abandon.md`
- Post-purchase: `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/flows/post_purchase.md`
- Replenishment: `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/flows/replenishment.md`
- Failed payment (dunning): `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/flows/dunning.md`
- Trial conversion: `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/flows/trial_convert.md`
- Sunset and re-permission: `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/flows/sunset.md`

Copy for the touches: write the drafts from `copy_briefs.md` with the `lifecycle-copy` skill, save `drafts.json`, and
re-run `lcm plan ... --drafts drafts.json`; the renderer adds the template code, unsubscribe, address and SMS opt-out.

## 3. Deliver

Lead with the plan's status (READY, READY WITH ASSUMPTIONS, BLOCKED) and its "Confirm before you build" list, then
every section of the job prompt's output format, then the checks run.

## Rules

- Never build a flow in the platform without the user's approval; new automations are created disabled and
  enabled by a person.
- Never drop the holdout to make a read date look sooner; show what a larger or smaller share would buy.
