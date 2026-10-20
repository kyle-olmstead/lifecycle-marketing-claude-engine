---
name: lifecycle-teammate
description: >
  Use when the user gives a campaign brief in plain English and wants the whole plan at once:
  "build a 5-touch win-back flow for users who haven't done X in 14 days", "plan a campaign",
  "fill in a campaign brief", "turn this brief into a flow", "end-to-end lifecycle plan", or asks
  for the daily cohort health check ("audit our cohort", "any anomalies in activation?"). Runs the
  teammate's four agents (cohort analyst, campaign architect, copywriter, experimentation) and a QA
  pass, and returns a build-ready plan with SQL, a schema-valid flow spec, linted copy and a sized
  holdout.
metadata:
  version: "2.1.0"
---

# Lifecycle teammate

## Step 0: Rules and tools

Read `${CLAUDE_PLUGIN_ROOT}/core/system_prompt.md`. The teammate's code needs Python 3.12 or later
and `${CLAUDE_PLUGIN_ROOT}/requirements.txt` (install once with
`python3 -m pip install -r ${CLAUDE_PLUGIN_ROOT}/requirements.txt`). If code cannot run here, use
the module prompts directly (`lifecycle-flows`, `lifecycle-copy`, `lifecycle-measurement`) and label
every artifact "unchecked".

## Step 1: Get a brief

- A sentence is enough to start. Turn it into an editable brief and show the user the assumptions
  at the top of the file before planning:

  ```text
  python3 ${CLAUDE_PLUGIN_ROOT}/agents/orchestrator.py brief --text "<the user's sentence>" --product "<product>" --out <workdir>/lmm-engine/briefs/<name>.yaml
  ```

- Fill what you can from `lmm-engine/context.xml` (platform, sending gate, postal address, sender,
  voice) and from connected platforms. The core action must be a real event name from their data:
  ask if you cannot verify it. Never invent the event.

## Step 2: Plan

```text
python3 ${CLAUDE_PLUGIN_ROOT}/agents/orchestrator.py plan --brief <brief.yaml> [--data <folder of users, orders, track_events exports>] --copy-mode handoff --out <workdir>/lmm-engine/plans
```

- With a warehouse connector, run `cohort/entry_<dialect>.sql` and `cohort/history_<dialect>.sql`
  from the output through the connector and put the measured monthly entrants and baseline in the
  brief; label them LIVE PULL. Without data, the plan says the read date is not computed. Keep it so.
- **Copy:** use `--copy-mode handoff`, then write the drafts yourself from `copy_briefs.md` following
  `${CLAUDE_PLUGIN_ROOT}/skills/lifecycle-copy/SKILL.md`, save `drafts.json`, and re-run with
  `--drafts drafts.json`. The renderer adds the template code, unsubscribe, address and SMS opt-out,
  and lints everything; fix any variant it rejects. (Outside a Claude session, `--copy-mode claude`
  calls the API with ANTHROPIC_API_KEY; `offline` uses the starter library.)

## Step 3: Report

Lead with the plan's status (READY, READY WITH ASSUMPTIONS, BLOCKED) and its "Confirm before you
build" list, then the flow table and the read date. Link `plan.md`. A BLOCKED plan names the check
that failed; say what would unblock it.

## Daily cohort audit

```text
python3 ${CLAUDE_PLUGIN_ROOT}/agents/cohort_analyst.py audit --brief <brief.yaml> --data <exports> --out <workdir>/lmm-engine/audit
```

With a warehouse connector instead of files: `python3 ${CLAUDE_PLUGIN_ROOT}/agents/cohort_analyst.py audit-sql --brief
<brief> --dialect <bigquery|snowflake|postgres> --out <dir>`, run the three queries through the
connector, save each result as JSON (`daily.json`, `snapshot.json`, `funnel.json`), then
`audit --from-results <dir>`.

## Rules

- Nothing is built or sent in any platform without the user's approval of the specific change; new
  automations are created disabled.
- Never drop the holdout to make a read date look sooner; show the sensitivity grid instead.
- Never report a lift from a plan whose read is "not readable" or "directional only" as causal.
