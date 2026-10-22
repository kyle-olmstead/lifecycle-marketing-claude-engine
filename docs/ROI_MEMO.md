# The business case, in plain English

*What this engine is worth, explained so that anyone can follow the math. Every number here comes from one small program, `engine/measurement/roi.py`, that you can run with your own figures. Where a number comes from a public source, the source is at the end. Where a number is an assumption, it says so. Where something has not been measured yet, it says that too.*

---

## First, what this thing is

Lifecycle marketing is the email, text and in-app messages a company sends automatically based on what each customer does: the welcome series after someone signs up, the nudge when a new user stalls, the win-back when a customer goes quiet. These automated sequences are called flows. They are a small share of what a company sends and a large share of what email earns: across more than 183,000 Klaviyo customers, flows generate about **41 percent of email revenue from 5.3 percent of sends**.

The person who runs them, a lifecycle or CRM marketer, spends most of the week on work around the messages rather than in them: getting the audience defined in the data, writing copy in each email platform's own template code, checking that mail still reaches the inbox, building reports, and working out whether any of it made a difference.

The LMM Engine is free, open-source software that does that surrounding work. Describe a campaign in one sentence and it returns a launch-ready plan: who gets it, every message written and checked, the timing, a review against email and texting law, and a test that shows what the campaign truly added. Every morning it checks the health of every live flow and every sending domain. It never sends a message or changes a system on its own. A person approves everything.

This memo answers the question a CEO or CFO will ask next: *what is that worth?* It answers it for one person first, because that is the unit everything else is built from, then for the company.

---

## The 60-second version

| | One lifecycle marketer, per year |
|---|---|
| Time the engine gives back | 393 hours, about 22 percent of a working year |
| What that time costs you today, at about $113 an hour | $44,572 |
| What the engine costs | $1,345 a year, plus $560 once |

For every dollar spent on the engine, a marketer gets back about **33 dollars** of time the company is already paying for. It pays for itself if it saves **12 hours a year** per person, less than the time saved on one flow specification.

Time is not cash, so this memo also prices the two things that are:

- **One inbox problem caught in a day instead of three weeks** keeps about **$12,500** of revenue that would otherwise be lost, in a modest scenario (Part 3). That is about nine times what the engine costs per marketer per year.
- **One texting mistake** (a send at the wrong hour, or without the right consent) to 5,000 people carries **$2.5 million to $7.5 million** in statutory damages under US law (Part 5). The engine checks every plan for that before anyone builds it.

The rest of this memo shows where those numbers come from, what has to be true for them to hold, and what could go wrong.

---

## Part 1. What a lifecycle marketer really costs

| What you pay for | Where the number comes from | Per year |
|---|---|---|
| Pay | US Bureau of Labor Statistics: median for marketing managers, May 2025 | $166,790 |
| Benefits | Assumed at 20 percent of pay (conservative; see below) | $33,358 |
| Tools and equipment | Assumed: laptop and software outside the email platform | $4,000 |
| **Total** | | **$204,148** |

Divided by 1,800 productive hours a year (2,080 paid hours less leave and holidays), one marketer's time costs about **$113 an hour**.

Two honest notes. Lifecycle roles below director level often pay less than the median for all marketing managers; set your own pay and every time figure scales with it. And the benefits figure is deliberately low: government data for all private-industry workers puts benefits at 30 percent of total compensation, which is 43 percent on top of pay. At that rate the same person costs about $242,000 a year and every time figure here grows by nearly a fifth.

---

## Part 2. What the engine gives back to one marketer

Here is where the time goes today, and where it goes with the engine doing the first draft and the checking. These are modeled hours, not a time study; Part 6 says how to measure your own.

| Task | How often | Today | With the engine | Hours returned |
|---|---|---|---|---|
| Specifying a new or rebuilt flow: audience, logic, copy in the platform's code, QA, test size, legal check | 12 a year | 22 hours | 8 hours, confirming the assumptions and editing the copy | 168 |
| Revising a live flow | 24 a year | 3 hours | 1 hour; only what changed is redone, approved copy is kept | 48 |
| Routine deliverability checks | weekly | 1.5 hours | 15 minutes, reading the morning check | 65 |
| Investigating an inbox problem | 2 a year | 16 hours | 6 hours; the check names the broken record and writes the fix | 20 |
| Monthly flow performance reporting | monthly | 6 hours | 2 hours; the daily Google Sheet already holds the numbers | 48 |
| Reading a holdout test | 8 a year | 4 hours | 1 hour | 24 |
| List hygiene and sunset segments | quarterly | 6 hours | 1 hour; rebuilt daily | 20 |
| **Total** | | | | **393 hours, about 22 percent of a working year** |

Building the flow inside the email platform is not counted: it takes the same time either way, and the engine hands over instructions rather than doing it.

At $113 an hour that is **$44,572 of paid time per marketer per year**, moved from assembly and checking to the parts of the job that need a person: deciding what to say, to whom, and why.

What it costs, for a team of three:

| | One-time | Per year |
|---|---|---|
| Claude seats for 3 people (assumed $30 a month each; use your plan's price) | | $1,080 |
| Claude API for copy written outside a Claude conversation (budgeted at $300 a person) | | $900 |
| Running the daily check (scheduled job minutes, budgeted) | | $240 |
| Half a day a quarter to review the rules, thresholds and sunset list | | $1,815 |
| Setup: one engineer day for the daily check, two hours per marketer | $1,680 | |
| **Total** | **$1,680** | **$4,035** |
| **Per marketer** | **$560** | **$1,345** |

---

## Part 3. The inbox: what an early warning is worth

Every email program depends on one shared asset: the sending domain's reputation with Gmail, Yahoo and Microsoft. When it slips, mail starts landing in spam, and nothing announces it. Results drift down for a few weeks before someone notices, then it takes days to trace and fix. Every flow and every campaign loses money the whole time.

The engine shortens the first part. Its morning check reads the domain's authentication records, the engagement of the list, and the sending gate every day, so a broken record or a list going stale shows up the next morning.

A modest scenario, with every input replaceable:

| | |
|---|---|
| Revenue your email platform credits to email in a year | $3,000,000 (assumption) |
| Share of that the email truly causes, measured against a holdout | 38 percent (from the repository's worked readout, a fictional company; replace with your own) |
| Share of mail landing in spam during the incident | 20 percent (assumption) |
| True revenue lost per day while it lasts | $625 |
| Without the engine: about 21 days to notice, 7 to fix | $17,490 lost |
| With the engine: noticed the next morning, 7 days to fix | $4,997 lost |
| **Kept by finding it three weeks sooner** | **$12,493 per incident** |

That is one incident, at a company whose email earns $3 million. The figure scales directly with email revenue and with how long problems go unnoticed today. It does not count the slower, larger effect the engine also works on: removing people who never engage, who are what pulls a sender toward spam in the first place.

---

## Part 4. Measurement: spending on what works

Email platforms credit a flow with every purchase that followed it, including the ones that would have happened anyway. The engine ships every flow with a holdout, a small random group that gets nothing, and reports three numbers side by side: what the platform credited, what the flow truly added, and the gap.

In the repository's worked readout (a fictional company), the platform credited a win-back flow with **$41,900**. Against its holdout, the flow added about **$15,880**. The other **$26,020, 62 percent of the credit,** would have come in anyway, and the engine says plainly that even the $15,880 is not yet statistically certain.

This memo does not put a dollar figure on better decisions, because it depends entirely on what you would have done differently. The mechanism is simple: a team reading the credited number scales that flow; a team reading the true number keeps testing it and puts its next hours somewhere that pays. Across a program of twenty flows, that is where most of the money is.

---

## Part 5. Legal exposure: the downside the engine checks for

Most lifecycle mistakes cost a few points of performance. A few cost far more, and they are easy to make by accident:

| Mistake | What US law provides | At scale |
|---|---|---|
| A marketing text sent without the right consent or outside allowed hours | $500 in statutory damages per text, up to three times that if willful (47 U.S.C. 227(b)(3)) | One send to 5,000 people: **$2.5 million to $7.5 million** |
| A marketing email without a postal address or a working opt-out | Penalties up to $53,088 per email (FTC) | Every email in the send |

These are measures of exposure, not expected losses: most cases settle, and most companies never face one. The point is the asymmetry. The engine checks every plan against its 22 rules, for the countries in your audience, holds every text to the strictest state calling window it tracks, blocks a plan with no postal address, and lists every consent question for your counsel, for $1,345 a year per marketer.

---

## Part 6. What a leader can do with the time

The 393 hours are per person. For a team of three, they add up to **1,179 hours, about two thirds of one person's working year**, worth **$133,717** at the loaded cost in Part 1. Three common uses:

- **Ship more.** The same team specifies more flows, and more of them carry a real test. The constraint stops being "how many things can we assemble" and becomes "how many good ideas do we have."
- **Wait on the next hire.** Two thirds of a person is the difference between hiring this quarter and hiring next year. That is a leadership decision; the engine makes it safer, not automatic.
- **Raise the floor.** Every flow gets the same legal check, the same inbox check and the same honest measurement, whoever built it and however busy they were.

---

## Part 7. What has to be true, and what could go wrong

**The time model has to be roughly right.** The task hours in Part 2 are modeled. Measure them: have two marketers log their time on these tasks for two weeks before and two weeks after. If the real figure is half the model, the engine still returns about 200 hours a person, more than fifteen times its cost.

**People have to use the morning check.** It takes two minutes to read. A team that never reads it gets none of Part 3.

**The copy still needs a person.** The engine drafts every message and checks it; it marks the places where a real product detail must replace a placeholder. A team that ships drafts unedited will get generic copy.

**The compliance check is a checklist, not a lawyer.** It blocks what clearly breaks a rule it tracks and turns everything else into a question for counsel. It tracks some states' texting rules, not all fifty.

**Small lists read slowly.** A flow with a few dozen entrants a month cannot prove a lift inside a year. The engine says so instead of promising a date, which is honest but means Part 4 pays off slowly for small programs.

---

## Part 8. What is proven today, and what you should test yourself

This engine has **not** been run in a production deployment yet. Here is the honest split.

**Verified: the repository's 300-plus automated tests check these on every change.**

| Claim | How it is checked |
|---|---|
| It cannot send a message on its own | It sends no email or text itself. The two commands that reach a platform, `lcm send` and `lcm build --create`, only print what they would do unless given `--approve` and the platform's key, and what build creates starts as a draft; tests run both against a mocked API |
| The audience SQL selects the right people | Recomputed independently on 40 dates, and run on a real PostgreSQL database |
| Templates will not break on send | Every message is parsed in its platform's own template language |
| Every flow respects the rules | Simulated customers walk every generated flow: the holdout gets nothing, no send without consent, nothing after the goal, frequency caps and permitted send windows hold |
| Compliance rules are traceable | Every rule names its source and a check that exists |
| A failed or changed plan resumes cleanly | A failure resumes at the failed step; a change keeps approved copy only while its words are unchanged |

**Not yet measured, and how to measure it.**

| Open question | Method |
|---|---|
| How much time it actually returns | The two-week before and after log in Part 7 |
| How often inbox problems happen at your company, and how long they take to notice | Check your last year of deliverability incidents against the scenario in Part 3 |
| Performance on a very large list | Run the daily check against a full warehouse export and time it |
| Security and data handling | Certifications belong to the company running the software, not to the code. The engine reads through connectors you already authorized and keeps working files in your own folder; your security team confirms the rest |

---

## Run it with your numbers

```
python3 engine/measurement/roi.py                                           # the figures in this memo
python3 engine/measurement/roi.py --pay 130000 --team 4                     # your pay and team size
python3 engine/measurement/roi.py --benefits-load 0.43                      # the government benefits figure instead of 20 percent
python3 engine/measurement/roi.py --email-revenue 8000000 --incremental-share 0.3 --spam-share 0.25
python3 engine/measurement/roi.py --json
```

| Assumption | Used here | What moves when you change it |
|---|---|---|
| Pay | $166,790 (BLS median, marketing managers) | every time figure, in proportion |
| Benefits load | 20 percent of pay | every time figure; 43 percent adds nearly a fifth |
| Productive hours | 1,800 a year | the hourly rate |
| Task hours | Part 2 | the 393 hours; measure your own |
| Team size | 3 | the per-person share of the engine's fixed costs |
| Claude seat price | $30 a month | a few hundred dollars a year |
| Email revenue | $3 million a year | Part 3, in proportion |
| True share of email revenue | 38 percent | Part 3, in proportion; your holdout gives the real figure |
| Share of mail in spam during an incident | 20 percent | Part 3, in proportion |
| Days to notice without the engine | 21 | Part 3: each day is about $625 at these settings |
| Size of one SMS send | 5,000 people | Part 5, in proportion |

---

## Sources

- [U.S. Bureau of Labor Statistics, Occupational Outlook Handbook, Advertising, Promotions, and Marketing Managers](https://www.bls.gov/ooh/management/advertising-promotions-and-marketing-managers.htm): median annual wage for marketing managers $166,790 in May 2025.
- [U.S. Bureau of Labor Statistics, Employer Costs for Employee Compensation, June 2026](https://www.bls.gov/opub/ted/2026/compensation-costs-for-private-industry-workers-averaged-46-89-per-hour-worked-in-june-2026.htm): private industry total compensation $46.89 per hour; wages 70.0 percent, benefits 30.0 percent.
- [Klaviyo, email marketing benchmarks](https://www.klaviyo.com/products/email-marketing/benchmarks): flows generate about 41 percent of email revenue from 5.3 percent of sends, across more than 183,000 customers.
- [Burr & Forman, The TCPA: recoverable damages](https://www.burr.com/telephone-consumer-protection-act/the-tcpa-recoverable-damages), summarizing 47 U.S.C. 227(b)(3): $500 per violation, up to $1,500 for willful or knowing violations.
- [Federal Trade Commission, CAN-SPAM Act: a compliance guide for business](https://www.ftc.gov/business-guidance/resources/can-spam-act-compliance-guide-business): penalties up to $53,088 per violating email.
- `engine/measurement/roi.py` in this repository: the arithmetic. `examples/stats/winback_readout.json`: the fictional worked readout.
