---
id: lmm.flows.replenishment
version: 3.0.0
tools: [lcm plan, lcm size, lcm lint, lcm events]
---

# Replenishment flow

Buyers of a consumable hear from you just before it runs out.

**The deterministic plan comes from the engine**: `lcm plan --text "a replenishment flow for <product> in <platform>"
--data <exports>` writes it from `engine/flows/library.py` and `engine/flows/sql.py`. Entry: a purchase of a product in
scope whose supply (`products.replenish_after_days`, else measured from repeat orders, else the brief's
`replenish_after_days`) less `replenish_lead_days` (default 5) has passed, with no repeat purchase of that product.
Goal: a reorder (`order_completed`). Touches by role: replenish_reminder, replenish_last (default 2; 3 adds
replenish_nudge), at the due date and 5 days later (12 for a third). One entry per purchase cycle (`user_id +
anchor_date`), re-entry after exit; holdout 10%; outcome window 14 days. Needs `orders` and `line_items`. The warehouse
triggers it: the daily job queues who is due into the ledger `lcm_flow_entries`, and `lcm events` pushes the pending
rows; on Klaviyo the plan can build it natively on Ordered Product with a delay. Use this prompt for judgment.

## Check the data first

- `line_items` with `product_id` on every completed order, and which products are consumables at all.
- The supply interval: from the catalog, or measured from repeat orders (label it ESTIMATE, with the number of repeat
  buyers it rests on). A product with few repeat buyers has no measured cycle.
- Quantity: the cycle is per purchase, so a two-pack bought once looks due after one unit's supply. Set the interval
  for multi-unit orders, or exclude them.
- Variants: the match is on `product_id`, so someone who switched size or scent looks like they never reordered.

## Who should not get it

Beyond the suppressions every plan carries (`engine/policy/guardrails.py`):
- active subscribers to the product (subscribe and save): they reorder automatically;
- gift purchases and one-off buyers of a non-consumable that shares a catalog category;
- anyone who reordered a variant of the same product.

## What the copy must not claim

- "You are running low" as a fact: the due date is an estimate. Say "about now, if you use it like most people".
- Urgency or stock warnings; offers outside `offer_policy`, never in the first touch by default.
- The product name and link come from the item fields with fallbacks (`prompts/core/syntax_rules.md`).

## Reading the result

Reorders within 14 days of the due date, treatment against the holdout. Habitual buyers reorder anyway, so the
baseline is high and the incremental share is small: report attributed, incremental and the gap (`lcm readout`). If
most reorders land before the first reminder, the lead days are too short; if after the last, the interval is long.

## Output

Lead with the plan's status and its "Confirm before you build" list, then the decisions above (scope of products,
exclusions, claims allowed, data checks with results), the daily job, the copy and the read date.
