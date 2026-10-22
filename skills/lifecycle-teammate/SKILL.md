---
name: lifecycle-teammate
description: >
  Use when the user gives a campaign brief in plain English and wants the whole plan at once:
  "build a 5-touch win-back flow for users who haven't done X in 14 days", "plan a campaign",
  "fill in a campaign brief", "turn this brief into a flow", "end-to-end lifecycle plan"; when they
  want to change, approve or resume a plan ("make it 4 touches", "approve variant B", "it failed,
  try again"); when they ask what to build ("what are my top 3 priority builds today and why?",
  "what should I build next", "campaign ideas"); or for the daily health check, engagement tiers and
  sunset lists, tracking health, the retention curve, calendar blast vs trigger comparisons, and live
  domain checks ("what broke overnight", "are our events firing", "when do customers stop using
  the product", "who should we stop mailing", "is our newsletter worth keeping", "check our DMARC"). Runs the teammate's pipeline (Analyst,
  Strategist, Architect, Copywriter, Experiment, QA, Compliance) and returns a build-ready plan with
  SQL, a schema-valid flow spec, linted copy, a sized holdout and a compliance review.
metadata:
  version: "3.0.0"
---

# Lifecycle teammate

Always read (once per conversation; skip it if you already read it in this conversation):
`${CLAUDE_PLUGIN_ROOT}/prompts/core/principles.md`.

`lcm` below is the installed command, or `python3 ${CLAUDE_PLUGIN_ROOT}/engine/cli/main.py` (Python 3.12 or later and
`${CLAUDE_PLUGIN_ROOT}/requirements.txt`; `lcm doctor` checks the install, the workspace and the data). If code cannot
run, use the skills `lifecycle-flows`, `lifecycle-copy` and `lifecycle-measurement` by hand and label every artifact
"unchecked". Inside a workspace (`lmm.yaml` here or above) every command uses its platform, brand, gate, policies and
data.

**One command for every job**: `lcm "<the user's request>"` routes a sentence to the right command; run it with
`--dry-run` first when the request is ambiguous (it prints the job and anything missing, and runs nothing). The
sections below are the same jobs called directly, for their options (`lcm <command> --help`).

## What to build next

`lcm priorities --data <exports> --top 3 [--value-per-conversion <dollars>] [--domain <sending domain>]`

- Lead with the ranked list as written: fixes first, then the builds, each with its evidence, the monthly range with
  its assumptions, confidence, effort and next step. The range is not a forecast; the lift is assumed.
- Pass `--domain` (or set LCM_DOMAINS) so a failing domain shows up as a fix; otherwise say no domain was checked. If
  the user knows what one conversion is worth, pass `--value-per-conversion`; the default is estimated from orders.
- Offer to plan the top build: it has a ready brief under `priorities/`; plan it with step 2.

Routine questions a lifecycle manager asks, by cadence, with the job that answers each and what it cannot answer:
`${CLAUDE_PLUGIN_ROOT}/config/question_bank.yaml` (readable: `${CLAUDE_PLUGIN_ROOT}/docs/QUESTION_BANK.md`). Match the
question to the closest entry, follow its route, and say plainly when its limits apply.

## Step 1: Get a brief

`lcm brief --text "<the user's sentence>" --out briefs/<name>.yaml` turns a sentence into an editable brief that
inherits the workspace's platform, gate, frequency policy, offer policy and brand; show the assumptions at the top
before planning. The core action must be a real event name from their data: ask if you cannot verify it, never invent
it. A plan with no postal address is BLOCKED (CAN-SPAM) on platforms with no address token (Customer.io): ask for it.

## Step 2: Plan

`lcm plan --brief briefs/<name>.yaml [--data <exports>] --copy-mode handoff` (or `lcm workflow run new_flow --set
sentence="<the request>"`, which also checks collisions with the live flows). The engine plans ten flows and refuses
others by name; the flow type's judgment is in its prompt under `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/flows/`.

- Every run is saved under the workspace's `runs/`. If one stops part way (exit code 4), read the error, fix the
  cause, and `lcm resume --run <flow folder> [--data <folder>]` restarts at the stage that failed.
- With a warehouse connector, run the plan's `cohort/history_<dialect>.sql` through it and put the measured monthly
  entrants and baseline in the brief, labeled LIVE PULL. Without data the read date is not computed: keep it so.
- Warehouse-triggered flows (win-back, replenishment, sunset) get a daily job in `cohort/job/`: the ledger once, then
  each day the queue, pending and mark queries; `lcm events` pushes the pending rows. A failed push stays pending; each
  entry is in the ledger once, so nobody enters twice.
- **Copy**: write the drafts from `copy_briefs.md` with the `lifecycle-copy` skill, save `drafts.json`, and re-run
  with `--drafts drafts.json`. The renderer adds the template code, unsubscribe, address and SMS opt-out, and lints
  everything; fix any variant it rejects.

## Step 3: Change and approve

- "Make it 4 touches", "add SMS", "use a 20% holdout": `lcm "make it 4 touches"` revises the newest run, or `lcm
  revise --run <flow folder> --set touches=4` (repeat `--set`; lists as `channels=[email,sms]`). Copy for unchanged
  touches is kept, and so are approvals; say which touches got new copy.
- "Go with B for the first email": `lcm approve --run <flow folder> --step t1_email --variant B`.
- `lcm status --run <flow folder>` shows every stage, its attempts and any error.

## Step 4: Report

Lead with the plan's status (READY, READY WITH ASSUMPTIONS, BLOCKED) and its "Confirm before you build" list, then
"Why the plan looks like this", the flow table, the read date and any Compliance line that is not PASS. Link
`plan.md`. A BLOCKED plan names the check that failed; say what would unblock it. Compliance is a rules engine with
sources, not legal advice: present WARN lines as questions for the user or their counsel.

## The morning check and the rest

```text
lcm workflow run morning_check --set domain=<domain> --set data=<exports>
lcm daily --data <exports> --domains <example.com,...>
lcm engagement --data <exports> [--platform klaviyo|hubspot|braze|customerio] --out <dir>
lcm blast --brief <brief.yaml> --data <exports> [--dates 2026-09-01,2026-10-01]
lcm deliverability check <domain> --write-gate gate.json
lcm tracking --data <exports> [--events-dir <receiver folder>]
lcm retention --data <exports> [--by plan|country|lifecycle_stage]
lcm simulate --program
lcm workflow run quarterly_review --set data=<exports>
```

- The morning check audits every brief in `briefs/active/`; one failing step does not stop the others.
- Engagement tiers count clicks, never opens. The sunset list is an attribute (`lcm_sunset`) to exclude in marketing
  sends, not an unsubscribe; nothing reaches the ESP until the user sends the request files the tool wrote.
- A domain check needs DNS access; if lookups fail here, say so and ask for `dig` output instead.
- With a warehouse connector instead of files: `python3 ${CLAUDE_PLUGIN_ROOT}/engine/agents/cohort_analyst.py
  audit-sql --brief <brief> --dialect <bigquery|snowflake|postgres> --out <dir>`, run the three queries through the
  connector, save each result as JSON (`daily.json`, `snapshot.json`, `funnel.json`), then `lcm audit --brief <brief>
  --from-results <dir> --out <dir>`. For tiers, `lcm engagement --sql-only --dialect <dialect> --out <dir>`.

## Rules

- Nothing is built or sent in any platform without the user's approval of the specific change; new automations are
  created disabled.
- Never drop the holdout to make a read date look sooner; show the sensitivity grid instead.
- Never report a lift from a plan whose read is "not readable" or "directional only" as causal.
- Never turn a compliance WARN into a PASS by assumption; ask.
