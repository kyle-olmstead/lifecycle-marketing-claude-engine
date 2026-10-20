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
    assert r["ok"] and not r["warnings"] and r["bytes"] < 90 * 1024


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
