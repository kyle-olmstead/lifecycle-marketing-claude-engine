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
  version: "3.0.0"
---

# Deliverability gate and inbox recovery

Always read (once per conversation; skip it if you already read it in this conversation):
`${CLAUDE_PLUGIN_ROOT}/prompts/core/principles.md`.

Workspace: `lmm.yaml` (`sending_gate`, `platform`, `brand`); older setups keep the same facts in
`lmm-engine/context.xml`. No workspace yet: run `lifecycle-setup` first.

`lcm` below is the installed command, or `python3 ${CLAUDE_PLUGIN_ROOT}/engine/cli/main.py` (Python 3.12 or later and
`${CLAUDE_PLUGIN_ROOT}/requirements.txt`; `lcm doctor` checks the install). If code cannot run, do the job prompt's
steps by hand, say the analysis is manual, and mark every artifact a tool would have checked "unchecked".

## 1. Compute: the engine issues the gate

- A domain: `lcm deliverability check <domain> --write-gate gate.json` (add `--selectors s1,s2`, `--envelope <bounce
  domain>`, `--signals <signals.json>`). It looks up SPF, DKIM, DMARC, MTA-STS, TLS-RPT and BIMI and writes the gate
  `lmm.yaml` points to. If lookups fail here, ask for `dig` output with the date it was run.
- One raw message: `lcm deliverability headers <file>`. Evidence collected another way: `lcm deliverability analyze
  incident.json --markdown`. Signals only: `lcm deliverability gate signals.json`.
- Sending data: `lcm deliverability bounces`, `complaints`, `providers`, and the daily trend `lcm deliverability
  monitor`. Engagement tiers for the gate and the ladder: `lcm engagement --data <exports> --out <dir>`.
- A warming ladder: `lcm warmup --target <n> --tiers '<json>' --gate <verdict> --markdown`.

## 2. Judgment

Route (read the one job prompt that matches; it holds the method and the output format):
- Diagnosis, headers, DNS, root cause, remediation: `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/deliverability/header_analyzer.md`
- Warming a new or recovering IP or domain: `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/deliverability/warmup_schedule_generator.md`
- DMARC rollout or alignment audit: `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/deliverability/dmarc_alignment_audit.md`

Engagement fell with no obvious cause: `${CLAUDE_PLUGIN_ROOT}/prompts/core/diagnostics/degraded_inbox_placement.md`
first. The DNS record rule is in the job prompts; read `${CLAUDE_PLUGIN_ROOT}/prompts/core/syntax_rules.md` only if
you also write a template, SQL or an API payload.

Ask for raw headers from affected messages (several providers, several campaign types). With a messaging platform
connected, read bounce classes, complaint and unsubscribe rates, and engagement by recipient domain. Label everything.

## 3. Deliver

The gate is the first line. Then the root cause, the alignment report, the bulk-sender matrix, the remediation
protocol (every DNS change as host, type, TTL and value), the warming schedule the gate allows, and the verification
plan. Offer to save the report in the workspace's `runs/` folder and to record the gate with `--write-gate`.

## Rules

- Never recommend a volume increase on a RED or AMBER gate.
- Never present a pasted record as observed.
- DNS changes are recommendations; the user's DNS owner makes them.
