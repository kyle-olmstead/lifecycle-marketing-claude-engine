---
name: deliverability-gate
description: >
  Use when the user says "check our deliverability", "why are emails going to spam", "open rates
  collapsed", "analyze these headers", "is our DMARC right", "SPF too many lookups", "can we send
  more volume", "warm up a new IP" or "new domain", "Gmail or Yahoo or Microsoft bulk sender
  requirements", or before any plan that increases send volume. Issues the GREEN, AMBER or RED
  sending gate, diagnoses the root cause from headers, DNS and placement data, and produces a
  remediation protocol with exact DNS records and a warming ladder the gate allows.
metadata:
  version: "2.0.0"
---

# Deliverability gate and inbox recovery

## Step 0: Load context and rules

Read `${CLAUDE_PLUGIN_ROOT}/core/system_prompt.md` and `lmm-engine/context.xml` (run
`lifecycle-setup` first if it does not exist).

**Before running a tool:** the checks need Python 3.12 or later and the packages in
`${CLAUDE_PLUGIN_ROOT}/requirements.txt`. If an import fails, install them once with
`python3 -m pip install -r ${CLAUDE_PLUGIN_ROOT}/requirements.txt`. If code cannot run here (no
shell, or the install is blocked), still deliver, and write "unchecked" next to every artifact a
tool would have checked; never describe an unchecked artifact as verified.

## Step 1: Pick the module

- Diagnosis, headers, DNS, gate: `${CLAUDE_PLUGIN_ROOT}/modules/01_deliverability_and_infrastructure/prompts/header_analyzer.md`
- Warming a new or recovering IP or domain: `${CLAUDE_PLUGIN_ROOT}/modules/01_deliverability_and_infrastructure/prompts/warmup_schedule_generator.md`
- DMARC rollout or alignment audit: `${CLAUDE_PLUGIN_ROOT}/modules/01_deliverability_and_infrastructure/checklists/dmarc_alignment_audit.md`

Follow the module's protocol and output format exactly.

## Step 2: Gather evidence

Ask for raw headers from affected messages (several providers, several campaign types) and `dig`
output with the date it was run. If a messaging platform is connected, read bounce classes,
complaint and unsubscribe rates, and engagement split by recipient domain. Label everything.

## Step 3: Compute, do not guess

When code execution is available, assemble the analyzer input and run:

```text
python3 ${CLAUDE_PLUGIN_ROOT}/tools/deliverability.py analyze incident.json --markdown
python3 ${CLAUDE_PLUGIN_ROOT}/tools/warmup.py --target <n> --tiers '<json>' --gate <verdict> --markdown
```

Without code execution, do the module's steps by hand and label the analysis manual.

## Step 4: Deliver

The gate is the first line. Then the root cause, the alignment report, the bulk-sender matrix, the
remediation protocol (every DNS change as host, type, TTL and value), the warming schedule the gate
allows, and the verification plan. Offer to save the report to `lmm-engine/deliverability/<date>.md`
and to update the gate in the context file.

## Rules

- Never recommend a volume increase on a RED or AMBER gate.
- Never present a pasted record as observed.
- DNS changes are recommendations; the user's DNS owner makes them.
