"""
The daily deliverability monitor: problems caught the day they start, per mailbox provider.

Reads message_events (through engine/data/load.py) and counts, for every day and every mailbox provider:
deliveries, hard bounces, reputation and content blocks, authentication failures, deferrals, complaints and
unsubscribes. Each rate is shown with its numerator and its denominator, because a rate without one is how
a 0.4% complaint rate on 250 messages becomes a false alarm:

  hard_bounce_rate   hard bounces / sent              sent = delivered + bounced (each message ends one way)
  block_rate         reputation and content blocks / attempts   attempts = sent + deferral events
  deferral_rate      deferral events / attempts
  complaint_rate     complaints / delivered           at a provider that sends per-message complaints; for the
                                                      whole day, / deliveries to such providers (Gmail and Apple
                                                      send none, so their deliveries would only dilute the rate)
  unsubscribe_rate   unsubscribes / delivered

Every day it evaluates the warm-up's abort criteria (ABORT_RULES in engine/deliverability/warmup.py, or the
plan's own copy) and returns a verdict per day and per provider:

  CONTINUE   every criterion is clear: take the ladder's next step
  HOLD       stay at today's step (hard bounces in the 1 to 2% band, too few sends to judge the day, or fewer
             than two clean days since an abort)
  DROP BACK  go back one step and hold until two clean days (complaints above 0.1%, a reputation or content
             block, deferrals climbing two days running at one provider)
  STOP       the sending gate goes RED: stop bulk sending (an authentication failure, complaints above 0.3%,
             hard bounces above 2%, or blocks rising two days running)

Small samples never trigger an alarm: a rate is judged only when its denominator reaches the minimum sample
(200 by default, and never less than 1 / the threshold, so one complaint in 400 deliveries cannot cross 0.1%
by itself). Below that the rule says "too few sends to judge". Counts that are facts (a provider rejected the
mail for failing authentication) are not rates and need no minimum.

Bounces and deferrals are classed by engine/deliverability/bounces.py and providers mapped by
engine/deliverability/providers.py. A bounce that cannot be classed (no status code and no text, or nothing
recognizable) is counted as hard in the hard bounce rate (the conservative reading, matching
engine/agents/engagement.py, which treats every bounce as hard), and the output says how many were.

    python3 engine/deliverability/monitor.py --data examples/deliverability/monitor --markdown
    python3 engine/deliverability/monitor.py --data examples/deliverability/monitor --mx examples/deliverability/monitor/mx.json --warmup plan.json --warmup-start 2026-10-01
    lcm deliverability monitor --data <folder> [--warmup plan.json]     (once wired into engine/cli/main.py)
"""
from __future__ import annotations

if __package__ in (None, ""):  # run as a file (python3 <path>): make the repository importable
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from engine.deliverability import bounces as B
from engine.deliverability import providers as P
from engine.deliverability import warmup as W

VERDICT_RANK = {v: i for i, v in enumerate(W.VERDICTS)}
ABORTS = ("DROP BACK", "STOP")
ALL = "ALL"
VERDICT_MEANING = {
    "CONTINUE": "Every criterion is clear: take the next step of the ladder.",
    "HOLD": "Stay at today's volume and step. Do not advance until the reason clears.",
    "DROP BACK": "Go back one step of the ladder. " + W.ON_ABORT,
    "STOP": "The sending gate goes RED: stop bulk sending, fix the cause, re-run the sending gate, and resume only when it reads GREEN.",
}
GATE_FOR = {"STOP": "RED", "DROP BACK": "AMBER", "HOLD": "AMBER", "CONTINUE": None}
CLEAN_DAYS_TO_RESUME = 2   # W.ON_ABORT: "hold until two clean days"
EVENTS = ("delivered", "bounced", "deferred", "complained", "unsubscribed")

# ----------------------------------------------------------------------------------------------
# per-provider triage: what to check, and where (each tool with its source; verified October 2026 unless marked)
# ----------------------------------------------------------------------------------------------

GMAIL_ERRORS = ("Gmail SMTP errors and codes", B.GMAIL, "the meaning of each 4xx and 5xx reply")
PLAYBOOK: Dict[str, Dict[str, Any]] = {
    "google": {
        "note": "Personal Gmail accounts. Gmail sends no per-message complaints, so a Gmail complaint count here is not its spam rate.",
        "tools": [
            ("Postmaster Tools: Compliance status", "https://postmaster.google.com/v2/",
             "which sender requirements are Compliant, Need work, or have No data (https://support.google.com/mail/answer/14668346)"),
            ("Postmaster Tools: Spam rate", "https://postmaster.google.com/",
             "the user-reported spam rate: keep it below 0.1%, never 0.3% (https://support.google.com/a/answer/14229414)"),
            ("Postmaster Tools: Delivery errors", "https://postmaster.google.com/", "the share of authenticated mail rejected or deferred, with reasons"),
            GMAIL_ERRORS,
            ("Gmail feedback loop", "https://support.google.com/mail/answer/6254652", "add a Feedback-ID header to see spam rates per campaign"),
        ],
        "complaints": "Gmail reports no individual complaints. Read the day's Spam rate in Postmaster Tools.",
        "blocks": "Open Delivery errors and Compliance status in Postmaster Tools. A reputation block (5.7.28, 5.7.1 low reputation, 4.7.0 suspicious) "
                  "means Gmail has seen too much unwanted mail: send only to the most engaged tier at the last clean volume.",
        "auth": "Compliance status names the failing requirement (SPF, DKIM, DMARC alignment, reverse DNS, TLS). Fix it, then re-run the sending gate.",
        "deferrals": "421 4.7.28 is Gmail throttling on reputation: lower the hourly rate to Gmail and keep volume flat until deferrals fall.",
    },
    "google_workspace": {
        "note": "Business mailboxes on Google Workspace. Gmail's sender guidelines and Postmaster Tools cover personal Gmail accounts only "
                "(https://support.google.com/a/answer/14229414).",
        "tools": [GMAIL_ERRORS],
        "blocks": "Usually the recipient organization's own rule (an allowlist, TLS required, a policy that prohibits the sender): send the exact "
                  "error to the recipient's IT team.",
        "auth": "Workspace applies the same authentication checks as Gmail: fix SPF, DKIM and DMARC alignment first.",
        "deferrals": "Workspace throttling uses the same 4.7.x codes as Gmail: slow down and hold volume.",
    },
    "microsoft": {
        "note": "Outlook.com, Hotmail, Live and MSN consumer mailboxes.",
        "tools": [
            ("SNDS", "https://aka.ms/snds", "per-IP data from Outlook.com: complaint rate, trap hits, filter result"),
            ("JMRP", "https://substrate.office.com/ip-domain-management-snds/snds/jmrp",
             "per-message complaint reports; since June 2026 in ARF without the recipient address, so put a user-id header in every message"),
            ("Outlook.com troubleshooting", B.MS_OLC, "RP, SC, OU and DY codes, 5.7.515, and the sender support form"),
            ("550 5.7.515 help", B.MS_515, "authentication required of senders of 5,000+ messages a day to Microsoft consumer mailboxes"),
        ],
        "complaints": "Check JMRP is enrolled for every sending IP, ingest the reports (engine/deliverability/complaints.py) and suppress complainers "
                      "before the next send. SNDS shows the complaint rate per IP.",
        "blocks": "Check SNDS for the sending IPs. SC-004 is a complaint-driven IP block (enroll in JMRP, suppress complainers); OU-001 is a Spamhaus "
                  "listing (request removal there). If a block persists, use the support form on the Troubleshooting page.",
        "auth": "550 5.7.515: the From domain failed Outlook.com's requirement. SPF and DKIM must pass, and DMARC must pass with alignment.",
        "deferrals": "451 4.7.500-699 and RP-001/002/003 are Outlook.com throttling on IP reputation. Do not retry faster: lower the hourly rate to "
                     "Microsoft, hold volume, and watch SNDS until deferrals fall.",
    },
    "microsoft_365": {
        "note": "Business mailboxes on Microsoft 365 (Exchange Online Protection).",
        "tools": [
            ("Exchange Online NDR reference", B.MS_NDR, "every 4.x.x and 5.x.x code Exchange Online returns"),
            ("Microsoft 365 delist portal", "https://sender.office.com/",
             "removal for 5.7.606-649 banned sending IPs (named in Microsoft Q&A answers; the portal itself was not fetched)"),
        ],
        "blocks": "5.7.606-649 means the sending IP is banned: request removal at the delist portal. 5.7.7xx codes are the recipient tenant's own rules: "
                  "contact the recipient's IT team.",
        "auth": "5.7.509 means DMARC failed against a p=reject policy; 5.7.23 an SPF failure. Fix alignment for this sender.",
        "deferrals": "4.7.500-699 is 'Access denied, please try again later': slow down and hold volume.",
    },
    "yahoo": {
        "note": "Yahoo, AOL and the other domains Yahoo hosts.",
        "tools": [
            ("Yahoo Sender Hub", "https://senders.yahooinc.com/", "sender tools, CFL enrollment and the sender support request"),
            ("Yahoo SMTP error codes", B.YAHOO, "TS* temporary deferrals, PH* content blocks, 553/554 permanent errors"),
            ("Complaint Feedback Loop", "https://senders.yahooinc.com/complaint-feedback-loop", "ARF reports for each enrolled DKIM domain"),
            ("Sender best practices", "https://senders.yahooinc.com/best-practices",
             "spam rate below 0.3%, measured on mail delivered to the inbox; SPF, DKIM and DMARC for bulk senders"),
        ],
        "complaints": "Confirm the DKIM d= domain is enrolled in the CFL, ingest the reports (engine/deliverability/complaints.py), suppress complainers, "
                      "and stop mailing the segment that produced them until the rate is back under 0.1%.",
        "blocks": "PH* is a content block (malware, phishing, or links to them): check every link and attachment domain in the message.",
        "auth": "Yahoo requires SPF or DKIM from every sender, and SPF, DKIM and a DMARC policy (p=none at least) from bulk senders.",
        "deferrals": "TS* deferrals are temporary: retry later and lower volume to Yahoo. If the same TS code persists for days, open a Sender Support "
                     "Request with the codes from your logs.",
    },
    "apple": {
        "note": "iCloud Mail (icloud.com, me.com, mac.com). Apple offers no feedback loop, so complaints never appear here.",
        "tools": [("Postmaster information for iCloud Mail", P.SRC_APPLE,
                   "requirements (SPF, DKIM, DMARC, reverse DNS); write to icloudadmin@apple.com with your IPs and the SMTP errors")],
        "complaints": "No feedback loop: watch unsubscribes and clicks at iCloud instead.",
        "blocks": "Most iCloud errors include a URL with more information; send the IPs and errors to icloudadmin@apple.com.",
        "auth": "iCloud requires SPF, DKIM, a DMARC policy and reverse DNS.",
        "deferrals": "Slow down and hold volume to iCloud until deferrals fall.",
    },
    "gateway": {
        "note": "A corporate email gateway filters for one organization: most blocks are that organization's policy or the vendor's reputation network.",
        "tools": [],
        "blocks": "Ask the recipient's IT team to allowlist your sending domain and IPs, quoting the exact error, and check the vendor's sender support pages.",
        "auth": "Gateways check SPF, DKIM and DMARC like the big providers: fix authentication first.",
        "deferrals": "Gateways throttle per organization: slow down to that domain.",
    },
    "other": {
        "note": "Domains the engine could not map. Give their MX hosts (--mx) or allow lookups (--lookup-mx) to see which provider filters them.",
        "tools": [],
        "blocks": "Read the error text: it usually names the filter or blocklist.",
        "auth": "Fix SPF, DKIM and DMARC alignment; every receiver checks them.",
        "deferrals": "Slow down to these domains and hold volume.",
    },
}
GATEWAY_TOOLS = {
    "mimecast": [("Mimecast sender feedback", "https://community.mimecast.com/s/sender-feedback",
                  "Mimecast's unblock request (cited by https://www.spamresource.com/2024/02/isp-deliverability-guide-mimecast.html; not fetched)")],
}
HARD_BOUNCE_ADVICE = ("Hard bounces are addresses that do not exist: suppress them now, and check where those addresses came from "
                      "(an old import or a purchased list raises the rate fast).")


def _playbook(provider: str) -> Dict[str, Any]:
    kind = P.PROVIDERS.get(provider, P.PROVIDERS["other"]).kind
    if provider in PLAYBOOK:
        return PLAYBOOK[provider]
    pb = dict(PLAYBOOK["gateway" if kind == "gateway" else "other"])
    pb["tools"] = list(pb["tools"]) + GATEWAY_TOOLS.get(provider, [])
    return pb


# ----------------------------------------------------------------------------------------------
# counting
# ----------------------------------------------------------------------------------------------

def _blank() -> Dict[str, Any]:
    return {"delivered": 0, "bounces": {c: 0 for c in B.CLASSES}, "deferrals": {c: 0 for c in B.CLASSES},
            "reputation_blocks": 0, "other_blocks": 0, "auth_failures": 0, "complaints": 0, "unsubscribes": 0,
            "bounces_without_detail": 0, "unverified_rules": 0, "examples": Counter(), "domains": Counter()}


def load_rows(data: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Aggregated message_events rows: (day, domain, event_name, smtp_status, diagnostic, n). Missing recipient_domain
    is filled from users.email when the users table has the person."""
    from engine.data.load import connect
    con, notes = connect(data, require=("message_events",))
    sql = ("SELECT CAST(m.event_at AS DATE) AS day, "
           "LOWER(COALESCE(NULLIF(TRIM(m.recipient_domain), ''), NULLIF(split_part(u.email, '@', 2), ''))) AS domain, "
           "LOWER(m.event_name) AS event_name, NULLIF(TRIM(m.smtp_status), '') AS smtp_status, NULLIF(TRIM(m.diagnostic), '') AS diagnostic, "
           "COUNT(*) AS n "
           "FROM message_events m LEFT JOIN (SELECT user_id, MIN(email) AS email FROM users GROUP BY user_id) u ON u.user_id = m.user_id "
           "WHERE COALESCE(LOWER(m.channel), 'email') = 'email' AND LOWER(m.event_name) IN ('delivered', 'bounced', 'deferred', 'complained', 'unsubscribed') "
           "AND m.event_at IS NOT NULL "
           "GROUP BY ALL ORDER BY 1, 2, 3")
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()], notes


def has_bounce_detail(data: str) -> bool:
    """True when message_events carries a status code or diagnostic text on any bounce or deferral (the daily check's trigger).
    A folder without message_events is simply False."""
    from engine.data.load import connect
    con, _ = connect(data, require=())
    n = con.execute("SELECT COUNT(*) FROM message_events WHERE LOWER(event_name) IN ('bounced', 'deferred') "
                    "AND (NULLIF(TRIM(smtp_status), '') IS NOT NULL OR NULLIF(TRIM(diagnostic), '') IS NOT NULL)").fetchone()[0]
    return bool(n)


def count(rows: List[Dict[str, Any]], resolver: P.Resolver) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """{(day, provider or ALL): counts}."""
    cells: Dict[Tuple[str, str], Dict[str, Any]] = defaultdict(_blank)
    cache: Dict[Tuple[Any, ...], B.Classification] = {}
    for r in rows:
        day = r["day"].isoformat() if isinstance(r["day"], date) else str(r["day"])[:10]
        match = resolver(r["domain"])
        n = int(r["n"])
        for key in (match.provider, ALL):
            c = cells[(day, key)]
            c["domains"][r["domain"] or "(none)"] += n if r["event_name"] == "delivered" else 0
            ev = r["event_name"]
            if ev == "delivered":
                c["delivered"] += n
            elif ev == "complained":
                c["complaints"] += n
            elif ev == "unsubscribed":
                c["unsubscribes"] += n
            else:
                k = (r["smtp_status"], r["diagnostic"], match.provider, ev)
                if k not in cache:
                    cache[k] = B.classify(r["smtp_status"], r["diagnostic"], match.provider, ev)
                cl = cache[k]
                bucket = "deferrals" if ev == "deferred" else "bounces"
                c[bucket][cl.cls] += n
                if ev == "bounced" and not r["smtp_status"] and not r["diagnostic"]:
                    c["bounces_without_detail"] += n
                if cl.cls == "block":
                    c["reputation_blocks" if cl.reputation_block else "other_blocks"] += n
                elif cl.cls == "auth":
                    c["auth_failures"] += n
                if cl.rule != "none" and not cl.verified:
                    c["unverified_rules"] += n
                text = (r["diagnostic"] or r["smtp_status"] or "(no detail)")[:160]
                c["examples"][(bucket, cl.cls, cl.kind, cl.rule, cl.code or cl.reply or "", text)] += n
    return cells


def _derived(c: Dict[str, Any], fbl_delivered: Optional[int] = None) -> Dict[str, Any]:
    bounced = sum(c["bounces"].values())
    deferred = sum(c["deferrals"].values())
    sent = c["delivered"] + bounced
    attempts = sent + deferred
    hard = c["bounces"]["hard"] + c["bounces"]["unknown"]
    return {"sent": sent, "attempts": attempts, "bounced": bounced, "deferred": deferred, "hard": hard,
            "fbl_delivered": c["delivered"] if fbl_delivered is None else fbl_delivered}


# ----------------------------------------------------------------------------------------------
# rules
# ----------------------------------------------------------------------------------------------

def _pct(x: Optional[float]) -> str:
    """Three decimals under 1%, so 0.104% does not print as the 0.10% line it crossed."""
    if x is None:
        return "n/a"
    return f"{x:.3%}" if abs(x) < 0.01 else f"{x:.2%}"


def _min_for(rule: Dict[str, Any], min_sample: int) -> int:
    thresholds = [t for t in (rule.get("above"), rule.get("hold_above")) if t]
    return max(min_sample, math.ceil(1 / min(thresholds))) if thresholds else min_sample


def _result(rule: Dict[str, Any], status: str, evidence: str, verdict: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    return {"rule": rule["id"], "criterion": rule.get("criterion"), "status": status, "verdict": verdict, "evidence": evidence, **extra}


def _rate_rule(rule: Dict[str, Any], key: str, c: Dict[str, Any], d: Dict[str, Any], min_sample: int,
               postmaster: Optional[float]) -> Dict[str, Any]:
    metric = rule["metric"]
    where = "all providers" if key == ALL else P.PROVIDERS.get(key, P.PROVIDERS["other"]).name
    if metric == "postmaster_spam_rate":
        if postmaster is None:
            return _result(rule, "not evaluable", "needs the day's Gmail Postmaster Tools spam rate (--postmaster); message_events cannot show it")
        value = postmaster
        evidence = f"Postmaster Tools spam rate {_pct(value)} at {where}"
        if rule.get("escalate_above") is not None and value > rule["escalate_above"]:
            return _result(rule, "triggered", evidence + f", above {_pct(rule['escalate_above'])}", rule["escalate_verdict"], value=value)
        if value > rule["above"]:
            return _result(rule, "triggered", evidence + f", above {_pct(rule['above'])}", rule["verdict"], value=value)
        return _result(rule, "clear", evidence, value=value)
    if metric == "complaint_rate":
        num, den, per = c["complaints"], d["fbl_delivered"], "delivered"
        if key == ALL:
            per = "delivered to providers that send complaints"
        else:
            fl = P.PROVIDERS.get(key, P.PROVIDERS["other"]).feedback_loop
            if fl in ("aggregate only", "none") and num == 0:
                why = ("Gmail sends no per-message complaints (Postmaster Tools shows its spam rate)" if key == "google" else
                       f"{where} sends no complaint reports")
                return _result(rule, "not evaluable", why, numerator=0, denominator=den, per=per)
    elif metric == "hard_bounce_rate":
        num, den, per = d["hard"], d["sent"], "sent"
    elif metric == "unsubscribe_rate":
        num, den, per = c["unsubscribes"], c["delivered"], "delivered"
    else:
        raise ValueError(f"rule {rule['id']}: unknown rate metric {metric!r}")
    need = _min_for(rule, min_sample)
    value = (num / den) if den else None
    base = {"value": value, "numerator": num, "denominator": den, "per": per, "min_sample": need}
    label = metric.replace("_", " ")
    if den < need:
        return _result(rule, "too few sends to judge", f"{label}: {num} of {den:,} {per}; at least {need:,} are needed to judge it", **base)
    evidence = f"{label} {_pct(value)} ({num:,} of {den:,} {per}) at {where}"
    if rule.get("escalate_above") is not None and value > rule["escalate_above"]:
        return _result(rule, "triggered", evidence + f", above {_pct(rule['escalate_above'])}", rule["escalate_verdict"], **base)
    if value > rule["above"]:
        return _result(rule, "triggered", evidence + f", above {_pct(rule['above'])}", rule["verdict"], **base)
    if rule.get("hold_above") is not None and value > rule["hold_above"]:
        return _result(rule, "triggered", evidence + f", in the {_pct(rule['hold_above'])} to {_pct(rule['above'])} band", rule.get("hold_verdict", "HOLD"), **base)
    return _result(rule, "clear", evidence, **base)


def _top(c: Dict[str, Any], cls: Optional[str] = None, bucket: Optional[str] = None, reputation_only: bool = False, n: int = 2) -> str:
    items = [(k, v) for k, v in c["examples"].items() if (cls is None or k[1] == cls) and (bucket is None or k[0] == bucket)
             and (not reputation_only or k[2] in B.REPUTATION_KINDS)]
    items.sort(key=lambda kv: (-kv[1], kv[0]))
    return "; ".join(f"{v}x {k[5]}" for k, v in items[:n])


def _count_rule(rule: Dict[str, Any], key: str, day: str, cells: Dict[Tuple[str, str], Dict[str, Any]]) -> Dict[str, Any]:
    c = cells[(day, key)]
    metric = rule["metric"]
    where = P.PROVIDERS.get(key, P.PROVIDERS["other"]).name
    value = c[metric]
    if value < rule["at_least"]:
        return _result(rule, "clear", f"no {metric.replace('_', ' ')} at {where}", value=value)
    sample = _top(c, cls="auth") if metric == "auth_failures" else _top(c, cls="block", reputation_only=True)
    evidence = f"{value} {metric.replace('_', ' ')} at {where} ({sample})"
    if rule.get("escalate") == "rising":
        prev_day = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
        prev = cells.get((prev_day, key))
        if prev and prev[metric] >= rule["at_least"]:
            rate_today = value / max(1, _derived(c)["attempts"])
            rate_prev = prev[metric] / max(1, _derived(prev)["attempts"])
            if rate_today > rate_prev:
                return _result(rule, "triggered", evidence + f"; rising: {_pct(rate_prev)} of attempts on {prev_day}, {_pct(rate_today)} today",
                               rule["escalate_verdict"], value=value)
    return _result(rule, "triggered", evidence, rule["verdict"], value=value)


def _rising_rule(rule: Dict[str, Any], key: str, day: str, cells: Dict[Tuple[str, str], Dict[str, Any]], min_sample: int) -> Dict[str, Any]:
    span = int(rule.get("days", 2))
    days = [(date.fromisoformat(day) - timedelta(days=i)).isoformat() for i in range(span, -1, -1)]   # oldest first, today last
    series = []
    for d in days:
        c = cells.get((d, key))
        if c is None:
            return _result(rule, "not evaluable", f"no sends at this provider on {d}; a climb needs {span + 1} consecutive days")
        dd = _derived(c)
        series.append((d, sum(c["deferrals"].values()), dd["attempts"]))
    short = [d for d, _, att in series if att < min_sample]
    rates = [(d, n / att if att else 0.0, n, att) for d, n, att in series]
    trail = ", ".join(f"{d} {_pct(r)} ({n:,} of {att:,} attempts)" for d, r, n, att in rates)
    if short:
        return _result(rule, "too few sends to judge", f"deferral rate {trail}; fewer than {min_sample:,} attempts on {', '.join(short)}")
    climbing = all(rates[i + 1][1] > rates[i][1] for i in range(span))
    today_n = rates[-1][2]
    if climbing and today_n >= int(rule.get("min_events", 0)):
        return _result(rule, "triggered", f"deferrals climbing {span} days running: {trail}", rule["verdict"], value=rates[-1][1])
    note = ""
    if rates[-1][1] > rates[-2][1] and today_n >= int(rule.get("min_events", 0)):
        note = " (rose today: watch tomorrow)"
    elif climbing:
        note = f" (climbing, but only {today_n} deferrals today; at least {rule.get('min_events')} are needed)"
    return _result(rule, "clear", f"deferral rate {trail}{note}", value=rates[-1][1])


def evaluate(cells: Dict[Tuple[str, str], Dict[str, Any]], rules: List[Dict[str, Any]], min_sample: int = W.MIN_SAMPLE,
             postmaster: Optional[Dict[str, float]] = None) -> List[Dict[str, Any]]:
    """One row per (day, provider or ALL), oldest day first, with every rule's result and the row's verdict."""
    postmaster = postmaster or {}
    days = sorted({d for d, _ in cells})
    fbl_ok = {k for k, p in P.PROVIDERS.items() if p.feedback_loop in ("per-message", "unknown")}
    out: List[Dict[str, Any]] = []
    for day in days:
        keys = sorted((k for d, k in cells if d == day and k != ALL), key=lambda k: (-_derived(cells[(day, k)])["attempts"], k))
        fbl = sum(cells[(day, k)]["delivered"] for k in keys if k in fbl_ok)
        for key in [ALL] + keys:
            c = cells[(day, key)]
            d = _derived(c, fbl if key == ALL else None)
            results = []
            for rule in rules:
                scope = rule.get("scope", ["day", "provider"])
                if (key == ALL and "day" not in scope) or (key != ALL and "provider" not in scope):
                    continue
                if rule.get("providers") and key not in rule["providers"]:
                    continue
                kind = rule["kind"]
                if kind == "rate_above":
                    results.append(_rate_rule(rule, key, c, d, min_sample, postmaster.get(day) if key == "google" else None))
                elif kind == "count_at_least":
                    results.append(_count_rule(rule, key, day, cells))
                elif kind == "rising":
                    results.append(_rising_rule(rule, key, day, cells, min_sample))
                else:
                    raise ValueError(f"rule {rule.get('id')}: unknown kind {kind!r} (rate_above, count_at_least, rising)")
            triggered = [r for r in results if r["status"] == "triggered"]
            verdict = max((r["verdict"] for r in triggered), key=lambda v: VERDICT_RANK[v], default="CONTINUE")
            judged = any(r["status"] in ("triggered", "clear") for r in results)
            reasons = [r["evidence"] for r in sorted(triggered, key=lambda r: -VERDICT_RANK[r["verdict"]])]
            out.append({"day": day, "provider": key, "name": "All providers" if key == ALL else P.PROVIDERS[key].name,
                        "counts": _public_counts(c, d), "rates": _rates(c, d), "results": results, "raw_verdict": verdict,
                        "verdict": verdict, "reasons": reasons, "judged": judged})
    _apply_hold_after_abort(out)
    for row in out:
        if row["provider"] == ALL and not row["judged"] and VERDICT_RANK[row["verdict"]] < VERDICT_RANK["HOLD"]:
            row["verdict"] = "HOLD"
            row["reasons"].append("too few sends to judge the day: do not advance the ladder on no evidence")
    return out


def _apply_hold_after_abort(rows: List[Dict[str, Any]]) -> None:
    """W.ON_ABORT: after a DROP BACK or STOP, hold until two clean days. Applied per provider (and to the ALL row)."""
    last_abort: Dict[str, Tuple[str, str]] = {}
    clean: Dict[str, int] = defaultdict(int)
    for row in rows:
        key = row["provider"]
        if row["raw_verdict"] in ABORTS:
            last_abort[key] = (row["day"], row["raw_verdict"])
            clean[key] = 0
            continue
        if key in last_abort and clean[key] < CLEAN_DAYS_TO_RESUME:
            clean[key] += 1
            when, what = last_abort[key]
            if VERDICT_RANK[row["verdict"]] < VERDICT_RANK["HOLD"]:
                row["verdict"] = "HOLD"
            extra = (" (re-run the sending gate too: resume only when it reads GREEN)" if what == "STOP" else "")
            row["reasons"].append(f"{clean[key]} of {CLEAN_DAYS_TO_RESUME} clean days since the {what} on {when}: hold until two clean days, "
                                  f"then resume at that step{extra}")


def _public_counts(c: Dict[str, Any], d: Dict[str, Any]) -> Dict[str, Any]:
    return {"delivered": c["delivered"], "sent": d["sent"], "attempts": d["attempts"],
            "hard_bounces": d["hard"], "hard_bounces_without_detail": c["bounces_without_detail"],
            "bounces": dict(c["bounces"]), "deferrals": dict(c["deferrals"]), "deferrals_total": d["deferred"],
            "reputation_blocks": c["reputation_blocks"], "other_blocks": c["other_blocks"], "auth_failures": c["auth_failures"],
            "complaints": c["complaints"], "unsubscribes": c["unsubscribes"], "complaint_denominator": d["fbl_delivered"],
            "classified_by_unverified_rule": c["unverified_rules"]}


def _rates(c: Dict[str, Any], d: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    def r(num: int, den: int, per: str) -> Dict[str, Any]:
        return {"value": round(num / den, 6) if den else None, "numerator": num, "denominator": den, "per": per}
    return {"hard_bounce_rate": r(d["hard"], d["sent"], "sent"),
            "block_rate": r(c["reputation_blocks"], d["attempts"], "attempts"),
            "auth_failure_rate": r(c["auth_failures"], d["attempts"], "attempts"),
            "deferral_rate": r(d["deferred"], d["attempts"], "attempts"),
            "complaint_rate": r(c["complaints"], d["fbl_delivered"], "delivered"),
            "unsubscribe_rate": r(c["unsubscribes"], c["delivered"], "delivered")}


# ----------------------------------------------------------------------------------------------
# triage, warm-up, the whole report
# ----------------------------------------------------------------------------------------------

def triage(rows_today: List[Dict[str, Any]], cells: Dict[Tuple[str, str], Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Plain-English checks for every provider that needs attention today, with where to look."""
    out = []
    for row in rows_today:
        key = row["provider"]
        if key == ALL:
            continue
        c = cells[(row["day"], key)]
        triggered = {r["rule"] for r in row["results"] if r["status"] == "triggered"}
        watch = [r for r in row["results"] if "watch tomorrow" in r["evidence"]]
        if row["verdict"] == "CONTINUE" and not watch and not c["other_blocks"]:
            continue
        pb = _playbook(key)
        saw, check = [], []
        if "auth_failures" in triggered:
            saw.append(f"{c['auth_failures']} authentication failure(s): {_top(c, cls='auth')}")
            check.append(pb.get("auth", PLAYBOOK["other"]["auth"]))
        if "reputation_blocks" in triggered:
            saw.append(f"{c['reputation_blocks']} reputation or content block(s): {_top(c, cls='block', reputation_only=True)}")
            check.append(pb.get("blocks", PLAYBOOK["other"]["blocks"]))
        if "complaint_rate" in triggered or "postmaster_spam_rate" in triggered:
            r = row["rates"]["complaint_rate"]
            saw.append(f"complaints {r['numerator']} of {r['denominator']:,} delivered ({_pct(r['value'])})")
            check.append(pb.get("complaints", "Suppress complainers and stop mailing the segment that produced them."))
        if "deferrals_climbing" in triggered or watch:
            ev = next(r["evidence"] for r in row["results"] if r["rule"] == "deferrals_climbing")
            saw.append(ev + (f"; most common: {_top(c, bucket='deferrals')}" if _top(c, bucket="deferrals") else ""))
            check.append(pb.get("deferrals", PLAYBOOK["other"]["deferrals"]))
        if "hard_bounce_rate" in triggered:
            r = row["rates"]["hard_bounce_rate"]
            saw.append(f"hard bounces {r['numerator']} of {r['denominator']:,} sent ({_pct(r['value'])}): {_top(c, cls='hard')}")
            check.append(HARD_BOUNCE_ADVICE)
        if c["other_blocks"] and "reputation_blocks" not in triggered:
            saw.append(f"{c['other_blocks']} policy or technical block(s), not counted as reputation: {_top(c, cls='block')}")
            check.append(pb.get("blocks", PLAYBOOK["other"]["blocks"]))
        if not saw:
            saw = [x for x in row["reasons"]]
        out.append({"provider": key, "name": row["name"], "verdict": row["verdict"], "note": pb.get("note", ""), "what_we_saw": saw,
                    "what_to_check": list(dict.fromkeys(check)) or ["Hold at today's volume until the reason above clears."],
                    "where": [{"tool": t, "url": u, "what": w} for t, u, w in pb.get("tools", [])]})
    return out


def warmup_view(plan: Dict[str, Any], start: date, day_verdicts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Each monitored day against the plan: planned and actual volume, and the volume the verdict allows tomorrow."""
    sched = {int(r["day"]): r for r in plan.get("schedule", [])}
    if not sched:
        return {"start": start.isoformat(), "days": [], "note": "the plan has no schedule (its gate was not GREEN or AMBER)"}
    rows = []
    for dv in day_verdicts:
        n = (date.fromisoformat(dv["day"]) - start).days + 1
        cur = sched.get(n)
        if cur is None:
            rows.append({"day": dv["day"], "plan_day": n, "planned": None, "actual_sent": dv["sent"], "verdict": dv["verdict"],
                         "next_volume": None, "note": "outside the plan's schedule"})
            continue
        step = int(cur.get("step", 0))
        if dv["verdict"] == "STOP":
            nxt, how = 0, "bulk sending stops; the gate is RED"
        elif dv["verdict"] == "DROP BACK":
            earlier = [r["volume"] for r in sched.values() if int(r.get("step", 0)) == step - 1]
            nxt, how = (max(earlier), f"back to step {step - 1}") if earlier else (min(r["volume"] for r in sched.values()), "back to the first step")
        elif dv["verdict"] == "HOLD":
            nxt, how = cur["volume"], f"hold at step {step}"
        else:
            following = sched.get(n + 1)
            nxt, how = (following["volume"], f"plan day {n + 1}") if following else (cur["volume"], "the plan ends here")
        rows.append({"day": dv["day"], "plan_day": n, "step": step, "planned": cur["volume"], "actual_sent": dv["sent"],
                     "verdict": dv["verdict"], "next_volume": nxt, "next": how})
    return {"start": start.isoformat(), "days": rows}


def monitor(data: str, plan: Optional[Dict[str, Any]] = None, warmup_start: Optional[str] = None, mx: Optional[Dict[str, List[str]]] = None,
            lookup_mx: bool = False, mx_lookup: Optional[P.MxLookup] = None, postmaster: Optional[Dict[str, float]] = None,
            min_sample: int = W.MIN_SAMPLE) -> Dict[str, Any]:
    """The full daily report for a data folder. plan: a warm-up plan (engine/deliverability/warmup.py JSON)."""
    rows, notes = load_rows(data)
    if not rows:
        raise ValueError(f"{data}: message_events has no delivered, bounced, deferred, complained or unsubscribed email events")
    resolver = P.Resolver(mx, lookup=lookup_mx, mx_lookup=mx_lookup)
    cells = count(rows, resolver)
    rules = list((plan or {}).get("abort_rules") or W.ABORT_RULES)
    evaluated = evaluate(cells, rules, min_sample, postmaster)
    days: List[Dict[str, Any]] = []
    for day in sorted({r["day"] for r in evaluated}):
        drows = [r for r in evaluated if r["day"] == day]
        worst = max(drows, key=lambda r: VERDICT_RANK[r["verdict"]])
        reasons = []
        for r in sorted(drows, key=lambda r: -VERDICT_RANK[r["verdict"]]):
            if r["verdict"] != "CONTINUE":
                reasons += [f"{r['name']}: {x}" for x in r["reasons"]]
        all_row = next(r for r in drows if r["provider"] == ALL)
        days.append({"day": day, "verdict": worst["verdict"], "gate": GATE_FOR[worst["verdict"]], "sent": all_row["counts"]["sent"],
                     "providers": {r["provider"]: r["verdict"] for r in drows if r["provider"] != ALL}, "reasons": reasons})
    today = days[-1]
    rows_today = [r for r in evaluated if r["day"] == today["day"]]
    seen = resolver.seen()
    by_provider: Dict[str, List[str]] = defaultdict(list)
    for m in seen:
        by_provider[m.provider].append(m.domain or "(none)")
    unverified_maps = [m.as_dict() for m in seen if m.domain and not m.verified and m.provider != "other"]
    report_notes = list(notes)
    unknown = sum(r["counts"]["bounces"]["unknown"] for r in evaluated if r["provider"] == ALL)
    no_detail = sum(r["counts"]["hard_bounces_without_detail"] for r in evaluated if r["provider"] == ALL)
    if unknown:
        report_notes.append(f"{unknown} bounce(s) could not be classed ({no_detail} with no status code or text at all) and were counted as hard "
                            "in the hard bounce rate (the conservative reading). Their addresses are not suppressed on that alone.")
    unv = sum(r["counts"]["classified_by_unverified_rule"] for r in evaluated if r["provider"] == ALL)
    if unv:
        report_notes.append(f"{unv} bounce(s) or deferral(s) were classed by a rule not found in the provider's own documentation (verified=False in "
                            "engine/deliverability/bounces.py).")
    if any(r["provider"] == "google" for r in evaluated):
        report_notes.append("Gmail sends no per-message complaints: the complaint rate covers Yahoo, AOL, Outlook.com and unmapped domains. Read Gmail's "
                            "spam rate in Postmaster Tools (https://support.google.com/mail/answer/6254652).")
    out: Dict[str, Any] = {
        "data": data, "first_day": days[0]["day"], "as_of": today["day"], "min_sample": min_sample,
        "today": {"day": today["day"], "verdict": today["verdict"], "meaning": VERDICT_MEANING[today["verdict"]], "gate": today["gate"],
                  "reasons": today["reasons"], "providers": today["providers"]},
        "days": days,
        "triage": triage(rows_today, cells),
        "rows": evaluated,
        "providers": [{"provider": k, "name": P.PROVIDERS[k].name, "domains": sorted(v)} for k, v in sorted(by_provider.items())],
        "provider_mappings_unverified": unverified_maps,
        "rules": rules,
        "verdict_meanings": VERDICT_MEANING,
        "notes": report_notes,
        "warmup": None,
    }
    if plan:
        start = date.fromisoformat(warmup_start or plan.get("start_date") or days[0]["day"])
        out["warmup"] = warmup_view(plan, start, days)
    return out


# ----------------------------------------------------------------------------------------------
# rendering and CLI
# ----------------------------------------------------------------------------------------------

def _cell(num: int, den: int, per: str) -> str:
    return f"{num:,}" if not den else f"{num:,} ({_pct(num / den)} of {den:,} {per})"


def render_markdown(r: Dict[str, Any], last_days: int = 14) -> str:
    t = r["today"]
    L = [f"## Deliverability monitor, {t['day']}: {t['verdict']}", "", f"**What it means:** {t['meaning']}", ""]
    if t["gate"]:
        L += [f"Sending gate: {t['gate']} from this monitor.", ""]
    if t["reasons"]:
        L += ["Why:", ""] + [f"- {x}" for x in t["reasons"]] + [""]
    L += [f"### Today by provider ({t['day']})", "",
          "| Provider | Delivered | Hard bounces | Reputation blocks | Auth failures | Deferrals | Complaints | Unsubscribes | Verdict |",
          "|---|---:|---:|---:|---:|---:|---:|---:|---|"]
    for row in [x for x in r["rows"] if x["day"] == t["day"]]:
        c = row["counts"]
        L.append(f"| {row['name']} | {c['delivered']:,} | {_cell(c['hard_bounces'], c['sent'], 'sent')} | "
                 f"{_cell(c['reputation_blocks'], c['attempts'], 'attempts')} | {_cell(c['auth_failures'], c['attempts'], 'attempts')} | "
                 f"{_cell(c['deferrals_total'], c['attempts'], 'attempts')} | {_cell(c['complaints'], c['complaint_denominator'], 'delivered')} | "
                 f"{_cell(c['unsubscribes'], c['delivered'], 'delivered')} | {row['verdict']} |")
    L += ["", "Rates show their denominators. A rate with too few sends behind it is not judged (see each provider's rule results in the JSON).", ""]
    shown = r["days"][-last_days:]
    L += [f"### Day by day (last {len(shown)})", "", "| Day | Sent | Verdict | Why |", "|---|---:|---|---|"]
    for d in shown:
        why = "; ".join(d["reasons"][:3]) + (" ..." if len(d["reasons"]) > 3 else "") if d["reasons"] else "all clear"
        L.append(f"| {d['day']} | {d['sent']:,} | {d['verdict']} | {why.replace('|', '/')} |")
    if r["triage"]:
        L += ["", "### What to check, by provider", ""]
        for tr in r["triage"]:
            L += [f"**{tr['name']}: {tr['verdict']}**" + (f" ({tr['note']})" if tr["note"] else ""), ""]
            L += [f"- Saw: {x}" for x in tr["what_we_saw"]]
            L += [f"- Check: {x}" for x in tr["what_to_check"]]
            L += [f"- Where: [{w['tool']}]({w['url']}): {w['what']}" for w in tr["where"]]
            L.append("")
    if r.get("warmup") and r["warmup"].get("days"):
        L += ["### Warm-up", "", f"Plan day 1 is {r['warmup']['start']}.", "",
              "| Day | Plan day | Planned | Sent | Verdict | Tomorrow |", "|---|---:|---:|---:|---|---|"]
        for w in r["warmup"]["days"][-last_days:]:
            nxt = "" if w.get("next_volume") is None else f"{w['next_volume']:,} ({w.get('next', '')})"
            planned = "" if w.get("planned") is None else f"{w['planned']:,}"
            L.append(f"| {w['day']} | {w['plan_day']} | {planned} | {w['actual_sent']:,} | {w['verdict']} | {nxt} |")
        L.append("")
    if r["notes"]:
        L += ["### Notes", ""] + [f"- {x}" for x in r["notes"]]
    return "\n".join(L).rstrip() + "\n"


def load_postmaster(path: str) -> Dict[str, float]:
    """A CSV with columns date and spam_rate (a fraction, 0.0012, or a percent, 0.12%) exported from Postmaster Tools."""
    out: Dict[str, float] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            raw = (row.get("spam_rate") or "").strip()
            if not raw or not row.get("date"):
                continue
            value = float(raw.rstrip("%")) / 100 if raw.endswith("%") else float(raw)
            out[datetime.fromisoformat(row["date"].strip()[:10]).date().isoformat()] = value
    return out


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Daily deliverability monitor by day and mailbox provider, with warm-up verdicts")
    ap.add_argument("--data", required=True, help="folder or URL prefix with message_events (users optional, to fill recipient domains)")
    ap.add_argument("--warmup", help="a warm-up plan JSON from engine/deliverability/warmup.py")
    ap.add_argument("--warmup-start", help="the date of plan day 1 (default: the plan's start_date, else the first day in the data)")
    ap.add_argument("--mx", help='JSON {"domain": ["mx host", ...]} for business domains')
    ap.add_argument("--lookup-mx", action="store_true", help="look up MX records for unmapped domains (network)")
    ap.add_argument("--postmaster", help="CSV of date,spam_rate from Gmail Postmaster Tools")
    ap.add_argument("--min-sample", type=int, default=W.MIN_SAMPLE, help=f"smallest denominator a rate is judged on (default {W.MIN_SAMPLE})")
    ap.add_argument("--days", type=int, default=14, help="days shown in the markdown (all days are evaluated)")
    ap.add_argument("--markdown", action="store_true")
    ap.add_argument("--out", help="also write monitor.json and monitor.md here")
    a = ap.parse_args(argv)
    try:
        plan = json.loads(Path(a.warmup).read_text(encoding="utf-8")) if a.warmup else None
        mx = P.load_mx_map(a.mx) if a.mx else None
        pm = load_postmaster(a.postmaster) if a.postmaster else None
        r = monitor(a.data, plan, a.warmup_start, mx, a.lookup_mx, postmaster=pm, min_sample=a.min_sample)
    except (ValueError, FileNotFoundError, ConnectionError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    md = render_markdown(r, a.days)
    if a.out:
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "monitor.json").write_text(json.dumps(r, indent=2, default=str), encoding="utf-8")
        (out / "monitor.md").write_text(md, encoding="utf-8")
    print(md if a.markdown else json.dumps(r, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
