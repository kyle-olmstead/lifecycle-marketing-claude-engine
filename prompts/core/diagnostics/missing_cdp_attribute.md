# Diagnostic: a missing CDP attribute or event

Read when a trigger, branch, filter or token depends on an attribute or event that is absent, sparse or stale.

1. **Detect**: not in the workspace's schema, fill rate under 60 percent (segmentation) or 90 percent (automation
   branching), or a stale last-updated distribution. `lcm check-data <folder>` and `lcm tracking --data <folder>`
   measure the last two on exports.
2. **Classify the cause**: never collected; collected but not forwarded by the CDP; forwarded but type-mismatched
   (silently dropped); renamed upstream; sparse because only one source writes it.
3. **Name the blast radius**: every flow step, segment and token that depends on it.
4. **Choose the fallback, in this order**: derive it in the warehouse from data that exists; ship the step switched
   off and marked BLOCKED; render a safe fallback in copy (never an invented value).
5. **Write the instrumentation spec**: the event or attribute as a JSON Schema addition to the tracking plan
   (`config/tracking_plan.json` shows the shape), its source, its owner, and the flow it unlocks.
