# Worked example: a test backlog for a second-order flow (fictional retailer)

A model answer for `prompts/jobs/measurement/test_hypothesis_matrix.md`. Read it only when a model answer would help.

| ID | Hypothesis (short) | Metric | Baseline | Effect | Sample (tool) | Weeks | Readable | Priority |
|---|---|---|---|---|---|---|---|---|
| SO-1 | item name in subject raises clicks | click rate | 3.1% | +15% rel. | 23,387 per arm | 5.2 at 9,000 a week | yes | high |
| SO-2 | person as sender raises replies | reply rate | 0.4% | +50% rel. | 19,523 per arm | 4.3 at 9,000 a week | yes | medium |
| SO-3 | free-shipping offer raises second orders | second-order rate in 60 days | 4.0% | +1.5 pts | 3,156 per arm at 50/50 | 0.7 to enter, plus the 60-day window | yes; costs margin | medium: report cost per incremental order |
| SO-4 | emoji in subject raises opens | open rate | n/a | n/a | n/a | n/a | no: open rate is not a valid metric | dropped |

SO-4 is in the table on purpose. Writing down why an idea was dropped stops it from coming back
every quarter.
