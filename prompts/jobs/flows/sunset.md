---
id: lmm.flows.sunset
version: 3.0.0
tools: [lcm plan, lcm engagement, lcm deliverability, lcm events]
---

# Sunset (re-permission) flow

People who stopped engaging are asked once whether to stay, then stop being mailed.

**The deterministic plan comes from the engine**: `lcm plan --text "a sunset flow for people who have not clicked in
180 days" --data <exports>` writes it from `engine/flows/library.py` and `engine/flows/sql.py`. Entry: at least 5 email
deliveries and no click or reply in `churn_threshold_days` (default 180 for this flow), not already sunset-suppressed;
goal: a click to stay subscribed. Touches by role: repermission, repermission_reminder (default and maximum 2) at entry
and 7 days later. Once per person; the daily job queues entrants into the ledger `lcm_flow_entries` and `lcm events`
pushes them. No holdout (0%): this is list hygiene, read from the sending gate's unengaged share and complaint rate,
not a lift. On a RED gate it is the one marketing send the method permits: from a separate subdomain, in small daily
batches, stopping at the first complaint spike; on AMBER, unengaged recipients in small daily batches. `lcm
engagement` builds the tiers and the sunset list from clicks. Use this prompt for judgment.

## The rules every plan applies (`engine/policy/guardrails.py`)

One re-permission ask per person per 180 days, across all flows. No click on the ask within 14 days: set
`lcm_sunset = true` and stop marketing mail; keep the record, never delete it. A sunset-suppressed person comes back
only through an explicit opt-in, never through a flow. The sunset flag is an attribute to exclude, not an
unsubscribe.

## Check the data first

- Clicks, never opens: privacy proxies open everything. But security scanners at company gateways click every link
  seconds after delivery; treat clicks within a minute of delivery as suspect before calling someone engaged.
- Click tracking works on every template and platform (a broken redirect makes everyone look unengaged).
- How many would enter: a first run on an old list can be large; plan the daily batches before the gate allows it.

## Who should not get it

Beyond the suppressions every plan carries:
- customers who bought or used the product recently but do not click email: unengaged with email is not
  disengaged from the business; exclude anyone with an order or session in the threshold, or say why not;
- transactional-only recipients and anyone who received a re-permission ask in the last 180 days.

## What the copy must not claim

- Anything but the truth: the question plainly ("Still want emails from us?"), what happens without a click and when
  (marketing mail stops after the 14-day window), and that they can come back.
- No guilt, no "last chance" beyond that real stop date; the stay link works in one click.

## Reading the result

Expect 1 to 5% to click to stay, and say so before it sends so the number is not read as failure. The outcome is the
gate: compare the unengaged share and complaint rate before and after (`lcm deliverability check <domain>`, `lcm
engagement`), not revenue.

## Output

Lead with the plan's status and its "Confirm before you build" list, then the entry size, the batching plan under
the current gate, the exclusions, the two messages (fallbacks per `prompts/core/syntax_rules.md`) and the gate
read-back date.
