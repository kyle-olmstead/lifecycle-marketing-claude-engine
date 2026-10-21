# Quickstart for lifecycle marketers

> **In plain English.** You describe a campaign the way you would to a colleague. The teammate
> answers with a plan you can hand to whoever builds it: who enters, what each message does and
> when, the copy, how you will know it worked, and a list of the things it had to assume. It never
> sends anything or changes your platform.

## 1. Pick how you want to work

| You use | Do this | Setup |
|---|---|---|
| Claude with the LMM Engine plugin | Type your brief into Claude (section 2) | Install once: see the README, section 4 |
| Claude Desktop without the plugin | Add the teammate as a local tool (section 6), then type your brief | 10 minutes, once |
| A terminal | Type what you want after `lcm` (section 3) | `pip install -r requirements.txt && pip install -e . --no-deps`, once |

## 2. Say what you want

Not sure what to build? Start here:

```text
What are my top 3 priority builds today and why?
```

The engine reads your data for opportunities (new users stalling in onboarding, customers lapsing with no flow
to catch them, first-time buyers who never order again), ranks them by estimated monthly revenue, the data behind
the estimate and the work involved, and puts anything that would undercut every build first (a failing sending
domain, a stale list, a release that broke the event a live flow runs on). Each comes with its evidence, a revenue
range with the assumptions spelled out, and its next step: for a win-back or activation build, a ready brief and the
one command that turns it into a full plan. When your email data shows a recurring newsletter, a win-back also says
how late its real sends reached the people the trigger would catch. The morning check (section 7) answers the same question every day without
being asked. For everything else a lifecycle manager asks daily, weekly, monthly, quarterly and yearly, see the
[question bank](QUESTION_BANK.md).

Or describe a campaign directly. Any of these works as a first message:

```text
Plan a 5-touch win-back flow for Acme users who completed onboarding but haven't created a report in 14 days. We use Customer.io.
```

```text
Build a 3-step welcome series for new signups who haven't connected a data source yet. Klaviyo, email and SMS.
```

```text
Run the daily cohort audit on our activation flow and tell me if anything looks off.
```

Claude turns the sentence into a short brief, shows you what it had to assume, and asks you to
confirm the one thing it cannot guess: **the event that means a customer is getting value** (for
example `feature_used` with `feature = reports`). Then it builds the plan.

## 3. Or use the terminal

Type what you want, the way you would say it:

```text
lcm "what are my top 3 priority builds today and why?" --data exports/
lcm "plan a 5-touch win-back for users who completed onboarding but haven't created a report in 14 days" --product Acme
lcm "check deliverability for example.com"
lcm "build the engagement tiers and the sunset list" --data exports/ --platform klaviyo
lcm "compare our monthly newsletter with the win-back trigger" --brief briefs/active/winback.yaml --data exports/
lcm "audit the cohort" --data exports/
lcm "how many people do we need to detect a 20% lift on a 10% baseline with a 15% holdout"
```

If `lcm` needs something it cannot work out (the product name, the data folder, a domain), it says
exactly what, and runs nothing. Add `--dry-run` to see which job it would run first. Anything you
would rather keep in a file can be a short YAML job instead of a sentence:

```yaml
# jobs/tiers.yaml, run with: lcm jobs/tiers.yaml
kind: engagement
data: exports/
platform: braze
```

### Fill in the brief yourself

Copy `briefs/brief_template.yaml`, fill in the three required lines, and run:

```text
lcm briefs/my_brief.yaml
```

| Field | What to put | Example |
|---|---|---|
| `product_name` | what customers call your product | `Acme` |
| `core_conversion_event` | the one action that means someone is getting value: the event name in your data, and optionally a property to narrow it | `event: feature_used`, `where: {feature: reports}` |
| `churn_threshold_days` | days without that action before someone enters a win-back flow | `14` |

Everything else has a default, and every default you did not set is listed back to you. Two
optional lines change the plan the most:

- `monthly_entrants` and `baseline_conversion_rate`: how many people cross the threshold each month,
  and how many come back on their own. Without them the plan cannot say when you will know whether
  the flow works. If you have exports of your users and events, add `--data <folder>` and the
  teammate measures both for you.
- `sending_gate`: GREEN, AMBER or RED from your deliverability check. Left blank, the plan mails
  engaged people only (anyone who clicked or replied in the last 90 days). Run the
  deliverability check first if you want the full audience.

Or start from a sentence and let it write the brief, setting anything you already know:

```text
python3 -m agents.orchestrator brief --text "5-touch win-back for users inactive 14 days" --product Acme \
  --set postal_address="100 Main St, Springfield, IL 62701" --out briefs/acme.yaml
```

When a flow goes live, copy its brief into `briefs/active/`: the daily health check (section 7)
audits every brief there.

## 4. Read the plan

The plan lands in `out/<your flow>/plan.md`. A worked example for a fictional company is in
`examples/teammate/ridgeview_winback_14d/plan.md`. Read it top to bottom:

| Section | What it tells you | What to do |
|---|---|---|
| **Status** | READY, READY WITH ASSUMPTIONS, or BLOCKED | BLOCKED names the check that failed; fix that first |
| **Confirm before you build** | everything the teammate assumed or could not verify | answer each line, update the brief, run it again |
| **Why the plan looks like this** | each decision (channels dropped, days spread, the offer stance) and the reason for it | disagree with one? change the brief and revise (section 4a) |
| **Who enters** | the entry rule, the exit, the trigger, the data it depends on, and the daily queries (who to push, then a record of who was pushed, so nobody enters twice) | check the event names are yours |
| **The flow** | a diagram and a day-by-day table: channel, job, conditions | sanity-check timing against your other flows |
| **Build it in your platform** | numbered build steps in your platform's own words | hand to whoever builds it |
| **The copy** | two variants per message, each with a hypothesis; files are in `copy/` | replace any `[bracketed]` text with real specifics |
| **Measurement** | the holdout, when you can read the result, and the decision rule | put the read date in your calendar |
| **Deliverability guardrails** | who is suppressed and the re-permission and sunset rules | nothing; they are built in |
| **Compliance** | the laws that apply to where your audience lives (US, Canada, UK, EU, Germany, Australia), each with its source | WARN lines are questions for you or your counsel; FAIL blocks the plan |
| **QA** | every automatic check and its result | nothing, unless one failed |

### 4a. Change one thing, keep the rest

Every plan is saved as a run. To change it without starting over:

```text
python3 -m agents.orchestrator revise --run out/acme_winback_14d --set touches=4
python3 -m agents.orchestrator revise --run out/acme_winback_14d --set channels=[email,sms] --set holdout_pct=20
python3 -m agents.orchestrator approve --run out/acme_winback_14d --step t1_email --variant B
python3 -m agents.orchestrator status --run out/acme_winback_14d
```

A revision keeps the copy of every message whose job did not change, and keeps the variants you
approved. If a run stops part way (a data export was missing, the API timed out), `resume` picks up
where it stopped: `python3 -m agents.orchestrator resume --run out/acme_winback_14d --data exports/`.

### Four things that surprise people

1. **15% of entrants get nothing.** That is the holdout. Without it you will see revenue credited to
   the flow but never know how much of it would have happened anyway. Win-back flows are the most
   over-credited flows in lifecycle marketing.
2. **"Directional only" or "not readable".** Your entry volume is too small to prove a lift inside a
   year. The plan says so instead of inventing a date. The grid in the Measurement section shows
   what volume would change that.
3. **The last message asks if they still want your emails.** People who do not answer stop getting
   marketing email. This protects your sender reputation with Gmail, Microsoft and Yahoo, which
   keeps the rest of your email in the inbox.
4. **No postal address, no plan.** US law (CAN-SPAM) requires one in every marketing email. Some
   platforms fill it in from your account settings; Customer.io does not, so put `postal_address` in
   the brief. Until then the plan is BLOCKED.

## 5. Copy: who writes it

| Mode | When | Result |
|---|---|---|
| Claude in the conversation (plugin) | default with the plugin | Claude writes from each message's brief; the teammate renders and checks it |
| `--copy-mode claude` | terminal, with an `ANTHROPIC_API_KEY` | the Claude API writes; failures get one automatic repair round |
| `--copy-mode offline` | no key | starter drafts with `[bracketed]` gaps for you to fill |

Whatever writes the words, the teammate adds the unsubscribe link and postal address your platform
requires, SMS opt-out text, a preview line and dark-mode support, then lints every template. Copy
with a fake deadline, an offer your policy does not allow, a subject that starts with "RE:", or an
image instead of text is rejected.

## 6. Claude Desktop without the plugin

1. Install Python 3.12 or later, download this repository, and run `pip install -r requirements.txt`
   in its folder.
2. In Claude Desktop, open **Settings, Developer, Edit Config**, and paste the `lcm-teammate` entry
   from `config/mcp_server_config.json` into `mcpServers`, replacing `REPLACE_WITH_ABSOLUTE_PATH`
   with where you saved the repository.
3. Restart Claude Desktop and type your brief.

`config/mcp_server_config.json` also lists the official servers for Klaviyo, HubSpot, Braze,
Customer.io, BigQuery, Snowflake and Google Sheets, so Claude can read your real numbers.

## 7. The daily health check

Every morning at 05:00 UTC, `.github/workflows/daily_cohort_audit.yml` runs `scripts/daily.py`. It
lists anything to fix and ranks today's top 3 builds, checks the cohort behind every flow in `briefs/active/`,
checks that the events those flows run on are still arriving, sorts your email audience into engagement tiers,
looks up each sending domain's authentication records, and logs it all to a Google Sheet. The summary opens with
the priorities:

| Tab | One row per | Shows |
|---|---|---|
| Priorities | ranked item per day | the fix or build, why, the monthly revenue range, confidence, effort, and the next step |
| Cohort Health | flow per day | qualified people by risk band (active, at risk, lapsed), the biggest onboarding leak, data freshness |
| Benchmarks | metric per day | signups, active users, core-action users, orders and threshold crossings against their 28-day median |
| Anomalies | finding | anything unusual, in a sentence |
| Engagement | day | people per tier (clicked in 30, 90, 180 days; unengaged; new; suppressed) and the unengaged share |
| Deliverability | domain per day | the sending gate, DMARC policy, SPF lookups, DKIM keys found, MTA-STS, the top finding |
| Campaign Plans | plan you generate | status, read date and open checks for each plan |

Each problem gets its own GitHub issue (one per flow, per domain, and for the unengaged share), and
the next day's result is added to the same issue while it stays open. To run the same check now:
`python3 scripts/daily.py --data exports/ --domains example.com`.

**Unengaged people.** Anyone with five or more emails in six months and no click in that time is
"unengaged". They get `lcm_sunset = true` in your ESP when you sync the tiers (`lcm "build the
engagement tiers and the sunset list" --platform klaviyo`), which you use as an exclusion on
marketing sends. Nobody is unsubscribed: a click brings them back the next day. Opens do not count,
because Apple and others open mail automatically.

**Set it up once** (or ask whoever owns your GitHub and Google Cloud):

1. In Google Cloud, create a service account, enable the Google Sheets API, and download a JSON key.
2. Share your Google Sheet with the service account's email address as an Editor.
3. In your GitHub repository, open **Settings, Secrets and variables, Actions**. Add the secret
   `GOOGLE_SERVICE_ACCOUNT_JSON` (the whole key file) and the variable `LCM_SHEET_ID` (the long id
   in the sheet's address).
4. Point it at your data: the variables `LCM_BQ_PROJECT` and `LCM_BQ_DATASET` for BigQuery (plus
   keyless Google Cloud authentication, described at the top of the workflow file), or
   `LCM_DATA_URL` for exported files.
5. List your sending domains in the variable `LCM_DOMAINS` (for example `example.com,em.example.com`).

Until step 4, the workflow runs on demo data so you can see what it produces.

## 8. Getting events in

The teammate reads four tables: users, orders, events and, for engagement tiers, message events
(delivered, clicked, bounced, complained, unsubscribed; export them from your ESP). If your events flow through Segment,
point a Webhooks destination at `scripts/webhook_parser.py` (your engineer can host it in minutes;
the file explains how), with a shared secret set and batching turned off so every request is signed
in full. GA4 events can come in through the same service, or through GA4's
BigQuery export. Events that do not match your tracking plan are held aside with the reason,
never silently dropped.

For an activation flow, the event that starts it has to reach your ESP the moment it happens, with
the holdout marked on it. The same service can do that: your engineer sets `LCM_FORWARD=1` and the
ESP's API key, and every qualifying event for an activation brief in `briefs/active/` is passed on
with `in_holdout` added.
