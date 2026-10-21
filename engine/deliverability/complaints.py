"""
Complaint ingestion: turn feedback-loop reports into message_events rows with event_name = complained.

When a Yahoo or Outlook.com user presses "This is spam", the provider can send the sender a report in the
Abuse Reporting Format (ARF, RFC 5965): a multipart/report message with report-type=feedback-report and
three parts, a human-readable note, a machine-readable message/feedback-report part (Feedback-Type,
User-Agent, Version, and optionally Original-Mail-From, Original-Rcpt-To, Arrival-Date, Reporting-MTA,
Source-IP, Incidents, Reported-Domain, Reported-URI, Authentication-Results), and the original message
(message/rfc822) or its headers (text/rfc822-headers). This module parses those reports, works out who
complained, and writes rows the rest of the engine already reads: engine/agents/engagement.py suppresses a
complainer, and engine/deliverability/monitor.py counts complaints per provider per day.

Who complained, in the order set by `order` (each strategy is tried until one gives a user id):

  header       a header in the original message that carries the user id (default X-LCM-User-Id). The
               most reliable: your ESP puts it in every message and the reports keep the original headers.
  unsubscribe  a token in the original List-Unsubscribe URL, read with a regex that has a named group
               `user` (the id itself) or `token` (looked up in a token map).
  verp         the envelope sender (Original-Mail-From, else Return-Path) when it encodes the recipient
               (VERP, bounce+u_123@bounces.example.com), read with a regex with a named group `user`.
  rcpt         the recipient address (Original-Rcpt-To, else the original To) joined to users.email.

Do not rely on the recipient address alone. Microsoft began moving JMRP to ARF on 11 June 2026, and the new
reports carry no recipient address: the original To is replaced with "Undisclosed Recipients" while X-headers,
Message-ID and the envelope sender are kept (Postmastery, https://www.postmastery.com/outlook-feedback-loop-is-transitioning-to-arf-recipient-address-no-longer-available/;
not on a Microsoft page we could fetch). Yahoo's CFL is DKIM-domain based, sends ARF with the full original
headers, comes from feedback@arf.mail.yahoo.com, and covers AOL and the other domains Yahoo hosts
(https://senders.yahooinc.com/complaint-feedback-loop, https://senders.yahooinc.com/faqs). Yahoo does not say
whether the recipient address is kept, so the same advice holds: put the user id in a header.

Gmail has no per-message feedback loop. It reports only aggregate spam rates per Feedback-ID value in Postmaster
Tools, for identifiers with enough volume, and only for @gmail.com recipients (https://support.google.com/mail/answer/6254652).
Apple iCloud Mail offers no feedback loop at all (https://support.apple.com/HT204137). So complaint counts from
this module are Yahoo, AOL and Outlook.com complaints; a Gmail complaint rate must be read in Postmaster Tools.

Only Feedback-Type abuse and fraud become complaints. not-spam (RFC 6430) and auth-failure (RFC 6591, a DMARC
failure report) are reported and skipped; virus and other are kept but flagged.

    python3 engine/deliverability/complaints.py examples/deliverability/complaints/ --users examples/brands/ridgeview_saas/data/users.csv --out out/complaints.csv
    python3 engine/deliverability/complaints.py report.eml --order header,verp --verp-pattern 'bounce\\+(?P<user>[^@]+)@'
"""
from __future__ import annotations

if __package__ in (None, ""):  # run as a file (python3 <path>): make the repository importable
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse
import csv
import email
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import timezone
from email import policy as email_policy
from email.message import Message
from email.parser import HeaderParser
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

COMPLAINT_TYPES = ("abuse", "fraud")                  # RFC 5965 section 7.3: these are a user saying "spam" or "phishing"
FLAGGED_TYPES = ("virus", "other")                    # kept as complaints, but flagged for review
SKIPPED_TYPES = ("not-spam", "auth-failure")          # RFC 6430 not-spam; RFC 6591 auth-failure is a DMARC failure report
STRATEGIES = ("header", "unsubscribe", "verp", "rcpt")
MESSAGE_EVENT_COLUMNS = ("event_id", "user_id", "channel", "event_name", "event_at", "message_id", "campaign",
                         "machine_open", "recipient_domain", "smtp_status", "diagnostic")

# reporter fingerprints: Yahoo's are documented (From "Yahoo! Mail AntiSpam Feedback", envelope feedback@arf.mail.yahoo.com,
# DKIM d=arf.mail.yahoo.com: https://senders.yahooinc.com/faqs). Microsoft's sender address for JMRP is not documented on a page
# we could fetch, so its pattern is a best effort on Microsoft names.
REPORTERS: Tuple[Tuple[str, re.Pattern], ...] = (
    ("yahoo", re.compile(r"arf\.mail\.yahoo\.com|yahoo! mail antispam feedback|yahoo", re.I)),
    ("microsoft", re.compile(r"hotmail|outlook\.com|microsoft|\bmsn\b|live\.com|jmrp", re.I)),
)


@dataclass
class FeedbackReport:
    format: str                                # arf | legacy (a report with only the original attached)
    feedback_type: Optional[str]
    user_agent: Optional[str]
    version: Optional[str]
    original_mail_from: Optional[str]
    original_rcpt_to: List[str]
    arrival_date: Optional[str]
    arrival_at: Optional[str]                  # Arrival-Date in UTC, "YYYY-MM-DD HH:MM:SS"
    reporting_mta: Optional[str]
    source_ip: Optional[str]
    incidents: Optional[str]
    reported_domain: List[str]
    reported_uri: List[str]
    authentication_results: List[str]
    reporter: Optional[str]                    # yahoo | microsoft | None
    report_date: Optional[str]
    original_headers: Dict[str, str] = field(default_factory=dict)
    issues: List[str] = field(default_factory=list)
    original: Optional[Message] = field(default=None, repr=False)

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("original", None)
        return d


def _utc(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _addr(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    found = [a for _, a in getaddresses([value]) if a]
    return found[0].strip().lower() if found else (value.strip().strip("<>").lower() or None)


def _fields(part: Message) -> Message:
    """The feedback-report fields as a Message of headers, however the parser stored the part."""
    payload = part.get_payload()
    if isinstance(payload, list) and payload and isinstance(payload[0], Message):
        return payload[0]
    if isinstance(payload, str):
        return HeaderParser(policy=email_policy.compat32).parsestr(payload.lstrip("\r\n"))
    return Message()


def _original(part: Message) -> Optional[Message]:
    payload = part.get_payload()
    if isinstance(payload, list) and payload and isinstance(payload[0], Message):
        return payload[0]
    if isinstance(payload, str):
        return HeaderParser(policy=email_policy.compat32).parsestr(payload.lstrip("\r\n"))
    return None


def parse_arf(raw: Union[str, bytes]) -> FeedbackReport:
    """Parse one feedback report. Raises ValueError when the message is neither ARF nor a report with an attached original."""
    msg = (email.message_from_bytes(raw, policy=email_policy.compat32) if isinstance(raw, (bytes, bytearray))
           else email.message_from_string(raw, policy=email_policy.compat32))
    issues: List[str] = []
    fb_part = orig_part = None
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype == "message/feedback-report" and fb_part is None:
            fb_part = part
        elif ctype in ("message/rfc822", "text/rfc822-headers") and orig_part is None and part is not msg:
            orig_part = part
    is_report = msg.get_content_type() == "multipart/report"
    report_type = (msg.get_param("report-type") or "").lower() if is_report else ""
    if fb_part is None and orig_part is None:
        raise ValueError("not a feedback report: no message/feedback-report part and no attached original message (RFC 5965 section 2)")
    if fb_part is not None and (not is_report or report_type != "feedback-report"):
        issues.append("the top level should be multipart/report; report-type=feedback-report (RFC 5965 section 2)")
    f = _fields(fb_part) if fb_part is not None else Message()

    def one(name: str) -> Optional[str]:
        v = f.get_all(name) or []
        if len(v) > 1:
            issues.append(f"{name} appears {len(v)} times; RFC 5965 allows it once")
        return re.sub(r"\s+", " ", str(v[0])).strip() if v else None

    def many(name: str) -> List[str]:
        return [re.sub(r"\s+", " ", str(v)).strip() for v in (f.get_all(name) or [])]

    arrival = one("Arrival-Date")
    received = one("Received-Date")
    if arrival and received:
        issues.append("both Arrival-Date and Received-Date are present: RFC 5965 section 3.1 calls that malformed; using Arrival-Date")
    arrival = arrival or received
    fmt = "arf" if fb_part is not None else "legacy"
    ftype = (one("Feedback-Type") or "").lower() or None
    if fmt == "arf":
        for req in ("Feedback-Type", "User-Agent", "Version"):
            if not f.get(req):
                issues.append(f"required field {req} is missing (RFC 5965 section 3.1)")
    else:
        ftype = "abuse"
        issues.append("legacy report: no message/feedback-report part, so the feedback type is assumed to be abuse")
    original = _original(orig_part) if orig_part is not None else None
    if original is None:
        issues.append("no original message or headers attached (RFC 5965 requires the third part)")
    rcpts = [a for a in (_addr(x) for x in many("Original-Rcpt-To")) if a]
    fingerprint = " ".join(str(x) for x in (msg.get("From"), msg.get("Return-Path"), msg.get("Sender"), msg.get("DKIM-Signature"),
                                            one("User-Agent") if fmt == "arf" else "", one("Reporting-MTA") if fmt == "arf" else "") if x)
    reporter = next((name for name, pattern in REPORTERS if pattern.search(fingerprint)), None)
    oh: Dict[str, str] = {}
    if original is not None:
        for k in ("Message-ID", "From", "To", "Subject", "Date", "Return-Path", "List-Unsubscribe", "Feedback-ID"):
            if original.get(k):
                oh[k] = re.sub(r"\s+", " ", str(original.get(k))).strip()
    return FeedbackReport(fmt, ftype, one("User-Agent"), one("Version"), _addr(one("Original-Mail-From")), rcpts, arrival, _utc(arrival),
                          one("Reporting-MTA"), one("Source-IP"), one("Incidents"), many("Reported-Domain"), many("Reported-URI"),
                          many("Authentication-Results"), reporter, str(msg.get("Date")) if msg.get("Date") else None, oh, issues, original)


# ----------------------------------------------------------------------------------------------
# who complained
# ----------------------------------------------------------------------------------------------

@dataclass
class ResolveConfig:
    order: Tuple[str, ...] = STRATEGIES
    user_header: str = "X-LCM-User-Id"
    campaign_header: Optional[str] = "X-LCM-Campaign"
    message_id_header: Optional[str] = "X-LCM-Message-Id"   # the ESP's send id; Message-ID is used when it is absent
    unsubscribe_pattern: Optional[str] = None                # regex with (?P<user>...) or (?P<token>...)
    token_map: Dict[str, str] = field(default_factory=dict)  # unsubscribe token to user id
    verp_pattern: Optional[str] = None                       # regex with (?P<user>...)
    users_by_email: Dict[str, str] = field(default_factory=dict)
    emails_by_user: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        bad = [s for s in self.order if s not in STRATEGIES]
        if bad:
            raise ValueError(f"unknown resolution strategy {bad}; choose from {', '.join(STRATEGIES)}")
        for name in ("unsubscribe_pattern", "verp_pattern"):
            pat = getattr(self, name)
            if pat and not re.compile(pat).groupindex.keys() & {"user", "token"}:
                raise ValueError(f"{name} needs a named group (?P<user>...)" + (" or (?P<token>...)" if name == "unsubscribe_pattern" else ""))


def resolve(report: FeedbackReport, cfg: ResolveConfig) -> Tuple[Optional[str], str, List[str]]:
    """(user_id, strategy that found it, notes on the strategies that did not)."""
    notes: List[str] = []
    orig = report.original
    for strategy in cfg.order:
        if strategy == "header":
            v = orig.get(cfg.user_header) if orig is not None and cfg.user_header else None
            if v and str(v).strip():
                return str(v).strip(), "header", notes
            notes.append(f"no {cfg.user_header} header in the original message")
        elif strategy == "unsubscribe":
            lu = str(orig.get("List-Unsubscribe") or "") if orig is not None else ""
            if not cfg.unsubscribe_pattern:
                notes.append("no unsubscribe pattern configured")
                continue
            m = re.search(cfg.unsubscribe_pattern, lu)
            if m and m.groupdict().get("user"):
                return m.group("user"), "unsubscribe", notes
            if m and m.groupdict().get("token"):
                uid = cfg.token_map.get(m.group("token"))
                if uid:
                    return uid, "unsubscribe", notes
                notes.append(f"unsubscribe token {m.group('token')!r} is not in the token map")
                continue
            notes.append("List-Unsubscribe did not match the pattern" if lu else "no List-Unsubscribe header in the original")
        elif strategy == "verp":
            env = report.original_mail_from or (_addr(str(orig.get("Return-Path"))) if orig is not None and orig.get("Return-Path") else None)
            if not cfg.verp_pattern:
                notes.append("no VERP pattern configured")
                continue
            m = re.search(cfg.verp_pattern, env or "")
            if m and m.groupdict().get("user"):
                return m.group("user"), "verp", notes
            notes.append("the envelope sender did not match the VERP pattern" if env else "no envelope sender in the report")
        elif strategy == "rcpt":
            candidates = list(report.original_rcpt_to)
            to = _addr(str(orig.get("To"))) if orig is not None and orig.get("To") else None
            if to and "undisclosed" not in str(orig.get("To")).lower():
                candidates.append(to)
            if not candidates:
                notes.append("no recipient address in the report (Microsoft's ARF reports omit it)")
                continue
            uid = next((cfg.users_by_email[a] for a in candidates if a in cfg.users_by_email), None)
            if uid:
                return uid, "rcpt", notes
            notes.append("recipient address not found in users.email" if cfg.users_by_email else "no users table to join the recipient address to")
    return None, "", notes


def to_event(report: FeedbackReport, user_id: str, cfg: ResolveConfig) -> Dict[str, Any]:
    """One message_events row (engine/data/schema.py columns) for a resolved complaint."""
    orig = report.original
    msg_id = None
    if orig is not None:
        msg_id = (orig.get(cfg.message_id_header) if cfg.message_id_header else None) or orig.get("Message-ID")
    msg_id = str(msg_id).strip().strip("<>") if msg_id else None
    campaign = str(orig.get(cfg.campaign_header)).strip() if orig is not None and cfg.campaign_header and orig.get(cfg.campaign_header) else None
    email_addr = cfg.emails_by_user.get(user_id) or (report.original_rcpt_to[0] if report.original_rcpt_to else None)
    domain = email_addr.rsplit("@", 1)[1].lower() if email_addr and "@" in email_addr else None
    when = report.arrival_at or _utc(report.report_date)
    key = "|".join(str(x) for x in (report.reporter, msg_id, user_id, when, report.feedback_type))
    diag = "; ".join(x for x in (f"ARF feedback-type={report.feedback_type}", f"user-agent={report.user_agent}" if report.user_agent else "",
                                 f"reporter={report.reporter}" if report.reporter else "",
                                 "flagged: review" if report.feedback_type in FLAGGED_TYPES else "") if x)
    return {"event_id": "cmp_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16], "user_id": user_id, "channel": "email",
            "event_name": "complained", "event_at": when, "message_id": msg_id, "campaign": campaign, "machine_open": None,
            "recipient_domain": domain, "smtp_status": None, "diagnostic": diag}


def ingest(reports: Iterable[Union[str, bytes, Tuple[str, Union[str, bytes]]]], cfg: Optional[ResolveConfig] = None) -> Dict[str, Any]:
    """Parse and resolve many reports. Items are raw messages or (name, raw) pairs.

    Returns {"events": [message_events rows, deduplicated], "unresolved": [...], "skipped": [...], "errors": [...]}."""
    cfg = cfg or ResolveConfig()
    events: Dict[str, Dict[str, Any]] = {}
    unresolved: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    for i, item in enumerate(reports):
        name, raw = item if isinstance(item, tuple) else (f"report {i + 1}", item)
        try:
            rep = parse_arf(raw)
        except ValueError as e:
            errors.append({"report": name, "error": str(e)})
            continue
        if rep.feedback_type in SKIPPED_TYPES or (rep.feedback_type and rep.feedback_type not in COMPLAINT_TYPES + FLAGGED_TYPES):
            skipped.append({"report": name, "feedback_type": rep.feedback_type, "reason": "not a complaint"})
            continue
        uid, how, notes = resolve(rep, cfg)
        if uid is None:
            unresolved.append({"report": name, "reporter": rep.reporter, "arrival_at": rep.arrival_at,
                               "message_id": rep.original_headers.get("Message-ID"), "tried": notes})
            continue
        ev = to_event(rep, uid, cfg)
        ev["_resolved_by"] = how
        events.setdefault(ev["event_id"], ev)
    return {"events": list(events.values()), "unresolved": unresolved, "skipped": skipped, "errors": errors}


def load_users(path: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    """(email to user_id, user_id to email) from a users.csv in the canonical shape."""
    by_email: Dict[str, str] = {}
    by_user: Dict[str, str] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            uid, em = (row.get("user_id") or "").strip(), (row.get("email") or "").strip().lower()
            if uid and em:
                by_email[em] = uid
                by_user[uid] = em
    return by_email, by_user


def _files(paths: List[str]) -> List[Path]:
    out: List[Path] = []
    for p in map(Path, paths):
        out += sorted(x for x in p.iterdir() if x.suffix.lower() in (".eml", ".txt", ".msg")) if p.is_dir() else [p]
    return out


def write_events(rows: List[Dict[str, Any]], path: Path, append: bool = False) -> None:
    exists = path.exists() and path.stat().st_size > 0
    mode = "a" if append and exists else "w"
    with path.open(mode, newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(MESSAGE_EVENT_COLUMNS), extrasaction="ignore")
        if mode == "w":
            w.writeheader()
        w.writerows(rows)


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Feedback-loop (ARF) reports to message_events complained rows")
    ap.add_argument("paths", nargs="+", help=".eml files, or folders of them")
    ap.add_argument("--users", help="users.csv, to join recipient addresses to user ids and fill recipient_domain")
    ap.add_argument("--order", default=",".join(STRATEGIES), help=f"resolution order (default {','.join(STRATEGIES)})")
    ap.add_argument("--user-header", default="X-LCM-User-Id")
    ap.add_argument("--campaign-header", default="X-LCM-Campaign")
    ap.add_argument("--message-id-header", default="X-LCM-Message-Id")
    ap.add_argument("--unsubscribe-pattern", help="regex on List-Unsubscribe with (?P<user>...) or (?P<token>...)")
    ap.add_argument("--token-map", help="JSON {token: user_id} for --unsubscribe-pattern tokens")
    ap.add_argument("--verp-pattern", help="regex on the envelope sender with (?P<user>...)")
    ap.add_argument("--out", help="write the rows to this CSV (message_events columns)")
    ap.add_argument("--append", action="store_true", help="append to --out instead of replacing it")
    a = ap.parse_args(argv)
    try:
        by_email, by_user = load_users(a.users) if a.users else ({}, {})
        cfg = ResolveConfig(order=tuple(s.strip() for s in a.order.split(",") if s.strip()), user_header=a.user_header,
                            campaign_header=a.campaign_header or None, message_id_header=a.message_id_header or None,
                            unsubscribe_pattern=a.unsubscribe_pattern, verp_pattern=a.verp_pattern,
                            token_map=json.loads(Path(a.token_map).read_text(encoding="utf-8")) if a.token_map else {},
                            users_by_email=by_email, emails_by_user=by_user)
        files = _files(a.paths)
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    r = ingest([(str(f), f.read_bytes()) for f in files], cfg)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        write_events(r["events"], Path(a.out), a.append)
    print(json.dumps({"complaints": len(r["events"]), "unresolved": r["unresolved"], "skipped": r["skipped"], "errors": r["errors"],
                      **({} if a.out else {"events": r["events"]})}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
