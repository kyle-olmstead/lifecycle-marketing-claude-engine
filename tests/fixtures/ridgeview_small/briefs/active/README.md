# Active briefs

> **In plain English.** Put a copy of each live campaign's brief here, and the engine checks that
> campaign's audience every morning.

Put the brief of every flow that is live, or about to be, in this folder (one `.yaml` file per flow,
in the shape of `templates/brief.yaml`). Two things read it:

- The daily job (`engine/workflows/daily.py`, 05:00 UTC in `.github/workflows/daily_cohort_audit.yml`) audits
  the cohort of every brief here and opens one GitHub issue per flow that shows an anomaly. With this
  folder empty it audits `examples/brands/ridgeview_saas/briefs/winback_14d.yaml`, so the workflow still shows what it does.
- Real-time forwarding in `adapters/sources/webhooks.py` (off unless `LCM_FORWARD=1`) sends each
  activation brief's qualifying event on to its platform with the holdout assignment added.

`lcm "audit the cohort" --data exports/` also picks the brief from here when there is exactly one.

To start: `lcm "plan a 5-touch win-back for users inactive 14 days" --product Acme`, review the brief
it writes, then copy that brief here.
