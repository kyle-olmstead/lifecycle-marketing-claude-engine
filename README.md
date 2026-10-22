# LMM Engine

**An AI teammate for lifecycle marketing: it plans, writes, checks and measures the emails and texts your company sends to customers, inside the tools you already use.**

**If you read nothing else:**
- Describe a campaign in one sentence, the way you would to a colleague. It comes back with everything needed to launch it: who gets it, every message written in your email platform's own code, the timing, a legal and inbox check, and an honest plan for proving whether it worked.
- Every morning it tells you the three things most worth building next, ranked from your own data with the reason and an estimated revenue range for each, and it checks that your emails still reach the inbox, raising a flag the day something breaks rather than the month after.
- It never sends a message or changes your systems without a person approving it. It is free, open source, and a marketer can install it in about fifteen minutes. A sample company is included so you can watch it work before connecting anything.

---

## Start here

```
                          What are you here to do?
                                    │
       ┌───────────────┬────────────┼─────────────┬───────────────┐
       ▼               ▼            ▼             ▼               ▼
  Understand       Plan a         Check that    Run it for     Change how
  what it is       campaign       our email     the whole      it works
  and why          or flow        reaches the   company        (rules, SQL,
                                  inbox         every day      prompts)
       │               │            │             │               │
    5 min           10 min       15 min       1-2 days        as needed
       │               │            │         (needs an eng)      │
       ▼               ▼            ▼             ▼               ▼
  Read §1-4       §5 Install   §5, then say   §11 The daily  §12 Under the
                  then type    "check our     check          hood
                  the brief    deliverability"
```

| If you are… | Start with | What you'll have | Time |
|---|---|---|---|
| **A CEO or exec** deciding if this is worth the team's time | §1 to 4 (§4 is the business case; the [full memo](docs/ROI_MEMO.md) shows every number), then ask your marketing lead to run it on one campaign | A clear picture of what it does, what it won't do, and what it needs from you | 5 min |
| **A lifecycle or CRM marketer** | §5 Install → say *"set up the lifecycle engine"* → describe a campaign | A build-ready plan with the copy written and checked | 15 min |
| **A head of marketing** rolling it out | The same, then §8 (the routine) and the [quickstart](docs/quickstart_for_marketers.md) | Every campaign planned, checked and measured the same way | 1 hour |
| **Marketing ops or data** | §11 The daily check | A 05:00 UTC health check on every flow, logged to Google Sheets | 1 to 2 days |
| **An engineer** | §12 → [`docs/architectural_overview.md`](docs/architectural_overview.md) and [`docs/AUDIT.md`](docs/AUDIT.md) | The full design, every rule, every test | As long as you like |

A few words used throughout: **lifecycle marketing** is the email, text and in-app messages a company sends automatically based on what each customer does (the welcome series after signup, the nudge when someone stalls, the win-back when they go quiet). A **flow** is one of those automated sequences. **Deliverability** is whether your email reaches the inbox rather than spam. A **holdout** is a small group that deliberately gets nothing, so you can see what the messages actually changed. Full list in §13.

---

## 1. The problem it solves

Lifecycle email and text is one of the cheapest growth channels a company has, and one of the easiest to get quietly wrong. The mistakes are rarely visible from the dashboard:

- **Mail stops reaching the inbox.** Gmail and Yahoo (since 2024) and Microsoft (since 2025) junk or reject mail from large senders that fail their authentication and complaint rules. Nothing alerts you; results just slide, and every other campaign suffers with them.
- **Customers get messages they shouldn't.** A text at 7 a.m., a promotional email with no postal address, a list padded with people who stopped reading a year ago. Each is a legal exposure or a complaint waiting to happen.
- **Results get overstated.** Email platforms credit a campaign with every purchase that followed it, including the ones that would have happened anyway. Teams keep flows that do nothing and cut ones that work.
- **Every campaign takes several specialists.** Someone to define the audience in the data, someone to build the flow, someone to write copy in the platform's template code, someone to size the test. Weeks pass between idea and launch.

This engine does that work, checks it, and keeps checking every morning.

---

## 2. What it does, in plain English

1. **Plans a whole campaign from one sentence.** It plans ten kinds of flow (welcome, activation, win-back, abandoned cart, browse abandonment, post-purchase, replenishment, failed payment, trial conversion and sunset) and refuses anything else by name rather than bending it into the nearest one. "A 5-touch win-back for users who completed onboarding but haven't created a report in 14 days" becomes: exactly who enters and when they leave, the messages and their timing across email, text, push and in-app, the reasoning behind each choice, and step-by-step build instructions in your platform's own terms. Anything it had to assume is listed for you to confirm.
2. **Writes the messages in the right code.** Copy rendered for Klaviyo, HubSpot, Braze, Customer.io or Salesforce Marketing Cloud, each in that platform's template language, with a fallback for every personalized field, laid out from your brand kit (logo, colors checked for contrast, fonts, footer, voice rules), with the cart or product shown from the platform's own event fields, and checked by a real parser so nothing breaks on send.
3. **Checks the law before anyone presses send.** A compliance review of every plan against the rules where your customers live: US email and texting law (including the stricter state texting laws), Canada, the UK, the EU, Germany and Australia, plus the mailbox providers' own rules. Every rule cites its source. A missing legal requirement blocks the plan.
4. **Keeps your email in the inbox.** It looks up your domain's authentication records live, reads real email headers, and issues a GREEN, AMBER or RED sending gate with the exact fixes. It finds the people who have stopped engaging and takes them out of marketing sends (reversibly), because they are what pulls a sender into spam.
5. **Tells you what actually worked.** Every flow ships with a holdout group sized so the result can be read, and a date when it can. Results report three numbers side by side: what the platform credited, what the flow truly added, and the gap.
6. **Tells you what to build next.** Ask "what are my top 3 priority builds today and why?" or just read the morning check. It looks for the opportunities your data shows (new users stalling at an onboarding step, customers lapsing with no flow to catch them, first-time buyers who never order again) and ranks them by estimated monthly revenue, how much data stands behind the estimate, and how much work the build is. When your email data shows a recurring newsletter, it also shows how late those real sends reached the people a trigger would catch. Problems that would undercut every build come first: a list going stale, a sending domain failing, or a release that broke the event a live flow runs on. Each idea comes with its evidence and its next step; win-back and activation ideas come with a ready brief and the one command that turns it into a full plan.
7. **Watches every day.** At 05:00 UTC it ranks the day's priorities, audits each live flow's audience for anything unusual, checks that the events your flows depend on are still arriving (with the exact property filters each flow uses), rebuilds the engagement picture, checks each sending domain, logs it all to a Google Sheet, and opens a ticket for each problem it finds.

**What it will never do:** send a message · change a platform or a list without your approval · state a number it can't trace to your data · call a result proven when the sample is too small · give legal sign-off (it flags questions for your counsel).

---

## 3. How it works (one picture)

```
  YOUR TOOLS                       THE ENGINE                                          YOU
  ──────────                       ──────────                                          ───
  Email platform     ┐
  (Klaviyo, HubSpot, │      1. Finds the audience in your data, checks the data
   Braze, Customer.io│      2. Decides the shape of the campaign, and says why
   SFMC)             │      3. Builds the flow and simulates it with test customers
  Data warehouse     ├──►   4. Writes every message in your platform's code     ──►   approve · edit · reject
  Event stream       │      5. Sizes the holdout and sets the read date                     │
  (Segment, GA4)     │      6. Checks the law and the inbox rules                           ▼
  Your domain's DNS  ┘                                                         7. Only then: you build it,
                                                                                  or it hands you the files
                            Every morning: audience health · engagement · inbox checks ──► Google Sheet + tickets
```

Each step saves its work. If a step fails (a data export is missing, an API times out) the plan picks up where it stopped. Change one thing ("make it 4 touches") and only what that change affects is redone; approved copy stays approved.

---

## 4. What it's worth

Start with one person. A lifecycle marketer costs about **$204,148 a year** fully loaded (the government's median pay for marketing managers plus a conservative benefits load), which is about **$113 an hour** of productive time. The engine takes over the assembly and the checking around the messages (specifying flows, revising them, deliverability checks, reporting, holdout reads, list hygiene), and in the model that returns **393 hours a year, about 22 percent of a working year**.

| | One lifecycle marketer, per year |
|---|---|
| Time the engine gives back | 393 hours (about 22 percent) |
| What that time costs you today | $44,572 |
| What the engine costs | $1,345 a year, plus $560 once |

That is about 33 dollars of time back for every dollar, and it pays for itself at 12 hours a year. Time is not cash, so the memo also prices the things that are:

| What it protects | What getting it wrong costs | What the engine does |
|---|---|---|
| **The inbox** | Gmail requires bulk senders to keep spam complaints under 0.3% and, since November 2025, rejects mail that breaks its rules; Yahoo and Microsoft enforce similar rules. In the memo's modest scenario, catching one problem the next morning instead of three weeks later keeps about **$12,500** of revenue. | Checks authentication live every morning, issues the sending gate before anything sends, and removes the unengaged people who drive complaints |
| **Legal exposure** | Under the TCPA a marketing text without the right consent or outside allowed hours carries $500 to $1,500 per message: **$2.5 million to $7.5 million** for one send to 5,000 people. CAN-SPAM penalties reach $53,088 per email. | Blocks a plan that breaks a tracked rule, holds texts to the strictest state calling window it tracks, and lists every consent question for counsel |
| **Budget decisions** | Platforms credit flows with sales that would have happened anyway, so money and attention go to the wrong programs | Measures every flow against a holdout and reports credited versus true lift (example below) |
| **Team time** | A team of three gets back about two thirds of a person's year, $133,717 of time, to ship more, test more, or wait on the next hire | Produces the full specification, checked, in minutes; the team reviews and builds |

**What the measurement difference looks like.** In the repository's worked readout (a fictional company), the email platform credited a win-back flow with **$41,900**. Compared against its holdout, the flow actually added about **$15,880**. The other **$26,020, 62% of the credit,** would have come in anyway, and the engine says plainly that even the $15,880 is not yet statistically certain. A team reading only the platform's number would scale the flow; a team reading this one would keep testing it.

These are modeled figures, not customer results, and the engine has not yet run in a production deployment. The full memo explains every number in plain English, names what could go wrong, separates what is verified from what you should test yourself, and ships with the calculator so you can rerun it with your own figures: [`docs/ROI_MEMO.md`](docs/ROI_MEMO.md).

---

## 5. Install (15 minutes)

You need a paid Claude plan. It works best in **Claude Code** or the **Claude desktop app with a working folder**, where it can save files and run its checks. In a plain web chat it still works, and it labels anything it could not check.

**In the Claude app:** open **Customize → Plugins → Add marketplace**, enter `kyle-olmstead/lifecycle-marketing-claude-engine`, then add the **lifecycle-engine** plugin. (The repository is called *lifecycle-marketing-claude-engine*; the plugin inside it is called *lifecycle-engine*. Same thing.)

**In Claude Code:**

```text
/plugin marketplace add kyle-olmstead/lifecycle-marketing-claude-engine
/plugin install lifecycle-engine@lifecycle-engine
```

Then, in any conversation: **"set up the lifecycle engine."** Setup looks at which platforms you have connected, records each one's template language and SQL dialect, and writes the context file everything else reads. Then describe a campaign.

**From a terminal** (Python 3.12 or later), the same engine is one command, `lcm`:

```text
git clone https://github.com/kyle-olmstead/lifecycle-marketing-claude-engine.git
cd lifecycle-marketing-claude-engine
uv venv && uv pip install -e .            # or: python3 -m venv .venv && . .venv/bin/activate && pip install -e .
lcm doctor                                # checks Python, packages, the email compiler, and what is set up
```

Try it on the two sample companies before connecting anything (both are fictional, with their data in the repository):

```text
cd examples/brands/northfield_dtc         # a coffee roaster on Shopify and Klaviyo; examples/brands/ridgeview_saas is a SaaS tool
lcm plan --text "a 3-touch abandoned cart flow with email and SMS" --data data
lcm "make it 4 touches"                   # revises the plan you just made; approved copy stays approved
lcm simulate --program                    # every live flow together against one frequency policy
lcm workflow run morning_check            # the daily check, step by step, with every output kept
```

Then make a folder for your own brand and point it at your data:

```text
lcm init ~/acme --name Acme --platform klaviyo        # lmm.yaml (your program) and brand.yaml (your brand kit)
cd ~/acme
lcm import shopify ~/Downloads/shopify-export --out data     # or klaviyo, stripe, hubspot, customerio, ga4, segment, rudderstack
lcm check-data data
lcm deliverability check acme.com --write-gate gate.json     # then set sending_gate: gate.json in lmm.yaml
lcm plan --text "a 5-touch win-back for customers who have not ordered in 60 days" --data data
```

`lcm --help` lists every command; [`docs/COMMANDS.md`](docs/COMMANDS.md) has the full table. Plain English works too: `lcm "check deliverability for example.com"`.

---

## 6. What it needs from you

Three things decide the quality of everything it produces. An hour here is worth more than any setting:

| Make sure you have… | Because | Where |
|---|---|---|
| **The one action that means a customer is getting value** (for example, "created a report") as an event in your data | Every flow is built around it: who enters, when they leave, how success is measured. The engine asks for it and never guesses it | Your product analytics or event stream |
| **Consent recorded per person and channel**, and each person's country | Who may be emailed or texted, and which laws apply, come from these fields | Your email platform or CRM |
| **Your sending domain and postal address** | The inbox checks need the domain; US law needs the address in every marketing email | Your brief, or setup |

**What it works with.** The engine reads platforms that have a Claude connector in your workspace, and works from exports or pasted evidence for the rest. Checked against Claude's connector directory in October 2026:

| Role | Connector available today | No connector found (export or paste instead) |
|---|---|---|
| Email and messaging platform | HubSpot, Klaviyo, Braze, Customer.io (also Mailchimp, Brevo, Kit, OneSignal) | Salesforce Marketing Cloud |
| Data warehouse and CDP | BigQuery, Snowflake, Neon (PostgreSQL), Clay | Segment, RudderStack (the engine includes a receiver for their events) |
| Analytics | Mixpanel, Amplitude | Google Analytics 4 (use its BigQuery export) |

Details, and the path for every tool without a connector: [`CONNECTORS.md`](CONNECTORS.md).

---

## 7. What to say to it

| Say… | What happens |
|---|---|
| "set up the lifecycle engine" | Finds your connected tools and configures itself |
| **"what are my top 3 priority builds today and why?"** | Anything to fix first, then the three builds most worth doing next, from your data: the evidence, an estimated monthly revenue range with its assumptions, confidence, effort, and the next step |
| "what broke overnight?" | The morning check: priorities, anything unusual in a flow's audience, tracking, the list, every sending domain |
| "are our events firing correctly?" | Every tracked event against its usual week, a stalled feed, missing properties, and each live flow whose trigger stopped |
| "when do customers stop using the product, by plan?" | The weekly retention curve, where it drops and where it levels off, per segment |
| "plan a 5-touch win-back for users who haven't created a report in 14 days" | The full plan: audience, flow, copy, holdout, compliance review, build steps |
| "make it 4 touches" / "add SMS" / "go with variant B for the first email" | Changes only what that affects; keeps approved copy |
| "check our deliverability" / "check deliverability for example.com" | Live lookup of your domain's records, the sending gate, the exact DNS fixes |
| "who should we stop emailing?" | Engagement tiers and a reversible sunset list, ready to sync to your platform |
| "is our monthly newsletter worth keeping?" | Sorts your newsletter's last three real sends by where each recipient was in their own lapse (active, at risk, on time, late), against the behavioral trigger |
| "audit the cohort for our win-back flow" | Today's health of that flow's audience, with anything unusual explained |
| "did the win-back flow actually work?" | Credited versus true lift, with the read labeled honestly |
| "how many people do we need to test this?" | Test and holdout sizing, and how long the test must run |
| "write the welcome email for Klaviyo" | Copy in Klaviyo's template language, checked |

These are a sample. The [question bank](docs/QUESTION_BANK.md) lists 27 questions by cadence (daily, weekly, monthly, quarterly, annual), each with what you get, the data it needs, and what it cannot answer. Every one ends the same way: it shows what it found and what it would build. Nothing is sent or changed until you say yes, and even then the engine hands you files and instructions; it has no way to send a message.

---

## 8. The routine (this is the part that makes it work)

| When | Who | What | Time |
|---|---|---|---|
| **Every morning** | Automatic | The 05:00 UTC check: anything to fix, then today's top 3 builds; every live flow's audience, tracking health, engagement, every sending domain. Results in a Google Sheet; a ticket for each problem | 2 min to read |
| **Before any new flow** | Lifecycle marketer | Plan it from a sentence, answer the "confirm before you build" list, approve the copy | 30 min |
| **Before a big send or a new domain** | Marketer or ops | "check our deliverability"; fix anything RED first | 15 min |
| **Weekly** | Marketing lead | Clear the open tickets; review the sunset list before it syncs | 15 min |
| **When a holdout reaches its read date** | Marketing lead | "did the flow actually work?" Keep, change or stop it on the true lift, not the credited one | 15 min |

A team that only reads the morning check still catches the expensive problems early. A team that skips the holdout reads ends up funding flows on the platform's word.

---

## 9. Two worked examples

*Both companies are made up. The reasoning is exactly what the engine does, and both runs are regenerated and checked by the test suite.*

**Ridgeview, a B2B reporting tool on Customer.io**, wants to win back users who finished onboarding but stopped creating reports. Full output: [`examples/brands/ridgeview_saas/runs/winback_14d/plan.md`](examples/brands/ridgeview_saas/runs/winback_14d/plan.md). The engine:

- defines the audience precisely (14 days without a report, entering once per lapse, leaving the moment they create one) and writes the SQL for three warehouses;
- plans five messages over 18 days, email and in-app, ending with an honest "do you still want these?" email, because people who ignore a whole win-back flow are the ones who mark it as spam;
- renders two versions of every message in Customer.io's template code, each with a testable hypothesis, from copy written against the engine's copy briefs (the handoff path: a person or a Claude chat writes the words, the engine does the code, the checks and the layout);
- reviews the plan against the laws of the six regions in Ridgeview's customer list and lists the consent questions to confirm;
- and tells the truth about measurement: about 28 people a month qualify, so a holdout would need roughly 187 months to prove a lift. It keeps the holdout anyway and labels every result "directional" instead of promising a date it cannot keep.

**Northfield Coffee Roasters, a Shopify store emailing from Klaviyo**, starts from raw vendor exports: `lcm import` turns the Shopify customers, orders and abandoned checkouts exports and the Klaviyo profile and event exports into the engine's tables, and `lcm check-data` passes them. Its abandoned cart plan ([`examples/brands/northfield_dtc/runs/cart_abandon/plan.md`](examples/brands/northfield_dtc/runs/cart_abandon/plan.md)) shows each cart in the email with Shopify's own Klaviyo event fields, sends the button back to that checkout, answers the grind question by text for people with SMS consent and by email for everyone else, and leaves the store's one offer (10% off a second order) out of a cart email, where it does not apply. With the welcome series also live, `lcm simulate --program` finds that a new subscriber who abandons a cart the next day can get five messages in a week against a cap of four, and says how to hold the cap.

The morning check on Ridgeview's data finds that 62% of the people Ridgeview can still email have received at least five emails in six months and clicked none. It marks the sending gate RED for that list and raises it as a problem to fix before scaling any campaign: sunset those people (reversibly), then grow.

---

## 10. Trust and control

- **You approve everything.** The engine writes plans, copy and request files. It has no way to send an email or a text, and it changes no platform without your explicit approval of the specific change.
- **Every number has a label.** LIVE PULL (looked up, dated), SUPPLIED (you provided it), ESTIMATE, DEMO DATA or UNKNOWN. Missing data becomes a flag and lower confidence, never a guess.
- **Measurement stays honest.** A result the sample cannot support is labeled "directional" or "not readable", never presented as proof.
- **Compliance is a checklist with sources, not legal advice.** It blocks what clearly breaks a tracked rule and turns everything else into a question for your counsel.
- **Where your data goes.** The engine reads through connectors you already authorized in Claude, or files you give it. Working files stay in your own folder. Nothing is sent to the author of this repository.

---

## 11. For bigger teams: the daily check

The same engine runs as a scheduled job (GitHub Actions, 05:00 UTC every day) against your warehouse or exported files: every flow listed in `briefs/active/`, the engagement tiers, every sending domain. It writes seven tabs to a Google Sheet (priorities, campaign plans, cohort health, benchmarks, anomalies, engagement, deliverability), opens one GitHub issue per problem, and comments on it while it stays open. With nothing configured it runs on the sample data, so you can see the output first. Setup needs an engineer for about a day: [`docs/quickstart_for_marketers.md`](docs/quickstart_for_marketers.md), section 7, and the top of [`.github/workflows/daily_cohort_audit.yml`](.github/workflows/daily_cohort_audit.yml).

---

## 12. Under the hood (for the technical reader)

```text
lifecycle-marketing-claude-engine/
├── engine/          the engine: the resumable planning pipeline (engine/pipeline), the agents (analyst, strategist,
│                    architect, copywriter, experiment, QA, compliance, priorities, engagement, tracking, retention),
│                    the flow library (engine/flows), typed policies and the simulators (engine/policy), deliverability,
│                    measurement, rendering (MJML, brand kit, linting, SMS), the data loader and check, the workspace,
│                    workflows, and lcm itself (engine/cli)
├── adapters/        sources: vendor exports to the canonical tables (Shopify, Klaviyo, Stripe, HubSpot, Customer.io,
│                    GA4, Segment, RudderStack) and the webhook receiver; destinations: ESP request bodies, Sheets
├── prompts/         the method as prompts: core principles, template syntax, diagnostics, one prompt per job, examples
├── skills/          the Claude plugin skills; each calls lcm for the deterministic work
├── schemas/         machine-readable contracts: flow spec, trigger, sending gate, frequency and offer policy, consent,
│                    workspace, brand kit, experiment, and every tool input
├── templates/       the workspace (lmm.yaml), brief and brand kit templates, MJML email components
├── workflows/       named job graphs: morning_check, new_flow, quarterly_review
├── config/          compliance rules (each with its source), the tracking plan, ESP request schemas, the question bank
├── examples/        two complete workspaces (examples/brands/ridgeview_saas, examples/brands/northfield_dtc) and sample inputs
├── docs/            commands, the flow library, importing data, measurement, deliverability operations, the brand kit,
│                    the question bank, the business case (ROI memo), architecture, SQL conventions
└── tests/           900+ checks CI runs on every change: unit, contracts (schema against model), drift (prose against
                     code), golden runs, SQL on DuckDB and a real PostgreSQL 16, planted data defects
```

| Tool | What it computes |
|---|---|
| `engine/flows/library.py` | the ten flows the engine plans: trigger, goal, touches and timing, AMBER-gate audience, re-entry, holdout default ([docs/FLOW_LIBRARY.md](docs/FLOW_LIBRARY.md)) |
| `engine/policy/contracts.py`, `engine/policy/program.py` | the typed sending gate, trigger, frequency, offer and consent policies; every live flow simulated together against one frequency policy |
| `engine/data/check.py`, `adapters/sources/` | check-data (missing, late, empty, malformed, future-dated, duplicate, orphaned) and the vendor-export mappers behind `lcm import` |
| `engine/deliverability/analyze.py` | SPF (lookup counts, loops), DKIM key size, DMARC (including subdomain policy), MTA-STS, TLS-RPT, BIMI, header alignment, one-click unsubscribe, bulk-sender rules, the sending gate, copy-paste DNS fixes |
| `engine/deliverability/dns_collect.py`, `monitor.py`, `bounces.py`, `complaints.py`, `warmup.py` | the live DNS lookup and the dated gate record; the daily trend check against the abort rules; bounce and complaint classification; a warming ladder that refuses on RED, holds on AMBER, and never warms with unengaged people |
| `engine/measurement/` | sample size, minimum detectable effect, holdout feasibility, credited versus true lift; cluster-aware sizing (ICC, design effect); group-sequential and always-valid stopping; the experiment registry; the global holdout; discount dependency; ROI |
| `engine/render/` | MJML email built from the brand kit (contrast checked to WCAG AA), product blocks per platform, syntax and fallback rules for Braze, Customer.io, Klaviyo, HubL and AMPscript, SMS encoding and segments |
| `engine/sql/check.py`, `adapters/destinations/payloads.py`, `adapters/destinations/event_payloads.py` | SQL parsing per dialect; ESP request bodies within each vendor's limits |

**What is verified, and what is not**

| Verified in CI | Not verified |
|---|---|
| every SQL example parses in its dialect; PostgreSQL examples run on fixtures and on a real PostgreSQL 16; Snowflake and BigQuery versions reproduce the PostgreSQL results | execution on a live Snowflake or BigQuery account |
| every template parses in its dialect; every rendered email passes the email checks | rendering inside each email platform |
| statistics match the standard formulas and reference values | |
| every generated flow passes the simulator: the holdout gets nothing, no send without consent, nothing after the goal, frequency caps, permitted send windows; injected defects are caught | your platform's own flow engine |
| a run that fails at any stage resumes there; a revision keeps only copy whose job, audience and voice are unchanged, and an approval only while its words are | a long-running production deployment |
| every compliance rule names a check and a source; the SMS send hour is held to the strictest state window the rules track | legal advice: it tracks some states and not all, and laws change |
| the DNS collector and the daily job, end to end, against a fake resolver and a transpiled warehouse | live DNS and a live BigQuery run in CI |
| request bodies validate against schemas written from vendor documentation (October 2026) | a live API call to each vendor |
| every JSON Schema and its Pydantic twin accept and reject the same documents; the tables in the docs are generated from the code and fail CI when stale | |
| the source mappers reproduce the committed sample data byte for byte; each planted data defect is caught with its column | every vendor export format variation |
| the repository contains no em dashes (house style) | taste |

Every technical document opens with a plain-English summary. Every change with the reason for it is in [`CHANGELOG.md`](CHANGELOG.md); the v2.2 method audit is [`docs/AUDIT.md`](docs/AUDIT.md).

---

## 13. Words used here

| Term | Meaning |
|---|---|
| **Lifecycle marketing** | Messages sent automatically based on what each customer does, rather than on a calendar |
| **Flow** | One automated sequence of messages, such as a welcome series or a win-back |
| **Win-back** | A flow for customers who have gone quiet, to bring them back |
| **Activation** | Getting a new customer to the first moment the product proves its value |
| **Deliverability** | Whether email reaches the inbox, rather than spam or nowhere |
| **Sending gate** | The engine's GREEN, AMBER or RED verdict on whether it is safe to send more |
| **SPF, DKIM, DMARC** | The three DNS records that prove your email really comes from you; mailbox providers require all three |
| **Unengaged / sunset** | People who have received many emails and clicked none in six months; sunsetting them stops marketing email until they engage again |
| **Holdout** | A randomly chosen group that gets no messages, so the difference shows what the flow truly caused |
| **Attributed vs incremental** | What the platform credits a flow with, versus what the flow actually added |
| **ESP** | Email service provider: Klaviyo, HubSpot, Braze, Customer.io, Salesforce Marketing Cloud |
| **CAN-SPAM, TCPA, CASL, PECR** | The US email law, the US texting and calling law, Canada's anti-spam law, and the UK's electronic marketing rules |

---

## FAQ

**Does it need an API key?** No. In Claude it uses the connectors already in your account. The terminal version can call the Claude API for copy if you give it a key; without one it writes starter drafts for you to finish.

**Does it work with our email platform?** Klaviyo, HubSpot, Braze, Customer.io and Salesforce Marketing Cloud are built in, each in its own template language. Others work from exports, through the same rules.

**Will it ever email or text our customers?** No. There is no send path. It writes plans, copy and request files for a person to use.

**Is the compliance check legal advice?** No. It is a checklist of specific rules, each with its source, that blocks clear violations and turns everything else into questions for your counsel.

**Our list is small. Is it still useful?** Yes, and it will tell you so honestly: small lists can't prove a lift quickly, and the engine says how long it would take instead of inventing a result.

**What does it cost?** The software is free (Apache 2.0). You need a paid Claude plan.

---

## License

Apache 2.0: use it, change it, ship it. Examples use fictional companies on domains reserved for documentation (`example.com`, `.example`, `.test`). Improvements to the rules, prompts and platform dialects are the most valuable pull requests.
