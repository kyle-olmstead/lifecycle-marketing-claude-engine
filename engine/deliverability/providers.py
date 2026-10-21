"""
Mailbox providers: which company filters the mail sent to a recipient domain.

Deliverability problems are per provider. Gmail, Outlook.com and Yahoo each keep their own reputation
for a sender, return their own error codes and offer their own sender tools, so a bounce report that
lumps them together hides the one provider that is throttling you. This module maps a recipient domain
to a provider so engine/deliverability/monitor.py can count by provider and engine/deliverability/bounces.py
can apply the provider's documented error codes.

How a domain is mapped, in order:

  1. Consumer mailbox domains from a fixed list (gmail.com, outlook.com, yahoo.com, icloud.com and the rest
     below), each with the source that names it.
  2. Name patterns for families that span many country domains (hotmail.<tld>, live.<tld>, outlook.<tld>,
     yahoo.<tld>). These are labeled "name pattern": Microsoft and Yahoo publish no complete list, so a match
     is a likely mapping, not a verified one. yahoo.co.jp is excluded: Yahoo! JAPAN is a different company.
  3. MX hosts, when the caller supplies them or allows a lookup: smtp.google.com and aspmx* for Google
     Workspace, *.mail.protection.outlook.com and *.mx.microsoft for Microsoft 365, ppe-hosted.com and
     pphosted.com for Proofpoint, iphmx.com for Cisco, and others below. This is how business domains,
     which have no name pattern, are recognized.
  4. Otherwise "other". Nothing is guessed.

Lookups happen only when `lookup=True` is passed (or --lookup-mx on the command line); tests never look
anything up. Provider identity changes which triage advice is shown, never a verdict.

    python3 engine/deliverability/providers.py gmail.com hotmail.co.uk acme.example --mx acme.example=acme-example.mail.protection.outlook.com
    python3 engine/deliverability/providers.py acme.example --lookup-mx
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
from typing import Callable, Dict, Iterable, List, Optional, Tuple

# ----------------------------------------------------------------------------------------------
# providers
# ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Provider:
    key: str
    name: str
    kind: str            # consumer | business | gateway | other
    family: str          # whose error codes apply: google, microsoft, yahoo, apple, or the key itself
    feedback_loop: str   # per-message complaint feed: "per-message" | "aggregate only" | "none" | "unknown"


PROVIDERS: Dict[str, Provider] = {p.key: p for p in (
    # Gmail sends no per-message complaint reports: only aggregate Feedback-ID data in Postmaster Tools
    # (https://support.google.com/mail/answer/6254652, verified October 2026)
    Provider("google", "Gmail (personal accounts)", "consumer", "google", "aggregate only"),
    Provider("google_workspace", "Google Workspace (business Gmail)", "business", "google", "none"),
    # JMRP returns per-message reports (https://substrate.office.com/ip-domain-management-snds/Postmaster/Services)
    Provider("microsoft", "Microsoft Outlook.com (consumer)", "consumer", "microsoft", "per-message"),
    Provider("microsoft_365", "Microsoft 365 (business)", "business", "microsoft", "none"),
    # Yahoo CFL returns ARF reports for the domains Yahoo hosts (https://senders.yahooinc.com/faqs)
    Provider("yahoo", "Yahoo and AOL", "consumer", "yahoo", "per-message"),
    # "We don't offer a feedback loop (FBL)" (https://support.apple.com/HT204137)
    Provider("apple", "Apple iCloud Mail", "consumer", "apple", "none"),
    Provider("proofpoint", "Proofpoint gateway", "gateway", "proofpoint", "none"),
    Provider("mimecast", "Mimecast gateway", "gateway", "mimecast", "none"),
    Provider("barracuda", "Barracuda gateway", "gateway", "barracuda", "none"),
    Provider("cisco", "Cisco Secure Email gateway", "gateway", "cisco", "none"),
    Provider("other", "Other or unmapped", "other", "other", "unknown"),
)}

# ----------------------------------------------------------------------------------------------
# consumer domains (step 1)
# ----------------------------------------------------------------------------------------------

SRC_GMAIL_FBL = "https://support.google.com/mail/answer/6254652"
SRC_MS_HVS = "https://support.microsoft.com/en-us/outlook/fix-ndr-error-550-5-7-515-in-outlook-com"
SRC_YAHOO = "https://senders.yahooinc.com/best-practices"
SRC_AOL_MX = "https://www.postmastery.com/aol-yahoo-merge-impact-mta-email-delivery/"
SRC_APPLE = "https://support.apple.com/HT204137"

# domain: (provider, source, verified). "verified" means the provider's own documentation names the domain;
# False means a secondary source (named) and should be confirmed with an MX lookup before it matters.
CONSUMER_DOMAINS: Dict[str, Tuple[str, str, bool]] = {
    # Google: the FBL page says its data covers "only @gmail.com recipients"
    "gmail.com": ("google", SRC_GMAIL_FBL, True),
    # googlemail.com: the address Google used in the UK and Germany after trademark disputes (press coverage,
    # e.g. https://www.computerworld.com/article/1533004/google-settles-gmail-trademark-dispute.html); not on a Google page we could fetch
    "googlemail.com": ("google", "https://www.computerworld.com/article/1533004/google-settles-gmail-trademark-dispute.html", False),
    # Microsoft consumer: the 5.7.515 help page names Outlook.com, Hotmail, Live.com and MSN
    "outlook.com": ("microsoft", SRC_MS_HVS, True),
    "hotmail.com": ("microsoft", SRC_MS_HVS, True),
    "live.com": ("microsoft", SRC_MS_HVS, True),
    "msn.com": ("microsoft", SRC_MS_HVS, True),
    # Yahoo: yahoo.com from Yahoo's own pages; ymail.com and rocketmail.com are Yahoo Mail address domains
    # (press coverage of their launch: https://www.infoworld.com/article/2168643/yahoo-to-add-new-domains-to-webmail-service-2.html)
    "yahoo.com": ("yahoo", SRC_YAHOO, True),
    "ymail.com": ("yahoo", "https://www.infoworld.com/article/2168643/yahoo-to-add-new-domains-to-webmail-service-2.html", False),
    "rocketmail.com": ("yahoo", "https://www.infoworld.com/article/2168643/yahoo-to-add-new-domains-to-webmail-service-2.html", False),
    # AOL and the domains it hosted moved onto Yahoo's MX (mx-aol.mail.gm0.yahoodns.net), per Postmastery;
    # Yahoo's FAQ confirms CFL covers "any of the thousands of domains we host and support"
    "aol.com": ("yahoo", SRC_AOL_MX, False),
    "aol.co.uk": ("yahoo", SRC_AOL_MX, False),
    "aim.com": ("yahoo", SRC_AOL_MX, False),
    "verizon.net": ("yahoo", SRC_AOL_MX, False),
    "netscape.net": ("yahoo", SRC_AOL_MX, False),
    "cs.com": ("yahoo", SRC_AOL_MX, False),
    # Apple: "iCloud Mail covers icloud.com, me.com and mac.com"
    "icloud.com": ("apple", SRC_APPLE, True),
    "me.com": ("apple", SRC_APPLE, True),
    "mac.com": ("apple", SRC_APPLE, True),
}

# Names that look like a family but belong to someone else.
NOT_FAMILY: Dict[str, str] = {
    "yahoo.co.jp": "Yahoo! JAPAN is operated by LY Corporation, not Yahoo Inc.; Yahoo's sender tools do not cover it",
}

# step 2: family name patterns. Unverified per domain: Microsoft and Yahoo publish no full list of their country
# domains (we found none in October 2026), so these map likely members and say so.
FAMILY_PATTERNS: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"^(hotmail|live|outlook|windowslive)\.(co\.[a-z]{2}|com\.[a-z]{2}|[a-z]{2,3})$"), "microsoft"),
    (re.compile(r"^(yahoo|ymail)\.(co\.[a-z]{2}|com\.[a-z]{2}|[a-z]{2,3})$"), "yahoo"),
)

# ----------------------------------------------------------------------------------------------
# MX hosts (step 3)
# ----------------------------------------------------------------------------------------------

# (pattern on the MX host, provider, source, verified)
MX_RULES: Tuple[Tuple[re.Pattern, str, str, bool], ...] = (
    # Google Workspace: "smtp.google.com" is the current value; legacy values "start with aspmx" and remain supported
    (re.compile(r"^smtp\.google\.com$|^aspmx\d*\.(l\.google|googlemail)\.com$|\.aspmx\.l\.google\.com$"), "google_workspace",
     "https://knowledge.workspace.google.com/kb/how-to-update-your-mx-records-to-work-with-gmail-000005563", True),
    # personal Gmail itself (gmail-smtp-in.l.google.com): observed MX, no Google page lists it
    (re.compile(r"(^|\.)gmail-smtp-in\.l\.google\.com$"), "google", "observed MX for gmail.com; not in Google documentation we could fetch", False),
    # Microsoft 365: <domain>.mail.protection.outlook.com, and <domain>.<random>.mx.microsoft for DANE-enabled
    # domains (Microsoft Exchange Team blog, as quoted by https://www.spamresource.com/2023/10/mx-records-changing-for-microsoft-host.html)
    (re.compile(r"\.mail\.protection\.outlook\.com$|\.mx\.microsoft$"), "microsoft_365",
     "https://techcommunity.microsoft.com/t5/exchange-team-blog/implementing-inbound-smtp-dane-with-dnssec-for-exchange-online/ba-p/3939694", True),
    # Outlook.com consumer domains: *.olc.protection.outlook.com is widely observed, but no Microsoft page we
    # could fetch documents it
    (re.compile(r"\.olc\.protection\.outlook\.com$"), "microsoft", "observed MX for hotmail.com and outlook.com; not in Microsoft documentation we could fetch", False),
    # Yahoo and AOL: mx-aol.mail.gm0.yahoodns.net (Postmastery); Yahoo's own MX sit under yahoodns.net as well
    (re.compile(r"\.yahoodns\.net$"), "yahoo", SRC_AOL_MX, False),
    # iCloud: mx01.mail.icloud.com and siblings are observed; Apple's postmaster page does not list them
    (re.compile(r"\.mail\.icloud\.com$"), "apple", "observed MX for icloud.com; not in Apple documentation", False),
    # Proofpoint Essentials: mx1-us1.ppe-hosted.com, mx1-eu1.ppe-hosted.com (Proofpoint help)
    (re.compile(r"\.ppe-hosted\.com$"), "proofpoint",
     "https://help.proofpoint.com/Proofpoint_Essentials/Administrator_Topics/000_gettingstarted/020_connectiondetails", True),
    # Proofpoint enterprise: mx0a-<id>.pphosted.com is observed in public DNS; not in Proofpoint documentation we could fetch
    (re.compile(r"\.pphosted\.com$"), "proofpoint", "observed MX hosts (mx0a-*.pphosted.com); vendor documentation not fetched", False),
    # Cisco Secure Email Cloud Gateway: mx1.<alloc>(.<region>).iphmx.com (Cisco docs)
    (re.compile(r"\.iphmx\.com$"), "cisco", "https://docs.ces.cisco.com/docs/hostnames", True),
    # Mimecast: <region>-smtp-inbound-<n>.mimecast.com (secondary source; Mimecast's own pages could not be fetched)
    (re.compile(r"(^|\.)mimecast(-offshore)?\.com$"), "mimecast", "https://www.captaindns.com/fr/blog/mimecast-secure-email-gateway", False),
    # Barracuda Email Gateway Defense: <customer id>.ess.barracudanetworks.com (secondary source citing Barracuda Campus)
    (re.compile(r"\.ess\.barracudanetworks\.com$|\.barracudanetworks\.com$"), "barracuda",
     "https://www.captaindns.com/en/blog/barracuda-secure-email-gateway", False),
)

MxLookup = Callable[[str], List[str]]   # domain to its MX hosts, best preference first; [] when none


@dataclass(frozen=True)
class ProviderMatch:
    domain: str
    provider: str
    name: str
    kind: str
    family: str
    basis: str            # consumer list | name pattern | mx | excluded | unknown
    evidence: str         # the domain or MX host that matched, and why
    source: str
    verified: bool

    def as_dict(self) -> Dict[str, object]:
        return asdict(self)


def normalize_domain(value: Optional[str]) -> Optional[str]:
    """The domain of an address or a bare domain, lower case, without a trailing dot. None when empty."""
    if not value:
        return None
    v = str(value).strip().strip("<>").lower()
    if "@" in v:
        v = v.rsplit("@", 1)[1]
    v = v.rstrip(".")
    return v or None


def _match(domain: str, provider: str, basis: str, evidence: str, source: str, verified: bool) -> ProviderMatch:
    p = PROVIDERS[provider]
    return ProviderMatch(domain, p.key, p.name, p.kind, p.family, basis, evidence, source, verified)


def from_mx(domain: str, mx_hosts: Iterable[str]) -> Optional[ProviderMatch]:
    """The provider behind a domain's MX hosts, from the first host (best preference) that a rule recognizes."""
    for host in mx_hosts:
        h = (host or "").strip().lower().rstrip(".")
        for pattern, provider, source, verified in MX_RULES:
            if h and pattern.search(h):
                return _match(domain, provider, "mx", f"MX {h}", source, verified)
    return None


def system_mx_lookup(timeout: float = 5.0) -> MxLookup:
    """MX lookups through the system resolver (dnspython). Used only when the caller opts in."""
    from engine.deliverability.dns_collect import system_resolver
    resolve = system_resolver(timeout=timeout)
    return lambda domain: resolve(domain, "MX")


def classify(domain: Optional[str], mx: Optional[Iterable[str]] = None, lookup: bool = False,
             mx_lookup: Optional[MxLookup] = None) -> ProviderMatch:
    """Map one recipient domain (or address) to its mailbox provider.

    mx: the domain's MX hosts, when known. lookup: allow an MX lookup for a domain that no list or pattern
    covers (never in tests); mx_lookup replaces the system resolver, for tests and offline runs."""
    d = normalize_domain(domain)
    if d is None:
        return _match("", "other", "unknown", "no recipient domain", "", False)
    if d in NOT_FAMILY:
        # general knowledge, not read from a fetched source; mapping to "other" is the safe direction either way
        return _match(d, "other", "excluded", NOT_FAMILY[d], "", False)
    if d in CONSUMER_DOMAINS:
        provider, source, verified = CONSUMER_DOMAINS[d]
        return _match(d, provider, "consumer list", d, source, verified)
    if mx:
        m = from_mx(d, mx)
        if m:
            return m
    for pattern, provider in FAMILY_PATTERNS:
        if pattern.match(d):
            return _match(d, provider, "name pattern", f"{d} looks like a {PROVIDERS[provider].name} country domain; "
                          "confirm with its MX record", "no complete list is published", False)
    if lookup:
        try:
            hosts = (mx_lookup or system_mx_lookup())(d)
        except Exception as e:  # noqa: BLE001 - a failed lookup leaves the domain unmapped, never guessed
            return _match(d, "other", "unknown", f"MX lookup failed: {type(e).__name__}", "", False)
        m = from_mx(d, hosts)
        if m:
            return m
        if hosts:
            return _match(d, "other", "unknown", f"MX {hosts[0]} is not a recognized provider", "", False)
    return _match(d, "other", "unknown", "not in the consumer list; no MX supplied" + ("" if lookup else " (lookups off)"), "", False)


class Resolver:
    """Classifies many domains with one MX map and a per-domain cache (the monitor calls it once per domain)."""

    def __init__(self, mx: Optional[Dict[str, List[str]]] = None, lookup: bool = False, mx_lookup: Optional[MxLookup] = None):
        self.mx = {normalize_domain(k): v for k, v in (mx or {}).items() if normalize_domain(k)}
        self.lookup = lookup
        self.mx_lookup = mx_lookup
        self._cache: Dict[str, ProviderMatch] = {}

    def __call__(self, domain: Optional[str]) -> ProviderMatch:
        d = normalize_domain(domain) or ""
        if d not in self._cache:
            self._cache[d] = classify(d, self.mx.get(d), self.lookup, self.mx_lookup)
        return self._cache[d]

    def seen(self) -> List[ProviderMatch]:
        return [self._cache[d] for d in sorted(self._cache)]


def load_mx_map(path: str) -> Dict[str, List[str]]:
    """Read {"domain": ["mx1", "mx2"]} (or {"domain": "mx1"}) from a JSON file."""
    from pathlib import Path
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a JSON object mapping each domain to its MX host(s)")
    return {k: ([v] if isinstance(v, str) else list(v)) for k, v in raw.items() if not str(k).startswith("_")}


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Map recipient domains to mailbox providers")
    ap.add_argument("domains", nargs="+", help="domains or addresses")
    ap.add_argument("--mx", action="append", default=[], help="domain=mxhost[,mxhost] (repeatable)")
    ap.add_argument("--mx-file", help='JSON {"domain": ["mx host", ...]}')
    ap.add_argument("--lookup-mx", action="store_true", help="look up MX records for unmapped domains (network)")
    a = ap.parse_args(argv)
    mx: Dict[str, List[str]] = load_mx_map(a.mx_file) if a.mx_file else {}
    for item in a.mx:
        if "=" not in item:
            print(f"error: --mx {item!r}: use domain=mxhost", file=sys.stderr)
            return 2
        d, hosts = item.split("=", 1)
        mx[d] = [h for h in hosts.split(",") if h]
    r = Resolver(mx, lookup=a.lookup_mx)
    print(json.dumps([r(d).as_dict() for d in a.domains], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
