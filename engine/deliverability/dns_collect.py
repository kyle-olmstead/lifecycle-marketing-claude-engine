"""Look up a sending domain's authentication records and build the deliverability tool's input.

engine/deliverability/analyze.py analyzes records; it never touches the network, so its results are
reproducible. This collector is the opt-in step in front of it: it resolves everything the
analysis reads, stamps the date (observed_at, so findings are labeled LIVE PULL), and writes an
input file that passes engine/contracts.py. Run it by hand, or daily from
.github/workflows/daily_cohort_audit.yml, and a broken record shows up the morning after it breaks.

What it looks up, for the From domain (and the envelope domain, when they differ):
  TXT at the domain (SPF), every include: and redirect= it reaches (for the 10-lookup count), MX,
  _dmarc (falling back to the organizational domain), _mta-sts and the HTTPS policy it points to,
  _smtp._tls (TLS-RPT), default._bimi, and selector._domainkey for each DKIM selector given or each
  fixed selector the common senders publish (see SELECTOR_SOURCES). Senders whose selectors are random
  or account-specific (Amazon SES, Postmark, Customer.io, SparkPost) need --selectors.

    python3 engine/deliverability/dns_collect.py example.com --selectors s1,s2 --out out/deliverability/input.json
    python3 engine/deliverability/dns_collect.py example.com --envelope em.example.com --analyze --markdown
    python3 engine/deliverability/dns_collect.py example.com --signals out/engagement/summary.json --analyze

Needs dnspython (requirements.txt). Uses the system resolver unless --nameserver is given.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Fixed selectors the common senders publish, read from each one's setup documentation in October 2026 (links in
# docs/architectural_overview.md). Used only when the caller gives none; a miss is not an error, since a domain publishes
# only the ones its senders use. Not probeable, so pass them with --selectors: Amazon SES Easy DKIM (random tokens),
# Postmark (<digits>pm), Braze on SparkPost (scph<digits>), Customer.io (cio under a cio<digits> subdomain), Klaviyo in
# dynamic mode (the subdomain is delegated), Salesforce Marketing Cloud (delegated). HubSpot's current selectors carry the
# hub id: --hubspot-hub-id adds hs1-<id> and hs2-<id>.
SELECTOR_SOURCES: Dict[str, Tuple[str, ...]] = {
    "Google Workspace": ("google",),
    "Microsoft 365": ("selector1", "selector2"),
    "Klaviyo (static CNAMEs)": ("km1", "km2", "kt1", "kt2", "ks1", "ks2", "kl1", "kl2"),
    "HubSpot (older portals)": ("hs1", "hs2"),
    "SendGrid, and Braze on SendGrid": ("s1", "s2"),
    "Mailchimp": ("k2", "k3"),
    "Mandrill": ("mte1", "mte2"),
    "Mailgun (automatic)": ("pdk1", "pdk2"),
    "self-hosted OpenDKIM default": ("default",),
}
DEFAULT_SELECTORS: Tuple[str, ...] = tuple(dict.fromkeys(s for group in SELECTOR_SOURCES.values() for s in group))
MAX_SPF_LOOKUPS = 10
Resolver = Callable[[str, str], List[str]]   # (name, rdtype) to the answers as text; [] when none


def system_resolver(nameserver: Optional[str] = None, timeout: float = 5.0) -> Resolver:
    import dns.exception
    import dns.resolver
    r = dns.resolver.Resolver()
    if nameserver:
        r.nameservers = [nameserver]
    r.lifetime = timeout

    def resolve(name: str, rdtype: str) -> List[str]:
        try:
            answer = r.resolve(name, rdtype)
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return []
        except dns.resolver.NoNameservers as e:
            raise LookupError(f"no nameserver answered for {rdtype} {name}: this machine's DNS may not be working") from e
        except dns.exception.Timeout as e:
            raise TimeoutError(f"DNS lookup timed out: {rdtype} {name}") from e
        if rdtype == "TXT":
            return [b"".join(rd.strings).decode("utf-8", errors="replace") for rd in answer]
        if rdtype == "MX":
            return [str(rd.exchange).rstrip(".") for rd in sorted(answer, key=lambda x: x.preference)]
        return [rd.to_text() for rd in answer]

    return resolve


def https_fetch(url: str, timeout: float = 5.0) -> Optional[str]:
    import httpx
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=False)
    except httpx.HTTPError:
        return None
    return resp.text if resp.status_code == 200 else None


def _one(records: Iterable[str], prefix: str) -> Optional[Any]:
    """The record that starts with prefix; a list when several do, which the analysis reports, since then none applies."""
    found = [t for t in records if t.lower().replace(" ", "").startswith(prefix)]
    return None if not found else (found[0] if len(found) == 1 else found)


def _spf_targets(record: str) -> List[str]:
    out = []
    for term in record.split()[1:]:
        m = re.match(r"^[+?~-]?(include):(.+)$", term, re.I) or re.match(r"^(redirect)=(.+)$", term, re.I)
        if m and "%{" not in m.group(2):
            out.append(m.group(2).lower().rstrip("."))
    return out


def collect(domain: str, selectors: Optional[Iterable[str]] = None, envelope_domain: Optional[str] = None,
            resolve: Optional[Resolver] = None, fetch: Callable[[str], Optional[str]] = https_fetch,
            today: Optional[str] = None) -> Dict[str, Any]:
    """Resolve every record the analysis reads. Returns a deliverability input document."""
    from engine.deliverability.analyze import organizational_domain
    resolve = resolve or system_resolver()
    domain = domain.lower().rstrip(".")
    doc: Dict[str, Any] = {"observed_at": today or datetime.now(timezone.utc).date().isoformat(), "from_domain": domain,
                           "message_class": "marketing", "dns": {}, "spf_includes": {}, "dkim_keys": {}, "mta_sts_policies": {}}
    if envelope_domain and envelope_domain.lower() != domain:
        doc["envelope_domain"] = envelope_domain.lower().rstrip(".")
    lookups: List[str] = []

    def txt(name: str) -> List[str]:
        lookups.append(name)
        return resolve(name, "TXT")

    org = organizational_domain(domain) or domain
    if not resolve(org, "SOA") and not resolve(org, "NS"):
        # every sending domain's registrable domain has a zone; no SOA or NS means a typo, or no working DNS here,
        # and reporting "no DMARC record" in either case would be wrong
        raise LookupError(f"{org} does not resolve (no SOA or NS record): check the spelling, or run where DNS works")
    for d in dict.fromkeys([domain, doc.get("envelope_domain"), org]):
        if not d:
            continue
        rec: Dict[str, Any] = {"txt": txt(d)}
        dm = _one(txt(f"_dmarc.{d}"), "v=dmarc1")
        if dm:
            rec["dmarc"] = dm
        if d in (domain, org):
            rec["mx"] = resolve(d, "MX")
            sts = _one(txt(f"_mta-sts.{d}"), "v=stsv1")
            if sts:
                rec["mta_sts"] = sts
            if isinstance(sts, str):
                policy = fetch(f"https://mta-sts.{d}/.well-known/mta-sts.txt")
                if policy:
                    doc["mta_sts_policies"][d] = policy
            tls = _one(txt(f"_smtp._tls.{d}"), "v=tlsrptv1")
            if tls:
                rec["tlsrpt"] = tls
            bimi = _one(txt(f"default._bimi.{d}"), "v=bimi1")
            if bimi:
                rec["bimi"] = bimi
        doc["dns"][d] = rec

    # SPF includes, breadth first, so the analysis can count DNS lookups the way receivers do
    queue = []
    for d in (doc.get("envelope_domain"), domain):
        spf = _one((doc["dns"].get(d) or {}).get("txt") or [], "v=spf1") if d else None
        for record in ([spf] if isinstance(spf, str) else spf or []):
            queue += _spf_targets(record)
    seen = set()
    while queue and len(seen) < 3 * MAX_SPF_LOOKUPS:
        target = queue.pop(0)
        if target in seen:
            continue
        seen.add(target)
        spf = _one(txt(target), "v=spf1")
        if isinstance(spf, list):
            spf = spf[0]   # an include with two SPF records is a permerror at the receiver; the analysis sees only the first
        if spf:
            doc["spf_includes"][target] = spf
            queue += _spf_targets(spf)

    for sel in dict.fromkeys(s.strip() for s in (selectors or DEFAULT_SELECTORS) if s.strip()):
        host = f"{sel}._domainkey.{domain}"
        key = next((t for t in txt(host) if "p=" in t.replace(" ", "")), None)
        if key:
            doc["dkim_keys"][host] = key
    doc["_lookups"] = len(lookups)
    return doc


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Look up a domain's authentication records for engine/deliverability/analyze.py")
    ap.add_argument("domain")
    ap.add_argument("--selectors", help="comma-separated DKIM selectors (default: the common ESP selectors)")
    ap.add_argument("--hubspot-hub-id", help="also probe HubSpot's hs1-<hub id> and hs2-<hub id> selectors")
    ap.add_argument("--envelope", help="the Return-Path (bounce) domain, when it differs from the From domain")
    ap.add_argument("--signals", help="JSON with sending-gate signals to merge in (for example out/engagement/summary.json)")
    ap.add_argument("--nameserver")
    ap.add_argument("--out", help="write the input document here")
    ap.add_argument("--analyze", action="store_true", help="run engine/deliverability/analyze.py analyze on the result")
    ap.add_argument("--markdown", action="store_true")
    a = ap.parse_args(argv)
    try:
        selectors = a.selectors.split(",") if a.selectors else list(DEFAULT_SELECTORS)
        if a.hubspot_hub_id:
            selectors += [f"hs1-{a.hubspot_hub_id}", f"hs2-{a.hubspot_hub_id}"]
        doc = collect(a.domain, selectors, a.envelope, system_resolver(a.nameserver))
    except (TimeoutError, LookupError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    lookups = doc.pop("_lookups")
    if a.signals:
        raw = json.loads(Path(a.signals).read_text(encoding="utf-8"))
        sig = raw.get("signals", raw)
        if "unengaged_share" in raw and "signals" not in raw:   # an engagement summary
            sig = {"unengaged_share": raw["unengaged_share"]}
        doc["signals"] = {k: v for k, v in sig.items() if v is not None}
    from engine.contracts import validate_deliverability
    doc = validate_deliverability(doc)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(doc, indent=2), encoding="utf-8")
        print(f"wrote {a.out} ({lookups} lookups, {len(doc['dkim_keys'])} DKIM key(s) found)", file=sys.stderr)
    if a.analyze:
        from engine.deliverability import analyze as deliverability
        r = deliverability.analyze(doc)
        print(deliverability.render_markdown(r) if a.markdown else json.dumps(r, indent=2, default=str))
    elif not a.out:
        print(json.dumps(doc, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
