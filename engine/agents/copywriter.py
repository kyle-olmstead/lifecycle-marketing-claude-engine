"""Copywriter Agent: direct-response copy variants for every touch, rendered in the platform dialect.

The split of work is strict. The writer (Claude, or the offline starter library) produces words
only: subject, preview text, headline, paragraphs, a call-to-action label, SMS text, push and
in-app fields, each variant with a framework and a falsifiable hypothesis. This module then does
everything that must never be guessed:

  * renders the words into the platform's template dialect (Customer.io and Braze Liquid, Klaviyo's
    Django-style language, HubL, AMPscript, Iterable Handlebars) with a fallback on every personalization token
  * adds the unsubscribe link and postal address the dialect requires, SMS opt-out language, a
    preheader and dark-mode metadata, so compliance does not depend on the writer remembering
  * lints every template with engine/render/template_lint.py, counts SMS segments with engine/render/sms.py, and
    applies engine/policy/guardrails.py (offer policy, fake urgency, deceptive subjects, text-to-image)
  * with Claude, sends lint errors back for one repair round; a variant that still fails is dropped,
    and a touch with no passing variant blocks the plan

Modes:
  claude    Claude API (needs ANTHROPIC_API_KEY; model from LCM_COPY_MODEL, default claude-sonnet-5-5)
  offline   a starter library of role-based drafts for every flow, written so nothing needs filling in: it names
            only what the brief knows (the product, the core action, the item) and invents no customer, number or feature
  handoff   writes copy_briefs.md for Claude in the desktop app or Claude Code to answer with a
            drafts JSON, then re-run the plan with --drafts
  auto      claude when a key is set, otherwise offline
"""
from __future__ import annotations

import html
import json
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict

from engine.policy import guardrails
from engine.models import Brief, CopyVariant, FlowPlan, Touch, TouchCopy, base_form
from engine.render import template_lint
from engine.render.sms import has_opt_out

DEFAULT_MODEL = "claude-sonnet-5-5"
PLACEHOLDER_CTA = "https://example.com/replace-with-your-deep-link"
PREVIEW_NAME = "Christopher"   # long-ish first name for worst-case length counts
LIMITS = {"push": {"title": 50, "body": 150}, "in_app": {"headline": 60, "body": 200, "cta_label": 24}}


# ----------------------------------------------------------------------------------------------
# structured output contracts for the writer (no numeric or length constraints: the API's
# structured-output grammar does not support them, and the guardrails check lengths afterwards)
# ----------------------------------------------------------------------------------------------

class EmailDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str
    preheader: str
    headline: str
    paragraphs: List[str]
    cta_label: str
    framework: str
    hypothesis: str


class SmsDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    framework: str
    hypothesis: str


class PushDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    body: str
    framework: str
    hypothesis: str


class InAppDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    headline: str
    body: str
    cta_label: str
    framework: str
    hypothesis: str


class EmailVariants(BaseModel):
    model_config = ConfigDict(extra="forbid")
    variants: List[EmailDraft]


class SmsVariants(BaseModel):
    model_config = ConfigDict(extra="forbid")
    variants: List[SmsDraft]


class PushVariants(BaseModel):
    model_config = ConfigDict(extra="forbid")
    variants: List[PushDraft]


class InAppVariants(BaseModel):
    model_config = ConfigDict(extra="forbid")
    variants: List[InAppDraft]


OUTPUT_MODEL = {"email": EmailVariants, "sms": SmsVariants, "push": PushVariants, "in_app": InAppVariants}


# ----------------------------------------------------------------------------------------------
# dialect rendering
# ----------------------------------------------------------------------------------------------

FIRST_NAME = {
    "customerio": '{{ customer.first_name | default: "there" }}',
    "braze": "{{ ${first_name} | default: 'there' }}",
    "klaviyo": "{{ first_name|default:'there' }}",
    "hubl": '{{ personalization_token("contact.firstname", "there") }}',
    "ampscript": "%%=v(@fn)=%%",
    # Iterable: defaultIfEmpty covers null, undefined and empty (support.iterable.com/hc/en-us/articles/205480365)
    "handlebars": '{{defaultIfEmpty firstName "there"}}',
}
# short channels have no room for a declaration block, so AMPscript uses one inline expression with the fallback
INLINE_AMPSCRIPT_NAME = '%%=IIf(Empty(AttributeValue("FirstName")), "there", AttributeValue("FirstName"))=%%'
AMPSCRIPT_HEAD = '%%[\nVAR @fn\nSET @fn = AttributeValue("FirstName")\nIF Empty(@fn) THEN SET @fn = "there" ENDIF\n]%%\n'


def unsubscribe_html(d: str) -> str:
    return {"customerio": '<a href="{% unsubscribe_url %}">Unsubscribe</a>',
            "braze": '<a href="{{${set_user_to_unsubscribed_url}}}">Unsubscribe</a>',
            "klaviyo": "{% unsubscribe 'Unsubscribe' %}",
            "hubl": '<a href="{{ unsubscribe_link }}">Unsubscribe</a>',
            "ampscript": '<a href="%%unsub_center_url%%">Unsubscribe</a>',
            # Iterable's built-in unsubscribe merge tag (support.iterable.com/hc/articles/206514205)
            "handlebars": '<a href="{{unsubscribeUrl}}">Unsubscribe</a>'}[d]


def address_html(d: str, brief: Brief) -> str:
    if d == "klaviyo":
        return "{{ organization.name }}, {{ organization.full_address }}"
    if d == "hubl":
        return ("{{ site_settings.company_name }}, {{ site_settings.company_street_address_1 }}, "
                "{{ site_settings.company_city }}, {{ site_settings.company_state }}")
    if d == "ampscript":
        return "%%member_busname%%, %%member_addr%%, %%member_city%%, %%member_state%% %%member_postalcode%%"
    if brief.postal_address:
        return html.escape(brief.postal_address)
    if d == "braze":
        return "{{${email_footer}}}"
    if d == "handlebars":
        # the mailing address set on Iterable's Project Settings page ({{companyName}} is the project name, so it is not used)
        return "{{physicalAddress}}"
    return "POSTAL ADDRESS REQUIRED: add it to the brief (postal_address) before sending"


def _tokens(text: str, d: str, brief: Brief, escape: bool = True, inline: bool = False) -> str:
    out = html.escape(text, quote=False) if escape else text
    out = out.replace("{product_name}", html.escape(brief.product_name) if escape else brief.product_name)
    if "{item_name}" in out:
        from engine.render.product_blocks import item_name
        out = out.replace("{item_name}", item_name(brief, d))
    return out.replace("{first_name}", INLINE_AMPSCRIPT_NAME if (inline and d == "ampscript") else FIRST_NAME[d])


def subject_lines(f: Dict[str, Any], brief: Brief) -> str:
    """Subject and preview text in the platform's syntax, for pasting into the ESP's subject and preheader fields."""
    d = brief.template_dialect
    return (f"Subject: {_tokens(f['subject'], d, brief, escape=False, inline=True)}\n"
            f"Preview text: {_tokens(f['preheader'], d, brief, escape=False, inline=True)}\n")


SMS_LINK_PREVIEW = "https://ex.co/a1B2c3d"   # what a platform-shortened link costs in the segment count (21 characters)


def _preview(text: str, brief: Brief) -> str:
    return text.replace("{first_name}", PREVIEW_NAME).replace("{product_name}", brief.product_name)


from typing import TYPE_CHECKING  # noqa: E402  (render_email's brand kit type, kept beside the renderer)

if TYPE_CHECKING:
    from engine.render.brand import BrandKit


def render_email_mjml(f: Dict[str, Any], brief: Brief, step_id: str, brand: Optional[BrandKit] = None,
                      product_items: Optional[str] = None) -> Tuple[str, str, str]:
    """(MJML source, compiled HTML, plain text) for one email, laid out by engine/render/mjml_render.py.

    The words go into the platform dialect here (fallback on every token); the layout, colors, logo, fonts and footer
    come from the brand kit (engine/render/brand.py; the neutral default when brand is None). The unsubscribe link and
    postal address come from unsubscribe_html/address_html; when the brief has no postal address, the kit's footer
    address is used. product_items fills the MJML product block slot (a no-op when None)."""
    from engine.render import mjml_render
    from engine.render.brand import DEFAULT_BRAND
    kit = brand or DEFAULT_BRAND
    d = brief.template_dialect
    from engine.render import product_blocks
    back = product_blocks.return_url(brief, d, brief.cta_url or kit.website)
    if back:
        # a link built from the trigger event (that checkout, that product) already carries the platform's own query string,
        # so no UTM is appended here; turn on the platform's UTM tracking for the flow instead
        href = back
    else:
        url = brief.cta_url or kit.website or PLACEHOLDER_CTA        # a deep link, else the brand's home page, else a placeholder
        sep = "&" if "?" in url else "?"
        href = html.escape(f"{url}{sep}utm_source=lifecycle&utm_medium=email&utm_campaign={brief.slug}&utm_content={step_id}")
    head = AMPSCRIPT_HEAD if d == "ampscript" else ""   # declared before first use, including the <title>
    file_start = (f"<!-- dialect: {d} -->\n"
                  f"<!-- {brief.slug} / {step_id}. Rendered by engine/agents/copywriter.py through MJML (engine/render/mjml_render.py); "
                  f"lint: python3 engine/render/template_lint.py --dialect {d} --email <this file> -->\n{head}")
    addr_brief = brief if (brief.postal_address or not kit.footer.postal_address) else brief.model_copy(
        update={"postal_address": kit.footer.postal_address})
    address = address_html(d, addr_brief)
    if kit.footer.legal_name and addr_brief.postal_address and d in ("customerio", "braze", "handlebars"):
        address = f"{html.escape(kit.footer.legal_name)}, {address}"   # the token dialects already print the organization name
    sender = brief.sender_name or kit.brand_name or brief.product_name
    extra = [_tokens(x, d, brief) for x in kit.footer.extra_lines]
    # the title is also the wrapper's aria-label, so quotes in the words are escaped as well (template tags stay as written)
    title = _tokens(html.escape(f["subject"], quote=True).replace("{product_name}", html.escape(brief.product_name, quote=True)),
                    d, brief, escape=False)
    from engine.render import product_blocks
    paragraphs = [p for p in f["paragraphs"] if p.strip() != "{product_block}"]
    wants_block = len(paragraphs) != len(f["paragraphs"])
    block_mjml, block_text = product_blocks.block(brief, d, kit.website) if wants_block else (None, "")
    if product_items is None and block_mjml:
        product_items = block_mjml
    content = mjml_render.EmailContent(
        dialect=d, title=title, preheader=_tokens(f["preheader"], d, brief), headline=_tokens(f["headline"], d, brief),
        paragraphs=tuple(_tokens(p, d, brief) for p in paragraphs), cta_label=_tokens(f["cta_label"], d, brief),
        cta_href=href, sender_line=html.escape(sender), address_line=address, unsubscribe=unsubscribe_html(d),
        extra_lines=tuple(extra), file_start=file_start, product_items=product_items)
    mjml_source, body = mjml_render.render(content, kit)
    unsub_text = {"customerio": "{% unsubscribe_url %}", "braze": "{{${set_user_to_unsubscribed_url}}}",
                  "klaviyo": "{% unsubscribe_link %}", "hubl": "{{ unsubscribe_link }}", "ampscript": "%%unsub_center_url%%",
                  "handlebars": "{{unsubscribeUrl}}"}[d]
    footer = [sender, html.unescape(address)] + [_tokens(x, d, brief, escape=False) for x in kit.footer.extra_lines]
    footer += [f"{s.label}: {s.url}" for s in kit.social] + [f"Unsubscribe: {unsub_text}"]
    text = head + "\n\n".join(
        [_tokens(f["headline"], d, brief, escape=False)] + [_tokens(p, d, brief, escape=False) for p in paragraphs]
        + ([block_text] if block_text else [])
        + [f"{_tokens(f['cta_label'], d, brief, escape=False)}: {html.unescape(href)}", "\n".join(footer)]) + "\n"
    return mjml_source, body, text


def render_email(f: Dict[str, Any], brief: Brief, step_id: str, brand: Optional[BrandKit] = None) -> Tuple[str, str]:
    """(HTML, plain text) for one email in the brief's dialect, compiled from MJML; see render_email_mjml."""
    _mjml, body, text = render_email_mjml(f, brief, step_id, brand=brand)
    return body, text


def render_short(channel: str, f: Dict[str, Any], brief: Brief) -> Dict[str, Any]:
    d = brief.template_dialect
    if channel == "sms":
        from engine.render import product_blocks
        text = f["text"].strip().replace("{product_name}", brief.product_name)
        if not text.lower().startswith(brief.product_name.lower()):
            text = f"{brief.product_name}: {text}"
        # cart and browse texts always carry the way back (the checkout or product); others carry cta_url where the copy
        # asks for {link}. The platform shortens the link, so the preview counts a shortened one.
        wants_link = "{link}" in text
        text = re.sub(r"\s*\{link\}", "", text).strip()
        url = product_blocks.return_url(brief, d, brief.cta_url or brand_for(brief).website) or (
            (brief.cta_url or brand_for(brief).website or PLACEHOLDER_CTA) if wants_link else None)
        tail = "" if has_opt_out(text) else " Reply STOP to opt out."
        rendered = _tokens(text, d, brief, escape=False, inline=True) + (f" {url}" if url else "") + tail
        return {"text": rendered, "preview": _preview(text + (f" {SMS_LINK_PREVIEW}" if url else "") + tail, brief)}
    keys = ("title", "body") if channel == "push" else ("headline", "body", "cta_label")
    return ({k: _tokens(f[k], d, brief, escape=False, inline=True) for k in keys}
            | {"preview": {k: _preview(f[k], brief) for k in keys}})


def _no_fallback(brief: Brief) -> List[str]:
    """A cart or browse button returns to the trigger's own link; with no fallback it is empty when the event lacks one."""
    from engine.render.product_blocks import RETURN_PROPERTY
    if brief.flow_type not in RETURN_PROPERTY or brief.template_dialect == "klaviyo" or brief.cta_url or brand_for(brief).website:
        return []
    prop = RETURN_PROPERTY[brief.flow_type][0]
    return [f"the button returns to {prop} from the trigger event, with nothing to fall back on when an event lacks it: set "
            f"website in brand.yaml (your store's home page) as the fallback"]


def _offer_allowed(brief: Brief, role: Optional[str], index: Optional[int]) -> bool:
    """Whether this touch may carry an offer: the policy must allow one, the role must be one that offers go in, and the
    first touch stays offer-free when the policy says so."""
    from engine.agents.campaign_architect import OFFER_ROLES
    if role is None:
        return True
    if brief.offer.never_in_first_touch and index == 1:
        return False
    return role in OFFER_ROLES


_BRANDS: Dict[str, Any] = {}


def brand_for(brief: Brief):
    """The brand kit for this brief: brief.brand_kit when set; else the workspace's kit when it belongs to this brand (or the
    workspace is the user's own, not the bundled demo); else the neutral default, so one brand's colors never reach another."""
    from engine import workspace
    from engine.render.brand import DEFAULT_BRAND, brand_path, load_brand
    if brief.brand_kit:
        path = brief.brand_kit
    else:
        ws = workspace.find()
        found = brand_path(ws)
        path = str(found) if found else None
        if path and ws.demo:
            kit = _BRANDS.get(path) or load_brand(path)
            _BRANDS[path] = kit
            if not (kit.brand_name or "").lower().startswith(brief.product_name.lower()[:6]):
                return DEFAULT_BRAND
    if not path:
        return DEFAULT_BRAND
    if path not in _BRANDS:
        _BRANDS[path] = load_brand(path)
    return _BRANDS[path]


def check_variant(channel: str, f: Dict[str, Any], brief: Brief, step_id: str, vid: str, role: Optional[str] = None,
                  index: Optional[int] = None, brand: Any = None) -> CopyVariant:
    errors: List[str] = []
    warnings: List[str] = []
    rendered: Dict[str, str] = {}
    brand = brand if brand is not None else brand_for(brief)
    tokens = guardrails.PRODUCT_TOKENS if brief.flow.product_block else set()
    offer_ok = _offer_allowed(brief, role, index)
    raw = json.dumps(f)
    if re.search(r"\{\{|\{%|%%|\$\{", raw):
        errors.append("the copy contains template syntax; write words only and use {first_name} or {product_name}")
    words = " ".join(x for v in f.values() for x in (v if isinstance(v, list) else [v]) if isinstance(x, str))
    if re.search(r"\[[^\]]{3,}\]", words):
        warnings.append("contains [bracketed] fill-ins: replace them with the real specifics before sending")
    if chr(0x2014) in raw:
        errors.append("contains an em dash (house style)")
    if channel == "email":
        e, w = guardrails.check_email_fields(f, brief.offer, tokens, offer_ok)
        errors += e
        warnings += w
        if not errors:
            mjml_src, body, text = render_email_mjml(f, brief, step_id, brand=brand)
            lint = template_lint.lint(body, brief.template_dialect, email=True)
            errors += [f"template: {x}" for x in lint["errors"]]
            warnings += [f"template: {x}" for x in lint["warnings"] if "postal address" not in x or "POSTAL ADDRESS REQUIRED" not in body]
            lint_t = template_lint.lint(text, brief.template_dialect)
            errors += [f"plain text: {x}" for x in lint_t["errors"]]
            tti = guardrails.text_to_image(body)
            errors += tti["errors"]
            warnings += tti["warnings"]
            if "POSTAL ADDRESS REQUIRED" in body:
                warnings.append("no postal address: CAN-SPAM requires one; add postal_address to the brief")
            if PLACEHOLDER_CTA in body:
                warnings.append("the button links to a placeholder; set website in brand.yaml (your home page), or cta_url in the "
                                "brief for a deep link to the core action")
            warnings += _no_fallback(brief)
            subj = subject_lines(f, brief)
            lint_s = template_lint.lint(subj, brief.template_dialect)
            errors += [f"subject: {x}" for x in lint_s["errors"]]
            rendered = {f"{step_id}_{vid}.html": body, f"{step_id}_{vid}.txt": text, f"{step_id}_{vid}.subject.txt": subj,
                        f"{step_id}_{vid}.mjml": mjml_src}
            voice = brand.check_voice([str(x) for x in f.values() if isinstance(x, str)] + list(f.get("paragraphs") or []))
            warnings += voice
    elif channel == "sms":
        r = render_short("sms", f, brief)
        e, w, _report = guardrails.check_sms(r["preview"], brief.product_name, first_message=False)
        e2, w2 = guardrails.check_text(f["text"], brief.offer, "sms", set(tokens) | {"link"}, offer_ok)   # {link}: the way back
        errors += e + e2
        warnings += w + w2
        lint = template_lint.lint(r["text"], brief.template_dialect)
        errors += [f"template: {x}" for x in lint["errors"]]
        if PLACEHOLDER_CTA in r["text"]:
            warnings.append("the text links to a placeholder; set website in brand.yaml (your home page), or cta_url in the "
                            "brief for a deep link to the core action")
        warnings += _no_fallback(brief)
        rendered = {f"{step_id}_{vid}.json": json.dumps(r, indent=2, ensure_ascii=False)}
    else:
        r = render_short(channel, f, brief)
        e, w = guardrails.check_short(r["preview"], LIMITS[channel], brief.offer, channel.replace("_", "-"), tokens, offer_ok)
        errors += e
        warnings += w
        for k, v in r.items():
            if k != "preview":
                lint = template_lint.lint(v, brief.template_dialect)
                errors += [f"template ({k}): {x}" for x in lint["errors"]]
        rendered = {f"{step_id}_{vid}.json": json.dumps(r, indent=2, ensure_ascii=False)}
    fields = {k: v for k, v in f.items() if k not in ("framework", "hypothesis")}
    return CopyVariant(variant_id=vid, framework=f.get("framework", ""), hypothesis=f.get("hypothesis", ""), fields=fields,
                       rendered=rendered, errors=errors, warnings=warnings)


# ----------------------------------------------------------------------------------------------
# copy briefs
# ----------------------------------------------------------------------------------------------

def copy_brief(t: Touch, brief: Brief, plan: FlowPlan) -> str:
    prior = [x for x in plan.touches if x.index < t.index and "fallback" not in (x.condition or "")]
    core = brief.core_conversion_event
    lines = [
        f"Product: {brief.product_name}. Flow: {brief.flow_type}, touch {t.index} of {brief.touches}, day {t.day}, channel {t.channel}.",
        f"Who: {_who(brief, core)}.",
        f"This message's job ({t.role.replace('_', ' ')}): {t.job}",
        f"Framework: {t.framework}. The single goal: {t.goal}.",
        f"Before this: " + ("; ".join(f"day {x.day} {x.channel} ({x.role.replace('_', ' ')})" for x in prior) if prior else "nothing; this is the first message") + ".",
        f"Sender: {brief.sender_name or brief.product_name}. Voice: {brief.brand_voice or brand_for(brief).voice_brief() or 'plain, specific, second person'}.",
        f"Offer policy (binding): {brief.offer.describe()}"
        + ("" if _offer_allowed(brief, t.role, t.index) or not brief.offer.allows_offers else "; no offer in this touch") +
        ". Never invent a deadline, scarcity, a statistic, a customer or a feature.",
        "Personalization: " + _tokens_line(brief) + "; write the template code nowhere, words only."
        + (" The cart or product itself is shown below the words by the product block, so do not list the items." if _has_items(brief) else ""),
    ]
    if t.channel == "email":
        lines.append("Email: subject carries the weight in the first 35 to 40 characters; preview text is a second, distinct promise; "
                     "one call to action; 2 to 4 short paragraphs; no images needed.")
    elif t.channel == "sms":
        lines.append("SMS: one segment (160 GSM-7 characters) including the brand prefix and 'Reply STOP to opt out.'"
                     + (", and the shortened link back (about 22 characters)" if _has_items(brief) and brief.flow_type in ("cart_abandon", "browse_abandon") else "")
                     + ", which are added for you; "
                     "plain ASCII punctuation.")
    elif t.channel == "push":
        lines.append("Push: title up to about 50 characters, body up to about 150; one action.")
    else:
        lines.append(f"In-app: shows at the next session; headline up to about 60 characters, body up to about 200, button label up to about 24.")
    return "\n".join(lines)


SYSTEM = """You write lifecycle messages for one reader in one situation. You are a senior direct-response copywriter who works inside a
specific flow position with one job.

Rules that are not negotiable:
- Specifics over abstraction: a real outcome, a real action, a number only if the brief gives one. If a specific is needed and you do
  not have it, write a [bracketed fill-in] for the marketer instead of inventing it.
- No fake urgency, fake scarcity, invented personalization, or curiosity-gap subjects the body does not honor.
- Respect the offer policy exactly. If it says no discounts, do not mention one.
- Avoid the tells of machine copy: three-item lists everywhere, uniform sentence length, an opening that restates the reader's
  situation, "That said", "Moreover", a closing summary. Vary sentence length. One idea per message.
- Never use an em dash. Use commas, colons or full stops.
- Personalize only with {first_name} and {product_name}. Never write template code ({{ }}, {% %}, %% %%).
- Each variant differs structurally (angle, opening, length, sender framing), not by synonyms, and carries a falsifiable hypothesis
  naming the metric and what result would falsify it, e.g. "Naming the report in the subject raises click rate because it signals
  relevance before the open; falsified if B's click rate is equal or higher."
"""


def _claude_write(client: Any, model: str, t: Touch, brief: Brief, cbrief: str, n: int, feedback: Optional[str] = None) -> List[Dict[str, Any]]:
    out_model = OUTPUT_MODEL[t.channel]
    user = f"{cbrief}\n\nWrite {n} variant(s)."
    if feedback:
        user += f"\n\nYour previous draft failed these checks. Fix every one and return the full set again:\n{feedback}"
    resp = client.messages.parse(model=model, max_tokens=8000, system=SYSTEM, messages=[{"role": "user", "content": user}],
                                 output_format=out_model)
    if getattr(resp, "stop_reason", None) in ("refusal", "max_tokens"):
        raise RuntimeError(f"the model stopped with {resp.stop_reason}")
    parsed = resp.parsed_output
    return [v.model_dump() for v in parsed.variants][:n]


def _claude_client() -> Any:
    import anthropic
    return anthropic.Anthropic(max_retries=3, timeout=120)


def write(brief: Brief, plan: FlowPlan, mode: str = "auto", drafts: Optional[Dict[str, List[Dict[str, Any]]]] = None,
          client: Any = None, model: Optional[str] = None, reuse: Optional[Dict[str, Any]] = None,
          reuse_key: Optional[Callable[[Touch], str]] = None) -> List[TouchCopy]:
    """Copy for every touch. reuse maps a touch identity (reuse_key) to (old copy, approval, run id) from an earlier run:
    those words are kept and re-rendered for this plan instead of being written again."""
    if drafts is not None:
        mode = "supplied"
    elif mode == "auto":
        mode = "claude" if (client is not None or os.environ.get("ANTHROPIC_API_KEY")) else "offline"
    model = model or os.environ.get("LCM_COPY_MODEL", DEFAULT_MODEL)
    if mode == "claude" and client is None:
        client = _claude_client()
    out: List[TouchCopy] = []
    for t in plan.touches:
        cb = copy_brief(t, brief, plan)
        k = reuse_key(t) if (reuse and reuse_key) else None
        if k and k in reuse:
            old, _approval, rid = reuse[k]
            variants = [check_variant(t.channel, {**v.fields, "framework": v.framework, "hypothesis": v.hypothesis}, brief, t.step_id, v.variant_id,
                                      t.role, t.index)
                        for v in old.variants]
            out.append(TouchCopy(step_id=t.step_id, channel=t.channel, mode=old.mode, copy_brief=cb, variants=variants,
                                 note=f"reused from run {rid} (same job, channel, audience and voice; re-rendered and re-linted for this plan)"))
            continue
        if mode == "handoff":
            out.append(TouchCopy(step_id=t.step_id, channel=t.channel, mode="handoff", copy_brief=cb,
                                 note="awaiting drafts: answer copy_briefs.md, then re-run with --drafts"))
            continue
        note = None
        if mode == "supplied":
            raw = drafts.get(t.step_id) or []
            if not raw:
                note = "no drafts supplied for this step"
        elif mode == "claude":
            try:
                raw = _claude_write(client, model, t, brief, cb, brief.copy_variants)
            except Exception as e:  # noqa: BLE001
                raw = offline_drafts(t, brief)
                note = f"Claude API call failed ({type(e).__name__}: {str(e)[:120]}); offline starter drafts used instead"
        else:
            raw = offline_drafts(t, brief)
        variants = [check_variant(t.channel, f, brief, t.step_id, chr(65 + i), t.role, t.index) for i, f in enumerate(raw)]
        if mode == "claude" and note is None and any(not v.ok for v in variants):
            feedback = "\n".join(f"Variant {v.variant_id}: {e}" for v in variants for e in v.errors)
            try:
                raw = _claude_write(client, model, t, brief, cb, brief.copy_variants, feedback)
                variants = [check_variant(t.channel, f, brief, t.step_id, chr(65 + i), t.role, t.index) for i, f in enumerate(raw)]
            except Exception as e:  # noqa: BLE001
                note = f"repair round failed ({type(e).__name__})"
        passing = [v for v in variants if v.ok]
        dropped = [v for v in variants if not v.ok]
        if passing and dropped:
            note = (note + "; " if note else "") + "dropped variant(s) " + ", ".join(
                f"{v.variant_id} ({'; '.join(v.errors[:2])})" for v in dropped)
            variants = passing
        out.append(TouchCopy(step_id=t.step_id, channel=t.channel, mode="offline" if (mode == "claude" and note and "offline" in note) else mode,
                             copy_brief=cb, variants=variants, note=note))
    return out



# ----------------------------------------------------------------------------------------------
# offline starter library: one structural A/B pair per role. Bracketed text is for the marketer.
# ----------------------------------------------------------------------------------------------

def _lib(brief: Brief) -> Dict[str, List[Dict[str, Any]]]:
    p = "{product_name}"
    core = brief.core_conversion_event
    act = core.label or core.event.replace("_", " ")
    do = base_form(act)
    sender = (brief.sender_name or brief.product_name).split(" at ")[0]
    n = brief.churn_threshold_days
    lapsed = ("you have not opened or clicked our emails in a while" if brief.flow_type == "sunset" else
              f"you have not used {p} in a while")
    return {
        "value_reminder": [
            {"subject": f"Your {p} workspace is where you left it", "preheader": f"Pick up from your last session in {p}",
             "headline": "Everything is still set up", "paragraphs": [
                 "Hi {first_name}, your data sources are still connected and your last work is saved.",
                 f"Most people who come back start with the same thing they did last time: they {do}. It takes a few minutes, and the setup is already done."],
             "cta_label": "Open where you left off", "framework": "BAB: before, after, bridge",
             "hypothesis": "Stressing that setup is already done raises the core-action rate within 7 days because it removes the restart cost; falsified if B converts as well or better."},
            {"subject": f"{n} days since you last {act}", "preheader": "A plain note, no offer",
             "headline": f"It has been {n} days", "paragraphs": [
                 f"Hi {{first_name}}, the last time you {act} in {p} was {n} days ago.",
                 "If something got in the way, reply and tell me what it was. If you just got busy, the button below takes you straight back."],
             "cta_label": f"Go back to {p}", "framework": "Plain, factual reminder",
             "hypothesis": "A factual, specific gap in the subject raises clicks more than a benefit line because it is concrete; falsified if A's click rate is equal or higher."},
        ],
        "friction_removal": [
            {"subject": "The step most people get stuck on", "preheader": "Tell us which one and we will help",
             "headline": "If you got stuck, tell us where", "paragraphs": [
                 "Hi {first_name}, when people stop using " + p + " after the first few weeks, it is usually because one step took longer than it should have.",
                 "Reply to this email with where you got stuck, and a person will walk you past it. If you would rather try again on your own, the button below takes you straight there."],
             "cta_label": "Try again now", "framework": "PAS: problem, agitation kept factual, solution",
             "hypothesis": "Naming the stall and offering a person raises the core-action rate among lapsed users because it addresses why they left; falsified if B converts as well or better."},
            {"subject": f"The two-minute way to {do}", "preheader": "Start small with what is already there",
             "headline": "Start small", "paragraphs": [
                 "Hi {first_name}, the slowest part of starting again is deciding where to begin.",
                 f"Begin with the smallest version: {do} once, using what is already in your account, and refine it later. Most of the setup is behind you."],
             "cta_label": "Start small", "framework": "Solution first, problem implied",
             "hypothesis": "Leading with the smallest next step raises the core-action rate more than naming the problem because it lowers effort at the moment of reading; falsified if A converts as well or better."},
        ],
        "whats_new": [
            {"subject": f"What has changed in {p}", "preheader": "Everything you set up is still there",
             "headline": "A quick look at what is new", "paragraphs": [
                 "Hi {first_name}, " + p + " has kept changing since you were last in, and everything you set up is where you left it.",
                 f"The quickest way to see what is different is to {do} once. It takes a few minutes."],
             "cta_label": "See what is new", "framework": "AIDA: attention, interest, desire, action",
             "hypothesis": "Inviting a look at what changed raises clicks among lapsed users because it gives a new reason to return; falsified if B's click rate is equal or higher."},
            {"subject": f"A fresh start in {p}", "preheader": "No setup to redo",
             "headline": "Pick up with a fresh start", "paragraphs": [
                 "Hi {first_name}, if " + p + " did not fit the way you work the first time, a lot may look different now.",
                 f"Open it, {do} once, and see whether it fits better. If it still does not, reply and tell us why."],
             "cta_label": f"Open {p}", "framework": "Reframe: a fresh start, not a reminder",
             "hypothesis": "Framing the return as a fresh start raises clicks more than a what-changed note because it releases past friction; falsified if A's click rate is equal or higher."},
        ],
        "social_proof": [
            {"subject": f"How people who stay with {p} use it", "preheader": "One small weekly habit",
             "headline": "One habit that makes it stick", "paragraphs": [
                 "Hi {first_name}, the people who get the most from " + p + f" tend to do one thing: they {do} on the same day each week, so it becomes a habit rather than a task.",
                 f"Pick your day, {do} once now, and set a reminder for next week."],
             "cta_label": "Start the habit", "framework": "Social proof through a common pattern",
             "hypothesis": "Describing what retained users do raises the core-action rate because it is easy to imitate; falsified if B converts as well or better."},
            {"subject": f"The weekly routine that makes {p} worth it", "preheader": "Ten minutes, once a week",
             "headline": "Ten minutes, once a week", "paragraphs": [
                 f"Hi {{first_name}}, a short weekly routine is how most people keep getting value from {p}: {do}, look at what changed, and share it with whoever needs it.",
                 "Put ten minutes in your calendar on the same day each week and start this week."],
             "cta_label": "Do it now", "framework": "Social proof through a routine",
             "hypothesis": "A concrete routine with a time cost raises the core-action rate more than a habit framing; falsified if A converts as well or better."},
        ],
        "human_help": [
            {"subject": "Can I help?", "preheader": f"A note from {sender}",
             "headline": "A quick note", "paragraphs": [
                 "Hi {first_name}, I noticed you have not been in " + p + " for a while.",
                 "If something is not working for you, I would like to hear it. Reply to this email, or book 15 minutes and I will help you get set up."],
             "cta_label": "Book 15 minutes", "framework": "Reciprocity: an offer of help, not a pitch",
             "hypothesis": "A personal offer of help raises replies and bookings because it lowers the cost of asking; falsified if B gets as many replies."},
            {"subject": f"{sender} from {p}", "preheader": "One question",
             "headline": "One question", "paragraphs": [
                 "Hi {first_name}, what is the one thing that would make " + p + " worth coming back to?",
                 "Reply in a line. I read every answer."],
             "cta_label": "Reply to " + sender, "framework": "Question-led",
             "hypothesis": "Asking one question raises replies more than offering a call because replying is cheaper than booking; falsified if A gets as many replies or more."},
        ],
        "use_case": [
            {"subject": "Another way to use " + p, "preheader": "Tell us what you are working on",
             "headline": "Something you have not tried", "paragraphs": [
                 "Hi {first_name}, if your first use of " + p + " is done, the next one is often closer than it looks.",
                 "Reply and tell us what you are working on this month, and we will suggest how " + p + " could help with it."],
             "cta_label": f"Open {p}", "framework": "Open loop closed by a reply",
             "hypothesis": "Inviting the next use case re-engages people who finished their first use; falsified if B converts as well or better."},
            {"subject": "Five minutes to try something new in " + p, "preheader": "Takes about five minutes",
             "headline": "Try one new thing", "paragraphs": [
                 f"Hi {{first_name}}, set aside five minutes and {do} for something you have not tried before in {p}.",
                 "If it does not fit, reply and tell us why. We read every answer."],
             "cta_label": "Try it", "framework": "How-to: one small experiment",
             "hypothesis": "A bounded five-minute experiment raises completion more than an open invitation because it removes uncertainty; falsified if A converts as well or better."},
        ],
        "check_in": [
            {"subject": "Quick question", "preheader": "One line is enough",
             "headline": "What would bring you back?", "paragraphs": [
                 "Hi {first_name}, what is one thing " + p + " would need to do for you to use it again?",
                 "Reply with a line. It goes straight to the team that decides what we build next."],
             "cta_label": "Reply", "framework": "Question-led",
             "hypothesis": "A direct question raises replies; falsified if reply rate is under 1%."},
            {"subject": "Was it something we did?", "preheader": "Honest answers help",
             "headline": "Help us get it right", "paragraphs": [
                 "Hi {first_name}, you stopped using " + p + ", and that usually means we missed something.",
                 "Tell us what it was. A reply is enough."],
             "cta_label": "Reply", "framework": "Question-led, accountability framing",
             "hypothesis": "Taking responsibility raises replies over a neutral question; falsified if A's reply rate is equal or higher."},
        ],
        "repermission": [
            {"subject": "Should we keep sending these?", "preheader": "One click to stay; nothing to do to stop",
             "headline": "Do you still want these emails?", "paragraphs": [
                 f"Hi {{first_name}}, {lapsed}, so we want to check before we send more.",
                 "If you want to keep hearing from us, click below. If not, do nothing: in two weeks we will stop these emails. Your account and data stay as they are."],
             "cta_label": "Yes, keep me subscribed", "framework": "Plain re-permission; no guilt, no countdown",
             "hypothesis": "An honest ask keeps the people who still care and lowers complaints from those who do not; expect 1 to 5% to click. Falsified if complaints on this send exceed the flow average."},
            {"subject": f"We will pause emails from {p}", "preheader": "Unless you tell us otherwise",
             "headline": "Pausing your emails", "paragraphs": [
                 f"Hi {{first_name}}, {lapsed}, so we plan to stop sending you these emails in two weeks.",
                 "Want to keep them? One click does it. Either way, your account stays open."],
             "cta_label": "Keep my emails", "framework": "Default-effect framing",
             "hypothesis": "Stating the default (a pause) raises stay clicks over asking a question because it makes the consequence concrete; falsified if A's stay rate is equal or higher."},
        ],
        "first_step": [
            {"subject": f"Your first step in {p}", "preheader": "It takes about five minutes",
             "headline": f"Welcome to {p}", "paragraphs": [
                 "Hi {first_name}, you are set up. The fastest way to see what " + p + " does is to " + do + ".",
                 "It takes about five minutes, and everything after that builds on it."],
             "cta_label": "Do the first step", "framework": "BAB: before, after, bridge",
             "hypothesis": "One named first step raises activation over a feature tour because it focuses effort; falsified if B activates as well or better."},
            {"subject": f"Welcome, {{first_name}}", "preheader": "Here is where to start",
             "headline": "Start here", "paragraphs": [
                 "Hi {first_name}, thanks for signing up for " + p + ".",
                 "Most new teams start the same way: they " + do + " first. The button below opens it."],
             "cta_label": "Get started", "framework": "Warm welcome",
             "hypothesis": "A warm welcome subject raises opens but not activation; falsified if B activates at a higher rate than A."},
        ],
        "unblock": [
            {"subject": "Stuck partway through setup?", "preheader": "Pick up at the step you stopped on",
             "headline": "Pick up where you stopped", "paragraphs": [
                 "Hi {first_name}, most new users pause at one step of setup, and the rest goes quickly once it is done.",
                 "The button below takes you back to where you stopped. Or reply with what got in the way, and we will do it with you."],
             "cta_label": "Pick up where I stopped", "framework": "PAS: problem, agitation kept factual, solution",
             "hypothesis": "Naming the pause and the way back raises completion of setup; falsified if B is as good or better."},
            {"subject": "Need a hand getting set up?", "preheader": "Reply and a person will help",
             "headline": "We can do it with you", "paragraphs": [
                 "Hi {first_name}, setting up " + p + " is quicker with help.",
                 "Reply to this email with a good time and we will walk you through it."],
             "cta_label": "Get help", "framework": "Reciprocity",
             "hypothesis": "An offer of help raises activation among stalled users more than instructions; falsified if A activates at a higher rate."},
        ],
        "show_outcome": [
            {"subject": f"What you get when you {do}", "preheader": "The reason people sign up",
             "headline": "Here is what you get", "paragraphs": [
                 "Hi {first_name}, the moment " + p + " starts paying off is the first time you " + do + ".",
                 "Most people get there in one sitting, using what they set up when they signed up."],
             "cta_label": "Make yours", "framework": "AIDA: attention, interest, desire, action",
             "hypothesis": "Showing the finished outcome raises activation because it makes the value concrete; falsified if B activates as well or better."},
            {"subject": f"The shortest way to {do}", "preheader": "One step at a time",
             "headline": "One step at a time", "paragraphs": [
                 "Hi {first_name}, the shortest path is to " + do + " once with what is already in your account, and refine it later.",
                 "The button below takes you to the right place."],
             "cta_label": "Start now", "framework": "How-to",
             "hypothesis": "Instructions beat an example for people who already intend to start; falsified if A activates at a higher rate."},
        ],
        "last_nudge": [
            {"subject": f"Still want to try {p}?", "preheader": "One step, whenever you are ready",
             "headline": "Whenever you are ready", "paragraphs": [
                 "Hi {first_name}, you have not had a chance to " + do + " yet. It is the step that shows what " + p + " can do.",
                 "If something is in the way, reply and we will help."],
             "cta_label": "Take the first step", "framework": "Plain reminder",
             "hypothesis": "A low-pressure last reminder converts some stalled users without raising unsubscribes; falsified if unsubscribes on this send exceed the flow average."},
            {"subject": "Is now a bad time?", "preheader": "We can wait",
             "headline": "No rush", "paragraphs": [
                 "Hi {first_name}, if now is not the right time for " + p + ", that is fine.",
                 "When it is, the first step is to " + do + ". The button below takes you there."],
             "cta_label": "Open " + p, "framework": "Permission framing",
             "hypothesis": "Acknowledging timing lowers unsubscribes versus a reminder; falsified if B's unsubscribe rate is equal or higher."},
        ],
    }


def _short_lib(brief: Brief) -> Dict[str, Dict[str, List[Any]]]:
    """Channel-native starter copy for SMS, push and in-app, per role: (A, B) pairs, with nothing for the marketer to fill
    in. {link} in an SMS is replaced by the way back (the checkout or product for cart and browse flows, else cta_url)."""
    p = "{product_name}"
    core = brief.core_conversion_event
    act = core.label or core.event.replace("_", " ")
    do = base_form(act)
    Do = do[:1].upper() + do[1:]
    n = brief.churn_threshold_days
    sender = (brief.sender_name or brief.product_name).split(" at ")[0]
    return {
        "value_reminder": {
            "sms": [f"Your {p} setup is saved. Pick up where you left off. {{link}}", f"{n} days since you last {act}. Back in one tap. {{link}}"],
            "push": [("Everything is still set up", f"Pick up where you left off in {p}."), (f"{n} days since your last visit", f"{Do} again in a few minutes.")],
            "in_app": [("Welcome back", f"Your setup is saved. The fastest way back in is to {do}.", "Continue"),
                       ("Pick up where you left off", f"It has been {n} days. {Do} from where you stopped.", f"{Do}"[:24])]},
        "friction_removal": {
            "sms": [f"Stuck partway in {p}? Reply with where, and a person will help.", f"The two-minute way to {do}: start small with what is already there. {{link}}"],
            "push": [("Stuck? We can help", "Reply to our email with where you got stuck."), ("Start small", f"{Do} once with what is already there.")],
            "in_app": [("Stuck? We can help", "Tell us where you got stuck and a person will walk you past it.", "Get help"),
                       ("Start small", f"{Do} once with what is already in your account, and refine it later.", "Start small")]},
        "whats_new": {
            "sms": [f"{p} has changed since your last visit. See what is new. {{link}}", f"A fresh start in {p}: everything you set up is still there. {{link}}"],
            "push": [(f"What is new in {p}", f"{Do} once to see what changed."), ("A fresh start", "Everything you set up is still there.")],
            "in_app": [("Welcome back", f"A lot may look different. {Do} once to see what changed.", "See what is new"),
                       ("A fresh start", "Everything you set up is still where you left it.", "Pick up again")]},
        "social_proof": {
            "sms": [f"People who stay with {p} {do} on the same day each week. Start yours. {{link}}",
                    f"Ten minutes a week keeps {p} working for you. {{link}}"],
            "push": [("One habit that sticks", f"{Do} on the same day each week."), ("Ten minutes a week", f"{Do} now and set a weekly reminder.")],
            "in_app": [("One habit that sticks", f"People who get the most from {p} {do} on the same day each week.", "Start the habit"),
                       ("Ten minutes a week", f"{Do}, see what changed, and share it, on the same day every week.", f"{Do}"[:24])]},
        "human_help": {
            "sms": [f"Hi {{first_name}}, it's {sender} at {p}. Anything I can help with? Just reply.",
                    f"{sender} from {p}: want a hand getting set up? Reply and I will help."],
            "push": [("Need a hand?", f"Reply to {sender}'s email and get set up."), (f"One question from {sender}", f"What would make {p} worth coming back to?")],
            "in_app": [("Need a hand?", f"Reply to {sender}'s email and we will set it up with you.", "Get help"),
                       ("Tell us what is missing", f"One line on what would make {p} worth coming back to.", "Answer")]},
        "use_case": {
            "sms": [f"Done with your first use of {p}? Reply with what you are working on and we will suggest the next.",
                    f"Five minutes to try something new in {p}. {{link}}"],
            "push": [(f"Another way to use {p}", "Tell us what you are working on this month."), ("Try one new thing", f"Five minutes to {do} for something new.")],
            "in_app": [("Try something new", "Tell us what you are working on this month and we will suggest how it can help.", "Tell us"),
                       ("Try one new thing", f"Set aside five minutes and {do} for something new.", "Try it")]},
        "check_in": {
            "sms": [f"{p}: what would make it worth coming back? Reply with one line.", f"{p} here. Was it something we did? A one-line reply helps."],
            "push": [("Quick question", f"What would make {p} worth coming back to?"), ("Help us get it right", "Tell us what we missed. One line is enough.")],
            "in_app": [("Quick question", f"What would make {p} worth coming back to?", "Answer"),
                       ("Help us get it right", "Tell us what we missed. One line is enough.", "Tell us")]},
        "repermission": {
            "sms": [f"{p}: still want these texts? Reply YES to keep them; otherwise we will stop in 2 weeks.", f"{p}: we will pause these texts in 2 weeks unless you reply YES."],
            "push": [("Keep getting updates?", "Tap to keep them; otherwise we will pause them."), ("Pausing updates", "Tap to keep them coming.")],
            "in_app": [("Still want our emails?", "One click keeps them coming. Otherwise we will pause them in two weeks.", "Keep my emails"),
                       ("Pausing your emails", "We plan to stop marketing emails in two weeks. Want them?", "Keep them")]},
        "first_step": {
            "sms": [f"Welcome to {p}. Your first step: {do}. About five minutes. {{link}}", f"Welcome to {p}, {{first_name}}. Start here. {{link}}"],
            "push": [("Your first step", f"{Do} in about five minutes."), (f"Welcome to {p}", "Here is where to start.")],
            "in_app": [("Start here", f"The fastest way to see what {p} does: {do}.", "Start"),
                       (f"Welcome to {p}", f"Most people start the same way: they {do} first.", "Get started")]},
        "unblock": {
            "sms": [f"Stuck partway through setup in {p}? Pick up where you stopped. {{link}}", f"Need a hand getting set up in {p}? Reply and a person will help."],
            "push": [("Stuck partway?", "Pick up at the step you stopped on."), ("Need a hand?", "Reply to our email and we will set it up with you.")],
            "in_app": [("Stuck partway?", "Pick up at the step you stopped on, or tell us what got in the way.", "Pick up"),
                       ("We can do it with you", "Reply to our email and we will set it up together.", "Get help")]},
        "show_outcome": {
            "sms": [f"{p} pays off the first time you {do}. {{link}}", f"The shortest way to {do}: start with what is already there. {{link}}"],
            "push": [("Here is what you get", f"{p} pays off the first time you {do}."), ("One step at a time", f"{Do} once with what is already there.")],
            "in_app": [("Here is what you get", f"{p} starts paying off the first time you {do}.", "Make yours"),
                       ("One step at a time", f"{Do} once with what is already in your account.", "Start now")]},
        "last_nudge": {
            "sms": [f"Still want to try {p}? Your first step: {do}. {{link}}", f"No rush. When you are ready, {p} starts here. {{link}}"],
            "push": [("Whenever you are ready", f"Your first step: {do}."), ("No rush", f"When you are ready, {p} starts with one step.")],
            "in_app": [("Whenever you are ready", f"Your first step is to {do}.", "Take the first step"),
                       ("No rush", f"When the time is right, the first step is to {do}.", f"Open {p}"[:24])]},
    }


def offline_drafts(t: Touch, brief: Brief) -> List[Dict[str, Any]]:
    from engine.agents import copy_library, copy_store
    store = copy_store.is_store(brief)
    lib = {**_lib(brief), **copy_library.email_library(brief), **(copy_store.email_library(brief) if store else {})}
    emails = lib[t.role][: brief.copy_variants]
    if t.channel == "email":
        return [dict(e) for e in emails]
    short = {**_short_lib(brief), **copy_library.short_library(brief),
             **(copy_store.short_library(brief) if store else {})}[t.role][t.channel]
    out = []
    for e, s in zip(emails, short):
        meta = {"framework": e["framework"], "hypothesis": e["hypothesis"]}
        if t.channel == "sms":
            out.append({"text": _shorten(s, 110), **meta})
        elif t.channel == "push":
            out.append({"title": _shorten(s[0], 50), "body": _shorten(s[1], 150), **meta})
        else:
            out.append({"headline": _shorten(s[0], 60), "body": _shorten(s[1], 200), "cta_label": _shorten(s[2], 24), **meta})
    return out


def _shorten(s: str, n: int) -> str:
    s = s.strip()
    if len(s) <= n:
        return s
    cut = s[: n - 3].rsplit(" ", 1)[0].rstrip(",;:")
    return cut + "..."


def _has_items(brief: Brief) -> bool:
    from engine.render.product_blocks import EVENT_FLOWS
    return brief.flow_type in EVENT_FLOWS


def _tokens_line(brief: Brief, short: bool = False) -> str:
    """The merge fields a writer may use: the first item's name too when the trigger event carries items."""
    if _has_items(brief):
        return ("{first_name}, {product_name} and {item_name}" if short else
                "{first_name}, {product_name} and {item_name} (the first item's name, from the trigger event) are available")
    return "{first_name} and {product_name}" if short else "only {first_name} and {product_name} are available"


def _who(brief: Brief, core: Any) -> str:
    """Who receives the flow, in a sentence a copywriter can write to."""
    did = core.label or core.event
    if brief.flow_type == "winback":
        return f"people who {brief.qualifying_event.describe()} and have not {did} in the last {brief.churn_threshold_days} days"
    if brief.flow_type == "activation":
        return f"new users who {brief.qualifying_event.describe()} and have not yet {did}"
    from engine.flows.library import FLOWS
    f = FLOWS[brief.flow_type]
    trig, goal = f.trigger_event.get("*"), f.goal_event.get("*")
    if trig and goal and trig != goal:
        return f"people who {f.trigger_label.get('*', trig)} and have not {f.goal_label.get('*', goal)} since"
    return f.summary


def handoff_markdown(brief: Brief, copy_plan: List[TouchCopy]) -> str:
    shapes = {"email": {"subject": "", "preheader": "", "headline": "", "paragraphs": [""], "cta_label": "", "framework": "", "hypothesis": ""},
              "sms": {"text": "", "framework": "", "hypothesis": ""},
              "push": {"title": "", "body": "", "framework": "", "hypothesis": ""},
              "in_app": {"headline": "", "body": "", "cta_label": "", "framework": "", "hypothesis": ""}}
    lines = [f"# Copy briefs: {brief.product_name} {brief.flow_type}", "",
             "Write the copy for each step below, then save it as drafts.json in this shape and re-run:",
             "", "```text", f"lcm plan --brief <brief> --drafts drafts.json", "```", "",
             f"Rules: words only; personalize only with {_tokens_line(brief, short=True)}; no em dashes; follow the offer policy; "
             "each variant gets a framework and a falsifiable hypothesis. The renderer adds the unsubscribe link, the address, "
             "SMS opt-out text and the template code.", ""]
    skeleton: Dict[str, Any] = {}
    for c in copy_plan:
        lines += [f"## {c.step_id} ({c.channel})", "", c.copy_brief, ""]
        skeleton[c.step_id] = [shapes[c.channel] for _ in range(brief.copy_variants)]
    lines += ["## drafts.json shape", "", "```json", json.dumps(skeleton, indent=2), "```", ""]
    return "\n".join(lines)
