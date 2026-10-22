---
id: lmm.flows.cart_abandon
version: 3.0.0
tools: [lcm plan, lcm size, lcm lint, lcm sms, lcm simulate]
---

# Abandoned cart flow

People who started checkout and did not order get their cart back within the hour.

**The deterministic plan comes from the engine**: `lcm plan --text "a 3-touch abandoned cart flow in <platform>"
--data <exports>` writes it from `engine/flows/library.py`. Trigger `checkout_started` by an identified person with no
`order_completed` within 1 hour (`abandon_after_hours`); goal `order_completed`. Touches by role: cart_reminder,
cart_objection, cart_last_call (default 3; 4 adds social_proof; 2 is reminder and last call), at entry and 23 and 71
hours later (167 for a fourth), each sent when its wait ends inside the permitted send window. Re-entry at most once
per 7 days, deduped on `user_id + checkout_id`; holdout 10%; outcome window 7 days. Stores only; needs `orders`. On an
AMBER gate the checkout itself counts as engagement, but anyone with no click, reply, order or session in 365 days is
still excluded. Use this prompt for judgment.

**The cart in the email** (`engine/render/product_blocks.py`): Klaviyo with Shopify reads `event.extra.line_items`
and links back to `event.extra.checkout_url`; Customer.io loops `event.items` (up to 3); Braze loops the Canvas
context variable `items` (Canvas Context must be enabled); HubSpot and Salesforce Marketing Cloud show one item from
synced fields (`lcm_last_item_name` and `lcm_last_item_url`, or `item_name` and `item_url` in entry data), because
they cannot loop event properties. The plan's build notes say what to sync.

## Check the data first

- `checkout_started` carries `checkout_id`, the items (name, url, image_url, price, quantity) and `checkout_url`,
  with fill rates. A page reload that fires the event twice must share one `checkout_id`.
- `order_completed` arrives within minutes. If orders land hours late (a nightly sync), people who bought get "you
  left something behind": measure the lag before launch.
- The share of checkouts with an email on the profile: anonymous checkouts cannot be mailed.
- The history SQL's baseline: many abandoners order within a day on their own.

## Who should not get it

Beyond the suppressions every plan carries (`engine/policy/guardrails.py`):
- anyone who ordered by any route since (phone, store, another device): check every order source feeds `orders`;
- declined payments at checkout: that is a billing problem for support or dunning, not abandonment;
- people in the dunning flow (it outranks this one in the frequency policy);
- SMS without SMS consent; SMS sends only inside the permitted SMS window and the daily SMS cap.

## What the copy must not claim

- Low stock, "reserved for you", "your cart expires" or "selling fast" unless inventory data says so for that item.
- A discount unless `offer_policy` lists one; by default no offer in the first touch (`never_in_first_touch`), and a
  cart discount trains people to abandon (check it later with `lcm discounts`).
- A price: prices on the event may have changed; show them as the cart's, or not at all.
- Count every SMS on its worst-case rendering with `lcm sms`.

## Reading the result

Orders within 7 days, treatment against the holdout. Attributed revenue overstates cart flows heavily because
intent was already high: report attributed, incremental and the gap (`lcm readout`). If touch 3 adds under a tenth of
touch 1's orders while adding unsubscribes, drop to two touches.

## Output

Lead with the plan's status and its "Confirm before you build" list, then the decisions above (exclusions, claims
allowed, data checks with results), the copy (fallbacks per `prompts/core/syntax_rules.md`) and the read date.
