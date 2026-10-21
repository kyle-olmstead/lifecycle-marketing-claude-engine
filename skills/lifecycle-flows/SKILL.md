---
name: lifecycle-flows
description: >
  Use when the user says "build an onboarding flow", "activation flow", "design a welcome
  journey", "win-back flow", "reactivation", "sunset flow", "re-permission", "lapsed customers",
  "how should users move through automation", or asks for a flow spec in Customer.io, Braze,
  Klaviyo, HubSpot or Salesforce Marketing Cloud. Produces a build-ready flow: gate check, leak map,
  data feasibility, the FLOW block, a schema-valid flow JSON, event payload schemas, branching rules,
  cadence, copy and a holdout sized to read.
metadata:
  version: "2.0.0"
---

# Behavioral flows

## Step 0: Load context and rules

Read `${CLAUDE_PLUGIN_ROOT}/prompts/core/system_prompt.md` and `lmm-engine/context.xml`. No sending gate in
the context: run `deliverability-gate` first, or design suppression and remediation only.

**Before running a tool:** the checks need Python 3.12 or later and the packages in
`${CLAUDE_PLUGIN_ROOT}/requirements.txt`. If an import fails, install them once with
`python3 -m pip install -r ${CLAUDE_PLUGIN_ROOT}/requirements.txt`. If code cannot run here (no
shell, or the install is blocked), still deliver, and write "unchecked" next to every artifact a
tool would have checked; never describe an unchecked artifact as verified.

## Step 1: Pick the module

- Onboarding and activation: `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/flows/activation.md`
- Win-back, reactivation, sunset: `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/flows/winback.md`

## Step 2: Verify the data

Read the tracking plan (`${CLAUDE_PLUGIN_ROOT}/config/tracking_plan.json`
or the user's). With a warehouse or analytics connector, pull fill rates and the leak map (the
activation prompt's query); otherwise ask. Steps that depend on missing events are BLOCKED and ship off.

## Step 3: Size the holdout

```text
python3 ${CLAUDE_PLUGIN_ROOT}/engine/measurement/stats.py feasible --entries-per-month <n> --holdout 0.10 --baseline <rate> --mde-abs <lift>
```

## Step 4: Write and check

Produce every section of the module's output format. Validate the flow JSON against
`${CLAUDE_PLUGIN_ROOT}/schemas/flow_spec.schema.json` and lint each template with
`python3 ${CLAUDE_PLUGIN_ROOT}/engine/render/template_lint.py --dialect <dialect> <file>`. Save to
`lmm-engine/flows/<flow-name>/`.

## Rules

- Never build a flow in the platform without the user's approval; new automations are created
  disabled and enabled by a person.
- Every filter says whether it applies at entry only or at every step.
