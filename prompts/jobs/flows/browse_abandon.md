---
id: lmm.flows.browse_abandon
version: 3.0.0
tools: [lcm plan, lcm size, lcm lint, lcm simulate]
---

# Browse abandonment flow

Identified visitors who viewed a product and left get one reminder of it.

**The deterministic plan comes from the engine**: `lcm plan --text "a browse abandonment flow in <platform>" --data
<exports>` writes it from `engine/flows/library.py`. Trigger `product_viewed` by an identified person with no
`order_completed`, `checkout_started` or `product_added` within 2 hours (those belong to the cart flow); goal
`order_completed`. Touches by role: browse_reminder, browse_alternatives (default 2; 3 adds social_proof), at entry and
22 hours later (70 for a third), sent inside the permitted send window. Re-entry at most once per 14 days, deduped on
`user_id + product_id + day`; holdout 10%; outcome window 7 days. Stores only. On an AMBER gate the view counts as
engagement; anyone with no click, reply, order or session in 365 days is still excluded. On Klaviyo the flow also
skips anyone in the abandoned cart flow in the last 7 days. Use this prompt for judgment.

**The product in the email** (`engine/render/product_blocks.py`): Klaviyo reads Viewed Product's `event.Name`,
`event.URL`, `event.ImageURL` and `event.Price`; Customer.io and Braze read the one viewed product from the
`product_viewed` event itself (name, url, image_url, price, each with a fallback); HubSpot and Salesforce Marketing
Cloud show one item from synced fields.

## Check the data first

- How visitors become identified: a cookie matched to a profile after a click or a login. A view with no profile
  cannot enter, so the reachable share may be small; measure it.
- Bot and crawler views, and internal staff browsing: filter them before they inflate entry.
- `product_id` and the product URL on the event, and whether the product is still in stock.
- The history SQL's baseline: orders within 7 days of a view with no flow.

## Who should not get it

Beyond the suppressions every plan carries (`engine/policy/guardrails.py`):
- anyone who started checkout, added to cart or ordered (excluded by the trigger) or is in the cart flow;
- people who viewed in the 14-day cooldown already: one reminder per product per fortnight;
- recipients with no consent for the channel; browse reminders are the lowest-intent flow and are never SMS by
  default.

## What the copy must not claim

- Surveillance details ("you spent five minutes looking at..."): show the product, not the tracking.
- "Price dropped" without price history; "low stock" without inventory data; no deadline.
- A discount unless `offer_policy` lists one; never in the first touch by default.

## Reading the result

Orders within 7 days, treatment against the holdout. Lifts are usually small and unsubscribes come early: if the
unsubscribe rate per send is high against the incremental orders (`lcm readout`), cut to one touch or narrow entry
to repeat viewers.

## Output

Lead with the plan's status and its "Confirm before you build" list, then the decisions above (exclusions, claims
allowed, data checks with results), the copy (fallbacks per `prompts/core/syntax_rules.md`) and the read date.
