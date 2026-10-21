# The question bank

> **In plain English.** These are the questions a lifecycle marketing manager asks every day, week, month, quarter
> and year, and what the engine does with each. Ask any of them in a Claude conversation with the plugin installed.
> The ones with an `lcm` command also run in a terminal: type the command shown. Each one lists what you get, what
> data it needs, and what it cannot answer, so a partial answer is never mistaken for a whole one. The single most
> useful one is the first: *what are my top 3 priority builds today and why?* The morning check answers it every day
> without being asked.

25 questions. The plain version is how you would say it; the expert version is the same question at full depth.
In Claude either works. After `lcm`, type the plain version: the terminal router is rules, not a model, and CI checks
that every plain question with an `lcm` command reaches the job named here. Questions marked *the ... skill in Claude*
have no terminal job; `lcm` says so instead of guessing. This page is written from `config/question_bank.yaml`
(`python3 tools/question_bank.py --write`).

| Cadence | Questions |
|---|---|
| [Daily](#daily) | What are my top 3 priority builds today and why?; What broke overnight?; Is anything threatening our inbox placement today?; Are our events firing correctly, or did a release break a trigger?; Any anomalies in our flow audiences today? |
| [Weekly](#weekly) | Is my test ready to call, or do I need more data?; Where are new users dropping off in onboarding this week?; Who should we stop emailing?; How many people do we need before this test can read?; Which flows are underperforming their holdout? |
| [Monthly](#monthly) | When do customers stop using the product, and does it differ by plan?; How healthy is our list, and is the unengaged share growing?; Is our monthly newsletter worth keeping, or should a trigger replace it?; What does our LTV curve look like by signup cohort?; Which tests should we run next? |
| [Quarterly](#quarterly) | What did our flows actually add this quarter, versus what the platform credits?; Does our tracking plan still match the product, or has the data drifted?; Is our DMARC policy ready to move to quarantine or reject?; Do our RFM segments still describe our customers?; Which flows should we retire? |
| [Annual](#annual) | What is our lifecycle program worth next year?; Can our stack support real-time triggers, or is latency holding us back?; Are our lifecycle programs raising LTV enough to lower blended CAC?; What does a safe sending plan look like for a new domain?; Which compliance questions are open across our flows? |

## Daily

### What are my top 3 priority builds today and why?

*Expert version:* Which flows, ranked by measured audience, baseline and value, would add the most incremental revenue per hour of build effort, and is anything capping all of them?

- **How:** `lcm` job `priorities`: `lcm "what are my top 3 priority builds today and why?" --data exports/`
- **You get:** Fixes that cap everything else first, then the top three builds, each with the evidence, a monthly value range with its assumptions, confidence, effort, and the next step (a ready brief and one command for win-back and activation builds).
- **Needs:** users, orders and track_events exports; message_events for the list check and the newsletter timing; LCM_DOMAINS (or --domain) for the domain check
- **Cannot answer:** The lift is assumed (10 to 25 percent relative by default, changeable); only a holdout turns the estimate into a result.

### What broke overnight?

*Expert version:* Did any flow's audience, any sending domain or the list's engagement move outside its normal range since yesterday?

- **How:** `lcm` job `daily`: `lcm "what broke overnight?" --data exports/`
- **You get:** The morning check, run now or read from 05:00 UTC; priorities, tracking health, cohort anomalies, engagement and every domain's sending gate.
- **Needs:** the same exports; LCM_DOMAINS for the domain checks
- **Cannot answer:** It sees what your data and DNS show; a provider-side block without bounces shows up only as a drop in engagement.

### Is anything threatening our inbox placement today?

*Expert version:* Are Google Postmaster domain reputation, user-reported spam rate, authentication and list engagement all inside their thresholds, or is something drifting toward the spam folder?

- **How:** `lcm` job `deliverability`: `lcm "check deliverability for example.com"`
- **You get:** A GREEN, AMBER or RED sending gate with the records looked up live, the bulk-sender checks, and the exact DNS fixes.
- **Needs:** the sending domain; Postmaster and bounce numbers, pasted or exported, sharpen it
- **Cannot answer:** Google Postmaster Tools and Microsoft SNDS have no Claude connector, and no provider reports inbox placement per flow; seed tests measure placement, this measures the causes.

### Are our events firing correctly, or did a release break a trigger?

*Expert version:* Is every event in the tracking plan arriving at its usual volume with its required properties, and is any live flow depending on one that stopped?

- **How:** `lcm` job `tracking`: `lcm "are our events firing correctly?" --data exports/`
- **You get:** Each tracked event's last 7 days against its usual week, a stalled feed, required properties going missing, events arriving off the plan, each live flow whose exact trigger or core action (with its property filters) has stopped, and the webhook receiver's quarantine reasons.
- **Needs:** track_events; the receiver's folder for quarantine counts
- **Cannot answer:** It checks shape and volume, not whether an event fires at the right moment in the product.

### Any anomalies in our flow audiences today?

*Expert version:* For each live flow, did signups, active users, core-action users, orders or threshold crossings move more than 3.5 robust standard deviations from their 28-day median?

- **How:** `lcm` job `audit`: `lcm "any anomalies in our flow audiences today?" --data exports/`
- **You get:** Each metric against its 28-day median with a robust z-score, the risk bands, and the largest funnel leak.
- **Needs:** users, orders, track_events; one brief in briefs/active/, or --brief when there are none or several
- **Cannot answer:** Small series (a median under 5) are not judged, so quiet flows stay quiet.


## Weekly

### Is my test ready to call, or do I need more data?

*Expert version:* Do the arms differ beyond chance at the planned alpha with the planned power, is the split still what we set, and are we about to fall for an early peek?

- **How:** `lcm` job `readout`: `lcm "is my test ready to call? results in results/subject_test.json"`
- **You get:** The difference with its interval, the read label (causal, inconclusive, directional), a sample-ratio check, and the gap between credited and true revenue.
- **Needs:** each arm's size and conversions (a short JSON file)
- **Cannot answer:** Power is decided before a test, not checked after; subject-line tests should be read on clicks, never opens.

### Where are new users dropping off in onboarding this week?

*Expert version:* At which onboarding step does this cohort's funnel lose the most people, and which trigger should have bridged signup to first value?

- **How:** `lcm` job `audit`: `lcm "where are new users dropping off in onboarding?" --data exports/`
- **You get:** The onboarding funnel with each step's share of signups and the largest leak; plan an activation flow aimed at it from the priorities list.
- **Needs:** onboarding_step_completed events with step_index; one brief in briefs/active/, or --brief when there are none or several
- **Cannot answer:** Which message failed needs per-flow send data from the ESP.

### Who should we stop emailing?

*Expert version:* Which subscribers have received five or more emails in six months and clicked none, and what does sunsetting them do to the sending gate?

- **How:** `lcm` job `engagement`: `lcm "who should we stop emailing?" --data exports/ --platform klaviyo`
- **You get:** Engagement tiers, the sunset list as a reversible attribute, and the request files to sync it to your ESP.
- **Needs:** users and message_events
- **Cannot answer:** Opens never count (privacy proxies open mail automatically), so people who only open are counted as unengaged.

### How many people do we need before this test can read?

*Expert version:* What sample size per arm detects the smallest lift worth acting on at 5 percent alpha and 80 percent power, and how long does our entry volume take to reach it?

- **How:** `lcm` job `size`: `lcm "how many people do we need to detect a 20% lift on a 10% baseline with a 15% holdout"`
- **You get:** People per arm, and with a monthly entry volume, the months until the test can read.
- **Needs:** the baseline rate and the lift worth detecting
- **Cannot answer:** It sizes a test; it does not say which test is worth running.

### Which flows are underperforming their holdout?

*Expert version:* For each flow with a persistent holdout, is the treatment arm beating the holdout on the primary metric, and by how much once the platform's attribution is set aside?

- **How:** `lcm` job `readout`: `lcm "read the holdout results in results/winback_q3.json"`
- **You get:** Credited, true lift and the gap per flow, each read labeled.
- **Needs:** arm sizes and conversions per flow
- **Cannot answer:** A flow with too few entrants gets "directional", not a verdict.


## Monthly

### When do customers stop using the product, and does it differ by plan?

*Expert version:* At what week after signup does our retention curve cliff or flatten for each plan tier, and where must a flow land to get ahead of it?

- **How:** `lcm` job `retention`: `lcm "when do customers stop using the product, by plan?" --data exports/`
- **You get:** The weekly retention curve on your core action, the cliff (only when it is bigger than chance) and the floor, per segment.
- **Needs:** users (with plan) and track_events
- **Cannot answer:** Which behavior predicts the cliff is a modeling question; the curve shows where to look, not why.

### How healthy is our list, and is the unengaged share growing?

*Expert version:* What share of mailable subscribers is unengaged on a click basis, how is it trending, and is it pushing the sending gate toward AMBER or RED?

- **How:** `lcm` job `engagement`: `lcm "how healthy is our list?" --data exports/`
- **You get:** Tier counts and the unengaged share with its gate band; the daily Engagement tab gives the trend.
- **Needs:** users and message_events
- **Cannot answer:** The trend needs the daily check running; one run is one point.

### Is our monthly newsletter worth keeping, or should a trigger replace it?

*Expert version:* What share of each calendar send reaches people too early or too late relative to their own lapse, and how do on-time recipients act compared with late ones?

- **How:** `lcm` job `blast`: `lcm "is our monthly newsletter worth keeping?" --brief briefs/active/winback.yaml --data exports/`
- **You get:** Your recurring campaign's last three real sends (from message_events), each send's recipients sorted into active, at risk, on time and late, with each band's core-action rate. Without message_events, or with --dates, it shows what a send to everyone qualified would have reached, labeled as such.
- **Needs:** a brief for the trigger; users and track_events; message_events for the real sends
- **Cannot answer:** The comparison is observed, not a test; a holdout on the trigger settles it.

### What does our LTV curve look like by signup cohort?

*Expert version:* How does cumulative net revenue per customer grow by 30-day period for each first-order quarter, counting only periods each customer was observed for, and what share buys again after the first order?

- **How:** the `lifecycle-sql` skill in Claude: ask Claude "build our LTV cohort curve in BigQuery" (or Snowflake, PostgreSQL)
- **You get:** The cohort LTV query for your warehouse, with right-censoring and the repeat share, and a reading of the curve.
- **Needs:** an orders table in the warehouse
- **Cannot answer:** A curve, not a forecast; cohorts under 30 customers are labeled too small to read.

### Which tests should we run next?

*Expert version:* Which hypotheses, ranked by expected impact, confidence and cost, fit inside this quarter's traffic and change budget?

- **How:** the `lifecycle-measurement` skill in Claude: ask Claude "prioritize our test backlog"
- **You get:** A ranked hypothesis matrix with each test's size and duration.
- **Needs:** your candidate tests and each flow's volume
- **Cannot answer:** It ranks ideas you bring and those the priorities list finds; it does not invent customer insight.


## Quarterly

### What did our flows actually add this quarter, versus what the platform credits?

*Expert version:* What share of the revenue our ESP attributes to automated flows is incremental against persistent holdouts, and what does that mean for where we invest next quarter?

- **How:** `lcm` job `readout`: `lcm "what did the win-back actually add? results in results/winback_q3.json"`
- **You get:** Credited, incremental and the gap, with intervals and the read labeled.
- **Needs:** holdout and treatment results per flow
- **Cannot answer:** Net revenue retention and expansion need account-level revenue; the engine reads what the holdouts show, not attribution models.

### Does our tracking plan still match the product, or has the data drifted?

*Expert version:* Do our event taxonomy and required properties still describe what the product does after this quarter's releases, or has drift degraded segmentation?

- **How:** `lcm` job `tracking`: `lcm "has the data drifted from our tracking plan?" --data exports/`
- **You get:** Events stopped, dropped or never tracked, required properties going missing, and events arriving outside the plan.
- **Needs:** track_events
- **Cannot answer:** Whether the plan matches the roadmap is a review with product; this gives that review its facts.

### Is our DMARC policy ready to move to quarantine or reject?

*Expert version:* Do aggregate reports and observed alignment show every legitimate sender passing aligned SPF or DKIM, so the next DMARC step will not quarantine our own mail?

- **How:** `lcm` job `deliverability`: `lcm "check deliverability for example.com"`
- **You get:** The current policy, subdomain policy, the next safe step as a copy-paste record, and what must be true first.
- **Needs:** the sending domain; a received message's headers help
- **Cannot answer:** It reads published records and one message's headers; aggregate reports confirm every sender.

### Do our RFM segments still describe our customers?

*Expert version:* Do recency, frequency and monetary scores, cut at this quarter's purchase cycle, still separate customers into segments that behave differently?

- **How:** the `lifecycle-sql` skill in Claude: ask Claude "rebuild our RFM segments in Snowflake"
- **You get:** RFM scores and eleven segments in your warehouse's SQL, each mapped to the flow it serves, with the sync payloads.
- **Needs:** an orders table
- **Cannot answer:** Low-frequency businesses need the banded version the module describes.

### Which flows should we retire?

*Expert version:* Which flows show no incremental effect over enough entrants to have detected one, and which merely look weak because they cannot yet read?

- **How:** the `lifecycle-measurement` skill in Claude: ask Claude "which of our flows should we retire?" with each flow's holdout results
- **You get:** Keep, change or retire per flow, separating "no effect" from "too small to tell".
- **Needs:** holdout results per flow
- **Cannot answer:** A flow too small to read is not a failed flow.


## Annual

### What is our lifecycle program worth next year?

*Expert version:* What do the time returned, the inbox protection and the measurement discipline add up to for our team, at our pay, volume and email revenue?

- **How:** `lcm` job `roi`: `lcm "what is our lifecycle program worth next year?"`
- **You get:** The business case from docs/ROI_MEMO.md at the default numbers; rerun scripts/roi_model.py with yours.
- **Needs:** your pay, team size and email revenue for your own figures
- **Cannot answer:** Modeled, not measured; the memo says how to measure the task hours.

### Can our stack support real-time triggers, or is latency holding us back?

*Expert version:* Do our ESP, CDP and warehouse support event ingestion and personalization fast enough for the triggers we want, or is architecture capping engagement?

- **How:** the `lifecycle-flows` skill in Claude: ask Claude "review our lifecycle stack for real-time triggers"
- **You get:** A checklist review of each trigger's path from event to send, where it waits, and what each platform supports.
- **Needs:** your stack, and which flows need real-time entry
- **Cannot answer:** It cannot measure your ESP's or CDP's latency; it tells you what to measure and where.

### Are our lifecycle programs raising LTV enough to lower blended CAC?

*Expert version:* How much do retention and expansion driven by lifecycle programs raise customer lifetime value, and what does that do to the acquisition cost we can afford?

- **How:** the `lifecycle-measurement` skill in Claude: ask Claude "what are our lifecycle programs doing to LTV and CAC?"
- **You get:** LTV curves from your orders, the incremental share from your holdouts, and the arithmetic with your acquisition spend.
- **Needs:** orders, holdout results, and acquisition spend by period
- **Cannot answer:** The engine has no ad-spend data; CAC comes from you.

### What does a safe sending plan look like for a new domain?

*Expert version:* Given our engaged tiers and target volume, what warm-up ladder can a new domain carry without damaging its reputation, and what stops it?

- **How:** the `deliverability-gate` skill in Claude: ask Claude "build a warm-up plan for our new sending domain"
- **You get:** A day-by-day ladder that starts with the most engaged, the abort criteria, and the volume that is never warmable.
- **Needs:** engagement tiers and target daily volume
- **Cannot answer:** It never warms with unengaged people, so some target volumes are refused.

### Which compliance questions are open across our flows?

*Expert version:* For every live flow, which email and texting rules apply to our audience's countries, which pass, and which need counsel's answer?

- **How:** the `lifecycle-teammate` skill in Claude: ask Claude "review compliance across our live flows"
- **You get:** Each flow's compliance table, with sources, merged into one list of open questions.
- **Needs:** the briefs in briefs/active/ and recipients' countries
- **Cannot answer:** A rules engine with sources, not legal advice; it tracks some states' texting rules, not all fifty.
