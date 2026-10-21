import pytest

from tests.conftest import ROOT
from tools.sms import analyze as sms
from tools.template_lint import lint

GOOD = {
    "braze": "Hi {{ ${first_name} | default: 'there' }} {% connected_content https://api.example.com/x :save r %}{% if r.items.size < 1 %}{% abort_message('none') %}{% endif %} {{${set_user_to_unsubscribed_url}}}",
    "customerio": "Hi {{ customer.first_name | default: 'there' }} {% unsubscribe_url %} {{ customer.created_at | add_day: 7 | default: 0 }}",
    "klaviyo": "Hi {{ first_name|default:'there' }} {{ person|lookup:'Favorite Color'|default:'x' }} {% unsubscribe 'Opt out' %}",
    "hubl": "Hi {{ personalization_token('contact.firstname', 'there') }} {% if contact.x %}{{ contact.x }}{% endif %}",
    "ampscript": "%%[ VAR @fn SET @fn = AttributeValue(\"FirstName\") IF Empty(@fn) THEN SET @fn = \"there\" ENDIF ]%% Hi %%=v(@fn)=%%",
}
BAD = {
    "braze": "{% abort_message({{ x }}) %}",
    "customerio": "{% if customer.plan %}open",
    "customerio_legacy": "{{ customer.first_name | default: 'there' }}",
    "klaviyo": "{{ first_name|default: 'there' }}",
    "hubl": "{% for x in y %}",
    "ampscript": "%%[ FOR @i = 1 TO 3 DO SET n = 1 ]%%",
}


@pytest.mark.parametrize("dialect", sorted(GOOD))
def test_good_templates_pass_without_warnings(dialect):
    r = lint(GOOD[dialect], dialect)
    assert r["ok"], r["errors"]
    assert not r["warnings"], r["warnings"]


@pytest.mark.parametrize("dialect", sorted(BAD))
def test_bad_templates_fail(dialect):
    assert not lint(BAD[dialect], dialect)["ok"]


def test_missing_fallback_is_a_warning_in_every_liquid_dialect():
    for d, src in (("customerio", "{{ customer.first_name }}"), ("braze", "{{ ${first_name} }}"), ("klaviyo", "{{ first_name }}"),
                   ("hubl", "{{ contact.firstname }}")):
        assert any("no fallback" in w for w in lint(src, d)["warnings"]), d


def test_unknown_filters_are_reported():
    assert any("unknown filter 'upcasee'" in w for w in lint("{{ customer.x | upcasee | default: 'y' }}", "customerio")["warnings"])
    assert any("unknown filter 'nosuch'" in w for w in lint("{{ x|nosuch|default:'y' }}", "klaviyo")["warnings"])


def test_modular_email_template_passes_email_checks():
    path = ROOT / "modules/03_copywriting_and_multichannel/templates/modular_email_components.html"
    r = lint(path.read_text(), "customerio", email=True)
    # the component library shows every image block once, so as a whole it is image-heavy; one email built from it is not
    assert r["ok"] and r["bytes"] < 90 * 1024
    assert [w for w in r["warnings"] if not w.startswith("image-heavy")] == []
    assert r["text_to_image"]["images"] == 5


def test_text_to_image_ignores_template_tags_and_hidden_preheaders():
    from tools.template_lint import text_to_image
    body = '<div style="display:none">' + "x" * 400 + '</div>{{ customer.first_name }}{% if a %}{% endif %}<img src="a.png" alt="a">'
    r = text_to_image("<html><body>" + body + "<p>Short.</p></body></html>")
    assert r["visible_text_chars"] == len("Short.") and r["errors"] and "image-only" in r["errors"][0]
    ok = text_to_image("<html><body><p>" + "Plenty of live text. " * 30 + '</p><img src="a.png" alt="a"></body></html>')
    assert not ok["errors"] and not ok["warnings"]


def test_braze_attribute_regex_leaves_liquid_after_a_dollar_sign():
    r = lint("Now ${{ price | default: '9' }} for {{${first_name} | default: 'you'}}", "braze")
    assert r["ok"], r["errors"]
    assert not lint("{{${first_name} | default: 'x'}} and ${", "braze")["ok"]


def test_hubl_unless_and_ampscript_keyword_variables():
    assert lint("{% unless contact.firstname %}Hi there{% endunless %}", "hubl")["ok"]
    assert not lint("{% unless contact.firstname %}Hi there", "hubl")["ok"]
    r = lint("%%[ VAR @next, @if SET @next = 1 IF @next == 1 THEN SET @if = 2 ENDIF ]%%", "ampscript")
    assert r["ok"], r["errors"]


def test_email_checks_catch_missing_unsubscribe_and_alt():
    r = lint('<html><head><meta name="color-scheme" content="light dark"></head><body><div>preheader</div><img src="x.png"></body></html>',
             "klaviyo", email=True)
    assert any("unsubscribe" in e for e in r["errors"]) and any("alt" in e for e in r["errors"])


def test_sms_encoding_and_segments():
    # worst case in the orchestrator's worked example: a 32-character truncated name and a 16-character user_id
    worst = ("Northfield: your Ridgeline Packable Rain Shell... is still in your cart. Free 60-day exchanges. "
             "https://nf.example.com/c/" + "u" * 16 + " Reply STOP to opt out")
    plain = sms(worst)
    assert (plain["encoding"], plain["units"], plain["segments"]) == ("GSM-7", 159, 1)
    assert sms(worst.replace("u" * 16, "u" * 18))["segments"] == 2  # past 17 id characters it spills into a second segment
    emoji = sms(worst.replace("exchanges.", "exchanges " + chr(0x1F6D2) + "."))
    assert (emoji["encoding"], emoji["units"], emoji["segments"]) == ("UCS-2", 162, 3)
    assert any("opt-out" in c for c in sms("Hello from Northfield")["checks"])
    assert sms("a{b}")["units"] == 6  # each brace is an extension character and costs two septets


def test_sms_escape_characters_never_split_across_segments():
    # 152 plain + one 2-septet euro sign: 154 septets, so two segments either way, but the euro must start segment two
    assert sms("a" * 152 + "€" + "b" * 10)["segments"] == 2
    # 306 septets of plain text is exactly two segments; moving one euro onto a boundary forces a third
    assert sms("a" * 306)["segments"] == 2
    assert sms("a" * 152 + "€" + "a" * 152)["segments"] == 3
    # an emoji is a surrogate pair in UCS-2 and cannot straddle a boundary either
    assert sms("a" * 66 + "😀" + "a" * 66)["segments"] == 3


def test_sms_opt_out_needs_an_instruction_not_the_word():
    from tools.sms import has_opt_out
    assert not has_opt_out("Acme: stop by our store this weekend")
    assert has_opt_out("Acme: 20% off. Reply STOP to opt out") and has_opt_out("Text END to 55555 to cancel")
    assert has_opt_out("STOP to unsubscribe")
