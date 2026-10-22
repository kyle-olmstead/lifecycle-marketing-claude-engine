"""Product blocks and item names, in each platform's own template language, from the event that triggered the flow.

Starter copy and Claude's drafts write {item_name} (the first item, with a fallback) and a {product_block} paragraph
(up to three items with image, name and price); this module turns them into the platform's syntax. Each path is from
the vendor's documentation, checked 2026-10-21:

  Klaviyo     event.<Property>, Django-style loops; Shopify's Checkout Started and Placed Order carry
              event.extra.line_items (item.product.title, item.line_price, item.product.images.0.src) and Viewed Product
              carries event.Name / event.URL / event.ImageURL / event.Price
              (help.klaviyo.com/hc/en-us/articles/115002779071, /4408802597659, /115005080447); events the engine sends
              carry items[] with name, url, image_url, price, quantity (config/tracking_plan.json line_item) (developers.klaviyo.com, use_conditionals).
  Customer.io {{event.<attribute>}} in event-triggered campaigns, {% for p in event.products %} (docs.customer.io/messaging/liquid/).
  Braze       {{context.${...}}} is current (canvas_entry_properties is legacy); the documented loop is
              {% for item in {{context.${products}}} %} (braze.com/docs, context_and_event_properties, ecommerce_use_cases).
  HubSpot     workflow emails cannot loop over event properties (event values are capped at 256 characters, 50 properties);
              the documented workaround is a contact property written at ingest, so the block shows one item from
              contact.lcm_last_item_* properties (knowledge.hubspot.com, personalize-your-content; developers.hubspot.com events guide).
  SFMC        AttributeValue() reads journey entry data and returns null rather than failing the send; entry data is flat, so
              the block shows one item from item_name / item_url / item_image_url entry attributes
              (developer.salesforce.com, mc-ampscript-reference-utilities-attribute-value).

What a platform cannot do is said, not faked: HubSpot and SFMC show one item, and the build steps name the property to sync.
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

from engine.models import Brief

FALLBACK = {"cart_abandon": "your cart", "browse_abandon": "this item", "post_purchase": "your order", "replenishment": "your usual",
            "welcome": "our favorites"}
EVENT_FLOWS = ("cart_abandon", "browse_abandon", "post_purchase", "replenishment")


def source(brief: Brief) -> str:
    """Where the items come from: Shopify's own Klaviyo events, a single viewed product, or the engine's items[] array."""
    if brief.platform == "klaviyo" and brief.business_model in ("ecommerce", "marketplace") and brief.flow_type in ("cart_abandon", "post_purchase"):
        return "shopify_line_items"
    if brief.platform == "klaviyo" and brief.flow_type == "browse_abandon":
        return "shopify_viewed_product"
    return "engine_items"


def _fb(brief: Brief) -> str:
    return FALLBACK.get(brief.flow_type, "your items")


def item_name(brief: Brief, dialect: str) -> str:
    """The first item's name in the platform's syntax, with a fallback."""
    fb = _fb(brief)
    src = source(brief)
    if dialect == "klaviyo":
        path = {"shopify_line_items": "event.extra.line_items.0.product.title", "shopify_viewed_product": "event.Name"}.get(src, "event.items.0.name")
        return "{{ " + path + "|default:'" + fb + "' }}"
    if dialect == "customerio":
        path = "event.name" if brief.flow_type == "browse_abandon" else "event.items.first.name"
        return '{{ ' + path + ' | default: "' + fb + '" }}'
    if dialect == "braze":
        path = "context.${name}" if brief.flow_type == "browse_abandon" else "context.${items}[0].name"
        return "{{ " + path + " | default: '" + fb + "' }}"
    if dialect == "hubl":
        return '{{ personalization_token("contact.lcm_last_item_name", "' + fb + '") }}'
    if dialect == "ampscript":
        return '%%=IIf(Empty(AttributeValue("item_name")), "' + fb + '", AttributeValue("item_name"))=%%'
    return fb


def _row_mjml(name: str, url: str, image: str, price: str, text_color: str = "#1C1C1C") -> str:
    img = (f'          <mj-image src="{image}" alt="{name}" width="96px" align="left" padding="8px 24px 0 24px" />\n' if image else "")
    return (img + f'          <mj-text css-class="lcm-text" padding="4px 24px 12px 24px"><a href="{url}" style="color:inherit;">'
                  f"{name}</a><br>{price}</mj-text>\n")


RETURN_PROPERTY = {"cart_abandon": ("checkout_url", "lcm_last_checkout_url"), "browse_abandon": ("url", "lcm_last_item_url")}


def return_url(brief: Brief, dialect: str, fallback: Optional[str]) -> Optional[str]:
    """Where the button of a cart or browse email goes: back to that checkout, or to that product, in the platform's syntax,
    with the store's home page as the fallback. None for other flows (they use cta_url).

    Klaviyo's own abandoned-cart template uses event.extra.checkout_url with organization.url as the default (Shopify's
    Checkout Started event); its Viewed Product event carries URL. Elsewhere the trigger event carries checkout_url or url
    (config/tracking_plan.json), and HubSpot and Marketing Cloud read the same contact or entry-data fields as the
    product block."""
    if brief.flow_type not in RETURN_PROPERTY:
        return None
    prop, contact_prop = RETURN_PROPERTY[brief.flow_type]
    src = source(brief)
    if dialect == "klaviyo":
        path = {"shopify_line_items": "event.extra.checkout_url", "shopify_viewed_product": "event.URL"}.get(src, f"event.{prop}")
        return "{{ " + path + "|default:organization.url }}"
    if not fallback:
        return None
    if dialect == "customerio":
        return "{{ event." + prop + " | default: '" + fallback + "' }}"
    if dialect == "braze":
        return "{{context.${" + prop + "} | default: '" + fallback + "'}}"
    if dialect == "hubl":
        return "{{ personalization_token('contact." + contact_prop + "', '" + fallback + "') }}"
    if dialect == "ampscript":
        return "%%=IIf(Empty(AttributeValue('" + prop + "')), '" + fallback + "', AttributeValue('" + prop + "'))=%%"
    return None


def block(brief: Brief, dialect: str, website: Optional[str] = None) -> Tuple[Optional[str], str]:
    """(MJML for the product slot, plain-text version). (None, '') when the flow's trigger carries no items."""
    if brief.flow_type not in EVENT_FLOWS:
        return None, ""
    src = source(brief)
    fb = _fb(brief)
    if brief.flow_type == "browse_abandon" and dialect in ("customerio", "braze"):
        # one viewed product, carried on the product_viewed event itself (config/tracking_plan.json), not an items[] list;
        # every field has a fallback, the link falls back to the store's home page
        home = website or ""
        if dialect == "customerio":
            f = lambda k, d="": "{{ event." + k + " | default: '" + d + "' }}"   # noqa: E731
        else:
            f = lambda k, d="": "{{context.${" + k + "} | default: '" + d + "'}}"  # noqa: E731
        return _row_mjml(f("name", fb), f("url", home), f("image_url"), f("price")), f"{f('name', fb)}: {f('url', home)}"
    if dialect == "klaviyo" and src == "shopify_viewed_product":
        row = _row_mjml("{{ event.Name|default:'" + fb + "' }}", "{{ event.URL|default:organization.url }}", "{{ event.ImageURL|default:'' }}",
                        "{{ event.Price|default:'' }}")
        return row, "{{ event.Name|default:'" + fb + "' }}: {{ event.URL|default:organization.url }}"
    if dialect == "klaviyo":
        if src == "shopify_line_items":
            coll, name, url, image, price = ("event.extra.line_items", "{{ item.product.title }}",
                                             "{{ organization.url }}products/{{ item.product.handle }}", "{{ item.product.images.0.src }}",
                                             "{{ item.quantity }} x {{ item.line_price }}")
        else:
            coll, name, url, image, price = ("event.items", "{{ item.name }}", "{{ item.url }}", "{{ item.image_url }}",
                                             "{{ item.quantity }} x {{ item.price }}")
        loop = "{% for item in " + coll + "|slice:':3' %}"
        return (f"          <mj-raw>{loop}</mj-raw>\n" + _row_mjml(name, url, image, price) + "          <mj-raw>{% endfor %}</mj-raw>\n",
                loop + name + ": " + url + "\n{% endfor %}")
    if dialect == "customerio":
        loop = "{% for item in event.items limit: 3 %}"
        row = _row_mjml("{{ item.name }}", "{{ item.url }}", "{{ item.image_url }}", "{{ item.quantity }} x {{ item.price }}")
        return (f"          <mj-raw>{loop}</mj-raw>\n" + row + "          <mj-raw>{% endfor %}</mj-raw>\n",
                loop + "{{ item.name }}: {{ item.url }}\n{% endfor %}")
    if dialect == "braze":
        loop = "{% for item in {{context.${items}}} limit: 3 %}"
        row = _row_mjml("{{item.name}}", "{{item.url}}", "{{item.image_url}}", "{{item.quantity}} x {{item.price}}")
        return (f"          <mj-raw>{loop}</mj-raw>\n" + row + "          <mj-raw>{% endfor %}</mj-raw>\n",
                loop + "{{item.name}}: {{item.url}}\n{% endfor %}")
    if dialect == "hubl":
        name = '{{ personalization_token("contact.lcm_last_item_name", "' + fb + '") }}'
        url = '{{ personalization_token("contact.lcm_last_item_url", "") }}'
        return _row_mjml(name, url, "", ""), f"{name}: {url}"
    if dialect == "ampscript":
        name = '%%=IIf(Empty(AttributeValue("item_name")), "' + fb + '", AttributeValue("item_name"))=%%'
        url = '%%=AttributeValue("item_url")=%%'
        return _row_mjml(name, url, "", ""), f"{name}: {url}"
    return None, ""


def sync_note(brief: Brief) -> Optional[str]:
    """What the build must sync for the block to show the right items, per platform."""
    if brief.flow_type not in EVENT_FLOWS:
        return None
    if brief.template_dialect == "hubl":
        return ("HubSpot workflow emails cannot loop over event properties: write the first item's name and link to the contact "
                "properties lcm_last_item_name and lcm_last_item_url when the event is sent, and the email shows that one item")
    if brief.template_dialect == "ampscript":
        return ("Journey entry data is flat in Marketing Cloud: include item_name and item_url in the API Event entry data (for more "
                "than one item, keep a related data extension and loop it with LookupRows)")
    if brief.template_dialect == "braze":
        return "Pass the items array as a Canvas context variable named items (Canvas Context must be enabled in the workspace)"
    if source(brief).startswith("shopify"):
        return "Uses Shopify's own Klaviyo events (Checkout Started / Placed Order line items, Viewed Product); no extra sync"
    return "The trigger event must carry items[] with name, url, image_url, price and quantity (config/tracking_plan.json line_item)"
