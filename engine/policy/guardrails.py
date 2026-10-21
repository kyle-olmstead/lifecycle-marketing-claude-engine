"""Deliverability and list-health guardrails every teammate plan passes through.

Three layers, each deterministic:

1. The sending gate decides who may be mailed at all. GREEN: every consented entrant. AMBER, or
   no gate on record: engaged recipients only (a click or reply in 90 days; opens are inflated by
   privacy proxies and do not count). RED: no marketing sends; the plan carries suppression and
   remediation only.
2. Mandatory suppressions and re-engagement rules are written into every flow spec, so list
   degradation cannot be designed in by omission.
3. Content checks run on every rendered message: an unsubscribe token in the platform dialect
   (engine/render/template_lint.py), opt-out language in SMS (engine/render/sms.py), honest subjects, no offer the
   offer policy does not allow, no urgency without a real deadline, and a text-to-image check.

On text-to-image: Gmail, Microsoft and Yahoo do not publish a ratio. What reliably hurts placement
is mail that is all or mostly image, because a filter cannot read it and a recipient with images
off sees nothing. The check therefore measures readable text against image count and fails
image-only mail, rather than enforcing a folklore 60/40 split.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from engine.render import sms as sms_tool

ENGAGED_RULE = "clicked or replied to any email in the last 90 days (opens do not count: privacy proxies inflate them)"
NEW_ENTRANT_RULE = ("signed up or started a trial in the last 30 days (a fresh opt-in), or clicked or replied to an email in the last 90 days; "
                    "opens do not count")
BEHAVIOR_RULE = ("the trigger is the person's own activity in the last day (a checkout, a product view, an order), which counts as "
                 "engagement; anyone with no click, reply, order or session in 365 days is still excluded")


def _gate_word(gate: Any) -> str:
    from engine.policy.contracts import effective_gate
    return effective_gate(gate)[0]


def gate_policy(gate: Any, flow_type: str = "winback") -> Dict[str, Any]:
    """Who the flow may mail under the sending gate. The gate may be a word or a SendingGateRecord; a bare GREEN reads AMBER
    (engine/policy/contracts.effective_gate). On AMBER, the engaged-only rule depends on who the flow is for: a welcome or
    trial flow mails fresh opt-ins (v2 applied the 90-day click rule to them, so a welcome flow enrolled nobody); a cart,
    browse or post-purchase flow is triggered by the person's own activity; a win-back keeps the strict 90-day click rule;
    billing notices are transactional and pass on every gate but RED's identity failures, where they go from the
    transactional stream."""
    from engine.flows.library import FLOWS
    from engine.policy.contracts import effective_gate
    word, why = effective_gate(gate)
    supplied = gate.verdict if hasattr(gate, "verdict") else str(gate or "UNKNOWN").upper()
    fd = FLOWS.get(flow_type)
    klass = fd.gate_class if fd else "lapsed"
    if klass == "transactional":
        allowed = True
        note = (f"{word} gate ({why}). Billing notices are transactional: they send on every gate, from the transactional stream "
                f"(its own subdomain) when the marketing gate is not GREEN")
        return {"gate": supplied, "effective_gate": word, "sending_allowed": allowed, "entry_filters": [],
                "audience": "every entrant with a deliverable address", "note": note}
    if word == "GREEN":
        return {"gate": supplied, "effective_gate": "GREEN", "sending_allowed": True, "entry_filters": [],
                "audience": "every entrant with consent for the channel", "note": f"GREEN gate: normal sending within the frequency cap ({why})"}
    if word == "RED":
        if klass == "remediation":
            return {"gate": supplied, "effective_gate": "RED", "sending_allowed": True, "entry_filters": [],
                    "audience": "unengaged recipients, in small daily batches from a separate subdomain",
                    "note": ("RED gate: bulk sending is stopped; re-permission is the one send the method permits. Send it from a "
                             "separate subdomain in small daily batches, and stop at the first complaint spike")}
        return {"gate": supplied, "effective_gate": "RED", "sending_allowed": False, "entry_filters": [],
                "audience": "nobody: marketing sends are stopped",
                "note": "RED gate: stop bulk sending. Fix identity and reputation first (lcm deliverability), then re-plan. "
                        "This plan contains the audience, suppression and measurement design only"}
    rule = {"new_entrant": NEW_ENTRANT_RULE, "behavior": BEHAVIOR_RULE, "lapsed": ENGAGED_RULE, "remediation": None}.get(klass, ENGAGED_RULE)
    audience = {"new_entrant": "new opt-ins, plus anyone engaged in the last 90 days", "behavior": "people active in the last day",
                "lapsed": "engaged recipients only", "remediation": "unengaged recipients, in small daily batches"}.get(klass)
    return {"gate": supplied, "effective_gate": "AMBER", "sending_allowed": True, "entry_filters": [rule] if rule else [],
            "audience": audience, "note": f"AMBER gate: {audience}, no volume growth ({why})"}


def mandatory_suppressions(flow_type: str) -> List[str]:
    from engine.flows.library import FLOWS
    fd = FLOWS.get(flow_type)
    if fd is not None and fd.message_class == "transactional":
        return ["hard bounced (the address does not exist)", "marked a message as spam (complaint): send only the legally required "
                "notice, from the transactional stream", "internal, test and seed addresses",
                "unsubscribed from marketing: does NOT suppress billing notices, so keep them free of marketing content"]
    rules = [
        "unsubscribed from marketing email, or hard bounced",
        "marked any message as spam (complaint), ever",
        "sunset-suppressed (lcm_sunset is true); only a fresh opt-in lifts it",
        "internal, test and seed addresses",
        "currently in a higher-priority flow (by the frequency policy's flow_priority: billing, cart, post-purchase first)",
    ]
    if flow_type in ("winback", "sunset"):
        rules.append("received a re-permission ask from any flow in the last 180 days")
    return rules


RE_ENGAGEMENT_RULES = [
    "One re-permission ask per person per 180 days, across all flows.",
    "No click on the re-permission ask within 14 days: set lcm_sunset = true and stop marketing mail. Keep the record; never delete it.",
    "A sunset-suppressed person re-enters only through an explicit opt-in action, never through a flow.",
    "Hard bounces are never mailed again; complaints are suppressed permanently.",
    "Unsubscribes, including one-click (RFC 8058), are honored within 2 days on every platform the person is on.",
    "On an AMBER gate, win-back entrants are limited to people who clicked or replied in the last 90 days.",
]

DECEPTIVE_PREFIX = re.compile(r"^\s*(re|fw|fwd)\s*:", re.I)
URGENCY = re.compile(r"\b(last chance|final hours?|ends (tonight|today|soon)|expires?|expiring|only today|hurry|act now|don't miss out|"
                     r"limited time|before it's gone|24 hours left)\b", re.I)
OFFER = re.compile(r"(\b\d{1,2}\s*% off\b|\bdiscount\b|\bcoupon\b|\bpromo code\b|\bfree (shipping|month|trial|upgrade)\b|\bsale\b|\bsave \$?\d)", re.I)
SPAMMY = re.compile(r"\b(100% free|risk[- ]free|guaranteed|click here|winner|cash bonus|no cost|act now|once in a lifetime)\b", re.I)
ALLOWED_TOKENS = {"first_name", "product_name"}
PRODUCT_TOKENS = {"item_name", "product_block"}   # flows with a product block (cart, browse, post-purchase, replenishment, welcome)
TOKEN = re.compile(r"\{([a-z_]+)\}")


def _policy(offer_policy: Any):
    from engine.policy.contracts import OfferPolicy, parse_offer_text
    return offer_policy if isinstance(offer_policy, OfferPolicy) else parse_offer_text(str(offer_policy or ""))[0]


def _policy_allows_offers(policy: Any) -> bool:
    """Kept for callers of v2; reads the typed policy (a typed OfferPolicy, or text parsed once into one)."""
    return _policy(policy).allows_offers


def _policy_has_deadline(policy: Any) -> bool:
    return _policy(policy).has_deadline


def check_text(text: str, offer_policy: Any, where: str, tokens: Optional[set] = None, offer_allowed_here: bool = True) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    allowed = ALLOWED_TOKENS | (tokens or set())
    policy = _policy(offer_policy)
    shown = policy.describe() if not policy.text else policy.text
    for tok in TOKEN.findall(text or ""):
        if tok not in allowed:
            errors.append(f"{where}: unknown personalization token {{{tok}}}; only {sorted(allowed)} are verified fields")
    if OFFER.search(text or "") and not policy.allows_offers:
        errors.append(f"{where}: mentions an offer ('{OFFER.search(text).group(0)}') but the offer policy allows none ('{shown}')")
    elif OFFER.search(text or "") and not offer_allowed_here:
        errors.append(f"{where}: mentions an offer ('{OFFER.search(text).group(0)}') in a touch the policy keeps offer-free (never the first touch)")
    if URGENCY.search(text or "") and not policy.has_deadline:
        errors.append(f"{where}: urgency ('{URGENCY.search(text).group(0)}') with no real deadline in the offer policy: fake urgency")
    if SPAMMY.search(text or ""):
        warnings.append(f"{where}: '{SPAMMY.search(text).group(0)}' reads as promotional boilerplate; say the specific thing instead")
    return errors, warnings


def check_email_fields(fields: Dict[str, Any], offer_policy: Any, tokens: Optional[set] = None,
                       offer_allowed_here: bool = True) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    subject = (fields.get("subject") or "").strip()
    pre = (fields.get("preheader") or "").strip()
    if not subject:
        errors.append("subject is empty")
    if DECEPTIVE_PREFIX.match(subject):
        errors.append("subject starts with RE: or FW:, implying a conversation that did not happen (deceptive under CAN-SPAM)")
    if len(subject) > 60:
        warnings.append(f"subject is {len(subject)} characters; mobile clients show about the first 35 to 40")
    if subject.count("!") > 1 or re.search(r"\b[A-Z]{4,}\b", subject):
        warnings.append("subject uses repeated exclamation marks or all-caps words")
    if not pre:
        warnings.append("no preview text: the inbox will show the first body line instead")
    elif pre.lower() == subject.lower():
        warnings.append("preview text repeats the subject; make it a second, distinct promise")
    if not fields.get("paragraphs"):
        errors.append("body has no paragraphs")
    if not (fields.get("cta_label") or "").strip():
        errors.append("no call to action label")
    text = " ".join([subject, pre, fields.get("headline") or "", *(fields.get("paragraphs") or []), fields.get("cta_label") or ""])
    e, w = check_text(text, offer_policy, "email", tokens, offer_allowed_here)
    return errors + e, warnings + w


def text_to_image(html_src: str) -> Dict[str, Any]:
    """Readable text against images; implemented in engine/render/template_lint.py so every linted email gets it."""
    from engine.render.template_lint import text_to_image as _tti
    return _tti(html_src)


def check_sms(text: str, brand: str, first_message: bool) -> Tuple[List[str], List[str], Dict[str, Any]]:
    r = sms_tool.analyze(text, brand=brand, first_message=first_message)
    errors: List[str] = []
    warnings: List[str] = list(r["checks"])
    if r["segments"] > 2:
        errors.append(f"SMS is {r['segments']} segments ({r['encoding']}); keep lifecycle SMS to one or two")
    elif r["segments"] == 2:
        warnings.append("SMS is 2 segments; one is cheaper and reads better")
    if r["non_gsm_characters"]:
        warnings.append("non-GSM characters force UCS-2: " + ", ".join(c["codepoint"] for c in r["non_gsm_characters"]))
    if any("opt-out" in c for c in r["checks"]):
        errors.append("no opt-out language in the SMS")
        warnings = [w for w in warnings if "opt-out" not in w]
    return errors, warnings, r


def check_short(fields: Dict[str, Any], limits: Dict[str, int], offer_policy: Any, where: str, tokens: Optional[set] = None,
                offer_allowed_here: bool = True) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    for k, limit in limits.items():
        v = (fields.get(k) or "").strip()
        if not v:
            errors.append(f"{where}: {k} is empty")
        elif len(v) > limit:
            warnings.append(f"{where}: {k} is {len(v)} characters; most devices truncate after about {limit}")
    e, w = check_text(" ".join(str(fields.get(k) or "") for k in limits), offer_policy, where, tokens, offer_allowed_here)
    return errors + e, warnings + w
