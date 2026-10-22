# Worked example: sizing a subject-line test

A model answer for `prompts/jobs/measurement/ab_power_calculator.md`. Read it only when a model answer would help.

A subject-line test on a lifecycle email with a 3.1% click rate (bot-excluded, last 90 days), where
the team would act on a 15% relative improvement (to 3.57%):

```text
lcm size --baseline 0.031 --mde-rel 0.15
```

Result: 23,387 sends per arm, 46,774 in total. At 9,000 eligible sends a week that is about five
weeks, read once at the end. Conversely, with 4,000 sends per arm the smallest detectable change is
1.18 points (a 38% relative lift, from 3.1% to about 4.3%), which a subject line almost never
produces. The honest recommendation at that volume: test a structural change (a different offer or
a person as the sender), or accumulate the test across five weekly sends with the same rule.
