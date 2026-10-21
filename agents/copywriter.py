"""Copywriter Agent: direct-response copy variants for every touch, rendered in the platform dialect.

The split of work is strict. The writer (Claude, or the offline starter library) produces words
only: subject, preview text, headline, paragraphs, a call-to-action label, SMS text, push and
in-app fields, each variant with a framework and a falsifiable hypothesis. This module then does
everything that must never be guessed:

  * renders the words into the platform's template dialect (Customer.io and Braze Liquid, Klaviyo's
    Django-style language, HubL, AMPscript) with a fallback on every personalization token
  * adds the unsubscribe link and postal address the dialect requires, SMS opt-out language, a
    preheader and dark-mode metadata, so compliance does not depend on the writer remembering
  * lints every template with tools/template_lint.py, counts SMS segments with tools/sms.py, and
    applies agents/guardrails.py (offer policy, fake urgency, deceptive subjects, text-to-image)
  * with Claude, sends lint errors back for one repair round; a variant that still fails is dropped,
    and a touch with no passing variant blocks the plan

Modes:
  claude    Claude API (needs ANTHROPIC_API_KEY; model from LCM_COPY_MODEL, default claude-sonnet-5-5)
  offline   a starter library of role-based drafts; specifics the library cannot know are left as
            [bracketed] fill-ins and flagged
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

from agents import guardrails
from agents.models import Brief, CopyVariant, FlowPlan, Touch, TouchCopy, base_form
from tools import template_lint
from tools.sms import has_opt_out

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
}
# short channels have no room for a declaration block, so AMPscript uses one inline expression with the fallback
INLINE_AMPSCRIPT_NAME = '%%=IIf(Empty(AttributeValue("FirstName")), "there", AttributeValue("FirstName"))=%%'
AMPSCRIPT_HEAD = '%%[\nVAR @fn\nSET @fn = AttributeValue("FirstName")\nIF Empty(@fn) THEN SET @fn = "there" ENDIF\n]%%\n'


def unsubscribe_html(d: str) -> str:
    return {"customerio": '<a href="{% unsubscribe_url %}">Unsubscribe</a>',
            "braze": '<a href="{{${set_user_to_unsubscribed_url}}}">Unsubscribe</a>',
            "klaviyo": "{% unsubscribe 'Unsubscribe' %}",
            "hubl": '<a href="{{ unsubscribe_link }}">Unsubscribe</a>',
            "ampscript": '<a href="%%unsub_center_url%%">Unsubscribe</a>'}[d]


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
    return "POSTAL ADDRESS REQUIRED: add it to the brief (postal_address) before sending"


def _tokens(text: str, d: str, brief: Brief, escape: bool = True, inline: bool = False) -> str:
    out = html.escape(text, quote=False) if escape else text
    out = out.replace("{product_name}", html.escape(brief.product_name) if escape else brief.product_name)
    return out.replace("{first_name}", INLINE_AMPSCRIPT_NAME if (inline and d == "ampscript") else FIRST_NAME[d])


def subject_lines(f: Dict[str, Any], brief: Brief) -> str:
    """Subject and preview text in the platform's syntax, for pasting into the ESP's subject and preheader fields."""
    d = brief.template_dialect
    return (f"Subject: {_tokens(f['subject'], d, brief, escape=False, inline=True)}\n"
            f"Preview text: {_tokens(f['preheader'], d, brief, escape=False, inline=True)}\n")


def _preview(text: str, brief: Brief) -> str:
    return text.replace("{first_name}", PREVIEW_NAME).replace("{product_name}", brief.product_name)


def render_email(f: Dict[str, Any], brief: Brief, step_id: str) -> Tuple[str, str]:
    d = brief.template_dialect
    url = brief.cta_url or PLACEHOLDER_CTA
    sep = "&" if "?" in url else "?"
    href = html.escape(f"{url}{sep}utm_source=lifecycle&utm_medium=email&utm_campaign={brief.slug}&utm_content={step_id}")
    paras = "\n".join(f'          <p class="lcm-text" style="margin:0 0 16px;font:16px/1.5 Arial,sans-serif;">{_tokens(p, d, brief)}</p>'
                      for p in f["paragraphs"])
    head = AMPSCRIPT_HEAD if d == "ampscript" else ""   # declared before first use, including the <title>
    body = f"""<!-- dialect: {d} -->
<!-- {brief.slug} / {step_id}. Rendered by agents/copywriter.py; lint: python3 tools/template_lint.py --dialect {d} --email <this file> -->
{head}<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <meta name="supported-color-schemes" content="light dark">
  <title>{_tokens(f['subject'], d, brief)}</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{ margin: 0; padding: 0; }}
    .lcm-card {{ background-color: #FFFFFF; }}
    .lcm-text {{ color: #1C1C1C; }}
    @media (prefers-color-scheme: dark) {{
      .lcm-card {{ background-color: #1E1E1E !important; }}
      .lcm-text {{ color: #F2F2F2 !important; }}
    }}
  </style>
</head>
<body>
<div class="preheader" style="display:none;max-height:0;overflow:hidden;">{_tokens(f['preheader'], d, brief)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
  <tr>
    <td align="center">
      <table role="presentation" class="lcm-card" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;">
        <tr><td style="padding:32px 24px;">
          <h1 class="lcm-text" style="margin:0 0 16px;font:bold 22px/1.3 Arial,sans-serif;">{_tokens(f['headline'], d, brief)}</h1>
{paras}
          <table role="presentation" cellpadding="0" cellspacing="0"><tr>
            <td style="border-radius:6px;background-color:#1F5F4A;">
              <a href="{href}" style="display:inline-block;padding:12px 20px;font:bold 16px Arial,sans-serif;color:#FFFFFF;text-decoration:none;">{_tokens(f['cta_label'], d, brief)}</a>
            </td>
          </tr></table>
        </td></tr>
        <tr><td class="lcm-text" style="padding:16px 24px;font:12px/1.5 Arial,sans-serif;">
          {html.escape(brief.sender_name or brief.product_name)}<br>
          {address_html(d, brief)}<br>
          {unsubscribe_html(d)}
        </td></tr>
      </table>
    </td>
  </tr>
</table>
</body>
</html>
"""
    unsub_text = {"customerio": "{% unsubscribe_url %}", "braze": "{{${set_user_to_unsubscribed_url}}}",
                  "klaviyo": "{% unsubscribe_link %}", "hubl": "{{ unsubscribe_link }}", "ampscript": "%%unsub_center_url%%"}[d]
    text = (AMPSCRIPT_HEAD if d == "ampscript" else "") + "\n\n".join(
        [_tokens(f["headline"], d, brief, escape=False)] + [_tokens(p, d, brief, escape=False) for p in f["paragraphs"]]
        + [f"{_tokens(f['cta_label'], d, brief, escape=False)}: {html.unescape(href)}",
           f"{brief.sender_name or brief.product_name}\n{html.unescape(address_html(d, brief))}\nUnsubscribe: {unsub_text}"]) + "\n"
    return body, text


def render_short(channel: str, f: Dict[str, Any], brief: Brief) -> Dict[str, Any]:
    d = brief.template_dialect
    if channel == "sms":
        text = f["text"].strip().replace("{product_name}", brief.product_name)
        if not text.lower().startswith(brief.product_name.lower()):
            text = f"{brief.product_name}: {text}"
        if not has_opt_out(text):
            text = f"{text} Reply STOP to opt out."
        return {"text": _tokens(text, d, brief, escape=False, inline=True), "preview": _preview(text, brief)}
    keys = ("title", "body") if channel == "push" else ("headline", "body", "cta_label")
    return ({k: _tokens(f[k], d, brief, escape=False, inline=True) for k in keys}
            | {"preview": {k: _preview(f[k], brief) for k in keys}})


def check_variant(channel: str, f: Dict[str, Any], brief: Brief, step_id: str, vid: str) -> CopyVariant:
    errors: List[str] = []
    warnings: List[str] = []
    rendered: Dict[str, str] = {}
    raw = json.dumps(f)
    if re.search(r"\{\{|\{%|%%|\$\{", raw):
        errors.append("the copy contains template syntax; write words only and use {first_name} or {product_name}")
    words = " ".join(x for v in f.values() for x in (v if isinstance(v, list) else [v]) if isinstance(x, str))
    if re.search(r"\[[^\]]{3,}\]", words):
        warnings.append("contains [bracketed] fill-ins: replace them with the real specifics before sending")
    if chr(0x2014) in raw:
        errors.append("contains an em dash (house style)")
    if channel == "email":
        e, w = guardrails.check_email_fields(f, brief.offer_policy)
        errors += e
        warnings += w
        if not errors:
            body, text = render_email(f, brief, step_id)
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
                warnings.append("the button links to a placeholder; set cta_url in the brief to a deep link on your own domain")
            subj = subject_lines(f, brief)
            lint_s = template_lint.lint(subj, brief.template_dialect)
            errors += [f"subject: {x}" for x in lint_s["errors"]]
            rendered = {f"{step_id}_{vid}.html": body, f"{step_id}_{vid}.txt": text, f"{step_id}_{vid}.subject.txt": subj}
    elif channel == "sms":
        r = render_short("sms", f, brief)
        e, w, _report = guardrails.check_sms(r["preview"], brief.product_name, first_message=False)
        e2, w2 = guardrails.check_text(f["text"], brief.offer_policy, "sms")
        errors += e + e2
        warnings += w + w2
        lint = template_lint.lint(r["text"], brief.template_dialect)
        errors += [f"template: {x}" for x in lint["errors"]]
        rendered = {f"{step_id}_{vid}.json": json.dumps(r, indent=2, ensure_ascii=False)}
    else:
        r = render_short(channel, f, brief)
        e, w = guardrails.check_short(r["preview"], LIMITS[channel], brief.offer_policy, channel.replace("_", "-"))
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
        f"Who: {('people who ' + brief.qualifying_event.describe() + ' and then went ' + str(brief.churn_threshold_days) + ' days without ' + (core.label or core.event)) if brief.flow_type == 'winback' else ('new users who ' + brief.qualifying_event.describe() + ' and have not yet ' + (core.label or core.event))}.",
        f"This message's job ({t.role.replace('_', ' ')}): {t.job}",
        f"Framework: {t.framework}. The single goal: {t.goal}.",
        f"Before this: " + ("; ".join(f"day {x.day} {x.channel} ({x.role.replace('_', ' ')})" for x in prior) if prior else "nothing; this is the first message") + ".",
        f"Sender: {brief.sender_name or brief.product_name}. Voice: {brief.brand_voice or 'plain, specific, second person'}.",
        f"Offer policy (binding): {brief.offer_policy}. Never invent a deadline, scarcity, a statistic, a customer or a feature.",
        "Personalization: only {first_name} and {product_name} are available; write the template code nowhere, words only.",
    ]
    if t.channel == "email":
        lines.append("Email: subject carries the weight in the first 35 to 40 characters; preview text is a second, distinct promise; "
                     "one call to action; 2 to 4 short paragraphs; no images needed.")
    elif t.channel == "sms":
        lines.append("SMS: one segment (160 GSM-7 characters) including the brand prefix and 'Reply STOP to opt out.', which are added for you; "
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
            variants = [check_variant(t.channel, {**v.fields, "framework": v.framework, "hypothesis": v.hypothesis}, brief, t.step_id, v.variant_id)
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
        variants = [check_variant(t.channel, f, brief, t.step_id, chr(65 + i)) for i, f in enumerate(raw)]
        if mode == "claude" and note is None and any(not v.ok for v in variants):
            feedback = "\n".join(f"Variant {v.variant_id}: {e}" for v in variants for e in v.errors)
            try:
                raw = _claude_write(client, model, t, brief, cb, brief.copy_variants, feedback)
                variants = [check_variant(t.channel, f, brief, t.step_id, chr(65 + i)) for i, f in enumerate(raw)]
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
            {"subject": "The step most people get stuck on", "preheader": "And the two-minute way past it",
             "headline": "If you got stuck, this is usually where", "paragraphs": [
                 "Hi {first_name}, when people stop using " + p + " after the first few weeks, it is usually one step: [the most common blocker from support tickets or the leak map].",
                 "[The shortest path past it, in one or two sentences]. If that is not it, reply to this email and a person will help."],
             "cta_label": "Try the shortcut", "framework": "PAS: problem, agitation kept factual, solution",
             "hypothesis": "Naming the blocker raises the core-action rate among lapsed users because it addresses the actual reason they left; falsified if B converts as well or better."},
            {"subject": f"A template to {do} in two minutes", "preheader": "Start from something that already works",
             "headline": "Start from a template", "paragraphs": [
                 "Hi {first_name}, starting from a blank page is the slowest part.",
                 "[Name the template or starter that fits their use case] is ready in your account. Open it, change two things, and you are done."],
             "cta_label": "Open the template", "framework": "Solution first, problem implied",
             "hypothesis": "Leading with a ready-made starting point raises the core-action rate more than naming the problem because it lowers effort at the moment of reading; falsified if A converts as well or better."},
        ],
        "whats_new": [
            {"subject": f"New in {p} since you last visited", "preheader": "One change that saves time",
             "headline": "One thing changed", "paragraphs": [
                 "Hi {first_name}, since you were last in " + p + ", we shipped [one specific change since their last visit].",
                 "[What it does for them in one sentence, with a concrete example]."],
             "cta_label": "See it in your account", "framework": "AIDA: attention, interest, desire, action",
             "hypothesis": "A single concrete change raises clicks more than a roundup because it is easy to evaluate; falsified if B's click rate is equal or higher."},
            {"subject": f"You asked, we built it", "preheader": "[The change in five words]",
             "headline": "[The change, as a benefit]", "paragraphs": [
                 "Hi {first_name}, [the change] is live in " + p + ".",
                 "It came from customer requests like yours. Try it on your own data; it takes about a minute."],
             "cta_label": "Try it now", "framework": "Reciprocity framed around feedback",
             "hypothesis": "Framing the change as a response to customer requests raises clicks because it signals the product is improving for them; falsified if A's click rate is equal or higher."},
        ],
        "social_proof": [
            {"subject": "How [customer type] use " + p + " each week", "preheader": "One routine, start to finish",
             "headline": "A weekly routine that sticks", "paragraphs": [
                 "Hi {first_name}, [a real customer or team like theirs] uses " + p + " every [day of week] to [specific outcome with a number].",
                 f"The routine is short: they {do}, share it, and move on. You can copy it as is."],
             "cta_label": "Copy the routine", "framework": "Social proof through one concrete case",
             "hypothesis": "A single specific customer routine raises the core-action rate more than general benefits because it is easy to imitate; falsified if B converts as well or better."},
            {"subject": "[Number] teams did this last week", "preheader": "A quick look at what is working",
             "headline": "What active teams are doing", "paragraphs": [
                 "Hi {first_name}, last week [number from your data] teams used " + p + " to " + do + ".",
                 "The most common reason: [the outcome they get, in one sentence]."],
             "cta_label": "Do it now", "framework": "Social proof through volume",
             "hypothesis": "A volume number raises clicks less than one concrete case; falsified if B's click rate is higher than A's."},
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
            {"subject": "Another way to use " + p, "preheader": "Built for [their role]",
             "headline": "Something you have not tried", "paragraphs": [
                 "Hi {first_name}, most people use " + p + " for [their primary use]. [Role] teams also use it for [second use case].",
                 "The first step: [one concrete step]."],
             "cta_label": "Try it", "framework": "Open loop closed in the body",
             "hypothesis": "A new use case re-engages people who finished their first use; falsified if B converts as well or better."},
            {"subject": "[Second use case] in " + p, "preheader": "Takes about five minutes",
             "headline": "[Second use case], step by step", "paragraphs": [
                 "Hi {first_name}, here is how to set up [second use case] in " + p + ".",
                 "1. [Step one]. 2. [Step two]. That is all it takes."],
             "cta_label": "Set it up", "framework": "How-to",
             "hypothesis": "Step-by-step instructions raise completion more than a teaser because they remove uncertainty; falsified if A converts as well or better."},
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
                 "Hi {first_name}, you have not opened " + p + " in a while, so we want to check before we send more.",
                 "If you want to keep hearing from us, click below. If not, do nothing: in two weeks we will stop these emails. Your account and data stay as they are."],
             "cta_label": "Yes, keep me subscribed", "framework": "Plain re-permission; no guilt, no countdown",
             "hypothesis": "An honest ask keeps the people who still care and lowers complaints from those who do not; expect 1 to 5% to click. Falsified if complaints on this send exceed the flow average."},
            {"subject": f"We will pause emails from {p}", "preheader": "Unless you tell us otherwise",
             "headline": "Pausing your emails", "paragraphs": [
                 "Hi {first_name}, since you have not used " + p + " lately, we plan to stop sending you these emails in two weeks.",
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
            {"subject": "Stuck on [the leak step]?", "preheader": "Here is the shortcut",
             "headline": "The shortcut past [the leak step]", "paragraphs": [
                 "Hi {first_name}, [the leak step] is where most new users pause.",
                 "[The shortest way past it]. Or reply, and we will do it with you."],
             "cta_label": "Take the shortcut", "framework": "PAS: problem, agitation kept factual, solution",
             "hypothesis": "Naming the leak step raises completion of that step; falsified if B is as good or better."},
            {"subject": "Need a hand getting set up?", "preheader": "Reply and a person will help",
             "headline": "We can do it with you", "paragraphs": [
                 "Hi {first_name}, setting up " + p + " is quicker with help.",
                 "Reply to this email with a good time and we will walk you through it."],
             "cta_label": "Get help", "framework": "Reciprocity",
             "hypothesis": "An offer of help raises activation among stalled users more than instructions; falsified if A activates at a higher rate."},
        ],
        "show_outcome": [
            {"subject": "What [outcome] looks like", "preheader": "A real example",
             "headline": "Here is what you get", "paragraphs": [
                 "Hi {first_name}, here is a real example of what teams get when they " + do + ": [example].",
                 "Yours takes about five minutes."],
             "cta_label": "Make yours", "framework": "AIDA: attention, interest, desire, action",
             "hypothesis": "Showing the finished outcome raises activation because it makes the value concrete; falsified if B activates as well or better."},
            {"subject": "Five minutes to your first [outcome]", "preheader": "Step by step",
             "headline": "Your first [outcome]", "paragraphs": [
                 "Hi {first_name}, here is the shortest path: [step one], then [step two].",
                 "That is it."],
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
    """Channel-native starter copy for SMS, push and in-app, per role: (A, B) pairs. Bracketed text is for the marketer."""
    p = "{product_name}"
    core = brief.core_conversion_event
    act = core.label or core.event.replace("_", " ")
    do = base_form(act)
    Do = do[:1].upper() + do[1:]
    n = brief.churn_threshold_days
    return {
        "value_reminder": {
            "sms": [f"Your {p} setup is saved. Pick up where you left off: [link]", f"{n} days since you last {act}. Back in one tap: [link]"],
            "push": [("Everything is still set up", f"Pick up where you left off in {p}."), (f"{n} days since your last visit", f"{Do} again in a few minutes.")],
            "in_app": [("Welcome back", f"Your setup is saved. The fastest way back in is to {do}.", "Continue"),
                       ("Pick up where you left off", f"It has been {n} days. {Do} from where you stopped.", f"{Do}"[:24])]},
        "friction_removal": {
            "sms": ["Stuck on [the common blocker]? The 2-minute way past it: [link]", f"A ready-made template to {do} in two minutes: [link]"],
            "push": [("Stuck? There is a shortcut", "[The shortest path past the most common blocker]."),
                     ("Start from a template", f"A ready-made template gets you to {do} in two minutes.")],
            "in_app": [("Stuck? Here is the shortcut", "[The shortest path past the most common blocker].", "Show me"),
                       ("Start from a template", f"Skip the blank page: a template gets you to {do} in two minutes.", "Open a template")]},
        "whats_new": {
            "sms": [f"New in {p}: [the change, in a few words]. See it: [link]", f"You asked, we built it: [the change]. Try it: [link]"],
            "push": [(f"New in {p}", "[The change, and what it does for them, in one sentence]."),
                     ("You asked, we built it", "[The change], live in your account now.")],
            "in_app": [("One thing changed", "[What the change does for them, with a concrete example].", "See it"),
                       ("[The change, as a benefit]", "It came from requests like yours. Try it on your own data.", "Try it now")]},
        "social_proof": {
            "sms": [f"How [customer type] use {p} each week, in one short routine: [link]", f"[Number] teams used {p} to {do} last week. Your turn: [link]"],
            "push": [("A weekly routine that sticks", f"[A team like theirs] uses {p} every [day] to [outcome]."),
                     ("What active teams are doing", f"[Number] teams used {p} to {do} last week.")],
            "in_app": [("A routine worth copying", f"[A team like theirs] uses {p} every [day] to [outcome].", "Copy the routine"),
                       ("What active teams do", f"[Number] teams used {p} to {do} last week.", f"{Do}"[:24])]},
        "human_help": {
            "sms": [f"Hi {{first_name}}, it's [name] at {p}. Anything I can help with? Just reply.", f"[Name] from {p}: want 15 minutes to get set up? Book here: [link]"],
            "push": [("Need a hand?", "Book 15 minutes with [name] and get set up."), ("One question from [name]", f"What would make {p} worth coming back to?")],
            "in_app": [("Need a hand?", "Book 15 minutes with [name] and we will set it up with you.", "Book a time"),
                       ("Tell us what is missing", f"One line on what would make {p} worth coming back to.", "Answer")]},
        "use_case": {
            "sms": [f"Another way to use {p}: [second use case]. Try it: [link]", f"[Second use case] in {p} takes about five minutes: [link]"],
            "push": [(f"Another way to use {p}", "[Second use case], built for [their role]."), ("[Second use case]", "Set it up in about five minutes.")],
            "in_app": [("Try something new", "[Second use case], built for [their role]. First step: [step].", "Try it"),
                       ("[Second use case]", "Set it up in about five minutes, step by step.", "Set it up")]},
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
            "sms": [f"Welcome to {p}. Your first step: {do}. About five minutes: [link]", f"Welcome to {p}, {{first_name}}. Start here: [link]"],
            "push": [("Your first step", f"{Do} in about five minutes."), (f"Welcome to {p}", "Here is where to start.")],
            "in_app": [("Start here", f"The fastest way to see what {p} does: {do}.", "Start"),
                       (f"Welcome to {p}", f"Most teams start the same way: they {do} first.", "Get started")]},
        "unblock": {
            "sms": ["Stuck on [the leak step]? Here is the shortcut: [link]", f"Need a hand getting set up in {p}? Reply and a person will help."],
            "push": [("Stuck on [the leak step]?", "[The shortest way past it]."), ("Need a hand?", "Reply to our email and we will set it up with you.")],
            "in_app": [("Stuck on [the leak step]?", "[The shortest way past it].", "Show me"),
                       ("We can do it with you", "Book a short call and we will set it up together.", "Get help")]},
        "show_outcome": {
            "sms": [f"See what teams get when they {do}: [link]", f"Five minutes to your first [outcome] in {p}: [link]"],
            "push": [("Here is what you get", "[A real example of the outcome]."), ("Your first [outcome]", "[Step one], then [step two]. That is it.")],
            "in_app": [("Here is what you get", "[A real example of the outcome].", "Make yours"),
                       ("Your first [outcome]", "[Step one], then [step two]. That is it.", "Start now")]},
        "last_nudge": {
            "sms": [f"Still want to try {p}? Your first step: {do}. [link]", f"No rush. When you are ready, {p} starts here: [link]"],
            "push": [("Whenever you are ready", f"Your first step: {do}."), ("No rush", f"When you are ready, {p} starts with one step.")],
            "in_app": [("Whenever you are ready", f"Your first step is to {do}.", "Take the first step"),
                       ("No rush", f"When the time is right, the first step is to {do}.", f"Open {p}"[:24])]},
    }


def offline_drafts(t: Touch, brief: Brief) -> List[Dict[str, Any]]:
    emails = _lib(brief)[t.role][: brief.copy_variants]
    if t.channel == "email":
        return [dict(e) for e in emails]
    short = _short_lib(brief)[t.role][t.channel]
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


def handoff_markdown(brief: Brief, copy_plan: List[TouchCopy]) -> str:
    shapes = {"email": {"subject": "", "preheader": "", "headline": "", "paragraphs": [""], "cta_label": "", "framework": "", "hypothesis": ""},
              "sms": {"text": "", "framework": "", "hypothesis": ""},
              "push": {"title": "", "body": "", "framework": "", "hypothesis": ""},
              "in_app": {"headline": "", "body": "", "cta_label": "", "framework": "", "hypothesis": ""}}
    lines = [f"# Copy briefs: {brief.product_name} {brief.flow_type}", "",
             "Write the copy for each step below, then save it as drafts.json in this shape and re-run:",
             "", "```text", f"python3 -m agents.orchestrator plan --brief <brief> --drafts drafts.json", "```", "",
             "Rules: words only; personalize only with {first_name} and {product_name}; no em dashes; follow the offer policy; "
             "each variant gets a framework and a falsifiable hypothesis. The renderer adds the unsubscribe link, the address, "
             "SMS opt-out text and the template code.", ""]
    skeleton: Dict[str, Any] = {}
    for c in copy_plan:
        lines += [f"## {c.step_id} ({c.channel})", "", c.copy_brief, ""]
        skeleton[c.step_id] = [shapes[c.channel] for _ in range(brief.copy_variants)]
    lines += ["## drafts.json shape", "", "```json", json.dumps(skeleton, indent=2), "```", ""]
    return "\n".join(lines)
