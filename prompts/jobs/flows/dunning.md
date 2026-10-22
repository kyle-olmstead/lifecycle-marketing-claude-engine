---
id: lmm.flows.dunning
version: 3.0.0
tools: [lcm plan, lcm lint, lcm simulate]
---

# Failed payment (dunning) flow

A failed renewal is recovered before the subscription lapses.

**The deterministic plan comes from the engine**: `lcm plan --text "a dunning flow for failed payments in <platform>"
--data <exports>` writes it from `engine/flows/library.py`. Trigger `payment_failed` for a subscription that is still
active or past due; goal `payment_succeeded`; it also exits on `subscription_cancelled`. Touches by role:
payment_failed, payment_retry, payment_final (default 3; 4 adds access_paused; 2 is the notice and the final) at
entry and days 3, 7 (and 10). Re-entry after exit, deduped on `invoice_id`. No holdout (0%): billing notices are never
withheld, because a holdout would cause involuntary churn. These are transactional messages: they send on every gate,
from the transactional stream (its own subdomain) when the marketing gate is not GREEN. Use this prompt for judgment.

## Check the data first

- The billing system's own retry schedule (Stripe Smart Retries, for example) and the date it cancels: the final
  notice states that real date, never one of its own.
- Decline reasons, if the billing system passes them: an expired card, insufficient funds and a suspected fraud need
  different notices.
- `payment_succeeded` arrives promptly: a late event mails "your payment failed" to someone who just paid.
- One `payment_failed` per invoice: retries that re-fire the event must not restart the flow (the dedupe key).
- The baseline: the share of failed payments recovered with processor retries only (the plan's history SQL).

## Who should not get it

The transactional suppressions (`engine/policy/guardrails.py`): hard bounces; complainers get only the legally
required notice, from the transactional stream; internal and test addresses. A marketing unsubscribe does not
suppress billing notices. Also exclude:
- customers who already updated their payment method or are cancelling on purpose;
- suspected fraud declines (no nudge to retry the same card);
- invoices paid by transfer or by a sales-managed account, where a person, not a flow, follows up.

## What the copy must not claim

- No marketing content: no offers, cross-sell or newsletter material. These notices reach people who unsubscribed
  from marketing, so marketing content in them breaks that promise.
- No dates, consequences or charges the billing system will not apply; no deceptive urgency.
- The likely cause only when the decline code supports it ("your card expired"). Link to a secure payment page; never
  ask for card details by email or SMS.

## Reading the result

There is no holdout, so there is no incremental read. Compare the recovery rate after launch with the pre-launch
baseline, label it a before and after comparison, and test message variants inside the flow (A/B with every person
still getting a notice). Report recovered revenue as recovered, never as incremental.

## Output

Lead with the plan's status and its "Confirm before you build" list, then the decisions above (exclusions, the real
billing dates, data checks with results), the notices (fallbacks per `prompts/core/syntax_rules.md`) and how the
recovery rate will be read.
