"""MJML rendering: one email's words plus a brand kit in, the MJML source and the compiled, client-proof HTML out.

Why MJML: hand-written email HTML drifts. MJML (https://mjml.io) describes an email in a few components and
compiles them to the table layout, Outlook conditional comments (<!--[if mso | IE]> tables that hold the grid in
Outlook for Windows) and responsive media queries that email clients need. The compiler here is the `mjml` package
on PyPI (mjml-python, pure Python, pinned in requirements.txt), so rendering stays offline and deterministic.

How one email is built:

  1. engine/agents/copywriter.py renders the words into the platform's dialect (Customer.io or Braze Liquid,
     Klaviyo, HubL, AMPscript) with a fallback on every token, and hands them over as an EmailContent.
  2. build_mjml() fills the components in templates/email/mjml/ with those words and the brand kit's logo,
     colors, fonts and footer (engine/render/brand.py; the neutral default kit when there is none).
  3. compile_mjml() compiles the MJML. ESP template tags must come out exactly as they went in, so before
     compiling every tag ({{ }}, {% %}, {# #}, %%[ ]%%, %%= =%%, %%name%%) and every character reference in the
     title and body (&amp;, &#8204;) is swapped for an opaque placeholder, and restored afterwards. Without that the
     compiler turns '>' inside a Liquid condition into '&gt;', breaks any attribute whose tag holds a double quote
     (Customer.io's default: "there", HubL's personalization_token("...")), decodes &amp; in links to a bare '&',
     and turns the preheader spacers into raw invisible characters. A tag that holds a double quote and lands in
     an attribute is restored inside single quotes. A tag the compiler drops (bare text between elements) is an
     error, not a silent loss. Text before the doctype (the dialect comments and the AMPscript declaration block)
     travels as <mj-raw position="file-start">, the way official MJML places it, and is prepended after compiling.
     Tags may sit in text and inside attribute values; a tag between a start tag's attributes (an if around an
     attribute) is not supported, because the parser would read it as an attribute name.

Usage:

    from engine.render.mjml_render import EmailContent, render
    mjml_source, html = render(content, brand)          # brand: engine.render.brand.load_brand(...)

    # preview a sample email for a kit and a dialect (writes the .html, .mjml and .txt side by side)
    python3 engine/render/mjml_render.py --brand examples/brands/ridgeview_saas/brand.yaml --dialect klaviyo
    python3 engine/render/mjml_render.py --dialect all --out-dir /tmp/previews
    python3 engine/render/mjml_render.py --compile my_email.mjml          # compile hand-edited MJML
    python3 engine/render/mjml_render.py --write-golden                   # refresh templates/email/compiled/
"""
from __future__ import annotations

if __package__ in (None, ""):  # run as a file (python3 <path>): make the repository importable
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse
import html as html_mod
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from engine.paths import DEMO, TEMPLATES
from engine.render.brand import DEFAULT_BRAND, BrandKit, BrandKitError, find_brand, load_brand

MJML_DIR = TEMPLATES / "email" / "mjml"
GOLDEN_DIR = TEMPLATES / "email" / "compiled"
GOLDEN_BRAND = DEMO / "brand.yaml"
DIALECTS = ("customerio", "braze", "klaviyo", "hubl", "ampscript")
MJML_PACKAGE_VERSION = "0.12.0"    # the mjml-python release the golden files were compiled with (requirements.txt)
PREHEADER_PADDING = "&#8204;&#160;" * 16


class MjmlRenderError(RuntimeError):
    """The MJML could not be built or compiled faithfully (a missing placeholder, a template tag the compiler dropped)."""


@dataclass(frozen=True)
class EmailContent:
    """One email's words, already rendered into the platform dialect: HTML-escaped text with the ESP's tags in place.

    file_start is placed before <!doctype html> (the dialect comments, and the AMPscript declaration block that has
    to run before the first use of a variable). product_items fills the product block slot; None keeps it a no-op."""
    dialect: str
    title: str
    preheader: str
    headline: str
    paragraphs: Tuple[str, ...]
    cta_label: str
    cta_href: str
    sender_line: str
    address_line: str
    unsubscribe: str
    extra_lines: Tuple[str, ...] = ()
    file_start: str = ""
    product_items: Optional[str] = None


# ----------------------------------------------------------------------------------------------
# building the MJML document
# ----------------------------------------------------------------------------------------------

_DOC_COMMENTS = re.compile(r"\A(?:\s*<!--(?!\[if).*?-->[ \t]*\n)+", re.S)
_PLACEHOLDER = re.compile(r"\[\[\s*([a-z_]+)\s*\]\]")
_LONE_PLACEHOLDER = re.compile(r"[ \t]*\[\[\s*([a-z_]+)\s*\]\][ \t]*")


@lru_cache(maxsize=None)
def component(name: str) -> str:
    """A component from templates/email/mjml/, without its leading documentation comment."""
    path = MJML_DIR / f"{name}.mjml"
    if not path.is_file():
        raise MjmlRenderError(f"missing MJML component {path}")
    return _DOC_COMMENTS.sub("", path.read_text(encoding="utf-8")).rstrip("\n")


def fill(template: str, values: Mapping[str, str]) -> str:
    """Replace [[name]] placeholders in one pass (inserted values are never rescanned).

    A placeholder alone on its line with an empty value removes the line, so optional parts leave no blank lines
    behind; a value that starts with whitespace is a block (a filled component) carrying its own indentation and
    takes the whole line."""
    def get(name: str) -> str:
        if name not in values:
            raise MjmlRenderError(f"no value for the MJML placeholder [[{name}]]")
        return values[name]

    out: List[str] = []
    for line in template.split("\n"):
        lone = _LONE_PLACEHOLDER.fullmatch(line)
        if lone:
            value = get(lone.group(1))
            if value[:1] in (" ", "\t"):
                out.append(value)
            elif value:
                out.append(_PLACEHOLDER.sub(lambda m: value, line))
            continue
        out.append(_PLACEHOLDER.sub(lambda m: get(m.group(1)), line))
    return "\n".join(out)


def _attr(value: str) -> str:
    return html_mod.escape(value, quote=True)


def build_mjml(content: EmailContent, brand: BrandKit = DEFAULT_BRAND) -> str:
    """The MJML source for one email: the components in templates/email/mjml/ filled from the content and the kit."""
    light, dark, fonts = brand.light, brand.dark, brand.fonts
    family = html_mod.escape(fonts.family, quote=False).replace('"', "&quot;")   # single quotes stay readable in style attributes
    colors = {"primary": light.primary, "button_text": light.button_text, "text": light.text, "background": light.background,
              "card": light.card, "link": light.link, "dark_primary": dark.primary, "dark_button_text": dark.button_text,
              "dark_text": dark.text, "dark_background": dark.background, "dark_card": dark.card, "dark_link": dark.link}

    header = ""
    if brand.logo:
        extra = (f' height="{brand.logo.height}px"' if brand.logo.height else "") + (f' href="{_attr(brand.website)}"' if brand.website else "")
        header = fill(component("header"), {"card": light.card, "logo_url": _attr(brand.logo.url), "logo_alt": _attr(brand.logo.alt),
                                            "logo_width": str(brand.logo.width), "logo_extra": extra})
    web_font = mso_fonts = ""
    if fonts.web_font_url:
        web_font = f'    <mj-font name="{_attr(fonts.web_font_name or "")}" href="{_attr(fonts.web_font_url)}" />'
        mso_fonts = ("    <mj-raw>\n      <!--[if mso]><style type=\"text/css\">body, table, td, div, p, a, h1 "
                     f"{{ font-family: {fonts.stack} !important; }}</style><![endif]-->\n    </mj-raw>")
    product_block = ""
    if content.product_items and content.product_items.strip():
        product_block = fill(component("product_block"), {"card": light.card, "product_items": content.product_items.rstrip("\n")})
    footer_lines = [content.sender_line, content.address_line, *content.extra_lines]
    if brand.social:
        footer_lines.append(" | ".join(f'<a href="{_attr(s.url)}">{html_mod.escape(s.label)}</a>' for s in brand.social))
    footer_lines.append(content.unsubscribe)

    values: Dict[str, str] = {
        "lang": _attr(brand.lang),
        "file_start": f'  <mj-raw position="file-start">{content.file_start}</mj-raw>' if content.file_start else "",
        "title": content.title,
        "web_font": web_font,
        "mso_fonts": mso_fonts,
        "font_family": family,
        **colors,
        "preheader": fill(component("preheader"), {"preheader": content.preheader, "padding": PREHEADER_PADDING}),
        "header": header,
        "content_padding": "16px 0 0 0" if header else "32px 0 0 0",
        "headline": fill(component("headline"), {"font_family": family, "headline": content.headline}),
        "paragraphs": "\n".join(fill(component("paragraph"), {"text": p}) for p in content.paragraphs),
        "product_block": product_block,
        "button": fill(component("button"), {"href": content.cta_href, "primary": light.primary, "button_text": light.button_text,
                                             "label": content.cta_label}),
        "footer": fill(component("footer"), {"background": light.background, "footer_lines": "<br>\n          ".join(footer_lines)}),
    }
    lead = (f"<!-- dialect: {content.dialect}. MJML source built by engine/render/mjml_render.py from templates/email/mjml/; "
            f"compile it with: python3 engine/render/mjml_render.py compile FILE -->\n")
    return lead + fill(component("base"), values) + "\n"


# ----------------------------------------------------------------------------------------------
# compiling with template tags protected
# ----------------------------------------------------------------------------------------------

_FILE_START = re.compile(r'[ \t]*<mj-raw\s+position=["\']file-start["\']\s*>(.*?)</mj-raw>[ \t]*\n?', re.S)
TEMPLATE_TAG = re.compile(r"%%\[.*?\]%%|%%=.*?=%%|%%[A-Za-z_]\w*%%|\{%.*?%\}|\{\{.*?\}\}(?!\})|\{#.*?#\}", re.S)
_CHAR_REF = re.compile(r"&(?:#[0-9]+|#[xX][0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]*);")
_MARK = "LMMTPL"
_TOKEN = re.compile(_MARK + r"(\d{5})X")
_START_TAG = re.compile(r"<[A-Za-z][^<>]*>")
_ATTR_DQ = re.compile(r'(\s[^\s=<>"\'/]+)="([^"]*)"')
_PROTECTED_SPANS = (re.compile(r"<mj-title\b.*?</mj-title>", re.S), re.compile(r"<mj-body\b.*?</mj-body>", re.S))


def _restore(compiled: str, originals: Sequence[str]) -> str:
    used: Set[int] = set()

    def original(i: int) -> str:
        used.add(i)
        return originals[i]

    def fix_attr(m: re.Match) -> str:
        name, value = m.group(1), m.group(2)
        if _MARK not in value:
            return m.group(0)
        parts = re.split(r"(" + _MARK + r"\d{5}X)", value)
        tags = [originals[int(p[len(_MARK):-1])] for p in parts if _TOKEN.fullmatch(p)]
        quote = '"'
        if any('"' in t for t in tags):
            if any("'" in t for t in tags):
                raise MjmlRenderError(f"a template tag in the {name.strip()} attribute holds both quote characters, so no HTML "
                                      f"quoting can carry it: {[t for t in tags if chr(34) in t or chr(39) in t]}")
            quote = "'"
        out = "".join(original(int(p[len(_MARK):-1])) if _TOKEN.fullmatch(p) else (p.replace("'", "&#39;") if quote == "'" else p)
                      for p in parts)
        return f"{name}={quote}{out}{quote}"

    compiled = _START_TAG.sub(lambda m: _ATTR_DQ.sub(fix_attr, m.group(0)) if _MARK in m.group(0) else m.group(0), compiled)
    compiled = _TOKEN.sub(lambda m: original(int(m.group(1))), compiled)
    lost = [originals[i] for i in range(len(originals)) if i not in used]
    if lost:
        raise MjmlRenderError("the MJML compiler dropped " + ", ".join(repr(t) for t in lost[:5]) +
                              ("" if len(lost) <= 5 else f" and {len(lost) - 5} more") +
                              "; template logic between elements has to sit inside <mj-raw>")
    return compiled


def compile_mjml(source: str) -> str:
    """Compile MJML to HTML with every ESP template tag and character reference restored exactly as written."""
    try:
        from mjml import mjml_to_html   # the mjml-python package
    except ImportError as e:  # pragma: no cover - only without the pinned requirements
        raise MjmlRenderError(f"the mjml package is not installed (pip install mjml=={MJML_PACKAGE_VERSION}, "
                              "or pip install -r requirements.txt)") from e

    starts = [m.group(1) for m in _FILE_START.finditer(source)]
    body = _FILE_START.sub("", source)
    if _MARK in body:
        raise MjmlRenderError(f"the MJML contains the reserved marker {_MARK!r}; rename it")
    originals: List[str] = []

    def protect(m: re.Match) -> str:
        originals.append(m.group(0))
        return f"{_MARK}{len(originals) - 1:05d}X"

    body = TEMPLATE_TAG.sub(protect, body)
    # character references stay verbatim in the title and the body; never inside mj-style or mj-font, where CSS reads them literally
    for span in _PROTECTED_SPANS:
        body = span.sub(lambda m: _CHAR_REF.sub(protect, m.group(0)), body)
    try:
        result = mjml_to_html(body, keep_comments=False)
    except Exception as e:  # noqa: BLE001 - the compiler raises plain ValueError/AssertionError on bad input
        raise MjmlRenderError(f"MJML did not compile: {type(e).__name__}: {e}") from e
    if result.errors:
        raise MjmlRenderError("MJML did not compile: " + "; ".join(map(str, result.errors)))
    return "".join(starts) + _restore(result.html, originals)


def render(content: EmailContent, brand: BrandKit = DEFAULT_BRAND) -> Tuple[str, str]:
    """(MJML source, compiled HTML) for one email."""
    source = build_mjml(content, brand)
    return source, compile_mjml(source)


# ----------------------------------------------------------------------------------------------
# sample email, golden files and CLI
# ----------------------------------------------------------------------------------------------

SAMPLE_FIELDS: Dict[str, Any] = {
    "subject": "{first_name}, your {product_name} setup is saved",
    "preheader": "Pick up where you left off in a couple of minutes",
    "headline": "Everything is where you left it",
    "paragraphs": [
        "Hi {first_name}, your settings and your last session in {product_name} are saved, so there is nothing to set up again.",
        "Most people who come back start with the same thing they did last time. It takes a few minutes, and the button below opens it.",
    ],
    "cta_label": "Open {product_name}",
}


def sample_brief(brand: BrandKit, dialect: str) -> Any:
    """A brief for previewing a kit: the kit's name, sender and address on the platform that writes `dialect`."""
    from engine.models import PLATFORM_DIALECT, Brief
    platform = {v: k for k, v in PLATFORM_DIALECT.items()}[dialect]
    sender = brand.sender("marketing")
    return Brief(product_name=brand.name_or("Ridgeview"), platform=platform, sender_name=sender.from_name if sender else None,
                 postal_address=brand.footer.postal_address, cta_url=(brand.website or "https://example.com").rstrip("/") + "/")


def sample_email(brand: BrandKit, dialect: str, fields: Optional[Dict[str, Any]] = None) -> Tuple[str, str, str]:
    """(MJML, HTML, plain text) of the sample email for a kit in one dialect, through the copywriter's renderer."""
    from engine.agents import copywriter
    return copywriter.render_email_mjml(fields or SAMPLE_FIELDS, sample_brief(brand, dialect), "sample", brand=brand)


def golden_paths(dialect: str) -> Tuple[Path, Path]:
    folder = GOLDEN_DIR / dialect
    return folder / "example.mjml", folder / "example.html"


def write_golden() -> List[Path]:
    """Recompile the committed examples in templates/email/compiled/<dialect>/ with the Ridgeview kit."""
    brand = load_brand(GOLDEN_BRAND)
    written: List[Path] = []
    for d in DIALECTS:
        mjml_source, html, _text = sample_email(brand, d)
        mjml_path, html_path = golden_paths(d)
        mjml_path.parent.mkdir(parents=True, exist_ok=True)
        mjml_path.write_text(mjml_source, encoding="utf-8")
        html_path.write_text(html, encoding="utf-8")
        written += [mjml_path, html_path]
    return written


def _lint_summary(html: str, dialect: str) -> Tuple[bool, str]:
    from engine.render import template_lint
    r = template_lint.lint(html, dialect, email=True)
    msg = f"lint ({dialect}): {'ok' if r['ok'] else 'FAILED'}, {r['bytes']:,} bytes, {r['text_to_image']['images']} image(s)"
    for e in r["errors"]:
        msg += f"\n    error: {e}"
    for w in r["warnings"]:
        msg += f"\n    warning: {w}"
    return r["ok"], msg


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Render a sample email for a brand kit, compile MJML, or refresh the golden files")
    ap.add_argument("command", nargs="?", choices=("render", "compile"), default="render",
                    help="render (default): a sample email for a kit; compile: compile an MJML file to HTML")
    ap.add_argument("file", nargs="?", help="compile: the .mjml file to compile")
    ap.add_argument("--brand", help="brand.yaml (default: the current workspace's kit, or the neutral default)")
    ap.add_argument("--dialect", default="customerio", choices=(*DIALECTS, "all"))
    ap.add_argument("--out", help="render: output .html path (default: lcm_preview_<dialect>.html); compile: default <file>.html")
    ap.add_argument("--out-dir", help="render: folder for the preview files (default: the current folder)")
    ap.add_argument("--compile", dest="compile_path", metavar="FILE", help="same as: compile FILE")
    ap.add_argument("--write-golden", action="store_true", help="recompile templates/email/compiled/ with the Ridgeview kit")
    a = ap.parse_args(argv)

    if a.write_golden:
        for p in write_golden():
            print(f"wrote {p}")
        return 0
    if a.command == "compile" or a.compile_path:
        src_path = Path(a.compile_path or a.file or "")
        if not src_path.is_file():
            print("compile needs an existing .mjml file")
            return 2
        html = compile_mjml(src_path.read_text(encoding="utf-8"))
        out = Path(a.out) if a.out else src_path.with_suffix(".html")
        out.write_text(html, encoding="utf-8")
        print(f"wrote {out}")
        m = re.search(r"<!--\s*dialect:\s*([a-z_]+)", html)
        if m:
            ok, msg = _lint_summary(html, m.group(1))
            print(msg)
            return 0 if ok else 1
        return 0

    try:
        brand = load_brand(a.brand) if a.brand else find_brand()
    except BrandKitError as e:
        print(str(e))
        return 1
    print(f"brand kit: {brand.source or 'neutral default'}")
    status = 0
    for d in (DIALECTS if a.dialect == "all" else (a.dialect,)):
        mjml_source, html, text = sample_email(brand, d)
        out = Path(a.out) if (a.out and a.dialect != "all") else Path(a.out_dir or ".") / f"lcm_preview_{d}.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        out.with_suffix(".mjml").write_text(mjml_source, encoding="utf-8")
        out.with_suffix(".txt").write_text(text, encoding="utf-8")
        ok, msg = _lint_summary(html, d)
        print(f"wrote {out} (+ .mjml, .txt)\n  {msg}")
        status = status or (0 if ok else 1)
    return status


if __name__ == "__main__":
    sys.exit(_cli())
