---
name: lifecycle-copy
description: >
  Use when the user says "write this email", "draft the sequence copy", "subject line options",
  "personalize this in Liquid", "convert this template to Klaviyo" or "to HubSpot" or "to
  AMPscript", "write the SMS", "push notification copy", "in-app message", "this copy isn't
  converting", or needs a template checked for broken tags or missing fallbacks. Writes copy for a
  specific position in a specific flow, in the platform's real template dialect, with fallbacks on
  every token and falsifiable hypotheses on every variant.
metadata:
  version: "2.0.0"
---

# Lifecycle copy in the right dialect

## Step 0: Load context and rules

Read `${CLAUDE_PLUGIN_ROOT}/prompts/core/system_prompt.md` and `lmm-engine/context.xml`, especially the voice
module (binding when present) and the messaging platform's template dialect.

**Before running a tool:** the checks need Python 3.12 or later and the packages in
`${CLAUDE_PLUGIN_ROOT}/requirements.txt`. If an import fails, install them once with
`python3 -m pip install -r ${CLAUDE_PLUGIN_ROOT}/requirements.txt`. If code cannot run here (no
shell, or the install is blocked), still deliver, and write "unchecked" next to every artifact a
tool would have checked; never describe an unchecked artifact as verified.

## Step 1: Pick the module

- Email in any dialect: `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/copy/email_writer.md`
- SMS, push, in-app and channel orchestration: `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/copy/sms_push_in_app.md`
- HTML building blocks: `${CLAUDE_PLUGIN_ROOT}/templates/email/components.html`

Syntax reference: `${CLAUDE_PLUGIN_ROOT}/docs/LIQUID_CHEATSHEET.md`.

## Step 2: Refuse to write blind

Flow position, segment, trigger, the single conversion goal and the platform are required. Ask for
what is missing; never invent brand facts, numbers or names.

## Step 3: Write, then check

```text
python3 ${CLAUDE_PLUGIN_ROOT}/engine/render/template_lint.py --dialect <dialect> --email email.html
python3 ${CLAUDE_PLUGIN_ROOT}/engine/render/sms.py "<rendered worst-case SMS>"
```

Paste the results under the copy, or label it unlinted.

## Rules

- One conversion goal per message; every token has a fallback; every variant has a falsifiable
  hypothesis.
- No fake urgency, fake scarcity or fabricated personalization.
- Never send or schedule anything; drafts only, unless the user approves a specific send.
