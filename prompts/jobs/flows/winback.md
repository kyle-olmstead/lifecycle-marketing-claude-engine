---
id: lmm.flows.winback
version: 3.0.0
inputs: [business_model, transaction_object, repeat_window, rfm_segments, platform_stack, lapsed_volume, baseline_repurchase, offer_policy, consent_state, gate_verdict]
tools: [lcm plan, lcm size, lcm lint, lcm readout]
---

# Win-back, reactivation and sunset

For customers who stopped. Churn is an absence, invisible in dashboards, and win-back is the most over-credited flow
in lifecycle marketing: some of the people it fires on were coming back anyway. So the holdout is not optional, the
discount is not the default, and the flow ends in an honest sunset, not an endless drip.

**The deterministic plan comes from the engine**: `lcm plan --text "<the request>" --data <exports>` (entry after
`churn_threshold_days` without the core action, from the person's own last one; roles value_reminder,
friction_removal, whats_new, social_proof, repermission and more up to 8; days 0, 3, 7, 12, 18; one entry per lapse
through the ledger `lcm_flow_entries`, queued by the daily job; the 15% holdout sized). A standalone re-permission
flow is the sunset flow (`prompts/jobs/flows/sunset.md`). A model answer: `prompts/examples/winback_northfield.md`;
read it only when one would help.

## Protocol

1. **Gate.** Win-back mails the least engaged customers: on AMBER only those who clicked or replied in 90 days (the
   plan applies it); on RED it becomes suppression and re-permission design.
2. **Define lapsed from the customer's own cycle**, never a round number: the repeat window from the RFM job (75th
   percentile of first-to-second-order gaps), and for repeat customers `overdue_vs_own_cycle` (recency beyond twice
   their own median gap). In episodic businesses (buyers return when an outside event happens) win-back is the wrong
   flow: detect the event instead.
3. **Build the absence robustly**: trigger on the last transaction and wait the window with an every-step filter that
   removes anyone who transacts, or enter from a dynamic "lapsed past cycle" segment with re-entry stated.
   Segment-triggered entry with re-entry on is a runaway-send cause.
4. **Branch by value, then reason, then channel.** `cannot_lose` and `champions`: a plain-text note from a named
   person, no offer (a discount to your best customers teaches them to wait). Reason, where known
   (`subscription_cancelled.reason`, support history): price leavers get a price path, missing-feature leavers the
   feature news, not-using leavers the shortest path back to value. SMS and push only with their own consent and only
   for people who engaged on them.
5. **Offers**: none in the first message. An offer is a message-level A/B test with a no-offer arm, read as cost per
   incremental order, not attributed revenue. The workspace's `offer_policy` decides what exists.
6. **Sunset**: the last message is an honest re-permission ask; no click within the window: suppress from marketing
   (keep the record, never delete). Expect 1 to 5% to opt back in, and say so before it sends.
7. **Holdout**: `lcm size --baseline <rate> --mde-abs <lift> --holdout 0.15 --entries-per-month <n>
   --outcome-window-days <window>`; the read date is the months of entries plus the outcome window (at least the
   median inter-purchase interval). Moderate entry volume may need 20 to 25% to read inside two quarters: show it.
8. **Write and check** the artifacts: lint every template (`lcm lint`), validate the flow JSON, `lcm simulate`.

## The absence on each platform

| Platform | Robust pattern | Watch for |
|---|---|---|
| Klaviyo | metric trigger (Placed Order), time delay = window, flow filter "Placed Order zero times since starting this flow" | flow filters apply every step, trigger filters at entry; Smart Sending can silently skip a message |
| Customer.io | event-triggered campaign on the order event, wait the window, True/False branch on "performed order since entering"; or segment-triggered with re-entry off | segment-triggered campaigns with re-entry on |
| Braze | Canvas with an Action Paths step ("made a purchase" within the window) after a delay, or segment entry with re-eligibility set deliberately | re-eligibility shorter than the flow |
| HubSpot | workflow on the last order or invoice date property, delay until date plus window, if/then on "last order date is after enrollment" | re-enrollment is off by default; deals do not record revenue in services businesses |
| Salesforce Marketing Cloud | Journey entry from a data extension refreshed by Automation Studio with a recency query; Decision Split on "ordered since entry" | the journey is only as fresh as the automation feeding it |

## Output format

1. Gate check. 2. Absence definition (window, source, the platform pattern). 3. Branch map. 4. FLOW block. 5. Flow
spec JSON (`json flow_spec`). 6. Offer policy and test. 7. Sunset rule. 8. Templates in the platform dialect
(fallbacks per `prompts/core/syntax_rules.md`). 9. Measurement plan with holdout sizing. 10. Checks run.

## Guardrails

- Lapsed is the customer's cycle, never a round number; no discount to `cannot_lose` or `champions` by default.
- No win-back without a holdout unless volume makes one unreadable, and then a labeled baseline.
- An every-step filter removes anyone who transacts: a win-back email after a purchase is the classic collision.
- The flow ends in a sunset. Suppress, never delete.
