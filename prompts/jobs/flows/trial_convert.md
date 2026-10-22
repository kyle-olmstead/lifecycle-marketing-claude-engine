---
id: lmm.flows.trial_convert
version: 3.0.0
tools: [lcm plan, lcm size, lcm lint, lcm simulate]
---

# Trial conversion flow

Trial users reach value and choose a plan before the trial ends.

**The deterministic plan comes from the engine**: `lcm plan --text "a trial conversion flow for a 14-day trial in
<platform>" --data <exports>` writes it from `engine/flows/library.py`. Trigger `trial_started`; goal
`subscription_started`. Touches by role: trial_welcome, trial_value, trial_ending, trial_last_day (default 4; 5 adds
trial_ended; 1 is trial_ending alone), anchored to the trial's end rather than its start: welcome at entry, value at the
midpoint, ending 3 days before the end, last day 1 day before, ended 1 day after (`trial_days`, default 14, so days 0,
7, 11, 13 and 15). Re-entry never, per `user_id`; holdout 10%; outcome window 7 days. Software only. On an AMBER gate
it mails fresh trials (started in 30 days) and anyone who clicked or replied in 90. Use this prompt for judgment.

## Check the data first

- The real trial length, and whether it differs by plan or campaign: a wrong `trial_days` puts "ends in 3 days" on
  the wrong day.
- Whether the trial converts to paid automatically (a card on file) or needs a choice. An automatic charge makes the
  ending notice a billing matter: automatic-renewal and negative-option rules in the US and elsewhere may require a
  clear notice before the charge. Name the exposure and have the user confirm with counsel.
- `activation_reached` during the trial: the strongest predictor of conversion, and what trial_value should push.
- Team trials: one decision-maker per account, and the randomization unit is the account (`randomization_unit`).

## Who should not get it

Beyond the suppressions every plan carries (`engine/policy/guardrails.py`):
- users who already subscribed (they exit), and anyone whose trial was extended by sales;
- invited members of a trial workspace: they did not start the trial and cannot buy it;
- sales-assisted or enterprise trials, where an account owner runs the conversation.

## What the copy must not claim

- Dates other than the real trial end; "price goes up" or "last chance at this price" unless the pricing says so.
- "You will lose your data" unless the product deletes it, and then say exactly when.
- Discounts outside `offer_policy`; no offer in the first touch by default.

## Reading the result

Paid conversion within the outcome window after the trial ends, treatment against the holdout, at the account level
for team trials. Report attributed, incremental and the gap (`lcm readout`); watch complaints on the ending notice.

## Output

Lead with the plan's status and its "Confirm before you build" list, then the decisions above (trial length,
automatic charge, exclusions, data checks with results), the copy (fallbacks per `prompts/core/syntax_rules.md`) and
the read date.
