# Worked example: warming a new dedicated IP and subdomain (fictional retailer)

A model answer for `prompts/jobs/deliverability/warmup_schedule_generator.md`. Read it only when a model answer would help.

A new dedicated IP and sending subdomain for a fictional retailer, gate GREEN, target 40,000 a day,
tiers of 30,000 (30 days), 45,000 (31 to 90), 50,000 (91 to 180) and 220,000 unengaged, three sends
per person per week:

```text
lcm warmup --target 40000 --tiers '{"engaged_30d": 30000, "engaged_90d": 45000, "engaged_180d": 50000, "unengaged": 220000}' --gate GREEN --provider-mix gmail=0.5,microsoft=0.25,yahoo=0.1,other=0.15 --markdown
```

Capacity: the 30-day tier supports 12,857 sends a day, the 90-day tier 19,285 more, the 180-day
tier 21,428 more (53,570 in total), so the 40,000 target is warmable. The ladder:

| Day | Volume | Step | Tiers mailed | Checkpoint |
|---|---|---|---|---|
| 1 to 2 | 500 | 1 | engaged_30d | day 2 |
| 3 to 4 | 1,000 | 2 | engaged_30d | day 4 |
| 5 to 6 | 2,000 | 3 | engaged_30d | day 6 |
| 7 to 8 | 4,000 | 4 | engaged_30d | day 8 |
| 9 to 10 | 8,000 | 5 | engaged_30d | day 10 |
| 11 to 13 | 16,000 | 6 | engaged_30d, engaged_90d | day 13 |
| 14 to 16 | 32,000 | 7 | engaged_30d, engaged_90d | day 16 |
| 17 onward | 40,000 | 8 | engaged_30d, engaged_90d, engaged_180d | hold and observe |

Steps lengthen from two days to three once the ladder reaches older tiers, because risk rises as
engagement falls. The 220,000 unengaged contacts are excluded; they get one re-permission send from
a separate subdomain after the ladder completes, and everyone who does not opt in is suppressed.
