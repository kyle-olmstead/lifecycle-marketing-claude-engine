# Worked example: reading a win-back holdout

A model answer for `prompts/jobs/measurement/holdout_incrementality.md`: `lcm readout examples/stats/winback_readout.json`
turned into the report and the client line. Read it only when a model answer would help.

`examples/stats/winback_readout.json`: the win-back flow from `prompts/examples/winback_northfield.md`, four months of entries
with a 20% persistent holdout, outcomes on the Shopify orders object.

```text
Win-back: lapsed past own cycle | entries 2026-04-01 to 2026-07-31, outcomes through 2026-09-29

  Entered:        10,200 (treatment 8,160 / holdout 2,040)
  Conversion:     5.05% vs 4.02%; difference +1.03 pts, 95% CI -0.02 to +1.95 pts; not distinguishable from zero at 95%
  Attributed:     $41,900   (Klaviyo flow attribution, 5-day click window)
  Incremental:    $15,880   (point estimate: revenue per entrant, treatment minus holdout, times treatment entrants; supply revenue_sd per arm for an interval)
  Gap:            $26,020

  Read:           causal, inconclusive
  Window:         60 days, chosen because the median inter-purchase interval for returning customers is 41 days, so 60 days covers a normal return
  Caveats:        a site-wide sale ran 2026-07-14 to 2026-07-20 and reached both arms equally (campaign sends were not suppressed for the holdout)
  Sample ratio:   chi2 0.0, p=1: split matches the planned allocation
```

How to say it to a client, in one line: "Klaviyo credits the win-back flow with $41,900; against
the holdout, the best estimate of what it actually added is about $15,900, and four months of data
cannot yet rule out zero. We keep it running and read again at the end of Q4."

What this does not license: calling the flow a $41,900 flow, calling it a failure, or adding a
discount to "boost" a number that has not yet been measured cleanly.
