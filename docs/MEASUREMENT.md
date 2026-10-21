# Measurement: how the engine proves a lifecycle program works

This page is for two readers. The lifecycle manager needs to know which test to set up and when it can be
read. The CFO needs to know what each number means, how much to trust it, and what the engine will refuse
to say. Everything here is computed by deterministic code in `engine/measurement/`; no number below is
estimated by a language model.

## The short version: which design, when

| Situation | Design | Read it with |
|---|---|---|
| A flow with steady volume, one decision at the end | Fixed-horizon persistent holdout (10 to 20%) | `stats.py readout` on the planned date, once |
| You will be asked "is it working yet?" every month | Group-sequential holdout, 3 to 5 planned looks | `sequential.py look` at each look |
| You want to check whenever you like, or stop early as soon as it is clear | Always-valid test (mSPRT) | `sequential.py msprt`, any time |
| B2B: whole accounts are held out | Any of the above, account-randomized, with a design effect | `cluster.py readout` |
| "What does lifecycle as a whole add?" | Global holdout: 5% of people held out of every flow | `global_holdout.py readout` |
| Volume too low to ever reach the floor | No statistical read | Report counts and say so |

Rules of thumb. Pick the design before the flow ships and register it. A fixed-horizon test is cheapest in
sample but may be read only once. With 3 to 5 looks, group-sequential costs about 1 to 2.5% more maximum
sample with O'Brien-Fleming spending (strict early looks) and about 17 to 21% more with Pocock spending (an
equal bar at every look), and it usually finishes early when the effect is real. Always-valid inference lets you look continuously, and pays
for it with wider intervals at any single moment.

## What each number means

- **Baseline**: the conversion rate people show with no flow at all, measured in the holdout.
- **MDE (minimum detectable effect)**: the smallest difference worth acting on. The test is sized to detect
  it 80% of the time; smaller real effects are often missed.
- **n per arm**: entrants needed in the holdout and in the treatment group. The engine never plans fewer
  than 30 entrants per arm, or fewer than enough for 5 expected conversions per arm at the baseline and at
  the treatment rate, because the readout refuses smaller arms. When that floor sets the size instead of
  power, the plan says `size_set_by: readout floor`, and `p_readable` gives the chance that both arms actually
  end with 5 or more conversions.
- **Design effect**: how much larger a sample must be when whole accounts are randomized. People at one
  company convert together, so 1,000 people in 100 accounts carry less information than 1,000 strangers. It
  is `1 + ((1 + cv^2) x mean account size - 1) x ICC`, where cv is the spread of account sizes.
- **ICC (intraclass correlation)**: how alike people in one account behave, from 0 (independent) to 1
  (everyone in an account does the same thing). Typical B2B values are 0.01 to 0.10. Estimate it from your
  own data with `cluster.py icc`; with fewer than 30 accounts the estimate is imprecise and the output says so.
- **Information fraction**: how far through a group-sequential test you are, as a share of the planned
  statistical information (roughly, the share of the planned holdout entrants reached).
- **Boundary**: the z value a group-sequential look must reach to stop for efficacy. Early boundaries are
  high (4.88 at the first of five O'Brien-Fleming looks), the last one is near the usual 1.96.
- **Repeated confidence interval**: the interval to report at a group-sequential look; it stays valid no
  matter which look you stop at.
- **Always-valid p-value and confidence sequence**: from the mSPRT. Both stay valid however often you check.
  The p-value only goes down over time and the interval only narrows.
- **Attributed, incremental, gap**: what the ESP credited to the flow, what the holdout shows the flow
  caused, and the difference. The engine reports all three together, every time.
- **SRM (sample ratio mismatch)**: a test that the split matches the plan. When accounts are randomized it
  counts accounts, not people. A mismatch means assignment or logging is broken.

## What the engine refuses to report

1. **No lift on a small sample.** Fewer than 30 entrants or 5 conversions in either arm, or fewer than 10
   accounts per arm in an account-level test: no lift percentage, only counts.
2. **No result through a broken split.** A sample ratio mismatch (p below 0.001) stops the read until it is fixed.
3. **No plan the readout cannot read.** Sizing is raised to the readout floor and says so.
4. **No "significant" from peeking.** A fixed-horizon test read before its date is flagged as peeking in the
   registry; a group-sequential look is only significant if it crosses its boundary.
5. **No independent-people math on accounts.** Account-randomized tests are read from account totals with a
   cluster-robust standard error and a t reference whose degrees of freedom follow the smaller arm.
6. **No point estimate dressed as the truth after an early stop.** The estimate at an early stop leans high;
   the engine says so and reports the repeated confidence interval.
7. **No program number from flow numbers.** Flow lifts do not add up to the program's effect; only the
   global holdout reads that.
8. **No revenue interval without the spread.** Incremental revenue gets an interval only when per-person
   revenue spread is known; otherwise it is labeled a point estimate.

## The experiment registry

Every experiment is registered before it starts in `registry/experiments.json` in the workspace, following
`schemas/experiment.schema.json`. An entry records the flow, the unit (user, account or household), the hash
field and salt, the holdout share, the primary metric and its window, the plan (baseline, MDE, n per arm,
design effect, read date), the stopping rule, the status and every look taken. An account-level entry must
hash `account_id` and carry a design effect. The registry check flags peeking, two running experiments that
share a salt on audiences not declared disjoint (the same people would sit in both holdouts), reuse of the
global holdout's salt, plans below the readout floor, extra group-sequential looks, and overdue reads.

```json
{
  "experiment_id": "winback_14d_holdout",
  "flow": "winback_14d",
  "unit": "user",
  "assignment": {"hash_field": "user_id", "salt": "winback_14d"},
  "holdout_share": 0.15,
  "primary_metric": {"event": "order_completed", "window_days": 60},
  "plan": {"baseline": 0.10, "mde_abs": 0.02, "n_per_arm": {"holdout": 3000, "treatment": 17000}, "design_effect": 1.0, "read_on": "2027-05-10"},
  "stopping_rule": {"design": "group_sequential", "looks": 3, "information_fractions": [0.33, 0.67, 1.0], "spending": "obf"},
  "status": "running",
  "launched_on": "2026-11-02",
  "looks_taken": []
}
```

## The global holdout

A separate salt (`lmm_global`) and share (5% by default) define people who enter no flow at all. Every flow's
entry query adds `global_holdout.entry_filter(...)`, and because the salt differs from every flow's salt, each
flow's own holdout is drawn independently inside the remaining 95%. The program readout compares all
lifecycle-eligible people outside the global holdout with those inside it, over the same period, with the same
refusals as a flow readout. It answers "what does the program add over sending no lifecycle messages?"; it does
not say which flow did the work. Start the period after the global holdout went live.

## Discount dependency

Per customer, the engine looks at the last 3 completed orders and flags `discount_share_over_50pct_last_3_orders`
when more than half used a discount (a discount amount above zero, or a code in the discounts table), with at
least 2 orders so that one welcome code does not mark someone for life. The offer policy leaves flagged people
out of offers, because a code sent to them mostly pays for an order that was coming anyway. Program-wide, it
reports the share of revenue on discounted orders by month and a trend over complete months that have at least
20 orders. The warehouse version of the per-customer flag:

```sql postgres
WITH discount_orders AS (
  SELECT DISTINCT order_id FROM discounts WHERE code IS NOT NULL AND TRIM(code) <> ''
),
completed AS (
  SELECT o.user_id, o.order_id, o.order_at,
         CASE WHEN COALESCE(o.discount_amount, 0) > 0 OR d.order_id IS NOT NULL THEN 1 ELSE 0 END AS discounted,
         ROW_NUMBER() OVER (PARTITION BY o.user_id ORDER BY o.order_at DESC, o.order_id DESC) AS rn
  FROM orders o
  LEFT JOIN discount_orders d ON d.order_id = o.order_id
  WHERE LOWER(o.status) = 'completed'
)
SELECT user_id,
       COUNT(*) AS orders_considered,
       SUM(discounted) AS discounted_orders,
       CAST(SUM(discounted) AS DOUBLE PRECISION) / COUNT(*) AS discount_share_last_3,
       CASE WHEN COUNT(*) >= 2 AND CAST(SUM(discounted) AS DOUBLE PRECISION) / COUNT(*) > 0.5 THEN 1 ELSE 0 END
         AS discount_share_over_50pct_last_3_orders
FROM completed
WHERE rn <= 3
GROUP BY user_id
ORDER BY user_id
```

## Commands

    python3 engine/measurement/stats.py feasible --entries-per-month 1200 --holdout 0.10 --baseline 0.04 --mde-rel 0.20
    python3 engine/measurement/cluster.py size --baseline 0.10 --mde-abs 0.03 --icc 0.05 --mean-size 8 --cv 0.6 --holdout 0.20
    python3 engine/measurement/sequential.py plan --baseline 0.10 --mde-abs 0.02 --looks 4 --spending obf --holdout 0.20
    python3 engine/measurement/sequential.py msprt --xt 600 --nt 5000 --xc 100 --nc 1000 --mde-abs 0.02
    python3 engine/measurement/registry.py check
    python3 engine/measurement/global_holdout.py readout --data <folder> --start 2026-04-01 --end 2026-10-01 --markdown
    python3 engine/measurement/discounts.py report --data <folder>

## How the numbers were checked

The group-sequential boundaries reproduce the published Lan-DeMets values (4.877, 3.357, 2.680, 2.290, 2.031
for five O'Brien-Fleming looks; about 2.44 to 2.39 for Pocock), the classic Pocock and O'Brien-Fleming
constants and inflation factors in Jennison and Turnbull's Tables 2.1 to 2.4, and a seeded Monte Carlo type I
error of 0.05. The mSPRT keeps its false-positive rate at or below 5% when checked 150 times, where the ordinary
readout checked as often exceeds 20%. The ICC estimator recovers known ICCs from simulated accounts, the design
effect matches the simulated variance of the pooled rate, and the account-level readout holds a 5% error rate
where the naive test runs near 20%. Sources are cited in each module's docstring.
