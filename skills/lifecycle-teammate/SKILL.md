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
  version: "2.3.0"
---

# Lifecycle teammate

## Step 0: Rules and tools

Read `${CLAUDE_PLUGIN_ROOT}/core/system_prompt.md`. The teammate's code needs Python 3.12 or later
and `${CLAUDE_PLUGIN_ROOT}/requirements.txt` (install once with
`python3 -m pip install -r ${CLAUDE_PLUGIN_ROOT}/requirements.txt`). If code cannot run here, use
the module prompts directly (`lifecycle-flows`, `lifecycle-copy`, `lifecycle-measurement`) and label
every artifact "unchecked".

**One command for every job.** `python3 ${CLAUDE_PLUGIN_ROOT}/agents/cli.py "<the user's request>"`
routes a plain-English request (or a YAML job file) to the right tool. Run it with `--dry-run` first
when the request is ambiguous: it prints the job and anything still missing, and runs nothing. The
sections below are the same jobs called directly, for when you need their options.

## What to build next

When the user asks what to build, what their priorities are, or for campaign ideas, run:

```text
python3 ${CLAUDE_PLUGIN_ROOT}/agents/priorities.py --data <exports> --top 3 --out <workdir>/lmm-engine [--value-per-conversion <dollars>] [--domain <sending domain>]
```

- Lead with the ranked list as written: fixes first, then the top builds, each with its evidence, the monthly range
  with the assumptions, confidence, effort and the next step. Never present the range as a forecast; the lift is assumed.
- Add `--domain <sending domain>` (or set LCM_DOMAINS) so a failing domain shows up as a fix; otherwise say that no
  domain was checked.
- If the user knows what one conversion is worth, ask for it and pass `--value-per-conversion`; the default is an
  estimate from their orders.
- Offer to plan the top build: each win-back and activation build has a ready brief under
  `<workdir>/lmm-engine/priorities/`; plan it with Step 2. A post-purchase build has no brief type yet: plan it from
  the request it names with the lifecycle-flows skill.

Every other question a lifecycle manager asks, by cadence, with the job or skill that answers it and what it cannot
answer, is in `${CLAUDE_PLUGIN_ROOT}/config/question_bank.yaml` (readable version: `docs/QUESTION_BANK.md`). Match
the user's question to the closest entry and follow its route; say plainly when an entry's limits apply.

## Step 1: Get a brief

- A sentence is enough to start. Turn it into an editable brief and show the user the assumptions
  at the top of the file before planning:

  ```text
  python3 ${CLAUDE_PLUGIN_ROOT}/agents/orchestrator.py brief --text "<the user's sentence>" --product "<product>" [--set postal_address="<address>"] --out <workdir>/lmm-engine/briefs/<name>.yaml
  ```

- Fill what you can from `lmm-engine/context.xml` (platform, sending gate, postal address, sender,
  voice, recipient countries) and from connected platforms. The core action must be a real event name
  from their data: ask if you cannot verify it. Never invent the event.
- A plan without a postal address is BLOCKED (CAN-SPAM) on platforms with no address token
  (Customer.io). Ask for the address rather than letting the plan block.

## Step 2: Plan

```text
python3 ${CLAUDE_PLUGIN_ROOT}/agents/orchestrator.py plan --brief <brief.yaml> [--data <folder of users, orders, track_events exports>] --copy-mode handoff --out <workdir>/lmm-engine/plans
```

- Every run is saved under `plans/<flow>/runs/<run id>/`. If it stops part way (exit code 4), read the
  error, fix the cause, and `resume --run plans/<flow> [--data <folder>]`: it restarts at the stage
  that failed.
- With a warehouse connector, run `cohort/history_<dialect>.sql` from the output through the
  connector and put the measured monthly entrants and baseline in the brief; label them LIVE PULL.
  Without data, the plan says the read date is not computed. Keep it so.
- The daily win-back job is in `cohort/job/`: `ledger_<dialect>.sql` once, then each day
  `queue_<dialect>.sql` (writes who is due as pending), `pending_<dialect>.sql` (the rows to push with
  `tools/event_payloads.py`), and `mark_<dialect>.sql` for each row once its request succeeded. A
  failed push stays pending for the next run; each lapse is in the ledger once, so nobody enters twice.
- **Copy:** use `--copy-mode handoff`, then write the drafts yourself from `copy_briefs.md` following
  `${CLAUDE_PLUGIN_ROOT}/skills/lifecycle-copy/SKILL.md`, save `drafts.json`, and re-run with
  `--drafts drafts.json`. The renderer adds the template code, unsubscribe, address and SMS opt-out,
  and lints everything; fix any variant it rejects. (Outside a Claude session, `--copy-mode claude`
  calls the API with ANTHROPIC_API_KEY; `offline` uses the starter library.)

## Step 3: Change and approve

- "Make it 4 touches", "add SMS", "use a 20% holdout": `revise --run plans/<flow> --set touches=4`
  (repeat `--set` for several fields; lists as `channels=[email,sms]`). Copy for unchanged touches is
  kept, and so are approvals. Say which touches got new copy.
- "Go with B for the first email": `approve --run plans/<flow> --step t1_email --variant B`.
- `status --run plans/<flow>` shows every stage, its attempts and any error.

## Step 4: Report

Lead with the plan's status (READY, READY WITH ASSUMPTIONS, BLOCKED) and its "Confirm before you
build" list, then "Why the plan looks like this", the flow table, the read date and any Compliance
line that is not PASS. Link `plan.md`. A BLOCKED plan names the check that failed; say what would
unblock it. Compliance is a rules engine with sources, not legal advice: present WARN lines as
questions for the user or their counsel.

## Daily health check, tiers, blasts and domains

```text
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/daily.py --data <exports> --domains <example.com,...> --out <workdir>/lmm-engine/daily
python3 ${CLAUDE_PLUGIN_ROOT}/agents/engagement.py --data <exports> [--platform klaviyo|hubspot|braze|customerio] --out <workdir>/lmm-engine/engagement
python3 ${CLAUDE_PLUGIN_ROOT}/agents/cohort_analyst.py blast-vs-trigger --brief <brief.yaml> --data <exports> [--dates 2026-09-01,2026-10-01]
python3 ${CLAUDE_PLUGIN_ROOT}/tools/dns_collect.py <domain> [--selectors s1,s2] --analyze --markdown
python3 ${CLAUDE_PLUGIN_ROOT}/agents/tracking.py --data <exports> [--events-dir <receiver folder>]
python3 ${CLAUDE_PLUGIN_ROOT}/agents/retention.py --data <exports> [--by plan|country|lifecycle_stage]
```

- The daily job audits every brief in `briefs/active/`; `summary.md` is the report and `status.json`
  says what failed. One failing step does not stop the others.
- Engagement tiers count clicks, never opens. The sunset list is an attribute (`lcm_sunset`) to
  exclude in marketing sends, not an unsubscribe; nothing is sent to the ESP until the user sends the
  request files the tool wrote.
- A domain check needs DNS access; if lookups fail here, say so and ask for `dig` output instead.

With a warehouse connector instead of files: `python3 ${CLAUDE_PLUGIN_ROOT}/agents/cohort_analyst.py audit-sql --brief
<brief> --dialect <bigquery|snowflake|postgres> --out <dir>`, run the three queries through the
connector, save each result as JSON (`daily.json`, `snapshot.json`, `funnel.json`), then
`audit --from-results <dir>`. For tiers, run `engagement.py --sql-only --dialect <dialect>` and run
the query through the connector.

## Rules

- Nothing is built or sent in any platform without the user's approval of the specific change; new
  automations are created disabled.
- Never drop the holdout to make a read date look sooner; show the sensitivity grid instead.
- Never report a lift from a plan whose read is "not readable" or "directional only" as causal.
- Never turn a compliance WARN into a PASS by assumption; ask.
