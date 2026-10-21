"""
Deliverability analyzer: the deterministic half of modules/01_deliverability_and_infrastructure.

Claude does the diagnosis. This file does the parts that must never be guessed: parsing SPF, DKIM,
DMARC, MTA-STS, TLS-RPT and BIMI records; reading Authentication-Results and DKIM-Signature headers;
computing SPF/DKIM alignment against the visible From domain; checking the Gmail, Yahoo and
Microsoft bulk-sender requirements that can be checked from evidence; and issuing the sending gate.

No network access. Records are taken from the input document, so every finding carries the exact
lookup that would confirm it (`verify_with`). Records without an `observed_at` date are treated as
supplied claims, not observations, and can lower the gate but never raise it.

    python3 tools/deliverability.py analyze examples/deliverability/incident.json
    python3 tools/deliverability.py analyze examples/deliverability/incident.json --markdown
    python3 tools/deliverability.py spf "v=spf1 include:_spf.esp.example.net ~all"
    python3 tools/deliverability.py dmarc "v=DMARC1; p=none; rua=mailto:dmarc@example.com"

Input shape: see examples/deliverability/incident.json (annotated) and healthy.json.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import email
import json
import re
import sys
from dataclasses import asdict, dataclass, field, replace
from email import policy as email_policy
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# ----------------------------------------------------------------------------------------------
# organizational domain
# ----------------------------------------------------------------------------------------------

try:  # bundled Public Suffix List snapshot; no network access needed
    from publicsuffixlist import PublicSuffixList

    _PSL = PublicSuffixList()

    def organizational_domain(domain: str) -> str:
        d = (domain or "").strip(".").lower()
        return _PSL.privatesuffix(d) or d
except ImportError:  # pragma: no cover - fallback keeps the tool usable without the package
    _MULTI = {"co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au", "co.nz", "co.jp", "com.br", "com.mx", "co.za"}

    def organizational_domain(domain: str) -> str:
        labels = (domain or "").strip(".").lower().split(".")
        if len(labels) >= 3 and ".".join(labels[-2:]) in _MULTI:
            return ".".join(labels[-3:])
        return ".".join(labels[-2:])

ORG_DOMAIN_METHOD = (
    "Public Suffix List (RFC 7489 method). RFC 9989 receivers use a DNS tree walk instead; for domains "
    "that publish no psd= tag the two agree in ordinary cases. If you publish psd=, confirm with the tree walk."
)

# ----------------------------------------------------------------------------------------------
# findings
# ----------------------------------------------------------------------------------------------

SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
CAUSES = ("identity", "reputation", "consent", "content_infrastructure")


@dataclass
class Finding:
    id: str
    severity: str
    cause: str
    title: str
    detail: str
    label: str = "SUPPLIED"            # LIVE PULL (observed, dated) | SUPPLIED (pasted, unverified) | UNKNOWN
    verify_with: str = ""
    fix: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        assert self.severity in SEVERITY_RANK, self.severity
        assert self.cause in CAUSES, self.cause


def dns_fix(host: str, value: str, rtype: str = "TXT", ttl: int = 3600, note: str = "") -> Dict[str, Any]:
    out = {"host": host, "type": rtype, "ttl": ttl, "value": value}
    if note:
        out["note"] = note
    return out


def _tags(record: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in record.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


# ----------------------------------------------------------------------------------------------
# SPF (RFC 7208)
# ----------------------------------------------------------------------------------------------

SPF_LOOKUP_MECHANISMS = ("include", "a", "mx", "ptr", "exists")


@dataclass
class SPFResult:
    record: str
    valid: bool
    all_qualifier: Optional[str]          # '+', '-', '~', '?' or None when no all and no redirect
    redirect: Optional[str]
    includes: List[str]
    lookup_terms: List[str]
    lookups_direct: int
    lookups_total: Optional[int]           # None when an include could not be resolved from the input
    unresolved: List[str]
    issues: List[Tuple[str, str]] = field(default_factory=list)  # (severity, message)


def _spf_terms(record: str) -> List[str]:
    return record.split()[1:]


def _spf_direct(record: str) -> Tuple[List[str], List[str], Optional[str], Optional[str], List[Tuple[str, str]]]:
    lookups, includes, issues = [], [], []
    redirect = all_q = None
    seen_all = False
    for term in _spf_terms(record):
        t = term
        q = "+"
        if t[:1] in "+-~?":
            q, t = t[0], t[1:]
        name = re.split(r"[:/=]", t, maxsplit=1)[0].lower()
        if seen_all and name not in ("exp",):
            issues.append(("medium", f"'{term}' appears after 'all' and is ignored by every receiver"))
        if name == "all":
            all_q, seen_all = q, True
        elif name in SPF_LOOKUP_MECHANISMS:
            lookups.append(term)
            if name == "include":
                includes.append(t.split(":", 1)[1] if ":" in t else "")
            if name == "ptr":
                issues.append(("medium", "the ptr mechanism is deprecated (RFC 7208 section 5.5) and slow; remove it"))
        elif name == "redirect":
            redirect = t.split("=", 1)[1] if "=" in t else ""
        elif name in ("ip4", "ip6", "exp"):
            pass
        elif re.match(r"^[a-z][a-z0-9_.-]*=", t, re.I):
            pass   # an unrecognized modifier (ra=, rp=, ...): RFC 7208 section 6 says receivers MUST ignore it
        else:
            issues.append(("high", f"unknown SPF mechanism '{term}' makes the record a permerror at strict receivers"))
    if redirect is not None and all_q is not None:
        issues.append(("info", f"redirect={redirect} is ignored because the record has an 'all' mechanism (RFC 7208 section 6.1)"))
        redirect = None
    elif redirect is not None:
        lookups.append(f"redirect={redirect}")
    return lookups, includes, redirect, all_q, issues


def parse_spf(record: str, resolved: Optional[Dict[str, str]] = None) -> SPFResult:
    """Parse one SPF record. `resolved` maps include/redirect targets to their records, for exact lookup counts."""
    record = " ".join(record.split())
    valid = record.lower().startswith("v=spf1") and (len(record) == 6 or record[6] == " ")
    lookups, includes, redirect, all_q, issues = _spf_direct(record) if valid else ([], [], None, None, [])
    resolved = {k.lower(): v for k, v in (resolved or {}).items()}
    unresolved: List[str] = []

    def count(rec: str, depth: int, chain: Tuple[str, ...]) -> Optional[int]:
        lk, inc, red, _, _ = _spf_direct(rec)
        total = len(lk)
        for target in inc + ([red] if red else []):
            key = (target or "").lower()
            if key in chain:
                return None  # loop: permerror
            if key not in resolved:
                if key not in unresolved:
                    unresolved.append(key)
                continue
            sub = count(resolved[key], depth + 1, chain + (key,))
            if sub is None:
                return None
            total += sub
        return total

    total = count(record, 0, ()) if valid else None
    looped = valid and total is None and not unresolved
    if looped:
        issues.append(("critical", "the include or redirect chain loops back on itself: every receiver returns permerror and SPF fails"))
    if not valid:
        issues.append(("critical", "record does not start with 'v=spf1'; receivers ignore it"))
    else:
        if all_q == "+":
            issues.append(("critical", "'+all' authorizes every server on the internet to send as this domain"))
        elif all_q == "?":
            issues.append(("high", "'?all' is neutral: unauthorized mail gets no SPF penalty"))
        elif all_q is None and redirect is None:
            issues.append(("medium", "no 'all' term and no redirect: the default result is neutral"))
        elif all_q == "~":
            issues.append(("info", "'~all' (softfail) is right during a migration; move to '-all' once the sending inventory is complete"))
        if total is not None and total > 10:
            issues.append(("critical", f"{total} DNS-resolving terms after expanding includes; the limit is 10 and strict receivers return permerror"))
        elif unresolved:
            issues.append(("info", f"{len(unresolved)} include(s) not supplied ({', '.join(unresolved)}); the full lookup count is UNKNOWN until they are resolved"))
        if len(lookups) > 10:
            issues.append(("critical", f"{len(lookups)} DNS-resolving terms at the top level alone; the limit is 10"))
    return SPFResult(record, valid, all_q, redirect, includes, lookups, len(lookups),
                     total if not unresolved else None, unresolved, issues)


# ----------------------------------------------------------------------------------------------
# DMARC (RFC 7489, superseded by RFC 9989 in May 2026)
# ----------------------------------------------------------------------------------------------

DMARC_POLICIES = ("none", "quarantine", "reject")


@dataclass
class DMARCResult:
    record: str
    valid: bool
    tags: Dict[str, str]
    policy: Optional[str]
    subdomain_policy: Optional[str]
    nonexistent_policy: Optional[str]
    adkim: str
    aspf: str
    testing: bool
    pct: Optional[int]
    enforcing: bool
    rua: List[str]
    issues: List[Tuple[str, str]] = field(default_factory=list)


def parse_dmarc(record: str) -> DMARCResult:
    record = " ".join(record.split())
    tags = _tags(record)
    issues: List[Tuple[str, str]] = []
    first = record.split(";", 1)[0].replace(" ", "")
    valid = first == "v=DMARC1"   # RFC 7489 section 6.3: the value MUST match precisely
    if not valid:
        issues.append(("critical", "the record must begin with 'v=DMARC1'; receivers ignore it otherwise"))
    p = tags.get("p", "").lower() or None
    if p not in DMARC_POLICIES:
        issues.append(("critical", f"policy p={p!r} is missing or invalid; it must be none, quarantine or reject"))
        valid = False
    sp = tags.get("sp", "").lower() or None
    np_ = tags.get("np", "").lower() or None
    for name, val in (("sp", sp), ("np", np_)):
        if val is not None and val not in DMARC_POLICIES:
            issues.append(("high", f"{name}={val!r} is invalid"))
    adkim = (tags.get("adkim") or "r").lower()
    aspf = (tags.get("aspf") or "r").lower()
    for name, val in (("adkim", adkim), ("aspf", aspf)):
        if val not in ("r", "s"):
            issues.append(("high", f"{name}={val!r} is invalid; use r or s"))
    testing = (tags.get("t") or "n").lower() == "y"
    pct: Optional[int] = None
    if "pct" in tags:
        try:
            pct = int(tags["pct"])
        except ValueError:
            issues.append(("high", f"pct={tags['pct']!r} is not an integer"))
        issues.append(("medium", "pct is removed in RFC 9989 (May 2026) and ignored by receivers that implement it; use t=y for a testing phase instead"))
        if "t" in tags:
            issues.append(("high", "pct and t are both present; RFC 7489 receivers honor pct and RFC 9989 receivers honor t, so the two populations apply different policies"))
    for legacy in ("ri", "rf"):
        if legacy in tags:
            issues.append(("info", f"{legacy}= moved out of the policy record in RFC 9989; it is harmless but can be removed"))
    rua = [u.strip() for u in tags.get("rua", "").split(",") if u.strip()]
    if not rua:
        issues.append(("high", "no rua= reporting address: without aggregate reports nobody can see which senders fail alignment, so enforcement can never be reached safely"))
    for uri in rua:
        if not uri.lower().startswith(("mailto:", "https:")):
            issues.append(("medium", f"rua URI {uri!r} is not a mailto: or https: URI"))
    if p == "none":
        issues.append(("medium", "p=none monitors only; spoofed mail is still delivered. It satisfies the bulk-sender minimum but is not protection"))
    if testing:
        issues.append(("info", "t=y (testing): RFC 9989 receivers apply the policy one level weaker than written"))
    enforcing = p in ("quarantine", "reject") and not testing and (pct is None or pct == 100)
    if sp == "none" and p in ("quarantine", "reject"):
        issues.append(("medium", "sp=none leaves every subdomain unprotected while the parent enforces"))
    return DMARCResult(record, valid, tags, p, sp, np_, adkim, aspf, testing, pct, enforcing, rua, issues)


def for_subdomain(d: DMARCResult) -> DMARCResult:
    """The organizational record as it applies to mail from a subdomain without its own record: sp= when present, else p=
    (RFC 7489 section 6.3, RFC 9989 section 4.7). The returned copy carries the policy that actually applies."""
    if d.subdomain_policy not in DMARC_POLICIES:
        return d
    sp = d.subdomain_policy
    return replace(d, policy=sp, enforcing=sp in ("quarantine", "reject") and not d.testing and (d.pct is None or d.pct == 100),
                   issues=list(d.issues))


def next_dmarc_step(current: DMARCResult, domain: str, aligned_pass_observed: bool = False,
                    subdomain: bool = False) -> Optional[Dict[str, Any]]:
    """The next record on the path none (with reports) -> quarantine (t=y) -> quarantine -> reject.

    Enforcement is never recommended blind. From p=none the next step is quarantine only when the record
    already has a rua= reporting address AND an aligned SPF or DKIM pass was observed for this mail;
    otherwise the step is p=none with reporting, because quarantining before every legitimate sender
    aligns sends your own mail to spam. Returns None when the record is already where it should be.

    subdomain=True: the mail comes from a subdomain covered by this (organizational) record. When the record
    has sp=, sp is what applies, so sp is the tag stepped and p is left alone; t=y is not added, since it
    would soften the parent domain's policy too.
    """
    step_sp = subdomain and current.subdomain_policy in DMARC_POLICIES
    if step_sp:
        current = for_subdomain(current)
    keep = {k: v for k, v in current.tags.items() if k in ("rua", "ruf", "adkim", "aspf", "fo", "sp", "np", "t")}
    if "rua" not in keep:
        keep["rua"] = f"mailto:dmarc-reports@{domain}"
    ready = bool(current.rua) and aligned_pass_observed
    if current.policy == "none" and not ready:
        nxt = {"p": "none"}
        missing = []
        if not current.rua:
            missing.append("add a rua= address so aggregate reports show who sends as this domain")
        if not aligned_pass_observed:
            missing.append("no aligned SPF or DKIM pass was observed for this mail, so enforcement now would quarantine your own sends")
        note = "Stay at p=none: " + "; ".join(missing) + ". Fix alignment first, then watch two weeks of reports before quarantine."
    elif current.policy == "none":
        nxt, note = {"p": "quarantine", "t": "y"}, ("Only after at least two weeks of aggregate reports show every legitimate sender passing "
                                                    "aligned SPF or DKIM. t=y keeps RFC 9989 receivers one level softer while you watch; receivers "
                                                    "still on RFC 7489 ignore t and quarantine in full, so publish this only when you are ready for quarantine.")
    elif current.policy == "quarantine" and (current.testing or (current.pct is not None and current.pct < 100)):
        nxt, note = {"p": "quarantine"}, "Drop the testing phase once reports stay clean for two more weeks."
    elif current.policy == "quarantine":
        nxt, note = {"p": "reject"}, "Final step. Confirm transactional senders (billing, password reset) pass aligned DKIM first."
    elif current.policy == "reject" and (current.testing or (current.pct is not None and current.pct < 100)):
        nxt, note = {"p": "reject"}, "Drop the testing phase (t=y or pct below 100) once reports stay clean for two more weeks."
    else:
        return None
    if step_sp:
        # t= belongs to the whole record: keep an existing t=y while stepping sp up, drop it when the step is ending the test
        nxt = {"p": current.tags.get("p", "none").lower(), "sp": nxt["p"], **({"t": keep["t"]} if "t" in keep and "t" in nxt else {})}
        note = "Mail from this subdomain gets sp=, not p=. " + note.replace("p=none", "sp=none")
    keep.pop("t", None)
    ordered = {"v": "DMARC1", **nxt, **{k: keep[k] for k in ("sp", "np", "adkim", "aspf", "rua", "ruf", "fo") if k in keep and k not in nxt}}
    value = "; ".join(f"{k}={v}" for k, v in ordered.items())
    if value.replace(" ", "").lower() == current.record.replace(" ", "").rstrip(";").lower():
        return None
    if "rua" not in current.tags:
        note += f" The rua mailbox dmarc-reports@{domain} must exist, or point rua at your DMARC report processor."
    return dns_fix(f"_dmarc.{domain}", value, note=note)


# ----------------------------------------------------------------------------------------------
# DKIM key records (RFC 6376, RFC 8301, RFC 8463)
# ----------------------------------------------------------------------------------------------

def _der_read(buf: bytes, i: int) -> Tuple[int, bytes, int]:
    tag = buf[i]
    length = buf[i + 1]
    j = i + 2
    if length & 0x80:
        n = length & 0x7F
        length = int.from_bytes(buf[j:j + n], "big")
        j += n
    return tag, buf[j:j + length], j + length


def rsa_modulus_bits(der: bytes) -> int:
    """Modulus size of an RSA public key in SubjectPublicKeyInfo or PKCS#1 DER form."""
    tag, body, _ = _der_read(der, 0)
    if tag != 0x30:
        raise ValueError("not a DER SEQUENCE")
    t1, v1, nxt = _der_read(body, 0)
    if t1 == 0x30:  # AlgorithmIdentifier: SubjectPublicKeyInfo
        t2, bitstring, _ = _der_read(body, nxt)
        if t2 != 0x03:
            raise ValueError("expected BIT STRING")
        tag, body, _ = _der_read(bitstring[1:], 0)
        t1, v1, nxt = _der_read(body, 0)
    if t1 != 0x02:
        raise ValueError("expected INTEGER modulus")
    n = v1.lstrip(b"\x00")
    return (len(n) - 1) * 8 + n[0].bit_length()


@dataclass
class DKIMKeyResult:
    selector_host: str
    valid: bool
    key_type: str
    bits: Optional[int]
    revoked: bool
    testing: bool
    issues: List[Tuple[str, str]] = field(default_factory=list)


def parse_dkim_key(selector_host: str, record: str) -> DKIMKeyResult:
    tags = _tags(record)
    issues: List[Tuple[str, str]] = []
    k = (tags.get("k") or "rsa").lower()
    p = re.sub(r"\s+", "", tags.get("p", ""))
    testing = "y" in [f.strip() for f in tags.get("t", "").split(":")]
    if tags.get("v") and tags["v"] != "DKIM1":
        issues.append(("critical", "v= must be DKIM1"))
    if "p" not in tags:
        return DKIMKeyResult(selector_host, False, k, None, False, testing, [("critical", "no p= tag: not a DKIM key record")])
    if p == "":
        return DKIMKeyResult(selector_host, True, k, None, True, testing, [("high", "p= is empty: this key is revoked and every signature with this selector fails")])
    bits: Optional[int] = None
    try:
        raw = base64.b64decode(p, validate=True)
        if k == "ed25519":
            bits = len(raw) * 8
        else:
            bits = rsa_modulus_bits(raw)
    except (binascii.Error, ValueError, IndexError):
        issues.append(("critical", "p= is not a valid base64 public key"))
    if k == "rsa" and bits is not None:
        if bits < 1024:
            issues.append(("critical", f"{bits}-bit RSA key: below the RFC 8301 minimum of 1024; receivers treat signatures as invalid"))
        elif bits < 2048:
            issues.append(("medium", f"{bits}-bit RSA key: valid, but 2048-bit is the standard where the provider supports it"))
    if "h" in tags and "sha256" not in tags["h"].lower():
        issues.append(("high", "h= restricts the key to sha1, which RFC 8301 forbids for signing"))
    if testing:
        issues.append(("medium", "t=y marks the key as testing; some verifiers treat its signatures as unsigned"))
    return DKIMKeyResult(selector_host, not any(s == "critical" for s, _ in issues), k, bits, False, testing, issues)


# ----------------------------------------------------------------------------------------------
# MTA-STS (RFC 8461), TLS-RPT (RFC 8460), BIMI
# ----------------------------------------------------------------------------------------------

MTA_STS_MAX_AGE = 31557600


def parse_mta_sts(txt: Optional[str], policy_text: Optional[str], mx_hosts: Optional[List[str]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"txt": txt, "mode": None, "mx_patterns": [], "max_age": None, "issues": []}
    issues = out["issues"]
    if txt:
        tags = _tags(txt)
        if tags.get("v") != "STSv1" or not tags.get("id"):
            issues.append(("high", "_mta-sts TXT must be 'v=STSv1; id=<policy id>'"))
    if txt and not policy_text:
        issues.append(("high", "the _mta-sts TXT exists but no policy file was supplied; fetch https://mta-sts.<domain>/.well-known/mta-sts.txt"))
    if policy_text:
        kv: Dict[str, List[str]] = {}
        for line in policy_text.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                kv.setdefault(k.strip().lower(), []).append(v.strip())
        if kv.get("version", [""])[0] != "STSv1":
            issues.append(("high", "policy file must declare 'version: STSv1'"))
        mode = kv.get("mode", [None])[0]
        out["mode"] = mode
        if mode not in ("enforce", "testing", "none"):
            issues.append(("high", f"mode {mode!r} is invalid"))
        elif mode == "testing":
            issues.append(("info", "mode testing: failures are reported (TLS-RPT) but not enforced; move to enforce once reports are clean"))
        elif mode == "none":
            issues.append(("medium", "mode none withdraws the policy"))
        out["mx_patterns"] = kv.get("mx", [])
        if mode in ("enforce", "testing") and not out["mx_patterns"]:
            issues.append(("high", "no mx: lines; in enforce mode this blocks all inbound mail"))
        try:
            out["max_age"] = int(kv.get("max_age", [""])[0])
            if not 0 <= out["max_age"] <= MTA_STS_MAX_AGE:
                issues.append(("high", f"max_age must be between 0 and {MTA_STS_MAX_AGE}"))
            elif out["max_age"] < 86400 and mode == "enforce":
                issues.append(("low", "max_age under one day gives little protection against downgrade attacks"))
        except ValueError:
            issues.append(("high", "max_age is missing or not an integer"))
        for host in (mx_hosts or []) if mode in ("enforce", "testing") else []:
            if not any(_mx_match(p, host) for p in out["mx_patterns"]):
                issues.append(("critical" if mode == "enforce" else "high", f"MX host {host} is not covered by any mx: pattern; enforce mode will reject delivery to it"))
    return out


def _mx_match(pattern: str, host: str) -> bool:
    pattern, host = pattern.lower().rstrip("."), host.lower().rstrip(".")
    if pattern.startswith("*."):
        return host.endswith(pattern[1:]) and host.count(".") == pattern.count(".")
    return pattern == host


def parse_tlsrpt(txt: Optional[str]) -> Dict[str, Any]:
    if not txt:
        return {"present": False, "issues": [("low", "no TLS-RPT record: TLS delivery failures to your domain are invisible")]}
    tags = _tags(txt)
    issues = []
    if tags.get("v") != "TLSRPTv1":
        issues.append(("medium", "TLS-RPT record must begin 'v=TLSRPTv1'"))
    if not tags.get("rua"):
        issues.append(("medium", "TLS-RPT record has no rua="))
    return {"present": True, "rua": tags.get("rua"), "issues": issues}


def bimi_eligibility(bimi_txt: Optional[str], dmarc: Optional[DMARCResult], effective: Optional[DMARCResult] = None) -> Dict[str, Any]:
    """dmarc is the organizational domain's record; effective is the policy that applies to the From domain (for_subdomain)."""
    out: Dict[str, Any] = {"present": bool(bimi_txt), "eligible": False, "reasons": []}
    if dmarc is None or not dmarc.valid:
        out["reasons"].append("no valid DMARC record")
        return out
    if dmarc.policy not in ("quarantine", "reject"):
        out["reasons"].append("DMARC policy must be quarantine or reject")
    if effective is not None and effective is not dmarc and effective.policy not in ("quarantine", "reject"):
        out["reasons"].append(f"the policy applying to the sending subdomain (sp={effective.policy}) must be quarantine or reject")
    if dmarc.pct is not None and dmarc.pct != 100:
        out["reasons"].append("pct must be absent or 100")
    if dmarc.testing:
        out["reasons"].append("t=y (testing) is not enforcement")
    if dmarc.subdomain_policy == "none":
        out["reasons"].append("sp=none is treated as non-enforcing by BIMI guidance; verify with the provider")
    if bimi_txt:
        tags = _tags(bimi_txt)
        if tags.get("v") != "BIMI1":
            out["reasons"].append("record must begin v=BIMI1")
        if not tags.get("a"):
            out["reasons"].append("no a= certificate: Gmail requires a VMC or CMC")
        if not tags.get("l") and not tags.get("a"):
            out["reasons"].append("no l= logo URL")
    out["eligible"] = not out["reasons"]
    out["priority_note"] = "BIMI is cosmetic. It is never a remediation priority."
    return out


# ----------------------------------------------------------------------------------------------
# headers: Authentication-Results, DKIM-Signature, List-Unsubscribe
# ----------------------------------------------------------------------------------------------

def _norm(v: str) -> str:
    return re.sub(r"\s+", " ", v or "").strip()


def _strip_comments(v: str) -> str:
    prev = None
    while prev != v:
        prev, v = v, re.sub(r"\([^()]*\)", "", v)
    return v


def parse_authentication_results(value: str) -> Dict[str, Any]:
    v = _strip_comments(_norm(value))
    parts = [p.strip() for p in v.split(";") if p.strip()]
    # Microsoft's headers omit the authserv-id and start with the first result (spf=pass ...): parse from the start then
    has_id = bool(parts) and not re.match(r"^[a-z0-9-]+\s*=\s*[a-z]+\b", parts[0], re.I)
    out: Dict[str, Any] = {"authserv_id": parts[0].split()[0] if has_id else None, "results": []}
    for p in (parts[1:] if has_id else parts):
        m = re.match(r"([a-z0-9-]+)\s*=\s*([a-z]+)(.*)", p, re.I)
        if not m:
            continue
        props = dict((k.lower(), val.strip('"')) for k, val in re.findall(r"([a-z0-9-]+\.[a-z0-9-]+)\s*=\s*(\S+)", m.group(3), re.I))
        out["results"].append({"method": m.group(1).lower(), "result": m.group(2).lower(), "props": props})
    return out


def _domain_of(addr: str) -> Optional[str]:
    addr = (addr or "").strip().strip("<>")
    if "@" in addr:
        return addr.rsplit("@", 1)[1].lower().rstrip(".")
    return addr.lower().rstrip(".") or None


def parse_headers(raw: str) -> Dict[str, Any]:
    raw = raw.replace("\r\n", "\n")
    if "\n\n" not in raw:
        raw = raw.rstrip("\n") + "\n\n"
    msg = email.message_from_string(raw, policy=email_policy.compat32)
    from_addrs = getaddresses([_norm(h) for h in msg.get_all("From", [])])
    from_domains = sorted({_domain_of(a) for _, a in from_addrs if _domain_of(a)})
    rp = _norm(msg.get("Return-Path", ""))
    dkim_sigs = []
    for h in msg.get_all("DKIM-Signature", []):
        t = _tags(_norm(h))
        dkim_sigs.append({"d": (t.get("d") or "").lower(), "s": t.get("s"), "a": t.get("a"), "b": re.sub(r"\s+", "", t.get("b") or ""),
                          "h": [x.strip().lower() for x in (t.get("h") or "").split(":") if x.strip()]})
    ars = [parse_authentication_results(h) for h in msg.get_all("Authentication-Results", [])]
    received = [_norm(h) for h in msg.get_all("Received", [])]
    lu = _norm(msg.get("List-Unsubscribe", ""))
    lup = _norm(msg.get("List-Unsubscribe-Post", ""))
    return {
        "from_domains": from_domains,
        "return_path_domain": _domain_of(rp) if rp else None,
        "message_id_domain": _domain_of(_norm(msg.get("Message-ID", "")).strip("<>")),
        "dkim_signatures": dkim_sigs,
        "authentication_results": ars,
        "received_hops": len(received),
        "tls_hops": sum(1 for r in received if re.search(r"\bESMTPS\b|\bTLS1?[._]?\d|version=TLS", r, re.I)),
        "list_unsubscribe": lu or None,
        "list_unsubscribe_post": lup or None,
        "subject": _norm(msg.get("Subject", "")) or None,
        "feedback_id": _norm(msg.get("Feedback-ID", "")) or None,
    }


def aligned(auth_domain: Optional[str], from_domain: Optional[str], mode: str,
            org: Callable[[str], str] = organizational_domain) -> bool:
    if not auth_domain or not from_domain:
        return False
    a, f = auth_domain.lower().rstrip("."), from_domain.lower().rstrip(".")
    return a == f if mode == "s" else org(a) == org(f)


def evaluate_alignment(h: Dict[str, Any], dmarc: Optional[DMARCResult]) -> Dict[str, Any]:
    """SPF and DKIM pass/alignment against the visible From domain, from the topmost Authentication-Results."""
    from_domain = h["from_domains"][0] if len(h["from_domains"]) == 1 else None
    ar = h["authentication_results"][0] if h["authentication_results"] else None
    adkim = dmarc.adkim if dmarc else "r"
    aspf = dmarc.aspf if dmarc else "r"
    spf = {"result": None, "domain": None, "aligned": None}
    dkim: List[Dict[str, Any]] = []
    dmarc_reported = None
    if ar:
        for r in ar["results"]:
            if r["method"] == "spf":
                spf["result"] = r["result"]
                spf["domain"] = _domain_of(r["props"].get("smtp.mailfrom") or r["props"].get("smtp.helo") or "")
            elif r["method"] == "dkim":
                d = r["props"].get("header.d") or _domain_of(r["props"].get("header.i", ""))
                dkim.append({"result": r["result"], "domain": (d or "").lower() or None, "selector": r["props"].get("header.s")})
            elif r["method"] == "dmarc":
                dmarc_reported = r["result"]
    if spf["domain"] is None and h.get("return_path_domain"):
        spf["domain"] = h["return_path_domain"]
    if not dkim:  # no receiver verdict: list signatures, result unknown
        dkim = [{"result": None, "domain": s["d"], "selector": s["s"]} for s in h["dkim_signatures"]]
    spf["aligned"] = aligned(spf["domain"], from_domain, aspf) if spf["domain"] else None
    for d in dkim:
        d["aligned"] = aligned(d["domain"], from_domain, adkim)
    spf_ok = spf["result"] == "pass" and bool(spf["aligned"])
    dkim_ok = any(d["result"] == "pass" and d["aligned"] for d in dkim)
    computed = "pass" if (spf_ok or dkim_ok) else ("fail" if (spf["result"] or any(d["result"] for d in dkim)) else None)
    return {
        "from_domain": from_domain, "authserv_id": ar["authserv_id"] if ar else None,
        "alignment_modes": {"adkim": adkim, "aspf": aspf},
        "spf": spf, "dkim": dkim, "dmarc_computed": computed, "dmarc_reported": dmarc_reported,
        "org_domain_method": ORG_DOMAIN_METHOD,
    }


def one_click_unsubscribe(h: Dict[str, Any]) -> Dict[str, Any]:
    lu, lup = h.get("list_unsubscribe") or "", h.get("list_unsubscribe_post") or ""
    has_https = bool(re.search(r"<https://[^>]+>", lu, re.I))
    has_mailto = bool(re.search(r"<mailto:[^>]+>", lu, re.I))
    post_ok = lup.replace(" ", "").lower() == "list-unsubscribe=one-click"
    covering = [s for s in h.get("dkim_signatures", []) if "list-unsubscribe" in s["h"] and "list-unsubscribe-post" in s["h"]]
    # RFC 8058 section 4: both headers must be in the h= tag of a VALID DKIM signature; validity comes from the receiver's verdict
    ar = h["authentication_results"][0] if h.get("authentication_results") else None
    results = [r for r in (ar or {}).get("results", []) if r["method"] == "dkim"]

    def verdict(sig: Dict[str, Any]) -> Optional[bool]:
        """The receiver's verdict on this one signature: matched on d=, then on the selector (header.s) and the signature
        prefix (header.b) when the receiver reports them. None when it cannot be told apart from another signature."""
        mine = []
        for r in results:
            p = r["props"]
            if (p.get("header.d") or _domain_of(p.get("header.i", "")) or "").lower() != sig["d"]:
                continue
            if p.get("header.s") and sig.get("s") and p["header.s"].lower() != str(sig["s"]).lower():
                continue
            if p.get("header.b") and sig.get("b") and not sig["b"].startswith(re.sub(r"\s+", "", p["header.b"])):
                continue
            mine.append(r)
        precise = [r for r in mine if r["props"].get("header.s") or r["props"].get("header.b")]
        if precise:
            return any(r["result"] == "pass" for r in precise)
        same_d = [x for x in h.get("dkim_signatures", []) if x["d"] == sig["d"]]
        if mine and (len(same_d) == 1 or len({r["result"] for r in mine}) == 1):
            return any(r["result"] == "pass" for r in mine)
        return None

    seen = [verdict(sig) for sig in covering]
    if not covering:
        valid: Optional[bool] = False
    elif any(v is True for v in seen):
        valid = True
    elif seen and all(v is False for v in seen):
        valid = False
    else:
        valid = None   # signatures cover both headers, but no receiver verdict says whether they verify
    compliant = (has_https and post_ok and valid) if valid is not None else (None if (has_https and post_ok) else False)
    return {"list_unsubscribe": bool(lu), "https_uri": has_https, "mailto_uri": has_mailto, "post_header_ok": post_ok,
            "dkim_covers_both": bool(covering), "covering_signature_verified": valid, "rfc8058_compliant": compliant}


# ----------------------------------------------------------------------------------------------
# the sending gate
# ----------------------------------------------------------------------------------------------

GATE_ORDER = {"GREEN": 0, "AMBER": 1, "RED": 2}
GATE_PERMITS = {
    "GREEN": "Volume growth within a warming ladder.",
    "AMBER": "Hold volume flat. Remediate. Engaged-segment sends only.",
    "RED": "Stop bulk sending. Remediation and re-permission only.",
}


def _band(value: Optional[float], green_below: float, red_above: float) -> Optional[str]:
    if value is None:
        return None
    if value < green_below:
        return "GREEN"
    return "RED" if value > red_above else "AMBER"


def sending_gate(signals: Dict[str, Any]) -> Dict[str, Any]:
    """Thresholds from the deliverability-engineer method. The worst signal sets the verdict."""
    rows: List[Dict[str, Any]] = []

    def add(signal: str, value: Any, status: Optional[str], rule: str) -> None:
        rows.append({"signal": signal, "value": value, "status": status or "UNKNOWN", "rule": rule})

    s = signals
    add("complaint_rate", s.get("complaint_rate"), _band(s.get("complaint_rate"), 0.001, 0.003), "GREEN <0.1%, AMBER 0.1 to 0.3%, RED >0.3%")
    add("hard_bounce_rate", s.get("hard_bounce_rate"), _band(s.get("hard_bounce_rate"), 0.01, 0.02), "GREEN <1%, AMBER 1 to 2%, RED >2%")
    drb, trend = s.get("domain_reputation_bounces_90d"), (s.get("domain_reputation_bounces_trend") or "").lower()
    add("domain_reputation_bounces", drb, None if drb is None else ("RED" if trend == "rising" else "AMBER" if drb > 0 else "GREEN"),
        "GREEN zero, AMBER any occurrence, RED rising trend (leading indicator: precedes complaint movement by weeks)")
    add("filtered_share", s.get("filtered_share"), _band(s.get("filtered_share"), 0.005, 0.05),
        "GREEN <0.5% (negligible), AMBER 0.5 to 5% (present), RED >5% (material share)")
    add("gmail_postmaster_spam_rate", s.get("postmaster_spam_rate"), _band(s.get("postmaster_spam_rate"), 0.001, 0.003),
        "GREEN <0.1%, AMBER 0.1 to 0.3%, RED >0.3% (Gmail's hard limit)")
    comp = (s.get("postmaster_compliance") or "").lower() or None
    add("gmail_postmaster_compliance", comp, {"pass": "GREEN", "fail": "AMBER"}.get(comp or ""), "any failing requirement is AMBER")
    rep = (s.get("postmaster_domain_reputation") or "").lower() or None
    if rep:
        add("postmaster_domain_reputation (retired dashboard)", rep,
            "RED" if rep in ("low", "bad") else "AMBER" if "declin" in (s.get("postmaster_reputation_trend") or "").lower() else "GREEN",
            "Google retired the domain and IP reputation dashboards in 2025; accepted only from older exports")
    ce, ee = s.get("consumer_engagement_rate"), s.get("enterprise_engagement_rate")
    ratio = (ee / ce) if (ce and ee is not None) else None
    add("enterprise_vs_consumer_engagement", None if ratio is None else round(ratio, 3),
        None if ratio is None else ("GREEN" if ratio >= 0.6 else "AMBER" if ratio >= 0.3 else "RED"),
        "enterprise gateway engagement as a share of consumer: GREEN comparable (>=60%), AMBER 30 to 60%, RED <30% (quarantine, not disinterest). Use clicks or bot-excluded opens")
    add("unengaged_share", s.get("unengaged_share"), _band(s.get("unengaged_share"), 0.20, 0.50), "GREEN <20%, AMBER 20 to 50%, RED >50%")
    auth = (s.get("auth_state") or "unknown").lower()
    add("authentication_state", auth, {"verified": "GREEN", "partial": "AMBER", "failing": "RED", "unknown": "RED"}.get(auth, "RED"),
        "GREEN verified, AMBER partial, RED failing or UNKNOWN")

    statuses = [r["status"] for r in rows if r["status"] != "UNKNOWN"]
    verdict = max(statuses, key=lambda x: GATE_ORDER[x]) if statuses else "AMBER"
    reasons = [f"{r['signal']} is {r['status']}" for r in rows if r["status"] in ("RED", "AMBER")]
    missing = [r["signal"] for r in rows if r["status"] == "UNKNOWN" and r["signal"] in ("complaint_rate", "hard_bounce_rate")]
    if verdict == "GREEN" and missing:
        verdict = "AMBER"
        reasons.append("unverified: " + ", ".join(missing) + " not supplied; an unverified signal can lower the gate, never raise it")
    return {"verdict": verdict, "permitted": GATE_PERMITS[verdict], "reasons": reasons, "signals": rows}


# ----------------------------------------------------------------------------------------------
# bulk-sender requirements (Gmail and Yahoo since Feb 2024, Gmail enforcement from Nov 2025, Microsoft from 5 May 2025)
# ----------------------------------------------------------------------------------------------

def bulk_sender_checks(al: Optional[Dict[str, Any]], dmarc: Optional[DMARCResult], unsub: Optional[Dict[str, Any]],
                       hdr: Optional[Dict[str, Any]], signals: Dict[str, Any], volume: Dict[str, Any]) -> Dict[str, Any]:
    def st(ok: Optional[bool]) -> str:
        return "UNKNOWN" if ok is None else ("PASS" if ok else "FAIL")

    spf_pass = None if not al or al["spf"]["result"] is None else al["spf"]["result"] == "pass"
    dkim_pass = None if not al or not al["dkim"] or all(d["result"] is None for d in al["dkim"]) else any(d["result"] == "pass" for d in al["dkim"])
    dmarc_ok = None if dmarc is None else (dmarc.valid and dmarc.policy in DMARC_POLICIES)
    dmarc_aligned = None if not al or al["dmarc_computed"] is None else al["dmarc_computed"] == "pass"
    spam = signals.get("postmaster_spam_rate")
    rows = [
        {"requirement": "SPF passes", "applies_to": "Gmail, Yahoo, Microsoft", "status": st(spf_pass)},
        {"requirement": "DKIM passes", "applies_to": "Gmail, Yahoo, Microsoft", "status": st(dkim_pass)},
        {"requirement": "DMARC record published, p=none at minimum", "applies_to": "Gmail, Yahoo, Microsoft", "status": st(dmarc_ok)},
        {"requirement": "DMARC passes through an aligned SPF or DKIM domain", "applies_to": "Gmail, Yahoo, Microsoft", "status": st(dmarc_aligned)},
        {"requirement": "One-click unsubscribe (RFC 8058) on marketing mail, honored within 48 hours", "applies_to": "Gmail, Yahoo (Microsoft expects a working unsubscribe link)",
         "status": st(None if unsub is None else unsub["rfc8058_compliant"])},
        {"requirement": "TLS on the delivery hop", "applies_to": "Gmail", "status": st(None if not hdr or hdr["received_hops"] == 0 else hdr["tls_hops"] > 0),
         "note": "read from Received headers; heuristic"},
        {"requirement": "Forward-confirmed reverse DNS on sending IPs", "applies_to": "Gmail", "status": "UNKNOWN", "verify_with": "dig -x <sending IP> +short, then dig +short A <returned name>"},
        {"requirement": "User-reported spam rate below 0.3% (target below 0.1%)", "applies_to": "Gmail, Yahoo",
         "status": st(None if spam is None else spam < 0.003)},
    ]
    gmail = volume.get("daily_to_gmail")
    ms = volume.get("daily_to_microsoft_consumer")
    return {
        "bulk_sender_gmail": None if gmail is None else gmail >= 5000,
        "bulk_sender_microsoft": None if ms is None else ms >= 5000,
        "note": ("Gmail bulk-sender status is permanent once reached (5,000+ messages a day to personal Gmail). Microsoft's rules cover "
                 "Outlook.com, Hotmail.com and Live.com consumer mailboxes at 5,000+ a day; failures bounce 550 5.7.515. Verify current "
                 "thresholds against provider documentation before certifying compliance."),
        "requirements": rows,
    }


# ----------------------------------------------------------------------------------------------
# the full analysis
# ----------------------------------------------------------------------------------------------

def _label(doc: Dict[str, Any]) -> str:
    return f"LIVE PULL {doc['observed_at']}" if doc.get("observed_at") else "SUPPLIED"


def _issues_to_findings(prefix: str, issues: List[Tuple[str, str]], cause: str, label: str, verify: str,
                        fix: Optional[Dict[str, Any]] = None) -> List[Finding]:
    out = []
    attached = False
    for i, (sev, msg) in enumerate(sorted(issues, key=lambda x: SEVERITY_RANK[x[0]])):
        give = fix is not None and not attached and sev in ("critical", "high", "medium")
        attached = attached or give
        out.append(Finding(f"{prefix}.{i + 1}", sev, cause, msg.split(":")[0][:90], msg, label, verify, fix if give else None))
    return out


SINGLE_RECORDS = {"dmarc": ("_dmarc", "RFC 7489 section 6.6.3: receivers apply no DMARC policy at all"),
                  "mta_sts": ("_mta-sts", "RFC 8461 section 3.1: senders assume there is no MTA-STS policy"),
                  "tlsrpt": ("_smtp._tls", "RFC 8460 section 3: senders assume there is no TLS-RPT policy"),
                  "bimi": ("default._bimi", "BIMI: mailbox providers show no logo")}


def analyze(doc: Dict[str, Any]) -> Dict[str, Any]:
    label = _label(doc)
    findings: List[Finding] = []
    # one record per name, or none at all: more than one means none applies, so report it and treat the name as empty
    dns: Dict[str, Any] = {}
    multi_dmarc = set()
    for host, rec in (doc.get("dns") or {}).items():
        rec = dict(rec)
        for key, (prefix, effect) in SINGLE_RECORDS.items():
            v = rec.get(key)
            if isinstance(v, list):
                if len(v) > 1:
                    if key == "dmarc":
                        multi_dmarc.add(host)
                    findings.append(Finding(f"{key}.multiple", "critical" if key == "dmarc" else "high", "identity" if key == "dmarc" else "content_infrastructure",
                                            f"{len(v)} records at {prefix}.{host}",
                                            f"There are {len(v)} records where exactly one is allowed ({effect}). Delete all but one: "
                                            + " | ".join(v), label, f"dig +short TXT {prefix}.{host}"))
                    rec.pop(key)
                else:
                    rec[key] = v[0] if v else None
        dns[host] = rec
    raw = doc.get("headers")
    if isinstance(raw, dict) and raw.get("path"):
        raw = Path(raw["path"]).read_text(encoding="utf-8")
    hdr = parse_headers(raw) if raw else None
    from_domain = (doc.get("from_domain") or (hdr["from_domains"][0] if hdr and len(hdr["from_domains"]) == 1 else None) or "").lower() or None
    if hdr and len(hdr["from_domains"]) != 1:
        findings.append(Finding("hdr.from", "critical", "identity", "From header does not contain exactly one address",
                                f"From domains found: {hdr['from_domains']}. DMARC requires exactly one author domain.", "SUPPLIED"))
    org = organizational_domain(from_domain) if from_domain else None
    checked: Dict[str, Any] = {"from_domain": from_domain, "organizational_domain": org, "org_domain_method": ORG_DOMAIN_METHOD}

    # DMARC: the From domain's own record, else the organizational domain's (subdomain policy applies)
    dmarc = None
    dmarc_host = None
    for cand in [d for d in (from_domain, org) if d]:
        rec = (dns.get(cand) or {}).get("dmarc")
        if rec:
            dmarc, dmarc_host = parse_dmarc(rec), cand
            break
    if from_domain and dmarc is None and not (multi_dmarc & {from_domain, org}):
        findings.append(Finding("dmarc.missing", "critical", "identity", "No DMARC record",
                                f"No record at _dmarc.{from_domain} or _dmarc.{org}. Every major mailbox provider requires one for bulk mail.",
                                "UNKNOWN" if not dns else label, f"dig +short TXT _dmarc.{org}",
                                dns_fix(f"_dmarc.{org}", f"v=DMARC1; p=none; rua=mailto:dmarc-reports@{org}",
                                        note=f"Start at p=none to collect reports. The mailbox dmarc-reports@{org} must exist, or use your DMARC report processor's address.")))
    effective = dmarc
    if dmarc:
        sub = dmarc_host != from_domain
        effective = for_subdomain(dmarc) if sub else dmarc
        if sub and effective.policy != dmarc.policy:
            dmarc.issues.append(("high" if effective.policy == "none" else "info",
                                 f"mail from {from_domain} is covered by the organizational record's sp={effective.policy}, not p={dmarc.policy}"
                                 + (": this subdomain is not protected while the parent enforces" if effective.policy == "none" else "")))
        observed = bool(hdr) and evaluate_alignment(hdr, dmarc)["dmarc_computed"] == "pass"
        nxt = next_dmarc_step(dmarc, dmarc_host, aligned_pass_observed=observed, subdomain=sub)
        findings += _issues_to_findings("dmarc", dmarc.issues, "identity", label, f"dig +short TXT _dmarc.{dmarc_host}", nxt)
        checked["dmarc"] = {"host": f"_dmarc.{dmarc_host}", **{k: v for k, v in asdict(dmarc).items() if k != "issues"},
                            "applies_as": "own record" if dmarc_host == from_domain else "organizational domain record (sp applies)",
                            "effective_policy": effective.policy, "effective_enforcing": effective.enforcing}
        if nxt:
            checked["dmarc_next_step"] = nxt

    # SPF at the envelope (Return-Path) domain: that is the domain SPF authenticates
    spf_domain = (hdr or {}).get("return_path_domain") or doc.get("envelope_domain") or from_domain
    if spf_domain:
        txts = (dns.get(spf_domain) or {}).get("txt")
        if txts is not None:
            spf_recs = [t for t in txts if t.lower().startswith("v=spf1")]
            if len(spf_recs) > 1:
                findings.append(Finding("spf.multiple", "critical", "identity", "More than one SPF record",
                                        f"{len(spf_recs)} TXT records at {spf_domain} start with v=spf1. RFC 7208 makes that a permerror: SPF fails everywhere.",
                                        label, f"dig +short TXT {spf_domain}"))
            if not spf_recs:
                findings.append(Finding("spf.missing", "high", "identity", f"No SPF record at {spf_domain}",
                                        "The envelope domain publishes no SPF record, so SPF can never pass for this traffic.", label, f"dig +short TXT {spf_domain}"))
            else:
                spf = parse_spf(spf_recs[0], doc.get("spf_includes"))
                findings += _issues_to_findings("spf", spf.issues, "identity", label, f"dig +short TXT {spf_domain}")
                checked["spf"] = {"domain": spf_domain, **{k: v for k, v in asdict(spf).items() if k != "issues"}}

    # the From domain's own SPF, when it differs from the envelope domain (it governs other mail sent as this domain)
    if from_domain and spf_domain and from_domain != spf_domain:
        txts = (dns.get(from_domain) or {}).get("txt")
        recs = [t for t in (txts or []) if t.lower().startswith("v=spf1")]
        if len(recs) > 1:
            findings.append(Finding("spf_from.multiple", "critical", "identity", "More than one SPF record on the From domain",
                                    f"{len(recs)} SPF records at {from_domain}: permerror for any mail whose envelope uses this domain.",
                                    label, f"dig +short TXT {from_domain}"))
        elif recs:
            s2 = parse_spf(recs[0], doc.get("spf_includes"))
            findings += _issues_to_findings("spf_from", [(sv, f"{from_domain} (mail with this envelope domain): {m}") for sv, m in s2.issues],
                                            "identity", label, f"dig +short TXT {from_domain}")
            checked["spf_from_domain"] = {"domain": from_domain, **{k: v for k, v in asdict(s2).items() if k != "issues"}}

    # DKIM keys supplied
    for host, rec in (doc.get("dkim_keys") or {}).items():
        k = parse_dkim_key(host, rec)
        findings += _issues_to_findings(f"dkim[{host}]", k.issues, "identity", label, f"dig +short TXT {host}")
        checked.setdefault("dkim_keys", []).append({k2: v for k2, v in asdict(k).items() if k2 != "issues"})

    # alignment from headers
    al = None
    unsub = None
    if hdr:
        al = evaluate_alignment(hdr, dmarc)
        checked["alignment"] = al
        if al["spf"]["result"] == "pass" and al["spf"]["aligned"] is False:
            findings.append(Finding("align.spf", "medium", "identity", "SPF passes but is not aligned",
                                    f"SPF authenticated {al['spf']['domain']}, which does not align with From domain {from_domain} "
                                    f"({'strict' if al['alignment_modes']['aspf'] == 's' else 'relaxed'} mode). SPF contributes nothing to DMARC here; "
                                    "DMARC rests entirely on DKIM. Use a custom return-path (bounce) subdomain on your own domain.", "SUPPLIED"))
        if al["dkim"] and not any(d["result"] == "pass" and d["aligned"] for d in al["dkim"]):
            signer = ", ".join(sorted({d["domain"] or "?" for d in al["dkim"]}))
            findings.append(Finding("align.dkim", "critical" if al["dmarc_computed"] == "fail" else "high", "identity",
                                    "No aligned passing DKIM signature",
                                    f"Signing domain(s): {signer}. From domain: {from_domain}. A signature from the ESP's shared domain passes DKIM but "
                                    "does not align, so it cannot carry DMARC and does not survive forwarding. Configure the ESP to sign with a selector on your domain.",
                                    "SUPPLIED"))
        if not al["dkim"]:
            findings.append(Finding("align.nodkim", "critical", "identity", "Message is not DKIM-signed",
                                    "No DKIM-Signature header and no dkim= result. Every bulk-sender requirement needs DKIM.", "SUPPLIED"))
        if al["dmarc_reported"] and al["dmarc_computed"] and al["dmarc_reported"] != al["dmarc_computed"]:
            findings.append(Finding("align.mismatch", "info", "identity", "Receiver's DMARC verdict differs from the recomputed one",
                                    f"Receiver reported dmarc={al['dmarc_reported']}; recomputed {al['dmarc_computed']}. Usually an organizational-domain "
                                    "or ARC difference; read the receiver's verdict as the fact.", "SUPPLIED"))
        unsub = one_click_unsubscribe(hdr)
        checked["one_click_unsubscribe"] = unsub
        if doc.get("message_class", "marketing") == "marketing" and unsub["rfc8058_compliant"] is None:
            findings.append(Finding("unsub.rfc8058_unverified", "medium", "content_infrastructure",
                                    "One-click unsubscribe headers are signed, but nothing shows the signature verifies",
                                    "A DKIM signature covers List-Unsubscribe and List-Unsubscribe-Post, but the headers carry no receiver "
                                    "dkim= verdict for its d= domain. RFC 8058 needs a valid signature; check a copy received at Gmail.",
                                    "SUPPLIED", "Show original in Gmail: the dkim= result for the signing domain"))
        elif doc.get("message_class", "marketing") == "marketing" and not unsub["rfc8058_compliant"]:
            missing = [n for n, ok in (("List-Unsubscribe with an https URI", unsub["https_uri"]),
                                       ("List-Unsubscribe-Post: List-Unsubscribe=One-Click", unsub["post_header_ok"]),
                                       ("DKIM h= covering both headers", unsub["dkim_covers_both"]),
                                       ("a passing DKIM signature covering them", unsub["covering_signature_verified"] or not unsub["dkim_covers_both"]))
                       if not ok]
            findings.append(Finding("unsub.rfc8058", "high", "content_infrastructure", "One-click unsubscribe is not RFC 8058 compliant",
                                    "Missing: " + "; ".join(missing) + ". Gmail and Yahoo require one-click unsubscribe on marketing mail from bulk senders.",
                                    "SUPPLIED"))

    # MTA-STS, TLS-RPT, BIMI on the organizational domain
    if org:
        od = dns.get(org) or {}
        mta = parse_mta_sts(od.get("mta_sts"), (doc.get("mta_sts_policies") or {}).get(org), od.get("mx"))
        if od.get("mta_sts") or (doc.get("mta_sts_policies") or {}).get(org):
            findings += _issues_to_findings("mtasts", mta["issues"], "content_infrastructure", label, f"curl -s https://mta-sts.{org}/.well-known/mta-sts.txt")
        elif "mta_sts" in od or od:
            findings.append(Finding("mtasts.missing", "low", "content_infrastructure", "No MTA-STS policy",
                                    "Inbound TLS is opportunistic and downgradeable. Publish a policy in testing mode, read TLS-RPT, then enforce.",
                                    label, f"dig +short TXT _mta-sts.{org}"))
        checked["mta_sts"] = {k: v for k, v in mta.items() if k != "issues"}
        tls = parse_tlsrpt(od.get("tlsrpt"))
        if od:
            findings += _issues_to_findings("tlsrpt", tls["issues"], "content_infrastructure", label, f"dig +short TXT _smtp._tls.{org}",
                                            dns_fix(f"_smtp._tls.{org}", f"v=TLSRPTv1; rua=mailto:tls-reports@{org}",
                                                    note=f"The mailbox tls-reports@{org} must exist.") if not tls["present"] else None)
        checked["tls_rpt"] = {k: v for k, v in tls.items() if k != "issues"}
        bimi = bimi_eligibility(od.get("bimi"), dmarc, effective)
        checked["bimi"] = bimi
        if od.get("bimi") and not bimi["eligible"]:
            findings.append(Finding("bimi.ineligible", "low", "content_infrastructure", "BIMI record published but not eligible",
                                    "; ".join(bimi["reasons"]) + ". Cosmetic only; never a remediation priority.", label, f"dig +short TXT default._bimi.{org}"))

    signals = dict(doc.get("signals") or {})
    if "auth_state" not in signals:
        signals["auth_state"] = _auth_state(dmarc, al, label, bool(dns), bool(hdr))
    gate = sending_gate(signals)
    for r in gate["signals"]:
        if r["status"] in ("RED", "AMBER") and r["signal"] not in ("authentication_state",):
            cause = {"complaint_rate": "consent", "hard_bounce_rate": "consent", "domain_reputation_bounces": "reputation",
                     "filtered_share": "consent", "unengaged_share": "consent"}.get(r["signal"], "reputation")
            findings.append(Finding(f"signal.{r['signal']}", "critical" if r["status"] == "RED" else "high", cause,
                                    f"{r['signal']} is {r['status']}", f"value {r['value']}; rule: {r['rule']}", "SUPPLIED"))
    bulk = bulk_sender_checks(al, dmarc, unsub, hdr, signals, doc.get("volume") or {})

    findings.sort(key=lambda f: (SEVERITY_RANK[f.severity], f.id))
    top = next((rank for rank in (0, 1) if any(SEVERITY_RANK[f.severity] == rank for f in findings)), None)
    primary, basis = None, None
    if top is not None:
        counts = {c: sum(1 for f in findings if SEVERITY_RANK[f.severity] == top and f.cause == c) for c in CAUSES}
        primary = max(CAUSES, key=lambda c: (counts[c], -CAUSES.index(c)))
        level = "critical" if top == 0 else "high"
        basis = (f"the cause class with the most {level} findings ({', '.join(f'{c.replace(chr(95), chr(32))} {n}' for c, n in counts.items() if n)}). "
                 "Ties go to identity, then reputation, consent, content and infrastructure. A triage starting point, not a diagnosis")
    return {
        "gate": gate,
        "primary_cause_class": primary,
        "primary_cause_basis": basis,
        "checked": checked,
        "bulk_sender": bulk,
        "findings": [asdict(f) for f in findings],
        "evidence_note": ("Records without observed_at are SUPPLIED: pasted, not observed. Each finding names the lookup that confirms it. "
                          "Header-derived findings describe one message; confirm across a sample before generalizing."),
    }


def _auth_state(dmarc: Optional[DMARCResult], al: Optional[Dict[str, Any]], label: str, has_dns: bool, has_hdr: bool) -> str:
    if not has_dns and not has_hdr:
        return "unknown"
    verdict = (al.get("dmarc_reported") if al and al.get("dmarc_reported") in ("pass", "fail") else None) or (al or {}).get("dmarc_computed")
    if al and verdict == "fail":
        return "failing"
    if dmarc is None and has_dns:
        return "failing"
    if al and verdict == "pass" and dmarc and dmarc.valid and label.startswith("LIVE PULL"):
        return "verified"
    return "partial"


# ----------------------------------------------------------------------------------------------
# rendering and CLI
# ----------------------------------------------------------------------------------------------

def render_markdown(r: Dict[str, Any]) -> str:
    g = r["gate"]
    L = [f"## Sending gate: {g['verdict']}", "", f"**Permitted:** {g['permitted']}", ""]
    if g["reasons"]:
        L += ["Why: " + "; ".join(g["reasons"]) + ".", ""]
    if r["primary_cause_class"]:
        L += [f"**Start triage with:** {r['primary_cause_class'].replace('_', ' ')}. Basis: {r['primary_cause_basis']}.", ""]
    L += ["| Signal | Value | Status |", "|---|---|---|"]
    L += [f"| {s['signal']} | {s['value'] if s['value'] is not None else 'not supplied'} | {s['status']} |" for s in g["signals"]]
    L += ["", "## Findings", ""]
    for f in r["findings"]:
        L.append(f"- **{f['severity'].upper()}** [{f['cause'].replace('_', ' ')}] {f['detail']} ({f['label']}{'; verify: `' + f['verify_with'] + '`' if f['verify_with'] else ''})")
        if f.get("fix"):
            fx = f["fix"]
            L.append(f"  - Record: `{fx['host']}` {fx['type']} TTL {fx['ttl']}: `{fx['value']}`" + (f" ({fx['note']})" if fx.get("note") else ""))
    L += ["", "## Bulk-sender requirements", "", "| Requirement | Applies to | Status |", "|---|---|---|"]
    L += [f"| {q['requirement']} | {q['applies_to']} | {q['status']} |" for q in r["bulk_sender"]["requirements"]]
    L += ["", r["bulk_sender"]["note"], "", f"_{r['evidence_note']}_"]
    return "\n".join(L) + "\n"


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze")
    a.add_argument("input")
    a.add_argument("--markdown", action="store_true")
    for name in ("spf", "dmarc"):
        p = sub.add_parser(name)
        p.add_argument("record")
    d = sub.add_parser("dkim")
    d.add_argument("host")
    d.add_argument("record")
    hp = sub.add_parser("headers")
    hp.add_argument("path")
    gp = sub.add_parser("gate")
    gp.add_argument("signals_json")
    args = ap.parse_args(argv)
    if args.cmd == "analyze":
        doc = json.loads(Path(args.input).read_text(encoding="utf-8"))
        if isinstance(doc.get("headers"), dict) and doc["headers"].get("path"):
            doc["headers"]["path"] = str((Path(args.input).parent / doc["headers"]["path"]).resolve())
        try:
            from tools.contracts import InputError, validate_deliverability
        except ImportError:  # run as a script from tools/
            from contracts import InputError, validate_deliverability  # type: ignore
        try:
            doc = validate_deliverability(doc)
        except InputError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        r = analyze(doc)
        print(render_markdown(r) if args.markdown else json.dumps(r, indent=2))
    elif args.cmd == "spf":
        print(json.dumps(asdict(parse_spf(args.record)), indent=2))
    elif args.cmd == "dmarc":
        print(json.dumps(asdict(parse_dmarc(args.record)), indent=2))
    elif args.cmd == "dkim":
        print(json.dumps(asdict(parse_dkim_key(args.host, args.record)), indent=2))
    elif args.cmd == "headers":
        h = parse_headers(Path(args.path).read_text(encoding="utf-8"))
        print(json.dumps({"headers": h, "alignment": evaluate_alignment(h, None), "one_click_unsubscribe": one_click_unsubscribe(h)}, indent=2))
    elif args.cmd == "gate":
        print(json.dumps(sending_gate(json.loads(Path(args.signals_json).read_text(encoding="utf-8"))), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
