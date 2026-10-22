"""Every event field a rendered template reads is one the tracking plan says the trigger event carries."""
import pytest

from engine.brief import parse_text
from engine.flows.library import FLOWS
from engine.pipeline import orchestrator
from engine.render import fields

EVENT_FLOWS = {"cart_abandon": "a 3-touch abandoned cart flow with email and SMS", "browse_abandon": "a browse abandonment flow",
               "post_purchase": "a post-purchase flow", "replenishment": "a replenishment flow every 30 days"}


@pytest.mark.parametrize("platform", ["customerio", "braze", "klaviyo"])
@pytest.mark.parametrize("flow", sorted(EVENT_FLOWS))
def test_templates_read_only_fields_the_event_carries(platform, flow):
    b = parse_text(EVENT_FLOWS[flow], "Acme", overrides={"platform": platform, "postal_address": "1 Example Way, Springfield, OR 97477"},
                   defaults={})
    if platform == "klaviyo":            # the engine's own events, not Shopify's (whose fields are Shopify's contract)
        b = b.model_copy(update={"business_model": "subscription"})
    plan = orchestrator.run(b, None, copy_mode="offline")
    trigger = FLOWS[flow].trigger_event["*"]
    assert fields.allowed(trigger) is not None, f"{trigger} is not in config/tracking_plan.json"
    bad = {f"{name}: {x}" for tc in plan.copy_plan for v in tc.variants for name, text in v.rendered.items()
           for x in fields.unknown(text, b.template_dialect, trigger)}
    assert not bad, sorted(bad)


def test_an_unplanned_field_is_named():
    tpl = "{% for item in event.items limit: 3 %}{{ item.colour }}{% endfor %} {{ event.coupon_code | default: '' }}"
    assert fields.unknown(tpl, "customerio", "checkout_started") == ["coupon_code", "items[].colour"]
    assert fields.unknown("{{context.${checkout_url} | default: 'x'}}", "braze", "checkout_started") == []
    assert fields.allowed("message_delivered") is None    # an ESP message event (message_events), not the event stream
