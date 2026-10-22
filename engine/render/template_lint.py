"""
Template linter for every personalization dialect the engine writes (prompts/jobs/copy).

Only two of the five major platforms use Liquid. The engine never writes "Liquid" without naming
the platform, and this linter checks each dialect with a real parser where one exists:

  braze        Liquid (Shopify Liquid 5) plus Braze attribute syntax {{${first_name}}} and
               Braze tags (connected_content, abort_message). Parsed with python-liquid after
               the Braze-only syntax is checked by pattern and set aside.
  customerio   Liquid ("latest") plus Customer.io tags ({% unsubscribe_url %}, render_liquid ...).
  customerio_legacy  the legacy Customer.io Liquid, which has no default filter.
  klaviyo      Klaviyo's Django-style template language ({{ first_name|default:'there' }},
               {{ person|lookup:'Prop' }}). Compiled with Django's template engine, with
               Klaviyo's own tags and filters registered.
  hubl         HubSpot HubL (Jinja-derived). Parsed with Jinja2; fallbacks via
               personalization_token("contact.firstname", "there").
  ampscript    Salesforce Marketing Cloud AMPscript. No public parser exists; this checks block and
               inline delimiters, IF/ENDIF and FOR/NEXT balance, string termination and variable
               naming. Treat a pass as structural, not semantic.
  handlebars   Iterable's Handlebars ({{field}}, {{defaultIfEmpty field "fallback"}}, {{#each items}}...{{/each}}).
               No maintained Python Handlebars parser is pinned, so this checks structure: balanced braces,
               block helpers opened and closed in order ({{#if}}...{{/if}}, {{#each}}...{{/each}}), {{else}}
               only inside a block, terminated strings, and defaultIfEmpty's two arguments. Treat a pass as
               structural, not semantic (support.iterable.com/hc/en-us/articles/205480365 and /36531450481300).
  liquid       plain Shopify Liquid.

Errors are syntax that will fail to render or will be rejected. Warnings are rules from the
copywriter method: every personalization token carries an explicit fallback; every marketing
email carries an unsubscribe link and a postal address. Email checks also measure readable text
against images: mostly-image mail is a filtering and accessibility risk, and image-only mail fails.

    python3 engine/render/template_lint.py --dialect braze path/to/email.html
    python3 engine/render/template_lint.py --dialect klaviyo --email - < email.html
"""
from __future__ import annotations

if __package__ in (None, ""):  # run as a file (python3 engine/render/template_lint.py): make the repository importable
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse
import json
import re
import sys
from html import unescape as html_unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DIALECTS = ("braze", "customerio", "customerio_legacy", "klaviyo", "hubl", "ampscript", "handlebars", "liquid")
GMAIL_CLIP_BYTES = 102 * 1024

CIO_TAGS = ("unsubscribe_url", "unsubscribe", "manage_subscription_preferences_url", "tracking_consent_url", "subscription_topic_name",
            "view_in_browser_url", "cio_link_id", "cio_link", "render_liquid", "countdown", "generate_uuid")
CIO_FILTERS = {"where_exp", "group_by_exp", "json_array_uniq", "array_to_sentence_string", "format_number", "rounded_currency",
               "add_day", "add_month", "add_year", "subtract_day", "subtract_month", "subtract_year", "timezone", "date_time", "titlecase"}
BRAZE_FILTERS = {"number_with_delimiter", "json_escape", "property_accessor", "time_zone", "url_param_escape", "md5", "sha1", "sha2",
                 "hmac_sha1", "hmac_sha256", "base64_encode", "number_to_currency", "url_escape"}
KLAVIYO_FILTERS = ("lookup", "currency_format", "missing_product_image", "trim_slash", "find_replace", "format_date_string", "base64_encode",
                   "hash_md5", "hash_sha256", "split", "percentage", "list_to_sentence")
KLAVIYO_SIMPLE_TAGS = ("unsubscribe", "unsubscribe_link", "manage_preferences", "manage_preferences_link", "web_view", "web_view_link",
                       "today", "coupon_code", "subscribe_link")
KLAVIYO_BLOCK_TAGS = ("catalog",)
HUBL_SIMPLE_TAGS = ("module", "require_css", "require_js", "require_head", "email_simple_subscription", "global_module", "widget_block_end")
HUBL_BLOCK_TAGS = ("dnd_area", "dnd_section", "dnd_column", "dnd_row", "require_css_block", "widget_block")

UNSUBSCRIBE_TOKENS = {
    "braze": [r"\$\{set_user_to_unsubscribed_url\}", r"\$\{email_footer\}"],
    "customerio": [r"\{%-?\s*unsubscribe(_url)?\s*-?%\}", r"manage_subscription_preferences_url"],
    "customerio_legacy": [r"\{%-?\s*unsubscribe(_url)?\s*-?%\}", r"manage_subscription_preferences_url"],
    "klaviyo": [r"\{%-?\s*unsubscribe(_link)?\b", r"\{%-?\s*manage_preferences(_link)?\b"],
    "hubl": [r"\bunsubscribe_link\b", r"\bunsubscribe_section\b"],
    "ampscript": [r"%%unsub_center_url%%", r"%%profile_center_url%%", r"%%subscription_center_url%%", r"unsub_center_url", r"profile_center_url"],
    # Iterable's built-in merge tags (support.iterable.com/hc/articles/206514205, Handlebars Reference: Built-In Merge Tags)
    "handlebars": [r"\{\{\{?~?\s*(unsubscribeUrl|unsubscribeMessageTypeUrl|hostedUnsubscribeUrl)\s*~?\}?\}\}"],
    "liquid": [r"unsubscribe"],
}
ADDRESS_TOKENS = {
    "klaviyo": r"organization\.(full_address|street_address)",
    "hubl": r"site_settings\.company_street_address_1",
    "ampscript": r"member_addr",
    "braze": r"\$\{email_footer\}",
    "handlebars": r"\{\{\{?~?\s*physicalAddress\s*~?\}?\}\}",   # set on Iterable's Project Settings page (same source)
}
BRAZE_SYSTEM_ATTRS = ("set_user_to_unsubscribed_url", "set_user_to_subscribed_url", "set_user_to_opted_in_url", "email_footer",
                      "user_id", "braze_id", "date_of_birth")  # always present, or system links: no fallback needed
SHORTENERS = re.compile(r"https?://(bit\.ly|tinyurl\.com|t\.co|goo\.gl|ow\.ly|is\.gd|buff\.ly)/", re.I)


class LintResult(Dict[str, Any]):
    pass


def _result(dialect: str) -> LintResult:
    r = LintResult()
    r.update({"dialect": dialect, "errors": [], "warnings": [], "outputs": 0})
    return r


# ----------------------------------------------------------------------------------------------
# Liquid family
# ----------------------------------------------------------------------------------------------

def _liquid_env():
    from liquid import Environment
    return Environment()


def _strip_platform_tags(src: str, dialect: str, r: LintResult) -> str:
    if dialect == "braze":
        def cc(m: re.Match) -> str:
            url = m.group(1)
            if not (url.startswith(("http://", "https://")) or "{{" in url or url.startswith("__braze_attr__")):
                r["errors"].append(f"connected_content needs an http(s) URL as its first argument, got {url!r}")
            return ""
        src = re.sub(r"\{%-?\s*connected_content\s+(\S+)(.*?)-?%\}", cc, src, flags=re.S)

        def ab(m: re.Match) -> str:
            arg = m.group(1)
            if arg is None:
                r["errors"].append("abort_message must be written abort_message() or abort_message('reason')")
            elif arg.strip() and not re.fullmatch(r"\s*(['\"])[^'\"{}%]*\1\s*", arg):
                r["errors"].append("abort_message takes one plain-text string; Liquid is not allowed inside the reason")
            return ""
        src = re.sub(r"\{%-?\s*abort_message(?:\((.*?)\))?\s*-?%\}", ab, src, flags=re.S)
    if dialect in ("customerio", "customerio_legacy"):
        for tag in CIO_TAGS:
            src = re.sub(r"\{%-?\s*" + tag + r"\b[^%]*-?%\}", "", src)
    return src


# ${name}: a Braze attribute. Names may hold spaces (custom attributes) but never { } or %, so a literal dollar sign before
# Liquid (${{ price }} or ${% if %}) is left for the Liquid parser instead of being swallowed as an attribute.
BRAZE_ATTR = re.compile(r"\$\{([^{}%\n]*)\}")


def _braze_attrs(src: str, r: LintResult) -> str:
    for m in BRAZE_ATTR.finditer(src):
        if not m.group(1).strip():
            r["errors"].append("empty Braze attribute ${}")
    if re.search(r"\$\{[^{}%\n]*$", src, re.M):
        r["errors"].append("unterminated Braze attribute: '${' without '}'")
    src = re.sub(r"\$\{(" + "|".join(BRAZE_SYSTEM_ATTRS) + r")\}", "__braze_sys__", src)
    src = BRAZE_ATTR.sub("__braze_attr__", src)
    # Braze's documented idiom puts an output inside a tag: {% for item in {{context.${products}}} %}. Its Liquid accepts it;
    # standard Liquid does not, so the inner braces are dropped before parsing (braze.com/docs, ecommerce_use_cases).
    return re.sub(r"(\{%-?[^%]*?)\{\{\s*([^{}]+?)\s*\}\}([^%]*?-?%\})", r"\1\2\3", src)


def _fallback_warnings_liquid(src: str, r: LintResult, default_pattern: str = r"\|\s*default\b") -> None:
    loop_vars = set(re.findall(r"\{%-?\s*for\s+(\w+)\s+in\b", src))
    assigned = set(re.findall(r"\{%-?\s*(?:assign|capture)\s+(\w+)", src))
    guarded = set(re.findall(r"\{%-?\s*(?:if|unless|elsif)\s+([\w.]+)", src))
    for m in re.finditer(r"\{\{-?(.*?)-?\}\}", src, re.S):
        expr = m.group(1).strip()
        r["outputs"] += 1
        if re.search(default_pattern, expr) or expr.startswith(("'", '"')) or not expr:
            continue
        root = re.split(r"[\s|.\[]", expr, maxsplit=1)[0]
        head = re.split(r"\s*\|", expr, maxsplit=1)[0].strip()
        if root in loop_vars or root in assigned or head in guarded or root in guarded:
            continue
        if root == "__braze_sys__":
            continue
        r["warnings"].append(f"output {{{{ {expr} }}}} has no fallback; add a default or guard it with an if, or a missing value renders as an empty string")


def lint_liquid(src: str, dialect: str) -> LintResult:
    r = _result(dialect)
    original = src
    if dialect == "braze":
        src = _braze_attrs(src, r)
    src = _strip_platform_tags(src, dialect, r)
    if dialect == "customerio_legacy" and re.search(r"\|\s*default\b", src):
        r["errors"].append("legacy Customer.io Liquid has no default filter; use {% if x != blank %}...{% else %}...{% endif %} or upgrade the workspace to latest Liquid")
    env = _liquid_env()
    try:
        tpl = env.from_string(src)
    except Exception as e:  # python-liquid raises LiquidSyntaxError and friends
        msg = str(e).splitlines()[0]
        hint = " (if this is a platform tag, check its exact name in the platform docs)" if "unexpected tag" in msg else ""
        r["errors"].append(f"Liquid syntax: {msg}{hint}")
        return r
    known = set(env.filters.keys()) | (CIO_FILTERS if dialect.startswith("customerio") else set()) | (BRAZE_FILTERS if dialect == "braze" else set())
    try:
        used = set(tpl.analyze().filters.keys())
    except Exception:  # analysis is best effort
        used = set()
    for f in sorted(used - known):
        r["warnings"].append(f"unknown filter '{f}': not a Shopify Liquid filter{' or a documented ' + dialect + ' filter' if dialect != 'liquid' else ''}; it will error or pass through at send time")
    # legacy Customer.io has no default filter, so nothing counts as a default there (the pattern never matches)
    _fallback_warnings_liquid(src, r, r"\|\s*default\b" if dialect != "customerio_legacy" else r"(?!x)x")
    if dialect == "customerio_legacy":
        r["warnings"] = [w for w in r["warnings"] if "has no fallback" not in w] + [
            w.replace("add a default or guard it with an if", "guard it with an if/else (legacy Liquid has no default filter)")
            for w in r["warnings"] if "has no fallback" in w]
    if dialect == "braze":
        r["warnings"] = [w.replace("__braze_attr__", "${...}") for w in r["warnings"]]
    _ = original
    return r


# ----------------------------------------------------------------------------------------------
# Klaviyo (Django-style)
# ----------------------------------------------------------------------------------------------

_DJANGO_ENGINE = None


def _klaviyo_engine():
    global _DJANGO_ENGINE
    if _DJANGO_ENGINE is not None:
        return _DJANGO_ENGINE
    import types
    import django
    from django.conf import settings
    if not settings.configured:
        settings.configure(TEMPLATES=[{"BACKEND": "django.template.backends.django.DjangoTemplates"}], USE_I18N=False)
        django.setup()
    from django import template as djt
    from django.template import Engine

    lib = djt.Library()
    for name in KLAVIYO_FILTERS:
        lib.filter(name, lambda value, arg=None: value)

    class _Noop(djt.Node):
        def render(self, context):
            return ""

    def simple(parser, token):
        return _Noop()

    def block_factory(end):
        def block(parser, token):
            parser.parse((end,))
            parser.delete_first_token()
            return _Noop()
        return block

    for name in KLAVIYO_SIMPLE_TAGS:
        lib.tag(name, simple)
    for name in KLAVIYO_BLOCK_TAGS:
        lib.tag(name, block_factory("end" + name))
    mod = types.ModuleType("lmm_klaviyo_stub")
    mod.register = lib
    sys.modules["lmm_klaviyo_stub"] = mod
    _DJANGO_ENGINE = Engine(builtins=["lmm_klaviyo_stub"])
    return _DJANGO_ENGINE


def lint_klaviyo(src: str) -> LintResult:
    r = _result("klaviyo")
    from django.template.exceptions import TemplateSyntaxError
    eng = _klaviyo_engine()
    probe = src
    for _ in range(25):
        try:
            eng.from_string(probe)
            break
        except TemplateSyntaxError as e:
            msg = str(e)
            m = re.search(r"Invalid filter: '([^']+)'", msg)
            if m:
                r["warnings"].append(f"unknown filter '{m.group(1)}': not a Django built-in or a documented Klaviyo filter; confirm it exists in Klaviyo")
                probe = re.sub(r"\|\s*" + re.escape(m.group(1)) + r"(:'[^']*'|:\"[^\"]*\"|:\S+)?", "", probe)
                continue
            r["errors"].append(f"Klaviyo template syntax: {msg.splitlines()[0]}")
            return r
    loop_vars = set(re.findall(r"\{%-?\s*for\s+(\w+)\s+in\b", src))
    guarded = set(re.findall(r"\{%-?\s*(?:if|elif)\s+([\w.|:' ]+?)\s*%\}", src))
    for m in re.finditer(r"\{\{(.*?)\}\}", src, re.S):
        expr = m.group(1).strip()
        r["outputs"] += 1
        root = re.split(r"[\s|.]", expr, maxsplit=1)[0]
        if "default:" in expr.replace(" ", "") or root in loop_vars or root == "organization" or expr in guarded or not expr:
            continue
        r["warnings"].append(f"output {{{{ {expr} }}}} has no fallback; add |default:'...' so a missing value does not render empty")
    return r


# ----------------------------------------------------------------------------------------------
# HubL
# ----------------------------------------------------------------------------------------------

def lint_hubl(src: str) -> LintResult:
    r = _result("hubl")
    import jinja2
    from jinja2.ext import Extension

    class HubLTags(Extension):
        tags = set(HUBL_SIMPLE_TAGS) | set(HUBL_BLOCK_TAGS)

        def parse(self, parser):
            tok = next(parser.stream)
            while parser.stream.current.type != "block_end":
                next(parser.stream)
            if tok.value in HUBL_BLOCK_TAGS:
                parser.parse_statements(("name:end_" + tok.value,), drop_needle=True)
            return jinja2.nodes.Output([], lineno=tok.lineno)

    env = jinja2.Environment(extensions=[HubLTags])
    # HubL has unless/endunless; Jinja does not, so check them as the if-not they mean
    probe = re.sub(r"\{%(-?)\s*unless\s+(.*?)\s*(-?)%\}", r"{%\1 if not (\2) \3%}", src, flags=re.S)
    probe = re.sub(r"\{%(-?)\s*endunless\s*(-?)%\}", r"{%\1 endif \2%}", probe)
    try:
        env.parse(probe)
    except jinja2.TemplateSyntaxError as e:
        r["errors"].append(f"HubL syntax (line {e.lineno}): {e.message}")
        return r
    guarded = set(re.findall(r"\{%-?\s*(?:if|elif|unless)\s+([\w.]+)", src))
    for m in re.finditer(r"\{\{(.*?)\}\}", src, re.S):
        expr = m.group(1).strip()
        r["outputs"] += 1
        if re.match(r"(contact|company)\.", expr) and "default" not in expr and expr.split("|")[0].strip() not in guarded:
            r["warnings"].append(f"output {{{{ {expr} }}}} has no fallback; use personalization_token(\"{expr.split('|')[0].strip()}\", \"<fallback>\")")
        if expr.startswith("personalization_token(") and not re.search(r"personalization_token\(\s*['\"][^'\"]+['\"]\s*,\s*['\"]", expr):
            r["warnings"].append(f"{{{{ {expr} }}}} sets no default; pass a second argument")
    return r


# ----------------------------------------------------------------------------------------------
# AMPscript (structural)
# ----------------------------------------------------------------------------------------------

def lint_ampscript(src: str) -> LintResult:
    r = _result("ampscript")
    if src.count("%%[") != src.count("]%%"):
        r["errors"].append(f"unbalanced block delimiters: {src.count('%%[')} '%%[' vs {src.count(']%%')} ']%%'")
    if src.count("%%=") != src.count("=%%"):
        r["errors"].append(f"unbalanced inline delimiters: {src.count('%%=')} '%%=' vs {src.count('=%%')} '=%%'")
    blocks = re.findall(r"%%\[(.*?)\]%%", src, re.S) + re.findall(r"<script[^>]*language=[\"']ampscript[\"'][^>]*>(.*?)</script>", src, re.S | re.I)
    inline = re.findall(r"%%=(.*?)=%%", src, re.S)
    r["outputs"] = len(inline)
    code = "\n".join(blocks)
    no_strings = _ampscript_strip_strings(code, r)
    stack: List[str] = []
    # keywords, never variables: @next and @if are legal variable names
    for tok in re.findall(r"(?<![@\w])(IF|ELSEIF|ELSE|ENDIF|FOR|NEXT)\b", no_strings, re.I):
        t = tok.upper()
        if t in ("IF", "FOR"):
            stack.append(t)
        elif t in ("ELSEIF", "ELSE"):
            if not stack or stack[-1] != "IF":
                r["errors"].append(f"{t} outside an IF block")
        elif t == "ENDIF":
            if not stack or stack.pop() != "IF":
                r["errors"].append("ENDIF without a matching IF")
        elif t == "NEXT":
            if not stack or stack.pop() != "FOR":
                r["errors"].append("NEXT without a matching FOR")
    for open_ in stack:
        r["errors"].append(f"{open_} block never closed ({'ENDIF' if open_ == 'IF' else 'NEXT'} missing)")
    for m in re.finditer(r"(?<![@\w])SET\s+([^\s=]+)\s*=", no_strings, re.I):
        if not m.group(1).startswith("@"):
            r["errors"].append(f"SET target {m.group(1)!r} must be a variable starting with @")
    if no_strings.count("(") != no_strings.count(")"):
        r["errors"].append("unbalanced parentheses in AMPscript code")
    declared = {v.lower() for v in re.findall(r"(?<![@\w])VAR\s+((?:@\w+\s*,?\s*)+)", no_strings, re.I) for v in re.findall(r"@\w+", v)}
    used = {v.lower() for v in re.findall(r"@\w+", no_strings + "\n".join(inline))}
    guarded = {v.lower() for v in re.findall(r"(?:Empty|IsNull)\s*\(\s*(@\w+)", no_strings + "\n".join(inline), re.I)}
    for m in re.finditer(r"(?:v|Output)\s*\(\s*(@\w+)\s*\)", "\n".join(inline) + "\n" + no_strings, re.I):
        if m.group(1).lower() not in guarded:
            r["warnings"].append(f"{m.group(1)} is output without an Empty() or IsNull() guard; set a fallback value first")
    if declared and used - declared:
        r["warnings"].append(f"variables used but not declared with VAR: {sorted(used - declared)}")
    if re.search(r"\[[A-Za-z_ ]+\]", no_strings) and "attributevalue" not in no_strings.lower():
        r["warnings"].append("attributes referenced as [Name] error when the field is missing from the sendable data extension; AttributeValue(\"Name\") returns empty instead")
    return r


def _ampscript_strip_strings(code: str, r: LintResult) -> str:
    out, i, n = [], 0, len(code)
    while i < n:
        c = code[i]
        if c in ("'", '"'):
            j = code.find(c, i + 1)
            if j == -1:
                r["errors"].append("unterminated string literal")
                break
            out.append('""')
            i = j + 1
        elif code.startswith("/*", i):
            j = code.find("*/", i + 2)
            i = n if j == -1 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


# ----------------------------------------------------------------------------------------------
# Handlebars (Iterable), structural
# ----------------------------------------------------------------------------------------------

# Iterable built-in merge tags that are always filled by the platform, so they need no fallback
# (support.iterable.com/hc/articles/206514205).
HANDLEBARS_SYSTEM = {"unsubscribeUrl", "unsubscribeMessageTypeUrl", "hostedUnsubscribeUrl", "unsubscribeByPhoneUrl", "physicalAddress",
                     "companyName", "viewInBrowserUrl", "email", "this", "@index", "@first", "@last", "@key"}
HB_TOKEN = re.compile(r"\{\{\{(.*?)\}\}\}|\{\{!--(.*?)--\}\}|\{\{(.*?)\}\}", re.S)
HB_STRING = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'')


def _hb_args(expr: str, r: LintResult) -> List[str]:
    """Split a Handlebars expression into words, keeping quoted strings and [bracketed names] whole."""
    out, i, n = [], 0, len(expr)
    while i < n:
        c = expr[i]
        if c.isspace():
            i += 1
            continue
        if c in ("'", '"'):
            j = i + 1
            while j < n and expr[j] != c:
                j += 2 if expr[j] == "\\" else 1
            if j >= n:
                r["errors"].append(f"unterminated string in {{{{{expr.strip()}}}}}")
                return out
            out.append(expr[i:j + 1])
            i = j + 1
            continue
        j = i
        while j < n and not expr[j].isspace():
            if expr[j] == "[":
                k = expr.find("]", j)
                if k == -1:
                    r["errors"].append(f"unclosed [bracketed name] in {{{{{expr.strip()}}}}}")
                    return out
                j = k + 1
                continue
            j += 1
        out.append(expr[i:j])
        i = j
    return out


def lint_handlebars(src: str) -> LintResult:
    r = _result("handlebars")
    stack: List[Tuple[str, List[str]]] = []          # (helper name, its arguments)
    rest = HB_TOKEN.sub(" ", src)
    if "{{" in rest or "}}" in rest:
        r["errors"].append("unbalanced Handlebars braces: a '{{' without '}}' (or the reverse)")
    for m in HB_TOKEN.finditer(src):
        if m.group(2) is not None:                     # {{!-- comment --}}
            continue
        raw = m.group(1) if m.group(1) is not None else m.group(3)
        expr = raw.strip().strip("~").strip()
        if "{{" in expr or "}}" in expr:
            r["errors"].append(f"nested braces inside a Handlebars tag: {m.group(0)[:60]}")
            continue
        if not expr:
            r["errors"].append("empty Handlebars tag {{}}")
            continue
        if expr.startswith("!"):
            continue
        head = expr[0]
        if head in "#^":
            words = _hb_args(expr[1:], r)
            if not words:
                r["errors"].append(f"block tag without a helper name: {m.group(0)}")
                continue
            stack.append((words[0], words[1:]))
            if words[0] in ("if", "unless", "each", "with") and len(words) < 2:
                r["errors"].append(f"{{{{#{words[0]}}}}} needs an argument")
            continue
        if head == "/":
            name = expr[1:].strip()
            if not stack:
                r["errors"].append(f"{{{{/{name}}}}} closes a block that was never opened")
            elif stack[-1][0] != name:
                r["errors"].append(f"{{{{/{name}}}}} closes {{{{#{stack[-1][0]}}}}}: blocks must close in order")
                stack.pop()
            else:
                stack.pop()
            continue
        if expr == "else" or expr.startswith("else "):
            if not stack:
                r["errors"].append("{{else}} outside a block helper")
            continue
        if head == ">":
            r["warnings"].append(f"partial {m.group(0)[:40]}: Iterable reuses content through snippets, not partials; confirm it renders")
            continue
        words = _hb_args(expr, r)
        if not words:
            continue
        r["outputs"] += 1
        if words[0] == "defaultIfEmpty":
            if len(words) != 3:
                r["errors"].append(f"defaultIfEmpty takes a field and a fallback: {m.group(0)}")
            continue
        if len(words) > 1:
            continue                                   # another helper call: its own arguments decide the output
        path = words[0]
        if HB_STRING.fullmatch(path) or re.fullmatch(r"-?\d+(\.\d+)?", path):
            continue
        root = re.split(r"[.\[]", path, maxsplit=1)[0] or path
        scoped = any(name in ("each", "with") for name, _ in stack)
        guarded = any(name in ("if", "unless") and args and args[0] in (path, root) for name, args in stack)
        if path in HANDLEBARS_SYSTEM or root in HANDLEBARS_SYSTEM or root.startswith(("@", "../")) or scoped or guarded:
            continue
        r["warnings"].append(f"output {{{{{path}}}}} has no fallback; write {{{{defaultIfEmpty {path} \"...\"}}}} or guard it with {{{{#if {path}}}}}")
    for name, _ in reversed(stack):
        r["errors"].append(f"{{{{#{name}}}}} block never closed ({{{{/{name}}}}} missing)")
    return r


# ----------------------------------------------------------------------------------------------
# email HTML checks
# ----------------------------------------------------------------------------------------------

class _Html(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.imgs_no_alt = 0
        self.imgs = 0
        self.links: List[str] = []
        self.has_color_scheme = False
        self.open: List[str] = []
        self.mismatched: List[str] = []
        self.text: List[str] = []

    VOID = {"img", "br", "hr", "meta", "link", "input", "col", "area", "base", "source", "wbr"}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "img":
            self.imgs += 1
            if "alt" not in a:
                self.imgs_no_alt += 1
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        if tag == "meta" and (a.get("name") or "").lower() in ("color-scheme", "supported-color-schemes"):
            self.has_color_scheme = True
        if tag not in self.VOID:
            self.open.append(tag)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if tag in self.open:
            while self.open and self.open[-1] != tag:
                self.mismatched.append(self.open.pop())
            self.open.pop()
        else:
            self.mismatched.append(f"/{tag}")


class _Visible(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.hidden_depth: List[bool] = []
        self.text: List[str] = []
        self.images = 0
        self.images_no_alt = 0

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        a = dict(attrs)
        if tag in ("style", "script", "head", "title"):
            self.skip += 1
        if tag == "img":
            self.images += 1
            if "alt" not in a:
                self.images_no_alt += 1
        style = (a.get("style") or "").replace(" ", "").lower()
        if tag in ("div", "span", "td", "p") and ("display:none" in style or "max-height:0" in style):
            self.skip += 1
            self.hidden_depth.append(True)
        elif tag in ("div", "span", "td", "p"):
            self.hidden_depth.append(False)

    def handle_endtag(self, tag):  # noqa: ANN001
        if tag in ("style", "script", "head", "title"):
            self.skip = max(0, self.skip - 1)
        if tag in ("div", "span", "td", "p") and self.hidden_depth:
            if self.hidden_depth.pop():
                self.skip = max(0, self.skip - 1)

    def handle_data(self, data):  # noqa: ANN001
        if not self.skip:
            self.text.append(data)


def text_to_image(html_src: str) -> Dict[str, Any]:
    """Readable text against images. Logic tags are stripped; an output tag (a merge field such as a product name or the
    first name) renders as words, so it counts as a short word of text (10 characters, less than any real value), never
    as an image and never as nothing."""
    src = re.sub(r"<!--.*?-->", "", html_src, flags=re.S)
    src = re.sub(r"\{%.*?%\}|%%\[.*?\]%%", " ", src, flags=re.S)
    src = re.sub(r"\{\{~?\s*(?:[#/^!>]|else\b).*?\}\}", " ", src, flags=re.S)     # Handlebars block, comment and else tags are logic
    src = re.sub(r"\{\{.*?\}\}\}?|%%=.*?=%%|%%\w+%%", " mergefield ", src, flags=re.S)
    p = _Visible()
    p.feed(src)
    text = re.sub(r"\s+", " ", html_unescape(" ".join(p.text))).strip()
    chars = len(text)
    result: Dict[str, Any] = {"visible_text_chars": chars, "images": p.images, "images_without_alt": p.images_no_alt,
                              "chars_per_image": None if not p.images else round(chars / p.images), "errors": [], "warnings": []}
    if p.images and chars < 200:
        result["errors"].append(f"image-only email: {chars} characters of readable text with {p.images} image(s). Filters cannot read it and "
                                "recipients with images off see nothing; put the message in live text")
    elif p.images and chars / p.images < 250:
        result["warnings"].append(f"image-heavy: about {chars // p.images} characters of text per image; keep the message readable with images off")
    if p.images_no_alt:
        result["errors"].append(f"{p.images_no_alt} image(s) without alt text")
    return result


def email_checks(src: str, dialect: str, r: LintResult) -> None:
    size = len(src.encode("utf-8"))
    r["bytes"] = size
    if size > GMAIL_CLIP_BYTES:
        r["errors"].append(f"{size:,} bytes: over Gmail's ~102 KB clip limit; the unsubscribe link and footer will be hidden")
    elif size > 90 * 1024:
        r["warnings"].append(f"{size:,} bytes: within 12 KB of Gmail's clip limit once the ESP adds tracking")
    h = _Html()
    h.feed(re.sub(r"<!--.*?-->", "", src, flags=re.S))
    if h.imgs_no_alt:
        r["errors"].append(f"{h.imgs_no_alt} of {h.imgs} images have no alt attribute (use alt=\"\" for decorative images)")
    unclosed = [t for t in h.open if t not in ("html", "body", "head", "p", "li", "td", "tr")]
    if unclosed or [m for m in h.mismatched if m.startswith("/")]:
        r["warnings"].append(f"HTML nesting: unclosed {unclosed[:5]} mismatched {h.mismatched[:5]}")
    if not h.has_color_scheme:
        r["warnings"].append("no <meta name=\"color-scheme\">: dark-mode clients will invert colors on their own terms")
    if not re.search(r"preheader|preview", src, re.I):
        r["warnings"].append("no preheader: clients will show the first body text (often 'View in browser') as the preview")
    if not any(re.search(p, src, re.I) for p in UNSUBSCRIBE_TOKENS.get(dialect, [])):
        r["errors"].append(f"no {dialect} unsubscribe token found: every marketing email needs a working unsubscribe link")
    addr = ADDRESS_TOKENS.get(dialect)
    if addr and not re.search(addr, src, re.I) and not re.search(r"\d+\s+\w+.*\b(St|Street|Ave|Avenue|Rd|Road|Blvd|Suite|Way|Dr|Drive)\b", src):
        r["warnings"].append("no postal address found (token or literal): CAN-SPAM requires a physical address")
    tti = text_to_image(src)
    r["text_to_image"] = {k: tti[k] for k in ("visible_text_chars", "images", "chars_per_image")}
    r["errors"] += [e for e in tti["errors"] if "without alt" not in e]       # alt text is reported above
    r["warnings"] += tti["warnings"]
    for href in h.links:
        if SHORTENERS.match(href):
            r["warnings"].append(f"public link shortener {href[:40]}: a deliverability liability; link to your own domain")


def lint(src: str, dialect: str, email: bool = False) -> LintResult:
    if dialect not in DIALECTS:
        raise ValueError(f"dialect must be one of {DIALECTS}")
    if dialect in ("braze", "customerio", "customerio_legacy", "liquid"):
        r = lint_liquid(src, dialect)
    elif dialect == "klaviyo":
        r = lint_klaviyo(src)
    elif dialect == "hubl":
        r = lint_hubl(src)
    elif dialect == "handlebars":
        r = lint_handlebars(src)
    else:
        r = lint_ampscript(src)
    if email:
        email_checks(src, dialect, r)
    r["ok"] = not r["errors"]
    return r


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Lint a template in a named ESP dialect")
    ap.add_argument("--dialect", required=True, choices=DIALECTS)
    ap.add_argument("--email", action="store_true", help="also run marketing-email HTML checks")
    ap.add_argument("path", help="file path, or - for stdin")
    a = ap.parse_args(argv)
    src = sys.stdin.read() if a.path == "-" else Path(a.path).read_text(encoding="utf-8")
    r = lint(src, a.dialect, a.email)
    print(json.dumps(r, indent=2))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(_cli())
