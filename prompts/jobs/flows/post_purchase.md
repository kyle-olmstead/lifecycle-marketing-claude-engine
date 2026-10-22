---
id: lmm.flows.post_purchase
version: 3.0.0
tools: [lcm plan, lcm size, lcm lint, lcm simulate]
---

# Post-purchase flow

First-time buyers get the most from what they bought, then a reason to come back.

**The deterministic plan comes from the engine**: `lcm plan --text "a post-purchase flow for first-time buyers in
<platform>" --data <exports>` writes it from `engine/flows/library.py`. Trigger: the person's first completed order
(orders over all time = 1, not refunded or canceled); goal: a second `order_completed`. Touches by role: order_thanks,
how_to_use, review_request, cross_sell (default and maximum 4; 3 drops the review request; 2 is thanks and cross-sell)
on days 1, 7, 14 and 30 after the order. Re-entry never, first order only; holdout 10%; outcome window 30 days. Stores
only; needs `orders`. The purchased items fill the product block per platform (`engine/render/product_blocks.py`;
HubSpot and Salesforce Marketing Cloud show one synced item). Use this prompt for judgment.

## Check the data first

- `orders` is the transaction object: completed and paid, with refunds and cancellations marked.
- Delivery time. The schedule counts from the order, not from delivery: if shipping takes a week, the day-1 and day-7
  touches arrive before the parcel, so a "how is it holding up" message must wait for a fulfillment event or move.
- Line items for the cross-sell, and which products are complements of which (the copy needs a real pairing).
- The review tool and whether it already sends its own request (two review asks is one too many).
- The baseline: the share of first-time buyers who order again within the window with no flow (the plan's history
  SQL).

## Who should not get it

Beyond the suppressions every plan carries (`engine/policy/guardrails.py`):
- refunded or canceled first orders, and anyone with an open complaint or return (no review request, no cross-sell);
- gift orders where the buyer is not the user; wholesale and B2B accounts on retail flows;
- returning customers: the flow is the first order only.

## What the copy must not claim

- This flow is marketing, not a receipt: it carries the unsubscribe and consent rules, and must not dress up as an
  order confirmation or a shipping notice, or state delivery dates the platform does not know.
- Reviews: no incentive tied to a positive review, and no filtering of who is asked by expected sentiment (the FTC's
  consumer-review rule bars both in the US; have the user confirm with counsel elsewhere).
- Cross-sell is a complement, not an upsell; no discount in the first touch by default, and only offers in
  `offer_policy`.

## Reading the result

Second-order rate within the window, treatment against the holdout, on `orders`. Second orders are slow for many
catalogs: if the median gap from first to second order (the RFM job's repeat window) is longer than the window, say
the read is early. The review request is read on review rate, not revenue.

## Output

Lead with the plan's status and its "Confirm before you build" list, then the decisions above (exclusions, claims
allowed, data checks with results), the copy (fallbacks per `prompts/core/syntax_rules.md`) and the read date.
