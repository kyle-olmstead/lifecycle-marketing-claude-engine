# Deliverability operations: the daily read

Deliverability problems start small and at one provider. Outlook.com begins deferring a little more each
day, or one Yahoo segment draws three complaints, and a week later the whole domain is in the spam folder.
The deliverability monitor reads your ESP's message events every morning, splits them by mailbox provider,
and says what to do today: continue the ladder, hold, drop back a step, or stop.

It runs inside the morning check (`engine/workflows/daily.py`) whenever your message events carry bounce
detail, and on its own:

```bash
python3 engine/deliverability/monitor.py --data exports/ --markdown
python3 engine/deliverability/monitor.py --data exports/ --warmup plan.json --warmup-start 2026-10-01 --mx mx.json --markdown
```

Try it on the scenario data, a six-day warm-up that goes wrong on purpose:

```bash
python3 engine/deliverability/monitor.py --data examples/deliverability/monitor --mx examples/deliverability/monitor/mx.json --markdown
```

## What it needs

The `message_events` table (`engine/data/schema.py`), with three optional columns filled for bounces and deferrals:

| Column | What it holds | Example |
|---|---|---|
| `recipient_domain` | the domain of the recipient address | `outlook.com` |
| `smtp_status` | the enhanced status code | `4.7.650` |
| `diagnostic` | the receiving server's text | `451 4.7.650 The mail server [192.0.2.10] has been temporarily rate limited due to IP reputation.` |

Bounces are `event_name = bounced` (the message failed for good), deferrals are `event_name = deferred` (a
delivery attempt failed and will be retried). When `recipient_domain` is empty the monitor takes the domain
from `users.email`. Complaints are `event_name = complained`; see "Feeding complaints in" below.

## The daily read

Read it top down. The first line is the verdict for the latest day in the data, then what it means and why.

1. **Verdict and why.** The worst verdict across all providers, with each reason and its numbers.
2. **Today by provider.** Delivered, hard bounces, reputation blocks, authentication failures, deferrals,
   complaints and unsubscribes, each with its rate and the denominator behind it, for every provider.
3. **Day by day.** The verdict for each day, so you can see when a problem started.
4. **What to check, by provider.** For each provider that needs attention: what the monitor saw, what to
   check, and where (the provider's own tools, with links).
5. **Warm-up.** With `--warmup`, each day against the plan: planned and actual volume, and the volume the
   verdict allows tomorrow.

### The rates and their denominators

| Rate | Numerator | Denominator |
|---|---|---|
| Hard bounce rate | hard bounces, plus bounces that could not be classed (the conservative reading) | sent: delivered plus bounced |
| Block rate | reputation and content blocks | attempts: sent plus deferral events |
| Deferral rate | deferral events | attempts |
| Complaint rate | complaints | delivered, at providers that send complaints |
| Unsubscribe rate | unsubscribes | delivered |

Gmail and Apple send no per-message complaints, so the day's complaint rate is complaints divided by
deliveries to the providers that do (Yahoo, AOL, Outlook.com and unmapped domains). Counting Gmail's
deliveries would only make the rate look better than it is.

### Small samples

A rate is judged only when its denominator reaches the minimum sample: 200 messages by default, and never
fewer than one divided by the threshold, so a complaint rate (line at 0.1%) needs at least 1,000 deliveries.
Below that the rule says "too few sends to judge" and changes nothing: one complaint in 400 deliveries is
not an alarm. If the whole day is too small to judge, the day reads HOLD, because the ladder should not
advance on no evidence. Authentication failures are different: a provider saying your mail failed SPF,
DKIM or DMARC is a fact, not a rate, so one is enough.

## What each verdict means

| Verdict | What triggers it | What to do |
|---|---|---|
| CONTINUE | every criterion is clear | take the next step of the ladder |
| HOLD | hard bounces between 1% and 2%; the day is too small to judge; or fewer than two clean days since a DROP BACK or STOP | stay at today's volume and step |
| DROP BACK | complaints above 0.1%; any reputation or content block at a provider; deferrals climbing two days running at one provider; Postmaster spam rate above 0.1% | go back one step, hold until two clean days, then resume at that step |
| STOP | any authentication failure; complaints above 0.3%; hard bounces above 2%; reputation blocks rising two days running | the sending gate goes RED: stop bulk sending, fix the cause, re-run the gate, and resume only when it reads GREEN |

The criteria are the warm-up's abort criteria (`ABORT_RULES` in `engine/deliverability/warmup.py`), and
every threshold names its source there: the 0.1% and 0.3% complaint lines are Google's and Yahoo's
published spam-rate guidance, the hard bounce bands are the sending gate's (`engine/deliverability/analyze.py`).
A plan written by `warmup.py` carries its own copy of the rules, and the monitor uses the plan's copy, so a
team that wants stricter lines edits the plan.

## Bounce classes

`engine/deliverability/bounces.py` puts every bounce and deferral in one class, from the enhanced status code
(RFC 3463, the RFC 5248 registry, RFC 7372) and the server's text, using each provider's documented codes first.

| Class | Meaning | Action | Examples |
|---|---|---|---|
| hard | the address is bad | suppress, never retry | `550 5.1.1`, Yahoo `554 delivery error: dd This user doesn't have a yahoo.com account` |
| soft | a temporary mailbox or server problem | retry | `452 4.2.2` inbox out of storage, `421 4.4.5` server busy |
| block | the receiver refused the sender | fix the sender | `550 5.7.1` low reputation, Outlook.com `550 SC-001`, Microsoft 365 `5.7.606` banned IP |
| auth | authentication or bulk-sender requirements failed | fix the sender | Gmail `550 5.7.26`, Outlook.com `550 5.7.515`, Microsoft 365 `5.7.509` |
| rate | the provider is throttling you | retry, slower | Gmail `421 4.7.28`, Outlook.com `451 4.7.650`, Yahoo `421 4.7.0 [TSS04]` |
| unknown | nothing recognizable | read the text; never suppress on this alone | `5.0.0 Unknown error` |

Blocks also carry a kind. Reputation and content blocks count toward the DROP BACK criterion. Policy blocks
(one organization's rule, such as an allowlist) and technical blocks (message size, format) are shown but say
nothing about your reputation. Check any server text yourself:

```bash
python3 engine/deliverability/bounces.py "550 5.7.26 This email has been blocked because the sender is unauthenticated." --provider gmail.com
```

The examples the classifier is tested on are in `examples/deliverability/bounces/corpus.json`.

## What to do, per provider

**Gmail (personal accounts).** Open [Postmaster Tools](https://postmaster.google.com/v2/): Compliance status
says which sender requirement fails, Spam rate shows the user-reported spam rate (keep it below 0.1%, never
0.3%), and Delivery errors shows what was rejected and why
([dashboards](https://support.google.com/mail/answer/14668346), [sender guidelines FAQ](https://support.google.com/a/answer/14229414)).
Look up any code in the [Gmail SMTP error reference](https://knowledge.workspace.google.com/admin/support/troubleshooting/gmail-smtp-errors-and-codes).
Gmail sends no individual complaints: add a Feedback-ID header and read the
[feedback loop](https://support.google.com/mail/answer/6254652) dashboard instead.

**Google Workspace (business Gmail).** Postmaster Tools and the sender guidelines cover personal accounts
only. A block here is usually the recipient organization's own rule (an allowlist, TLS required): send the
exact error to the recipient's IT team.

**Outlook.com, Hotmail, Live, MSN.** Check [SNDS](https://aka.ms/snds) for your sending IPs (complaint rate,
trap hits, filter result). Enroll every sending IP in [JMRP](https://substrate.office.com/ip-domain-management-snds/snds/jmrp)
so complaints come back. Codes RP, SC, OU and DY are explained on the
[Outlook.com troubleshooting page](https://substrate.office.com/ip-domain-management-snds/Postmaster/Troubleshooting),
which also has the sender support form. `550 5.7.515` means the From domain failed Microsoft's authentication
requirement for senders of 5,000 or more messages a day ([help page](https://support.microsoft.com/en-us/outlook/fix-ndr-error-550-5-7-515-in-outlook-com)).
Deferrals in the 4.7.500 to 4.7.699 range are throttling on IP reputation: lower the hourly rate, do not retry faster.

**Microsoft 365 (business).** Codes are in the
[Exchange Online NDR reference](https://learn.microsoft.com/en-us/exchange/mail-flow-best-practices/non-delivery-reports-in-exchange-online/non-delivery-reports-in-exchange-online).
`5.7.606` to `5.7.649` means the sending IP is banned: request removal at the delist portal (https://sender.office.com/).
`5.7.7xx` codes are the recipient tenant's own rules.

**Yahoo and AOL.** Use the [Sender Hub](https://senders.yahooinc.com/). TS codes are temporary deferrals caused
by complaints, content, reputation or volume: retry later and lower volume; if one persists for days, open a
sender support request. PH codes are content blocks (malware, phishing, or links to them)
([error codes](https://senders.yahooinc.com/smtp-error-codes/)). Enroll your DKIM domain in the
[Complaint Feedback Loop](https://senders.yahooinc.com/complaint-feedback-loop). Yahoo's line is a spam rate
below 0.3%, measured on mail delivered to the inbox ([best practices](https://senders.yahooinc.com/best-practices)).

**Apple iCloud Mail.** No feedback loop. Requirements and the postmaster contact (icloudadmin@apple.com, with
your IPs and the SMTP errors) are on [Postmaster information for iCloud Mail](https://support.apple.com/HT204137).

**Corporate gateways (Proofpoint, Mimecast, Barracuda, Cisco).** A gateway filters for one organization. Ask the
recipient's IT team to allowlist your domain and IPs, quoting the exact error.

### Mapping business domains

Consumer domains are mapped from a list. A business domain is mapped from its MX hosts, so give them in a file:

```json
{
  "acme-corp.example": ["smtp.google.com"],
  "globex.example": ["globex-example.mail.protection.outlook.com"],
  "initech.example": ["mx1-us1.ppe-hosted.com"]
}
```

Pass it with `--mx mx.json` (or `LCM_MX_MAP` for the morning check), or let the monitor look MX records up with
`--lookup-mx`. Nothing is looked up unless you ask.

## Feeding complaints in

Complaint reports arrive by email in the Abuse Reporting Format (RFC 5965). To use them:

1. **Enroll.** Yahoo's Complaint Feedback Loop (per DKIM domain, covering AOL and the other domains Yahoo
   hosts) and Microsoft's JMRP (per sending IP). Gmail has no per-message feedback loop.
2. **Put the user id in every message.** Add a header such as `X-LCM-User-Id: u_0042` in your ESP. Microsoft's
   reports no longer include the recipient's address (since June 2026 its JMRP reports carry no recipient field
   and replace the original To), but they keep your X-headers.
3. **Save the reports** as `.eml` files in a folder, and turn them into message events:

```bash
python3 engine/deliverability/complaints.py fbl_reports/ --users exports/users.csv --out exports/complaints.csv
```

The output has the `message_events` columns, so append it to your export (or pass `--out exports/message_events.csv
--append`). The engagement tiers then suppress each complainer, and the monitor counts the complaints by provider
and day. Reports it cannot match to a person are listed with what was tried. If you cannot add a header, it can
also read a user token from the List-Unsubscribe URL (`--unsubscribe-pattern`), from a VERP envelope sender
(`--verp-pattern`), or join the recipient address to `users.email` when the report includes it.

## What was not verified

Everything the monitor does is cited in the code. These parts rest on secondary sources, are marked
`verified=False`, and can lower a verdict but never raise one:

- Outlook.com's `451 4.7.650` wording ("temporarily rate limited due to IP reputation"): Microsoft documents only
  the 4.7.500 to 4.7.699 range ("Access denied, please try again later"); the wording comes from Postmastery.
- Outlook.com's S3150 block-list text, and the Microsoft 365 delist portal: Microsoft Q&A answers, not Microsoft's reference.
- Yahoo's exact wording for TS, BL and `5.7.9` errors: Yahoo publishes the categories, not every code.
- Microsoft's JMRP report format: from Postmastery's description, not a Microsoft page.
- Country domains (hotmail.co.uk, yahoo.de) are matched by name pattern, and several MX patterns (Outlook.com's
  consumer MX, pphosted.com, Mimecast, Barracuda) by observed hosts: confirm with an MX lookup.
