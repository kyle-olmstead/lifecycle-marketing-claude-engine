"""Compliance: checks the finished plan against the marketing rules of every jurisdiction in its audience.

The rules live in config/compliance_rules.json, each with the requirement in plain words and its
source. This module decides which jurisdictions apply (from the share of qualified people by country
when the Analyst had data, else from the brief's recipient_countries, else the United States with a
warning), then evaluates every rule against the plan itself: the rendered templates, the SMS send hour
and frequency, the consent each step requires, and the suppression rules. It reads what will actually
be sent, not what the copywriter intended, so it is an independent second check after QA.

It is a rules engine, not legal advice. A rule it cannot verify from the plan (the type of consent a
Canadian record carries, say) comes back as warn, with what to confirm.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from engine.models import Brief, CohortDefinition, ComplianceFinding, ComplianceReport, FlowPlan, TouchCopy
from engine.render.sms import has_opt_out

RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "compliance_rules.json"
EU = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT",
      "RO", "SK", "SI", "ES", "SE"}
MIN_SHARE = 0.005   # a country under half a percent of the audience still counts if it is named in the brief


def load_rules() -> Dict[str, Any]:
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))


def jurisdictions(brief: Brief, cohort: CohortDefinition) -> Tuple[List[str], Optional[str]]:
    """Jurisdictions in the audience, and a note when they had to be assumed."""
    shares = {str(c).upper(): s for c, s in (cohort.countries or {}).items()}
    unknown = sum(s for c, s in shares.items() if c in ("UNKNOWN", "", "NONE", "NULL"))
    countries = {c for c, s in shares.items() if s >= MIN_SHARE and c not in ("UNKNOWN", "", "NONE", "NULL")}
    countries |= {c.upper() for c in (brief.recipient_countries or [])}
    note = None
    if not countries:
        countries = {"US"}
        note = ("recipient geography unknown" + (f" ({unknown:.0%} of people have no country on record)" if unknown else "") +
                ": assumed United States only. Add recipient_countries to the brief, or fill users.country")
    elif unknown >= 0.05:
        countries.add("US")
        note = (f"{unknown:.0%} of people have no country on record: United States rules are applied to them as well. "
                f"Fill users.country, or list every country in recipient_countries")
    out = set()
    for c in countries:
        if c in ("US", "CA", "AU"):
            out.add(c)
        elif c in ("GB", "UK"):
            out.add("UK")
        elif c in EU:
            out.add("EU")
            if c == "DE":
                out.add("DE")
        elif c != "UNKNOWN":
            out.add("OTHER")
    return sorted(out), note


# ----------------------------------------------------------------------------------------------
# evidence from the plan
# ----------------------------------------------------------------------------------------------

def _emails(copy_plan: List[TouchCopy]) -> List[Tuple[str, str]]:
    return [(f"{tc.step_id}/{name}", text) for tc in copy_plan if tc.channel == "email" for v in tc.variants
            for name, text in v.rendered.items() if name.endswith(".html")]


def _sms(copy_plan: List[TouchCopy]) -> List[Tuple[str, str]]:
    out = []
    for tc in copy_plan:
        if tc.channel != "sms":
            continue
        for v in tc.variants:
            for name, text in v.rendered.items():
                try:
                    out.append((f"{tc.step_id}/{name}", json.loads(text)["preview"]))
                except (ValueError, KeyError):
                    continue
    return out


ADDRESS = re.compile(r"organization\.(full_address|street_address)|site_settings\.company_street_address_1|%%member_addr%%|"
                     r"\$\{email_footer\}|\{\{\{?~?\s*physicalAddress\s*~?\}?\}\}|\d+\s+\w+.*\b(St|Street|Ave|Avenue|Rd|Road|Blvd|Suite|Way|Dr|Drive|Lane|Ln)\b", re.I)
UNSUB = re.compile(r"unsubscribe", re.I)


def _sms_steps(flow: FlowPlan) -> List[Dict[str, Any]]:
    return [s for s in flow.flow_spec["steps"] if s.get("channel") == "sms"]


def _max_sms_per_24h(flow: FlowPlan) -> int:
    days = [t.day for t in flow.touches if t.channel == "sms"]
    return max((sum(1 for d in days if a <= d < a + 1) for a in days), default=0)


# ----------------------------------------------------------------------------------------------
# checks: each returns (status, detail)
# ----------------------------------------------------------------------------------------------

Check = Callable[[Dict[str, Any], Brief, CohortDefinition, FlowPlan, List[TouchCopy]], Tuple[str, str]]


def _postal_address(rule, brief, cohort, flow, copy_plan):
    emails = _emails(copy_plan)
    missing = [n for n, t in emails if not ADDRESS.search(t) or "POSTAL ADDRESS REQUIRED" in t]
    if not emails:
        return "pass", "no email in the flow"
    return ("fail", f"no postal address in {len(missing)} of {len(emails)} emails ({missing[0]}): add postal_address to the brief") if missing \
        else ("pass", f"a postal address or the platform's address token is in all {len(emails)} emails")


def _unsubscribe(rule, brief, cohort, flow, copy_plan):
    emails = _emails(copy_plan)
    missing = [n for n, t in emails if not UNSUB.search(t)]
    days = rule.get("honor_within")
    tail = f" Honoring requests within {days} is a process requirement on your platform: confirm it." if days else ""
    if missing:
        return "fail", f"no unsubscribe link in {missing[0]}"
    return "pass", f"every email carries the platform's unsubscribe link.{tail}"


def _sender_identified(rule, brief, cohort, flow, copy_plan):
    sms = _sms(copy_plan)
    bad = [n for n, t in sms if not t.lower().startswith(brief.product_name.lower())]
    if bad:
        return "fail", f"SMS {bad[0]} does not name the sender first"
    return "pass", "every email footer names the sender and every SMS starts with the brand name"


def _sms_hours(rule, brief, cohort, flow, copy_plan):
    if not _sms_steps(flow):
        return "pass", "no SMS in the flow"
    start, end = rule["allowed_hours"]
    h = brief.send_hour_local
    if start <= h < end:
        sunday = rule.get("sunday_start")
        if sunday is not None and h < sunday:
            return "warn", (f"SMS send at {h:02d}:00 is inside {start:02d}:00 to {end:02d}:00, but on Sundays the window opens at {sunday:02d}:00; "
                            f"set send_hour_local to {sunday:02d} or later, or have the ESP skip Sundays")
        return "pass", f"SMS send at {h:02d}:00 recipient local is inside {start:02d}:00 to {end:02d}:00"
    return "fail", f"SMS send at {h:02d}:00 recipient local is outside {start:02d}:00 to {end:02d}:00; set send_hour_local inside it"


def _sms_frequency(rule, brief, cohort, flow, copy_plan):
    if not _sms_steps(flow):
        return "pass", "no SMS in the flow"
    limit = rule["max_per_24h"]
    n = _max_sms_per_24h(flow)
    return ("pass", f"at most {n} SMS in any 24 hours (limit {limit})") if n <= limit else \
        ("fail", f"{n} SMS within 24 hours; the limit is {limit}")


def _sms_consent(rule, brief, cohort, flow, copy_plan):
    steps = _sms_steps(flow)
    if not steps:
        return "pass", "no SMS in the flow"
    missing = [s["id"] for s in steps if "sms_consent" not in (s.get("requires") or [])]
    if missing:
        return "fail", f"SMS step {missing[0]} does not require SMS consent"
    sms = _sms(copy_plan)
    no_stop = [n for n, t in sms if not has_opt_out(t)]
    if no_stop:
        return "fail", f"{no_stop[0]} has no STOP opt-out instruction"
    return "pass", "every SMS step requires recorded SMS consent and every SMS carries STOP opt-out language"


def _email_consent(rule, brief, cohort, flow, copy_plan):
    steps = [s for s in flow.flow_spec["steps"] if s.get("channel") == "email"]
    missing = [s["id"] for s in steps if "email_consent" not in (s.get("requires") or [])]
    if missing:
        return "fail", f"email step {missing[0]} does not require email consent"
    dep = next((d for d in cohort.data_dependencies if d.name == "email_consent"), None)
    fill = f"; {dep.fill_rate:.0%} of records carry a consent timestamp" if dep and dep.fill_rate is not None else ""
    return "warn", (f"every email requires a consent record{fill}. {rule['confirm']}")


def _confirm(rule, brief, cohort, flow, copy_plan):
    return "warn", rule["confirm"]


def _no_deception(rule, brief, cohort, flow, copy_plan):
    bad = [f"{tc.step_id}/{v.variant_id}" for tc in copy_plan for v in tc.variants
           if re.match(r"^\s*(re|fw|fwd)\s*:", str(v.fields.get("subject", "")), re.I)]
    return ("fail", f"subject implies a prior conversation in {bad[0]}") if bad else \
        ("pass", "no subject implies a prior conversation; the guardrails also block fake urgency and offers outside policy")


def _suppression(rule, brief, cohort, flow, copy_plan):
    sup = " ".join(flow.flow_spec.get("suppressions", [])).lower()
    if flow.flow_spec.get("message_class") == "transactional":
        missing = [w for w in ("bounce", "spam") if w not in sup]
        return (("fail", f"the flow does not suppress: {', '.join(missing)}") if missing else
                ("pass", "billing notices are transactional: hard bounces and complaints are suppressed; a marketing unsubscribe does not "
                         "stop them, so they carry no marketing content"))
    needed = [w for w in ("unsubscribed", "bounce", "spam") if w not in sup]
    if needed:
        return "fail", f"the flow does not suppress: {', '.join(needed)}"
    return "pass", "unsubscribes, hard bounces and spam complaints are suppressed in every step"


CHECKS: Dict[str, Check] = {"postal_address": _postal_address, "unsubscribe": _unsubscribe, "sender_identified": _sender_identified,
                            "sms_hours": _sms_hours, "sms_frequency": _sms_frequency, "sms_consent": _sms_consent,
                            "email_consent": _email_consent, "confirm": _confirm, "no_deception": _no_deception,
                            "suppression": _suppression}


def review(brief: Brief, cohort: CohortDefinition, flow: FlowPlan, copy_plan: List[TouchCopy]) -> ComplianceReport:
    config = load_rules()
    juris, note = jurisdictions(brief, cohort)
    findings: List[ComplianceFinding] = []
    if note:
        findings.append(ComplianceFinding(rule="recipient geography", jurisdiction="all", status="warn", detail=note))
    active = set(juris) | {"ALL"}
    channels = {t.channel for t in flow.touches}
    for rule in config["rules"]:
        if rule["jurisdiction"] not in active:
            continue
        if rule.get("channel") and rule["channel"] not in channels:
            continue
        status, detail = CHECKS[rule["check"]](rule, brief, cohort, flow, copy_plan)
        detail = detail.strip()
        if detail and detail[-1] not in ".:;":
            detail += "."
        findings.append(ComplianceFinding(rule=rule["id"], jurisdiction=config["jurisdictions"][rule["jurisdiction"]], status=status,
                                          detail=f"{rule['requirement']} {detail[:1].upper() + detail[1:]}".strip(), source=rule.get("source")))
    if "OTHER" in juris:
        others = sorted(c for c, s in (cohort.countries or {}).items() if s >= MIN_SHARE and c not in {"US", "CA", "AU", "GB", "UK", "UNKNOWN"} | EU)
        findings.append(ComplianceFinding(rule="uncovered jurisdictions", jurisdiction="other", status="warn",
                                          detail=f"no rules loaded for {', '.join(others) or 'some recipients'}; check local law before sending"))
    status = "fail" if any(f.status == "fail" for f in findings) else ("warn" if any(f.status == "warn" for f in findings) else "pass")
    return ComplianceReport(status=status, jurisdictions=[config["jurisdictions"][j] for j in juris if j != "OTHER"] + (["other"] if "OTHER" in juris else []),
                            findings=findings)
