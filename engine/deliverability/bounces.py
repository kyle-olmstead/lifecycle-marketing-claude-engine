"""
Bounce and deferral classes: what a receiving server's refusal means, and what to do about it.

A bounce is not one thing. "This address does not exist" means suppress the address forever. "Mailbox
full" means try again later. "Your IP has a poor reputation" means the address is fine and the sender is
the problem: suppressing it would hide the cause, and retrying at the same pace makes it worse. This
module reads the enhanced status code (RFC 3463 classes and subjects, the RFC 5248 registry, RFC 7372
for authentication) and the server's text, and puts every bounce or deferral into one class:

  hard     the address is bad (does not exist, domain gone, account disabled)     action: suppress
  soft     a temporary problem with the mailbox or server (full, busy, timed out)  action: retry
  block    the receiver refused the sender: reputation, content, policy or a       action: fix_sender
           technical fault in the message (size, format)
  auth     authentication or bulk-sender requirements failed (SPF, DKIM, DMARC,    action: fix_sender
           reverse DNS, TLS), such as Gmail's 550 5.7.26 or Outlook.com's 550 5.7.515
  rate     the provider is throttling this sender (Gmail 421 4.7.28, Outlook.com   action: retry (slower)
           451 4.7.650, Yahoo TS* deferrals)
  unknown  nothing recognizable: read the text; never suppress on this alone       action: retry

Blocks carry a kind: reputation and content blocks are reputation signals (the warm-up's abort criteria
count them); policy blocks (one recipient organization's rule) and technical blocks (size, format) are
real but say nothing about the sender's reputation.

The rules are a table, read in order: each provider's documented codes first (Gmail's SMTP error
reference, Microsoft's NDR and Outlook.com troubleshooting pages, Yahoo's Sender Hub error codes), then
the registry codes that settle a class on their own, then patterns in the text, then the subject of the
code, then the reply code. Every rule names its source and says whether it was verified against that
source. A rule matched by a provider's text but not found in its documentation is marked verified=False.

    python3 engine/deliverability/bounces.py "550 5.7.26 This email has been blocked because the sender is unauthenticated." --provider google
    python3 engine/deliverability/bounces.py --status 4.7.650 "451 4.7.650 The mail server [192.0.2.1] has been temporarily rate limited due to IP reputation."
    python3 engine/deliverability/bounces.py --corpus examples/deliverability/bounces/corpus.json
"""
from __future__ import annotations

if __package__ in (None, ""):  # run as a file (python3 <path>): make the repository importable
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

# ----------------------------------------------------------------------------------------------
# classes and actions
# ----------------------------------------------------------------------------------------------

CLASSES = ("hard", "soft", "block", "auth", "rate", "unknown")
ACTIONS: Dict[str, Tuple[str, str]] = {
    "hard": ("suppress", "Suppress the address now and never retry it: it does not exist or will never accept mail."),
    "soft": ("retry", "Retry with backoff: the address is fine. Suppress it only if it keeps failing across several sends."),
    "block": ("fix_sender", "Do not suppress the address: the receiver refused the sender. Stop raising volume to this provider "
                            "and fix the cause (reputation, content, policy or the message format)."),
    "auth": ("fix_sender", "Fix authentication before sending more: SPF, DKIM, DMARC alignment, reverse DNS or TLS failed, and "
                           "every message to this provider will fail the same way until it is fixed."),
    "rate": ("retry", "Retry later and slow down to this provider: it is throttling this sender, usually on reputation. "
                      "Do not suppress the address."),
    "unknown": ("retry", "Not recognized: read the server's text. Do not suppress on this alone."),
}
BLOCK_KINDS = ("reputation", "content", "policy", "technical")
REPUTATION_KINDS = ("reputation", "content")   # block kinds the warm-up's abort criteria count

# sources
GMAIL = "https://knowledge.workspace.google.com/admin/support/troubleshooting/gmail-smtp-errors-and-codes"
MS_NDR = "https://learn.microsoft.com/en-us/exchange/mail-flow-best-practices/non-delivery-reports-in-exchange-online/non-delivery-reports-in-exchange-online"
MS_OLC = "https://substrate.office.com/ip-domain-management-snds/Postmaster/Troubleshooting"
MS_515 = "https://support.microsoft.com/en-us/outlook/fix-ndr-error-550-5-7-515-in-outlook-com"
MS_4650 = "https://www.postmastery.com/new-smtp-replies-at-outlook-update-your-backoff-patterns-to-avoid-reputation-damage/"
MS_S3150 = "https://learn.microsoft.com/en-sg/answers/questions/5777377/how-can-i-remove-from-s3150-block-list"
YAHOO = "https://senders.yahooinc.com/smtp-error-codes/"
RFC3463 = "RFC 3463 (registry: https://www.iana.org/assignments/smtp-enhanced-status-codes/)"
RFC5248 = "RFC 5248 registry"
RFC7372 = "RFC 7372"
RFC7505 = "RFC 7505"
RFC5321 = "RFC 5321 section 4.2.1"


@dataclass(frozen=True)
class Rule:
    id: str
    cls: str
    kind: str                          # block: reputation | content | policy | technical; others: a short label
    source: str
    verified: bool = True
    family: Optional[str] = None       # google | microsoft | yahoo; None applies to any provider
    code: Optional[str] = None         # regex on "subject.detail" of the enhanced code (7\.26), anchored
    status_class: Optional[str] = None # "4" or "5": restrict to temporary or permanent codes
    reply: Optional[str] = None        # regex on the basic reply code (55[0-4]), anchored
    text: Optional[str] = None         # regex on the diagnostic text, case-insensitive
    no_code: bool = False              # only when the text carries no enhanced status code
    note: str = ""

    def __post_init__(self) -> None:
        assert self.cls in CLASSES, self.cls
        assert self.cls != "block" or self.kind in BLOCK_KINDS, (self.id, self.kind)


# ----------------------------------------------------------------------------------------------
# 1. provider rules (checked first, for the provider family only)
# ----------------------------------------------------------------------------------------------

PROVIDER_RULES: Tuple[Rule, ...] = (
    # Gmail SMTP errors and codes, read October 2026. Gmail reuses 5.7.29 (TLS) and 5.7.30 (DKIM) with meanings that differ
    # from the RFC registry (ARC, REQUIRETLS); both are authentication or bulk-sender requirements either way.
    Rule("gmail.no_such_user", "hard", "no such user", GMAIL, family="google", code=r"1\.1", status_class="5",
         note="550 5.1.1 The email account that you tried to reach does not exist."),
    Rule("gmail.no_domain", "hard", "domain not found", GMAIL, family="google", code=r"1\.2", status_class="5",
         note="553 5.1.2 We weren't able to find the recipient domain."),
    Rule("gmail.inactive", "hard", "account inactive", GMAIL, family="google", code=r"2\.[12]", status_class="5", text=r"inactive",
         note="550 5.2.1 / 552 5.2.2 ... is inactive"),
    Rule("gmail.receiving_too_fast", "soft", "recipient rate", GMAIL, family="google", code=r"2\.1",
         note="450 4.2.1 The user you are trying to contact is receiving email too quickly."),
    Rule("gmail.over_quota", "soft", "mailbox full", GMAIL, family="google", code=r"2\.2",
         note="452 4.2.2 The recipient's inbox is out of storage space."),
    Rule("gmail.unusual_rate", "rate", "unusual rate", GMAIL, family="google", code=r"7\.28", status_class="4",
         note="421 4.7.28 Gmail has detected an unusual rate of (unsolicited) email ..."),
    Rule("gmail.unsolicited_rate_block", "block", "reputation", GMAIL, family="google", code=r"7\.28", status_class="5",
         note="550 5.7.28 There is an unusual rate of unsolicited email originating from your IP address."),
    Rule("gmail.tls_required", "auth", "tls", GMAIL, family="google", code=r"7\.0", status_class="4", text=r"\bTLS\b",
         note="421 4.7.0 TLS required for RCPT domain, closing connection."),
    Rule("gmail.ptr_deferral", "auth", "reverse dns", GMAIL, family="google", code=r"7\.0", status_class="4", text=r"\bPTR\b",
         note="421 4.7.0 The IP address sending this message does not have a PTR record ..."),
    Rule("gmail.not_allowlisted", "block", "policy", GMAIL, family="google", code=r"7\.0", status_class="4", text=r"whitelist|allowlist",
         note="421 4.7.0 IP not in whitelist for RCPT domain, closing connection."),
    Rule("gmail.low_reputation_deferral", "block", "reputation", GMAIL, family="google", code=r"7\.0", status_class="4",
         text=r"reputation|suspicious|unsolicited", note="421 4.7.0 This message is suspicious due to the very low reputation of the sending IP address."),
    Rule("gmail.auth_rate_limited", "auth", "authentication", GMAIL, family="google", code=r"7\.(26|27|30|32|40)", status_class="4",
         note="421 4.7.26/27/30/32/40 Your email has been rate limited because SPF / DKIM / alignment / DMARC did not pass."),
    Rule("gmail.tls_rate_limited", "auth", "tls", GMAIL, family="google", code=r"7\.29", status_class="4",
         note="421 4.7.29 Your email has been rate limited because you're not using a TLS connection."),
    Rule("gmail.ptr_spf_deferral", "auth", "reverse dns or spf", GMAIL, family="google", code=r"7\.(23|24)", status_class="4",
         note="451 4.7.23 no PTR record; 451 4.7.24 suspicious SPF entries"),
    Rule("gmail.auth_block", "auth", "authentication", GMAIL, family="google", code=r"7\.(24|25|26|27|29|30|40)", status_class="5",
         note="550 5.7.26 This email has been blocked because the sender is unauthenticated (and 5.7.24/25/27/29/30/40: SPF, PTR, TLS, DKIM, DMARC)."),
    Rule("gmail.rate_limited_block", "rate", "rate limited", GMAIL, family="google", code=r"7\.1", status_class="5", text=r"rate limited",
         note="550 5.7.1 This email has been rate limited."),
    Rule("gmail.ipv6_guidelines", "auth", "ipv6 ptr and authentication", GMAIL, family="google", code=r"7\.1", text=r"IPv6",
         note="550 5.7.1 This message does not meet IPv6 sending guidelines regarding PTR records and authentication."),
    Rule("gmail.unsolicited", "block", "content", GMAIL, family="google", code=r"7\.1", text=r"unsolicited",
         note="550 5.7.1 This message is likely unsolicited email."),
    Rule("gmail.low_reputation", "block", "reputation", GMAIL, family="google", code=r"7\.1", text=r"reputation",
         note="550 5.7.1 This message is likely suspicious due to the very low reputation of the sending IP address / domain."),
    Rule("gmail.recipient_policy", "block", "policy", GMAIL, family="google", code=r"7\.1", text=r"policy that prohibits",
         note="550 5.7.1 The user or domain that you are sending to (or from) has a policy that prohibits ..."),
    Rule("gmail.rfc5322", "block", "technical", GMAIL, family="google", code=r"7\.1", text=r"RFC ?5322|duplicate|From: header",
         note="550 5.7.1 RFC 5322 compliance rejections (duplicate or missing headers)"),
    Rule("gmail.security_content", "block", "content", GMAIL, family="google", code=r"7\.0", status_class="5",
         note="552 5.7.0 This message was blocked because its content presents a potential security issue."),
    Rule("gmail.server_busy", "soft", "server busy", GMAIL, family="google", code=r"3\.0|4\.5", status_class="4",
         note="421 4.3.0 Temporary System Problem; 421 4.4.5 Server busy, try again later."),

    # Microsoft: Outlook.com troubleshooting page, the 5.7.515 help page, and the Exchange Online NDR reference
    Rule("microsoft.bulk_sender_auth", "auth", "bulk sender requirements", MS_515, family="microsoft", code=r"7\.515",
         note="550 5.7.515 Access denied, sending domain <domain> does not meet the required authentication level."),
    Rule("microsoft.dmarc_reject", "auth", "dmarc", MS_NDR, family="microsoft", code=r"7\.509",
         note="5.7.509 Access denied, sending domain does not pass DMARC verification and has a DMARC policy of reject."),
    Rule("microsoft.spf", "auth", "spf", MS_NDR, family="microsoft", code=r"7\.23",
         note="5.7.23 The message was rejected because of Sender Policy Framework violation."),
    Rule("microsoft.banned_ip", "block", "reputation", MS_NDR, family="microsoft", code=r"7\.(60[6-9]|6[1-4]\d)",
         note="5.7.606-649 Access denied, banned sending IP. Delist at https://sender.office.com/"),
    Rule("microsoft.spam_abuse", "block", "reputation", MS_NDR, family="microsoft", code=r"7\.(501|502|503|511)",
         note="5.7.501 spam abuse detected; 5.7.502/503/511 banned sender"),
    Rule("microsoft.limits", "rate", "permitted limits", MS_NDR, family="microsoft", code=r"7\.508",
         note="5.7.508 Access denied, <IP> has exceeded permitted limits within <range> range."),
    Rule("microsoft.rfc5322", "block", "technical", MS_NDR, family="microsoft", code=r"7\.(506|510|512)",
         note="5.7.506 Bad HELO; 5.7.510 no email over IPv6; 5.7.512 message must be RFC 5322 section 3.6.2 compliant"),
    Rule("microsoft.tenant_policy", "block", "policy", MS_NDR, family="microsoft", code=r"7\.(70\d|7[1-4]\d|750)",
         note="5.7.700-750 tenant-level blocks (5.7.703 Tenant Allow/Block List)"),
    Rule("microsoft.recipient_not_found", "hard", "recipient not found", MS_NDR, family="microsoft", code=r"1\.10",
         note="5.1.10 Recipient not found"),
    Rule("microsoft.dbeb", "hard", "recipient not in directory", MS_NDR, family="microsoft", code=r"4\.1", status_class="5",
         text=r"recipient address rejected", note="550 5.4.1 Recipient address rejected: Access denied"),
    Rule("microsoft.relay_denied", "block", "technical", MS_NDR, family="microsoft", code=r"4\.1", status_class="5", text=r"relay",
         note="5.4.1 Relay Access Denied"),
    # 4.7.500-699: "Access denied, please try again later" (Microsoft). The 4.7.650/651 wording "temporarily rate limited due to IP
    # reputation" and 4.7.652/653 (connection and message limits) are documented by Postmastery, not on a Microsoft page.
    Rule("microsoft.try_again_later", "rate", "ip reputation throttling", MS_NDR, family="microsoft", code=r"7\.[56]\d\d", status_class="4",
         note="451 4.7.500-699 Access denied, please try again later (4.7.650: temporarily rate limited due to IP reputation)"),
    Rule("microsoft.rp_rate", "rate", "rate limit", MS_OLC, family="microsoft", text=r"\bRP-00[123]\b",
         note="421 RP-001/002/003 exceeded the rate or connection limit allowed (reputation based)"),
    Rule("microsoft.sc_content", "block", "content", MS_OLC, family="microsoft", text=r"\b(SC-001|OU-002)\b",
         note="550 SC-001 / OU-002 content with spam-like characteristics, or poor IP or domain reputation"),
    Rule("microsoft.sc_complaints", "block", "reputation", MS_OLC, family="microsoft", text=r"\bSC-00[234]\b",
         note="550 SC-002 namespace mining; SC-003 open proxy or relay; SC-004 a block placed against your IP address (complaints)"),
    Rule("microsoft.outside_block", "block", "reputation", MS_OLC, family="microsoft", text=r"\b(OU-001|DY-00[12])\b",
         note="550 OU-001 blocked by an outside source (Spamhaus); DY-001/002 dynamic or compromised IP"),
    Rule("microsoft.s3150", "block", "reputation", MS_S3150, verified=False, family="microsoft", text=r"\bS3150\b|on our block list",
         note="550 5.7.1 ... part of their network is on our block list (S3150). Seen in Microsoft Q&A answers; not in Microsoft's NDR reference"),

    # Yahoo Sender Hub SMTP error codes, read October 2026. The page gives categories, not a full list of codes.
    Rule("yahoo.no_account", "hard", "no such user", YAHOO, family="yahoo", text=r"doesn't have an? [\w.-]+ account|does not have an? [\w.-]+ account",
         note="554 delivery error: dd This user doesn't have a yahoo.com account"),
    Rule("yahoo.ts_deferral", "rate", "temporary deferral (complaints, reputation, volume)", YAHOO, family="yahoo", text=r"\bTSS?\d{2}\b",
         note="421 4.7.0 [TS*] message temporarily deferred: complaints, spam-like content, poor IP reputation or unusual traffic"),
    Rule("yahoo.ph_content", "block", "content", YAHOO, family="yahoo", text=r"\bPH\d{2}\b",
         note="PH* content based blocks (malware, phishing, or links to them)"),
    Rule("yahoo.auth_failure", "auth", "dmarc or dkim", YAHOO, family="yahoo", text=r"failed authentication checks",
         note="Your message failed authentication checks against your sending domain's DMARC or DKIM policy."),
    Rule("yahoo.unauthenticated", "auth", "authentication", "https://www.valimail.com/blog/yahoo-blocked-sender-is-unauthenticated/",
         verified=False, family="yahoo", text=r"sender is unauthenticated",
         note="550 5.7.9 This mail has been blocked because the sender is unauthenticated. (quoted by Valimail; not on Yahoo's page)"),
    Rule("yahoo.bl_blocklist", "block", "reputation", YAHOO, verified=False, family="yahoo", text=r"\bBL\d{2}\b",
         note="553 5.7.1 [BL21] connections not accepted from IP addresses on Spamhaus PBL (code observed; Yahoo's page lists no BL codes)"),
    Rule("yahoo.unresolvable_from", "auth", "sender domain does not resolve", YAHOO, verified=False, family="yahoo",
         text=r"unresolvable|(rfc ?5321|rfc ?5322).{0,40}domain",
         note="451 / 554 unresolvable RFC5321 or RFC5322 from domain (category on Yahoo's page; exact wording not published)"),
    Rule("yahoo.permanent", "hard", "permanent failure", YAHOO, family="yahoo", reply=r"55[34]", no_code=True,
         note="553 / 554 permanent errors: Yahoo says do not retry and remove the address"),
    Rule("yahoo.temporary", "soft", "temporary", YAHOO, family="yahoo", reply=r"4(21|51)", no_code=True,
         note="421 / 451 temporary problem: retry later"),
)

# ----------------------------------------------------------------------------------------------
# 2. registry codes that settle the class on their own (RFC 3463, RFC 5248 registry, RFC 7372, RFC 7505)
# ----------------------------------------------------------------------------------------------

STRONG_CODES: Tuple[Rule, ...] = (
    Rule("rfc.mailbox_missing", "hard", "no such user", RFC3463, code=r"1\.1", status_class="5", note="X.1.1 bad destination mailbox address"),
    Rule("rfc.domain_missing", "hard", "domain not found", RFC3463, code=r"1\.2", status_class="5", note="X.1.2 bad destination system address"),
    Rule("rfc.address_malformed", "hard", "malformed address", RFC3463, code=r"1\.3", status_class="5", note="X.1.3 bad destination mailbox address syntax"),
    Rule("rfc.mailbox_moved", "hard", "mailbox moved", RFC3463, code=r"1\.6", status_class="5", note="X.1.6 destination mailbox has moved, no forwarding address"),
    Rule("rfc.null_mx_recipient", "hard", "domain accepts no mail", RFC7505, code=r"1\.10", note="X.1.10 recipient address has null MX"),
    Rule("rfc.sender_malformed", "block", "technical", RFC3463, code=r"1\.7", note="X.1.7 bad sender's mailbox address syntax"),
    Rule("rfc.sender_domain", "auth", "sender domain", RFC3463, code=r"1\.8", note="X.1.8 bad sender's system address"),
    Rule("rfc.mailbox_disabled", "hard", "mailbox disabled", RFC3463, code=r"2\.1", status_class="5", note="X.2.1 mailbox disabled, not accepting messages"),
    Rule("rfc.mailbox_full", "soft", "mailbox full", RFC3463, code=r"2\.2", note="X.2.2 mailbox full (RFC 3463: a persistent transient failure)"),
    Rule("rfc.message_too_big", "block", "technical", RFC3463, code=r"2\.3|3\.4", note="X.2.3 / X.3.4 message too big"),
    Rule("rfc.no_route", "hard", "no route to domain", RFC3463, code=r"4\.4", status_class="5", note="X.4.4 unable to route"),
    Rule("rfc.expired", "soft", "expired", RFC3463, code=r"4\.7", note="X.4.7 delivery time expired"),
    Rule("rfc.too_many_recipients", "block", "technical", RFC3463, code=r"5\.3", note="X.5.3 too many recipients"),
    Rule("rfc.dkim", "auth", "dkim", RFC7372, code=r"7\.2[012]", note="X.7.20-22 no passing, acceptable or author-matched DKIM signature"),
    Rule("rfc.spf", "auth", "spf", RFC7372, code=r"7\.2[34]", note="X.7.23 SPF validation failed; X.7.24 SPF validation error"),
    Rule("rfc.reverse_dns", "auth", "reverse dns", RFC7372, code=r"7\.25", note="X.7.25 reverse DNS validation failed"),
    Rule("rfc.multiple_auth", "auth", "authentication", RFC7372, code=r"7\.26", note="X.7.26 multiple authentication checks failed"),
    Rule("rfc.null_mx_sender", "auth", "sender domain", RFC7505, code=r"7\.27", note="X.7.27 sender address has null MX"),
    Rule("rfc.mail_flood", "block", "reputation", RFC5248, code=r"7\.28", note="X.7.28 mail flood detected"),
    Rule("rfc.arc", "auth", "arc", RFC5248, code=r"7\.29", note="X.7.29 ARC validation failure (RFC 8617)"),
    Rule("rfc.requiretls", "auth", "tls", RFC5248, code=r"7\.30", note="X.7.30 REQUIRETLS support required (RFC 8689)"),
)

# ----------------------------------------------------------------------------------------------
# 3. text patterns, for codes that do not settle the class (5.0.0, 5.7.1, no code at all)
# ----------------------------------------------------------------------------------------------

TEXT_SOURCE = "engine pattern on common server wording; the class follows RFC 3463 subjects"
# Heuristics, not provider documentation: every text rule is verified=False, so the monitor reports how many events they classed.
TEXT_RULES: Tuple[Rule, ...] = (
    Rule("text.auth", "auth", "authentication", TEXT_SOURCE, verified=False,
         text=r"unauthenticated|not authenticated|authentication (check )?(failed|failure|required|level)|failed authentication|"
              r"\bdmarc\b|\bspf\b.{0,30}(fail|error|violation)|\bdkim\b.{0,30}(fail|error)|(fail(ed|ing)?|no passing) (spf|dkim)|"
              r"reverse dns|\bptr record|\btls (is )?required|requiretls"),
    Rule("text.rate", "rate", "rate limit", TEXT_SOURCE, verified=False,
         text=r"rate[- ]limit|too many (messages|connections)|throttl|unusual (rate|volume|traffic)|unexpected volume|"
              r"exceeded .{0,40}(rate|limit|quota for sending)"),
    Rule("text.reputation", "block", "reputation", TEXT_SOURCE, verified=False,
         text=r"spamhaus|block ?list|black ?list|deny ?list|blocked using|listed (at|on|in)\b|reputation|banned|\brbl\b|\bdnsbl\b"),
    Rule("text.content", "block", "content", TEXT_SOURCE, verified=False, text=r"\bspam\b|unsolicited|junk|suspicious|phish|malware|virus"),
    Rule("text.hard", "hard", "no such user", TEXT_SOURCE, verified=False,
         text=r"user unknown|unknown (user|recipient)|no such (user|mailbox|recipient)|(does not|doesn't) exist|recipient not found|"
              r"mailbox not found|mailbox unavailable|invalid (recipient|mailbox|address)|recipient address rejected|no mailbox|"
              r"(account|mailbox) (is |has been )?(disabled|deactivated|closed)|unroutea?ble|domain not found|host not found"),
    Rule("text.soft", "soft", "temporary", TEXT_SOURCE, verified=False,
         text=r"mailbox (is )?full|over quota|quota exceeded|insufficient (storage|system resources)|out of storage|temporar|"
              r"try (again )?later|server busy|service (is )?unavailable|timed? ?out|connection (refused|reset)"),
    Rule("text.policy", "block", "policy", TEXT_SOURCE, verified=False, text=r"\bpolicy\b|not (allowed|permitted|authori[sz]ed)|access denied|refused"),
)

# ----------------------------------------------------------------------------------------------
# 4. subject fallback (RFC 3463 section 3) and 5. reply code (RFC 5321)
# ----------------------------------------------------------------------------------------------

SUBJECT_RULES: Tuple[Rule, ...] = (
    Rule("subject.address", "hard", "address", RFC3463, code=r"1\.\d+", status_class="5", note="X.1.XXX addressing status, permanent"),
    Rule("subject.address_temporary", "soft", "address", RFC3463, code=r"1\.\d+", status_class="4", note="X.1.XXX addressing status, temporary"),
    Rule("subject.mailbox", "soft", "mailbox", RFC3463, code=r"2\.\d+", note="X.2.XXX mailbox status"),
    Rule("subject.system", "soft", "mail system", RFC3463, code=r"3\.\d+", note="X.3.XXX mail system status"),
    Rule("subject.network", "soft", "network or routing", RFC3463, code=r"4\.\d+", note="X.4.XXX network and routing status"),
    Rule("subject.protocol", "block", "technical", RFC3463, code=r"5\.\d+", note="X.5.XXX mail delivery protocol status"),
    Rule("subject.content", "block", "technical", RFC3463, code=r"6\.\d+", note="X.6.XXX message content or media status"),
    Rule("subject.policy", "block", "policy", RFC3463, code=r"7\.\d+", note="X.7.XXX security or policy status"),
)
REPLY_RULES: Tuple[Rule, ...] = (
    Rule("reply.transient", "soft", "temporary", RFC5321, reply=r"4\d\d", note="4yz transient negative completion: may succeed if retried"),
)

RULES: Tuple[Rule, ...] = PROVIDER_RULES + STRONG_CODES + TEXT_RULES + SUBJECT_RULES + REPLY_RULES

# which family a server's text gives away, when the recipient's provider is not known
FAMILY_HINTS: Tuple[Tuple[str, re.Pattern], ...] = (
    ("google", re.compile(r"\bgsmtp\b|support\.google\.com|google\.com/mail|\bgmail\b", re.I)),
    ("microsoft", re.compile(r"protection\.outlook\.com|postmaster\.live\.com|\boutlook\.com\b|\bhotmail\b|sender\.office\.com|"
                             r"\bS3150\b|\b(RP|SC|OU|DY)-00\d\b|\bAS\(\d+\)|Protocol Filter Agent", re.I)),
    ("yahoo", re.compile(r"yahoo|\bTSS?\d{2}\b|\bPH\d{2}\b|\[BL\d{2}\]|aol\.com", re.I)),
)

ENHANCED = re.compile(r"(?<![\d.])([245])\.(\d{1,3})\.(\d{1,3})(?!\d|\.\d)")
REPLY_AT_START = re.compile(r"^\s*(?:smtp;\s*)?([245]\d\d)(?=[\s\-]|$)", re.I)
REPLY_BEFORE_CODE = re.compile(r"(?<![\d.])([245]\d\d)[\s\-]+[245]\.\d{1,3}\.\d{1,3}")


@dataclass(frozen=True)
class Classification:
    cls: str
    action: str
    advice: str
    kind: str
    temporary: Optional[bool]       # True for a 4xx (deferral), False for a 5xx, None when neither code is present
    reply: Optional[str]
    code: Optional[str]
    family: Optional[str]
    rule: str
    source: str
    verified: bool
    note: str

    @property
    def reputation_block(self) -> bool:
        return self.cls == "block" and self.kind in REPUTATION_KINDS

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def parse_codes(smtp_status: Optional[str], diagnostic: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """(basic reply code, enhanced status code) from the status column and the server's text; either may be None."""
    status = (smtp_status or "").strip()
    text = (diagnostic or "").strip()
    code = None
    for source in (status, text):
        m = ENHANCED.search(source)
        if m:
            code = ".".join(m.groups())
            break
    reply = None
    for source in (status, text):
        m = REPLY_AT_START.match(source) or REPLY_BEFORE_CODE.search(source)
        if m:
            reply = m.group(1)
            break
    return reply, code


def family_of(provider: Optional[str], text: str = "") -> Optional[str]:
    """The family whose documented codes apply: from the provider key when known, else from the server's own text."""
    if provider:
        from engine.deliverability.providers import PROVIDERS
        p = PROVIDERS.get(provider)
        if p and p.family in ("google", "microsoft", "yahoo", "apple"):
            return p.family
        if provider in ("google", "microsoft", "yahoo", "apple"):
            return provider
    for fam, pattern in FAMILY_HINTS:
        if pattern.search(text):
            return fam
    return None


def _matches(rule: Rule, family: Optional[str], reply: Optional[str], code: Optional[str], text: str) -> bool:
    if rule.family is not None and rule.family != family:
        return False
    if rule.no_code and code is not None:
        return False
    if rule.code is not None:
        if code is None or not re.fullmatch(rule.code, code.split(".", 1)[1]):
            return False
    if rule.status_class is not None:
        cls_digit = code[0] if code else (reply[0] if reply else None)
        if cls_digit != rule.status_class:
            return False
    if rule.reply is not None and (reply is None or not re.fullmatch(rule.reply, reply)):
        return False
    if rule.text is not None and not re.search(rule.text, text, re.I):
        return False
    return True


def classify(smtp_status: Optional[str] = None, diagnostic: Optional[str] = None, provider: Optional[str] = None,
             event_name: Optional[str] = None) -> Classification:
    """Classify one bounce or deferral. provider: a key from engine/deliverability/providers.py (or a family name).

    event_name ('bounced' or 'deferred') decides `temporary` only when neither code is present."""
    text = " ".join(x for x in ((smtp_status or "").strip(), (diagnostic or "").strip()) if x)
    reply, code = parse_codes(smtp_status, diagnostic)
    family = family_of(provider, text)
    digit = code[0] if code else (reply[0] if reply else None)
    temporary = {"4": True, "5": False, "2": False}.get(digit or "", None)
    if temporary is None and event_name in ("bounced", "deferred"):
        temporary = event_name == "deferred"
    chosen: Optional[Rule] = None
    if text:
        for rule in RULES:
            if _matches(rule, family, reply, code, text):
                chosen = rule
                break
    if chosen is None:
        action, advice = ACTIONS["unknown"]
        why = "no status code and no text" if not text else "nothing in the code or text matched a rule"
        return Classification("unknown", action, advice, "unrecognized", temporary, reply, code, family, "none", "", False, why)
    action, advice = ACTIONS[chosen.cls]
    return Classification(chosen.cls, action, advice, chosen.kind, temporary, reply, code, family, chosen.id, chosen.source,
                          chosen.verified, chosen.note)


def run_corpus(path: str) -> Dict[str, Any]:
    """Classify every example in a corpus file ([{smtp_status, diagnostic, provider, expect}, ...]) and report mismatches."""
    from pathlib import Path
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = doc["cases"] if isinstance(doc, dict) else doc
    results, wrong = [], []
    for c in cases:
        r = classify(c.get("smtp_status"), c.get("diagnostic"), c.get("provider"), c.get("event_name"))
        ok = r.cls == c["expect"] and (c.get("kind") is None or r.kind == c["kind"])
        results.append({"case": c.get("id"), "expect": c["expect"], "got": r.cls, "kind": r.kind, "rule": r.rule, "ok": ok})
        if not ok:
            wrong.append(results[-1])
    return {"cases": len(cases), "wrong": wrong, "results": results}


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Classify a bounce or deferral: hard, soft, block, auth, rate or unknown")
    ap.add_argument("diagnostic", nargs="?", help="the receiving server's text, e.g. '550 5.1.1 The email account ... does not exist'")
    ap.add_argument("--status", help="the enhanced status code, when it is a separate column (5.1.1)")
    ap.add_argument("--provider", help="the recipient's provider key (google, microsoft, yahoo, ...) or a recipient domain")
    ap.add_argument("--corpus", help="run a corpus file and report every mismatch")
    a = ap.parse_args(argv)
    if a.corpus:
        r = run_corpus(a.corpus)
        print(json.dumps({"cases": r["cases"], "wrong": r["wrong"]}, indent=2))
        return 1 if r["wrong"] else 0
    if not a.diagnostic and not a.status:
        ap.error("give the server's text, --status, or --corpus")
    provider = a.provider
    if provider and "." in provider:
        from engine.deliverability.providers import classify as provider_of
        provider = provider_of(provider).provider
    print(json.dumps(classify(a.status, a.diagnostic, provider).as_dict(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
