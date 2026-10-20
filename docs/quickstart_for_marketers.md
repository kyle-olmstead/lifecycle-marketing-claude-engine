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
| A terminal | Run one command (section 3) | `pip install -r requirements.txt`, once |

## 2. Say what you want

Any of these works as a first message:

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

## 3. Or fill in the brief yourself

Copy `briefs/brief_template.yaml`, fill in the three required lines, and run:

```text
python3 -m agents.orchestrator plan --brief briefs/my_brief.yaml
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

No terminal? Start from a sentence instead:

```text
python3 -m agents.orchestrator brief --text "5-touch win-back for users inactive 14 days" --product Acme --out briefs/acme.yaml
```

## 4. Read the plan

The plan lands in `out/<your flow>/plan.md`. A worked example for a fictional company is in
`examples/teammate/ridgeview_winback_14d/plan.md`. Read it top to bottom:

| Section | What it tells you | What to do |
|---|---|---|
| **Status** | READY, READY WITH ASSUMPTIONS, or BLOCKED | BLOCKED names the check that failed; fix that first |
| **Confirm before you build** | everything the teammate assumed or could not verify | answer each line, update the brief, run it again |
| **Who enters** | the entry rule, the exit, the trigger, the data it depends on, and the daily queries (who to push, then a record of who was pushed, so nobody enters twice) | check the event names are yours |
| **The flow** | a diagram and a day-by-day table: channel, job, conditions | sanity-check timing against your other flows |
| **Build it in your platform** | numbered build steps in your platform's own words | hand to whoever builds it |
| **The copy** | two variants per message, each with a hypothesis; files are in `copy/` | replace any `[bracketed]` text with real specifics |
| **Measurement** | the holdout, when you can read the result, and the decision rule | put the read date in your calendar |
| **Deliverability guardrails** | who is suppressed and the re-permission and sunset rules | nothing; they are built in |
| **QA** | every automatic check and its result | nothing, unless one failed |

### Three things that surprise people

1. **15% of entrants get nothing.** That is the holdout. Without it you will see revenue credited to
   the flow but never know how much of it would have happened anyway. Win-back flows are the most
   over-credited flows in lifecycle marketing.
2. **"Directional only" or "not readable".** Your entry volume is too small to prove a lift inside a
   year. The plan says so instead of inventing a date. The grid in the Measurement section shows
   what volume would change that.
3. **The last message asks if they still want your emails.** People who do not answer stop getting
   marketing email. This protects your sender reputation with Gmail, Microsoft and Yahoo, which
   keeps the rest of your email in the inbox.

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

Every morning at 05:00 UTC, `.github/workflows/daily_cohort_audit.yml` checks the cohort behind
your flow and logs it to a Google Sheet:

| Tab | One row per | Shows |
|---|---|---|
| Cohort Health | flow per day | qualified people by risk band (active, at risk, lapsed), the biggest onboarding leak, data freshness |
| Benchmarks | metric per day | signups, active users, core-action users, orders and threshold crossings against their 28-day median |
| Anomalies | finding | anything unusual, in a sentence |
| Campaign Plans | plan you generate | status, read date and open checks for each plan |

When something is unusual it also opens a GitHub issue.

**Set it up once** (or ask whoever owns your GitHub and Google Cloud):

1. In Google Cloud, create a service account, enable the Google Sheets API, and download a JSON key.
2. Share your Google Sheet with the service account's email address as an Editor.
3. In your GitHub repository, open **Settings, Secrets and variables, Actions**. Add the secret
   `GOOGLE_SERVICE_ACCOUNT_JSON` (the whole key file) and the variable `LCM_SHEET_ID` (the long id
   in the sheet's address).
4. Point it at your data: the variables `LCM_BQ_PROJECT` and `LCM_BQ_DATASET` for BigQuery (plus
   keyless Google Cloud authentication, described at the top of the workflow file), or
   `LCM_DATA_URL` for exported files.

Until step 4, the workflow runs on demo data so you can see what it produces.

## 8. Getting events in

The teammate reads three tables: users, orders and events. If your events flow through Segment,
point a Webhooks destination at `scripts/webhook_parser.py` (your engineer can host it in minutes;
the file explains how), with a shared secret set and batching turned off so every request is signed
in full. GA4 events can come in through the same service, or through GA4's
BigQuery export. Events that do not match your tracking plan are held aside with the reason,
never silently dropped.
