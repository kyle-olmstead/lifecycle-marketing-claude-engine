"""MJML rendering: compiler fidelity for every ESP dialect, the brand kit in the output, golden files, and the copywriter's contract."""
import re
from importlib import metadata

import pytest

from tests.conftest import ROOT
from engine.agents import copywriter
from engine.brief import load_brief
from engine.paths import DEMO_BRIEF
from engine.policy import guardrails
from engine.render import mjml_render, template_lint
from engine.render.brand import DEFAULT_BRAND, load_brand
from engine.render.mjml_render import DIALECTS, MjmlRenderError, compile_mjml, fill

RIDGEVIEW = load_brand(ROOT / "examples/brands/ridgeview_saas/brand.yaml")
NORTHFIELD = load_brand(ROOT / "examples/brands/northfield_dtc/brand.yaml")
PLATFORM = {"customerio": "customerio", "braze": "braze", "klaviyo": "klaviyo", "hubl": "hubspot", "ampscript": "sfmc",
            "handlebars": "iterable"}
EM_DASH = chr(0x2014)
FIELDS = {"subject": "Your reports are waiting, {first_name}", "preheader": "Two minutes to pick up again",
          "headline": "Pick up where you left off",
          "paragraphs": ["Hi {first_name}, your data sources in {product_name} are still connected & your last report is saved.",
                         "Open it, change the date range, and share it. That is the whole job, and it takes about two minutes."],
          "cta_label": "Open my last report", "framework": "BAB", "hypothesis": "h"}

# the tags the fidelity check names, per dialect: (tag, may it sit inside an href)
FIDELITY = {
    "klaviyo": [("{% unsubscribe 'Unsubscribe' %}", False), ("{{ first_name|default:'there' }}", True)],
    "hubl": [("{{ unsubscribe_link }}", True), ('{{ personalization_token("contact.firstname", "there") }}', True)],
    "ampscript": [("%%unsub_center_url%%", True), ("%%=v(@fn)=%%", True)],
    "braze": [("{{${set_user_to_unsubscribed_url}}}", True), ("{{ ${first_name} | default: 'there' }}", True)],
    "customerio": [("{% unsubscribe_url %}", True), ('{{ customer.first_name | default: "there" }}', True),
                   ("{% if customer.plan_size > 0 %}team{% endif %}", True)],
    "handlebars": [("{{unsubscribeUrl}}", True), ('{{defaultIfEmpty firstName "there"}}', True), ("{{{hostedUnsubscribeUrl}}}", True),
                   ("{{#if plan}}team{{else}}you{{/if}}", True), ("{{!-- note --}}", False)],
}


def _brief(dialect, **update):
    return load_brief(DEMO_BRIEF).model_copy(update={"platform": PLATFORM[dialect], **update})


def _doc(text_part, href, head="", title="T"):
    q = "'" if '"' in href else '"'      # a tag holding double quotes is written inside single quotes, as in HTML
    return (f'{head}<mjml lang="en"><mj-head><mj-title>{title}</mj-title></mj-head><mj-body><mj-section><mj-column>'
            f'<mj-text>{text_part} <a href={q}{href}{q}>link</a></mj-text>'
            f'<mj-button href={q}{href}{q}>Go {text_part}</mj-button>'
            f'</mj-column></mj-section></mj-body></mjml>')


# ----------------------------------------------------------------------------------------------
# compiler fidelity
# ----------------------------------------------------------------------------------------------

def test_the_pinned_mjml_package_is_the_one_installed():
    assert metadata.version("mjml") == mjml_render.MJML_PACKAGE_VERSION
    assert f"mjml=={mjml_render.MJML_PACKAGE_VERSION}" in (ROOT / "requirements.txt").read_text()
    assert '"mjml>=0.12,<0.13"' in (ROOT / "pyproject.toml").read_text()


def test_the_raw_compiler_mangles_tags_which_is_why_they_are_protected():
    from mjml import mjml_to_html
    raw = mjml_to_html(_doc("{% if a > 0 %}x{% endif %}", "https://x.example/?a=1&amp;b=2")).html
    assert "{% if a &gt; 0 %}" in raw                       # '>' inside a Liquid condition is escaped
    assert 'href="https://x.example/?a=1&b=2"' in raw       # &amp; in a link is decoded to a bare '&'
    tag = '{{ customer.first_name | default: "there" }}'
    quoted = mjml_to_html(_doc("x", tag)).html
    assert f'href="{tag}"' in quoted                        # the button's href ends at the tag's first quote: broken HTML
    assert f"href='{tag}'" in compile_mjml(_doc("x", tag))   # protected: carried inside single quotes
    head = '<mj-raw position="file-start">%%[ VAR @fn ]%%</mj-raw>'
    assert "VAR @fn" not in mjml_to_html(_doc("x", "https://example.com/").replace("<mj-head>", head + "<mj-head>")).html
    assert compile_mjml(_doc("x", "https://example.com/").replace("<mj-head>", head + "<mj-head>")).startswith("%%[ VAR @fn ]%%<!doctype")


@pytest.mark.parametrize("dialect", sorted(FIDELITY))
def test_every_dialect_tag_round_trips_in_text_and_in_href(dialect):
    for tag, in_href in FIDELITY[dialect]:
        out = compile_mjml(_doc(tag, tag if in_href else "https://example.com/"))
        assert out.count(tag) >= (4 if in_href else 2), (dialect, tag, out.count(tag))
        if in_href and '"' in tag:
            assert f"href='{tag}'" in out                     # carried inside single quotes, byte for byte
        elif in_href:
            assert f'href="{tag}"' in out


def test_quote_bearing_tags_in_the_title_become_a_single_quoted_aria_label():
    tag = '{{ customer.first_name | default: "there" }}'
    out = compile_mjml(_doc("x", "https://example.com/", title=f"It&#x27;s ready, {tag}"))
    assert f"aria-label='It&#x27;s ready, {tag}'" in out and f"It&#x27;s ready, {tag}" in out.split("</title>")[0]


def test_ampscript_declaration_block_travels_before_the_doctype():
    head = '<mj-raw position="file-start"><!-- dialect: ampscript -->\n' + copywriter.AMPSCRIPT_HEAD + "</mj-raw>"
    out = compile_mjml(head + _doc("Hi %%=v(@fn)=%%", "%%unsub_center_url%%"))
    assert out.startswith("<!-- dialect: ampscript -->\n%%[\nVAR @fn") and out.index("]%%") < out.index("<!doctype html>")


def test_character_references_survive_in_body_and_title_but_not_in_css():
    src = ('<mjml><mj-head><mj-title>A &amp; B</mj-title>'
           '<mj-font name="Inter" href="https://fonts.example/css?family=Inter&amp;display=swap" /></mj-head>'
           '<mj-body><mj-section><mj-column><mj-text font-family="Inter, Arial">x&#8204;&#160;y &lt;b&gt;</mj-text>'
           '<mj-button href="https://x.example/?a=1&amp;b=2">Go</mj-button></mj-column></mj-section></mj-body></mjml>')
    out = compile_mjml(src)
    assert "x&#8204;&#160;y &lt;b&gt;" in out and 'href="https://x.example/?a=1&amp;b=2"' in out
    assert "A &amp; B" in out
    assert "@import url(https://fonts.example/css?family=Inter&display=swap)" in out   # CSS gets a plain '&'


def test_a_dropped_tag_is_an_error_not_a_silent_loss():
    bare = "<mjml><mj-body><mj-section><mj-column>{% for i in items %}<mj-text>{{ i.name|default:'x' }}</mj-text>{% endfor %}" \
           "</mj-column></mj-section></mj-body></mjml>"
    with pytest.raises(MjmlRenderError, match="dropped .*mj-raw"):
        compile_mjml(bare)
    wrapped = bare.replace("{% for i in items %}", "<mj-raw>{% for i in items %}</mj-raw>").replace("{% endfor %}", "<mj-raw>{% endfor %}</mj-raw>")
    out = compile_mjml(wrapped)
    assert "{% for i in items %}" in out and "{% endfor %}" in out


def test_reserved_marker_and_impossible_quoting_are_errors():
    with pytest.raises(MjmlRenderError, match="reserved marker"):
        compile_mjml("<mjml><mj-body><mj-text>LMMTPL00001X</mj-text></mj-body></mjml>")
    both = "{{ x | default: \"it's\" }}"
    with pytest.raises(MjmlRenderError, match="both quote characters"):
        compile_mjml('<mjml><mj-body><mj-section><mj-column><mj-button href="' + both + '">Go</mj-button></mj-column></mj-section></mj-body></mjml>')


def test_fill_is_single_pass_and_drops_empty_lone_placeholders():
    assert fill("a\n  [[x]]\nb", {"x": ""}) == "a\nb"
    assert fill("  [[x]]", {"x": "word"}) == "  word"
    assert fill("  [[x]]", {"x": "    block"}) == "    block"
    assert fill("<[[x]]>", {"x": "[[y]]"}) == "<[[y]]>"
    with pytest.raises(MjmlRenderError, match=r"\[\[missing\]\]"):
        fill("[[missing]]", {})


# ----------------------------------------------------------------------------------------------
# the copywriter's renderer
# ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kit", [DEFAULT_BRAND, RIDGEVIEW, NORTHFIELD], ids=["default", "ridgeview", "northfield"])
@pytest.mark.parametrize("dialect", DIALECTS)
def test_rendered_email_keeps_every_guarantee(dialect, kit):
    b = _brief(dialect)
    mjml, html, text = copywriter.render_email_mjml(FIELDS, b, "t1", brand=kit)
    assert (html, text) == copywriter.render_email(FIELDS, b, "t1", brand=kit)
    assert compile_mjml(mjml) == html                                    # the MJML source is the HTML's real source
    r = template_lint.lint(html, dialect, email=True)
    assert r["ok"] and not r["warnings"], (r["errors"], r["warnings"])
    assert not template_lint.lint(text, dialect)["errors"]
    tti = guardrails.text_to_image(html)
    assert not tti["errors"] and not tti["warnings"]
    assert html.startswith(f"<!-- dialect: {dialect} -->\n")
    if dialect == "ampscript":
        assert html.index("%%[") < html.index("<!doctype html>") and text.startswith("%%[")
    assert '<html lang="' in html and 'name="color-scheme"' in html and "prefers-color-scheme: dark" in html
    assert 'class="preheader"' in html and "Two minutes to pick up again" in html
    assert "<!--[if mso | IE]>" in html and "@media only screen and (min-width:480px)" in html
    tables = re.findall(r"<table\b[^>]*>", html)
    assert tables and all('role="presentation"' in t for t in tables)
    assert "utm_source=lifecycle&amp;utm_medium=email&amp;utm_campaign=ridgeview_winback_14d&amp;utm_content=t1" in html
    assert copywriter.unsubscribe_html(dialect) in html and copywriter.address_html(dialect, b) in html
    assert "&amp; your last report" in html and EM_DASH not in html + mjml + text


def test_the_default_kit_looks_like_the_original_renderer():
    html, text = copywriter.render_email(FIELDS, _brief("customerio"), "t1")
    assert "<img" not in html
    assert 'bgcolor="#1F5F4A"' in html and "color:#FFFFFF" in html and "color:#1C1C1C" in html
    assert "background-color:#FFFFFF" in html and "#1E1E1E !important" in html and "#F2F2F2 !important" in html
    assert "font-size:22px" in html and "font-size:16px" in html and "border-radius:6px" in html and "Arial" in html
    # the plain-text version keeps its exact shape
    assert text.endswith("Dana Ortiz at Ridgeview\n100 Example Way, Suite 400, Springfield, OR 97477\nUnsubscribe: {% unsubscribe_url %}\n")
    assert text.startswith("Pick up where you left off\n\nHi {{ customer.first_name")


def test_the_brand_kit_drives_the_output():
    b = _brief("customerio")
    html, text = copywriter.render_email(FIELDS, b, "t1", brand=RIDGEVIEW)
    assert "#1F5F4A" not in html                                         # nothing of the neutral palette is hard-coded
    assert 'bgcolor="#2B4C7E"' in html and "background-color:#F3F5F8" in html and "#8DB8F2 !important" in html
    assert re.search(r'<img alt="Ridgeview" height="32" src="https://www.ridgeview.example/email/[^"]+" style="[^"]*" width="140"', html)
    assert '<a href="https://www.ridgeview.example" target="_blank">' in html
    assert "Ridgeview Analytics, Inc. (fictional), 100 Example Way" in html            # legal name before a literal address
    assert "You are getting this because you have a Ridgeview account." in html
    assert '<a href="https://www.linkedin.example/company/ridgeview">LinkedIn</a>' in html
    assert "LinkedIn: https://www.linkedin.example/company/ridgeview\nUnsubscribe:" in text
    k_html, _ = copywriter.render_email(FIELDS, _brief("klaviyo"), "t1", brand=RIDGEVIEW)
    assert "Ridgeview Analytics" not in k_html and "{{ organization.name }}, {{ organization.full_address }}" in k_html


def test_web_font_is_loaded_and_outlook_is_pinned_to_the_stack():
    html, _ = copywriter.render_email(FIELDS, _brief("klaviyo"), "t1", brand=NORTHFIELD)
    assert '<link href="https://fonts.googleapis.com/css2?family=Lora:wght@400;700&display=swap" rel="stylesheet"' in html
    assert "<!--[if mso]><style" in html and "font-family: Georgia, 'Times New Roman', serif !important;" in html
    assert "font-family:Lora, Georgia, 'Times New Roman', serif" in html


def test_the_kit_address_fills_in_when_the_brief_has_none():
    b = _brief("customerio", postal_address=None)
    html, text = copywriter.render_email(FIELDS, b, "t1", brand=RIDGEVIEW)
    assert "100 Example Way" in html and "POSTAL ADDRESS REQUIRED" not in html and "100 Example Way" in text
    html_default, _ = copywriter.render_email(FIELDS, b, "t1")
    assert "POSTAL ADDRESS REQUIRED" in html_default


def test_the_product_block_slot_is_a_no_op_by_default():
    b = _brief("klaviyo")
    none_mjml, none_html, _ = copywriter.render_email_mjml(FIELDS, b, "t1", brand=RIDGEVIEW)
    empty_mjml, empty_html, _ = copywriter.render_email_mjml(FIELDS, b, "t1", brand=RIDGEVIEW, product_items="  \n")
    assert (none_mjml, none_html) == (empty_mjml, empty_html) and "lcm-products" not in none_mjml + none_html
    items = ("        <mj-raw>{% for item in event.extra.items %}</mj-raw>\n"
             "        <mj-text css-class=\"lcm-text\">{{ item.name|default:'Your item' }}</mj-text>\n"
             "        <mj-raw>{% endfor %}</mj-raw>")
    mjml, html, _ = copywriter.render_email_mjml(FIELDS, b, "t1", brand=RIDGEVIEW, product_items=items)
    assert "lcm-products" in html and "{% for item in event.extra.items %}" in html and "{% endfor %}" in html
    assert html.index("{% endfor %}") < html.index("Open my last report")      # the slot sits before the button
    assert template_lint.lint(html, "klaviyo", email=True)["ok"]


def test_every_offline_draft_renders_with_a_logo_in_every_dialect():
    """A logo is one image: every starter draft must still clear the text-to-image check with each example kit."""
    for dialect in DIALECTS:
        b = _brief(dialect)
        for drafts in copywriter._lib(b).values():
            for f in drafts:
                for kit in (RIDGEVIEW, NORTHFIELD):
                    html, _ = copywriter.render_email(f, b, "t", brand=kit)
                    r = template_lint.lint(html, dialect, email=True)
                    assert r["ok"] and not r["warnings"], (dialect, kit.brand_name, f["subject"], r["errors"], r["warnings"])


# ----------------------------------------------------------------------------------------------
# golden files and components
# ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("dialect", DIALECTS)
def test_golden_mjml_recompiles_byte_for_byte(dialect):
    mjml_path, html_path = mjml_render.golden_paths(dialect)
    assert compile_mjml(mjml_path.read_text(encoding="utf-8")) == html_path.read_text(encoding="utf-8"), (
        f"{html_path} no longer matches its MJML: the compiler or its parser changed output. Review the diff, then "
        "run python3 engine/render/mjml_render.py --write-golden")


@pytest.mark.parametrize("dialect", DIALECTS)
def test_golden_files_match_what_the_renderer_builds_today(dialect):
    mjml, html, _ = mjml_render.sample_email(load_brand(mjml_render.GOLDEN_BRAND), dialect)
    mjml_path, html_path = mjml_render.golden_paths(dialect)
    assert mjml == mjml_path.read_text(encoding="utf-8") and html == html_path.read_text(encoding="utf-8"), (
        "the templates or the renderer changed: run python3 engine/render/mjml_render.py --write-golden and review the diff")


@pytest.mark.parametrize("dialect", DIALECTS)
def test_golden_html_lints_clean(dialect):
    html = mjml_render.golden_paths(dialect)[1].read_text(encoding="utf-8")
    r = template_lint.lint(html, dialect, email=True)
    assert r["ok"] and not r["warnings"], (r["errors"], r["warnings"])
    assert r["bytes"] < 90 * 1024 and r["text_to_image"]["images"] == 1


def test_mjml_components_are_documented_and_clean():
    names = {p.stem for p in mjml_render.MJML_DIR.glob("*.mjml")}
    assert names == {"base", "preheader", "header", "headline", "paragraph", "button", "product_block", "footer"}
    for p in mjml_render.MJML_DIR.glob("*.mjml"):
        raw = p.read_text(encoding="utf-8")
        assert raw.startswith("<!--") and EM_DASH not in raw, p
        body = mjml_render.component(p.stem)
        assert not re.search(r"\{\{|\{%|\{#|%%", body), p          # nothing a template linter would read as code
        assert "#1F5F4A" not in body and not re.search(r"#[0-9A-Fa-f]{6}", body), p   # every color comes from the kit
    for p in mjml_render.GOLDEN_DIR.rglob("*.mjml"):
        assert EM_DASH not in p.read_text(encoding="utf-8")


def test_components_html_still_lints():
    r = template_lint.lint((ROOT / "templates/email/components.html").read_text(), "customerio", email=True)
    assert r["ok"]


def test_cli_renders_previews_and_compiles(tmp_path, capsys):
    brand = str(ROOT / "examples/brands/ridgeview_saas/brand.yaml")
    assert mjml_render._cli(["--brand", brand, "--dialect", "all", "--out-dir", str(tmp_path)]) == 0
    for d in DIALECTS:
        for suffix in (".html", ".mjml", ".txt"):
            assert (tmp_path / f"lcm_preview_{d}{suffix}").is_file()
    assert "lint (klaviyo): ok" in capsys.readouterr().out
    out = tmp_path / "one.html"
    assert mjml_render._cli(["--brand", brand, "--dialect", "hubl", "--out", str(out)]) == 0 and out.is_file()
    assert mjml_render._cli(["compile", str(tmp_path / "lcm_preview_braze.mjml"), "--out", str(tmp_path / "c.html")]) == 0
    assert (tmp_path / "c.html").read_text() == (tmp_path / "lcm_preview_braze.html").read_text()
    bad = tmp_path / "bad.yaml"
    bad.write_text("brand_name: X\ncolors: {primary: '#EEEEEE'}\n")
    assert mjml_render._cli(["--brand", str(bad)]) == 1
