---
name: lifecycle-copy
description: >
  Use when the user says "write this email", "draft the sequence copy", "subject line options",
  "personalize this in Liquid", "convert this template to Klaviyo" or "to HubSpot" or "to
  AMPscript", "write the SMS", "push notification copy", "in-app message", "this copy isn't
  converting", or needs a template checked for broken tags or missing fallbacks. Writes copy for a
  specific position in a specific flow, in the platform's real template dialect, linted, with a
  falsifiable hypothesis on every variant.
metadata:
  version: "3.0.0"
---

# Lifecycle copy in the right dialect

Always read (once per conversation; skip any you already read in this conversation):
- `${CLAUDE_PLUGIN_ROOT}/prompts/core/principles.md`
- `${CLAUDE_PLUGIN_ROOT}/prompts/core/syntax_rules.md` (every copy job writes template code)

Workspace: `lmm.yaml` (`platform` sets the dialect; `offer_policy` says what copy may offer; `frequency` holds the
permitted send windows) and the brand kit `brand.yaml` (its voice is binding when present). Older setups keep the
voice module and dialect in `lmm-engine/context.xml`.

`lcm` below is the installed command, or `python3 ${CLAUDE_PLUGIN_ROOT}/engine/cli/main.py` (Python 3.12 or later and
`${CLAUDE_PLUGIN_ROOT}/requirements.txt`; `lcm doctor` checks the install). If code cannot run, still deliver and
label every template unlinted.

## 1. Refuse to write blind

Flow position, segment, trigger, the single conversion goal and the platform are required. Ask for what is missing;
never invent brand facts, numbers or names.

## 2. Write

Route (read the one job prompt that matches):
- Email in any dialect: `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/copy/email_writer.md`
- SMS, push, in-app and channel orchestration: `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/copy/sms_push_in_app.md`

For copy inside a planned flow, the plan's `copy_briefs.md` names each touch's role and job and the tokens to write
(`{first_name}`, `{product_name}`, and for event flows `{item_name}` and `{product_block}`), which the renderer turns
into the platform's syntax with fallbacks; never bracketed fill-ins. The flow type's prompt in
`${CLAUDE_PLUGIN_ROOT}/prompts/jobs/flows/` says what that flow's copy must not claim. HTML building blocks, when you
build a full email by hand: `${CLAUDE_PLUGIN_ROOT}/templates/email/components.html`; the brand's MJML templates render
with `lcm render`. Per-dialect details beyond the syntax rules: `${CLAUDE_PLUGIN_ROOT}/docs/LIQUID_CHEATSHEET.md`.

## 3. Check: the engine counts and lints

```text
lcm lint --dialect <dialect> --email email.html
lcm sms "<the worst-case rendering>" [--first-message]
lcm brand check
```

Paste the results under the copy, or label it unlinted.

## Rules

- One conversion goal per message; every variant has a falsifiable hypothesis.
- No fake urgency, fake scarcity or fabricated personalization; no offer the workspace's `offer_policy` does not list.
- Never send or schedule anything; drafts only, unless the user approves a specific send.
