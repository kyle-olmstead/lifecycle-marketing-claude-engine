"""Iterable as a sixth platform: the Handlebars dialect, its product blocks and links, and its track request."""
import pytest

from adapters.destinations import event_payloads
from engine.agents import copywriter
from engine.models import PLATFORM_DIALECT, PLATFORM_LABEL, Brief
from engine.render import product_blocks, template_lint


def _brief(flow="cart_abandon", **kw):
    return Brief(product_name="Northfield", flow_type=flow, platform="iterable", business_model="ecommerce",
                 postal_address="1 Example Way, Springfield, OR 97477", **kw)


def test_iterable_is_a_platform_with_handlebars():
    assert PLATFORM_DIALECT["iterable"] == "handlebars" and PLATFORM_LABEL["iterable"] == "Iterable"
    from engine.agents.campaign_architect import VOCAB
    from engine.agents.cohort_analyst import ACTIVATION_PATTERNS, PLATFORM_PATTERNS
    assert "iterable" in VOCAB and "iterable" in PLATFORM_PATTERNS and "iterable" in ACTIVATION_PATTERNS


@pytest.mark.parametrize("src,error", [
    ('{{#if x}}a{{/each}}', "closes {{#if}}"),
    ('{{#each items}}a', "never closed"),
    ('{{else}}', "outside a block"),
    ('{{ x ', "unbalanced"),
    ('{{defaultIfEmpty firstName}}', "takes a field and a fallback"),
    ('{{defaultIfEmpty firstName "there}}', "unterminated string"),
    ('{{}}', "empty"),
])
def test_handlebars_structure_errors(src, error):
    r = template_lint.lint(src, "handlebars")
    assert not r["ok"] and any(error in e for e in r["errors"]), r["errors"]


def test_handlebars_fallbacks_scopes_and_system_tags():
    ok = ('{{defaultIfEmpty firstName "there"}} {{#each items}}{{name}} {{price}}{{/each}} {{#if coupon}}{{coupon}}{{else}}none{{/if}} '
          '<a href="{{unsubscribeUrl}}">u</a> {{{hostedUnsubscribeUrl}}} {{physicalAddress}} {{!-- {{ignored}} --}}')
    r = template_lint.lint(ok, "handlebars")
    assert r["ok"] and not r["warnings"], (r["errors"], r["warnings"])
    r = template_lint.lint("Hi {{firstName}}", "handlebars")
    assert r["ok"] and any("defaultIfEmpty firstName" in w for w in r["warnings"])


def test_an_iterable_email_carries_unsubscribe_and_address_and_lints_clean():
    b = _brief()
    f = {"subject": "Your cart, {first_name}", "preheader": "Saved for you", "headline": "Still thinking it over?",
         "paragraphs": ["Hi {first_name}, {item_name} is still in your cart and ready when you are.", "{product_block}",
                        "Shipping is free on this order, and returns are easy for thirty days after it arrives at your door."],
         "cta_label": "Back to my cart"}
    from engine.render.brand import load_brand
    from tests.conftest import ROOT
    html, text = copywriter.render_email(f, b, "t1_email", brand=load_brand(ROOT / "examples/brands/northfield_dtc/brand.yaml"))
    r = template_lint.lint(html, "handlebars", email=True)
    assert r["ok"], r["errors"]
    assert '<a href="{{unsubscribeUrl}}">Unsubscribe</a>' in html and "{{#each items}}" in html and "{{/each}}" in html
    assert '{{defaultIfEmpty firstName "there"}}' in html and '{{defaultIfEmpty items.[0].name "your items"}}' in html
    assert "{{defaultIfEmpty checkout_url " in html                  # the button goes back to that checkout
    assert "Unsubscribe: {{unsubscribeUrl}}" in text
    assert copywriter.address_html("handlebars", b.model_copy(update={"postal_address": None})) == "{{physicalAddress}}"


def test_browse_reads_the_one_viewed_product():
    b = _brief("browse_abandon")
    assert product_blocks.item_name(b, "handlebars") == '{{defaultIfEmpty name "this item"}}'
    mjml, text = product_blocks.block(b, "handlebars", "https://northfield.example/")
    assert "#each" not in mjml and '{{defaultIfEmpty url "https://northfield.example/"}}' in mjml
    assert product_blocks.return_url(b, "handlebars", "https://northfield.example/") == '{{defaultIfEmpty url "https://northfield.example/"}}'


def test_the_iterable_track_request_validates_and_carries_the_event_id():
    req = event_payloads.build("iterable", {"user_id": "u_0001", "email": "U_0001@example.test", "event": "core_action_lapsed_14d",
                                            "properties": {"in_holdout": False}, "occurred_at": "2026-10-18T05:00:00Z",
                                            "event_id": "acme:u_0001:2026-10-04"})
    assert req["url"] == "https://api.iterable.com/api/events/track" and req["headers"]["Api-Key"] == "${ITERABLE_API_KEY}"
    assert req["body"]["id"] == "acme:u_0001:2026-10-04" and req["body"]["email"] == "u_0001@example.test" and req["body"]["createdAt"] == 1792299600
    name, errs = event_payloads.validate_request(req)
    assert name == "iterable_track_event" and not errs
    bad = dict(req, body={"dataFields": {}})
    assert event_payloads.validate_request(bad)[1]
    with pytest.raises(ValueError, match="512 bytes"):
        event_payloads.build("iterable", {"user_id": "u", "event": "x", "event_id": "x" * 600})
