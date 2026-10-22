---
id: lmm.flows.welcome
version: 3.0.0
tools: [lcm plan, lcm size, lcm lint, lcm simulate]
---

# Welcome flow

New subscribers or signups get the brand promise and one reason to buy or start.

**The deterministic plan comes from the engine**: `lcm plan --text "a 4-touch welcome flow in <platform>" --data
<exports>` writes it from `engine/flows/library.py`. Trigger `signed_up`; goal `order_completed` for stores (a first
order) and `activation_reached` for software. Touches by role: welcome_promise, best_sellers, social_proof,
first_order_nudge (default 4; 5 adds how_it_works; 2 is welcome_promise and last_nudge), sent at entry and days 2, 4,
7 (and 10). Re-entry never, one journey per `user_id`; holdout 10%; outcome window 14 days after the last touch. On an
AMBER gate it mails fresh opt-ins (signed up in 30 days) and anyone who clicked or replied in 90. The plan's build
notes give the native trigger per platform. Use this prompt for the judgment the plan cannot make.

## Check the data first

- What `signed_up` means here: a newsletter signup, an account, or both. A list import or a backfill is not a
  signup; keep entry to the last day's signups (an entry filter on the signup date), or old records flood the flow.
- The goal on the transaction object: a first completed order in `orders`, not a cart or a pending order.
- Email consent on the profile (`email_consent_at`) and where it was captured: a popup, checkout, an account form.
- The signup volume and the share who already order within 14 days with no flow (the plan's history SQL): this is
  the baseline the holdout reads against.

## Who should not get it

Beyond the suppressions every plan carries (`engine/policy/guardrails.py`):
- people who already placed an order (existing customers who join the newsletter get a customer path, not a
  first-order nudge);
- invited teammates and seats added by an admin (software): they did not choose the product;
- anyone whose record arrived by import, purchase or append: they have no consent basis for this flow.

## What the copy must not claim

- An offer the workspace's `offer_policy` does not list. If the signup form promised a discount, it must be in the
  policy, with its real terms; otherwise the welcome cannot mention it, and the form is the problem to fix.
- "Best sellers" or "most popular" unless computed from the orders data; social proof only with real, current numbers
  labeled with their source.
- A deadline or "only today": an offer without `expires_after` has no deadline.
- The welcome promise ("we only email when...") must match the frequency policy the person is about to receive.

## Reading the result

First-order rate within the outcome window, treatment against the 10% holdout, on the orders table. People who just
signed up carry intent, so attributed revenue overstates the flow more than for most flows: report attributed,
incremental and the gap (`lcm readout`). Watch the first touch's unsubscribe and complaint rates: high numbers on a
welcome point at consent capture, not copy.

## Output

Lead with the plan's status (READY, READY WITH ASSUMPTIONS, BLOCKED) and its "Confirm before you build" list, then
the judgment above as decisions (who is excluded and why, what the copy may and may not claim, the data checks with
their results), the copy drafts (fallbacks per `prompts/core/syntax_rules.md`) and the read date.
