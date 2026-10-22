# Worked example: one post-purchase email in all five dialects (fictional retailer)

A model answer for `prompts/jobs/copy/email_writer.md`: the same message rendered for Customer.io, Braze, Klaviyo,
HubSpot and Salesforce Marketing Cloud. Read it only when a model answer would help, or to see a dialect you have
not written before.

The same post-purchase message, second order flow, message 1, for a fictional outdoor retailer,
written once and rendered in all five dialects. Goal: a second order within the repeat window.
Trigger: the first `order_completed` event, three days after delivery. The logic in every dialect:
greet with a fallback; thank them for the specific item when the data has it; point to the one
next step; unsubscribe link.

| Variant | Subject | Preview text | Hypothesis |
|---|---|---|---|
| A | How is the {{item}} holding up? (rendered with a fallback to "new gear") | Three days in is when the fit questions show up. | Naming the purchased item raises reply and click rate among first-time buyers, because it reads as a person following up rather than a campaign. Falsified if B performs equally or better on clicks. |
| B | Your first Northfield order | A quick note on care and fit. | Control. |

Customer.io (event-triggered by `order_completed`):

```liquid customerio
Hi {{ customer.first_name | default: "there" }},

{% if event.items.size > 0 %}Your {{ event.items.first.name | default: "new gear" }} should have arrived a few days ago.{% else %}Your order should have arrived a few days ago.{% endif %} This is usually when fit questions come up, so here is the short version: if it is not right, exchanges are free for 60 days.

{% for item in event.items limit: 3 %}- {{ item.name | default: "item" }}
{% endfor %}
Care and fit guide: https://shop.northfield.example/care

Not useful? {% unsubscribe_url %}
```

Braze (Canvas entry from the custom event):

```liquid braze
Hi {{ ${first_name} | default: 'there' }},

{% assign items = event_properties.${items} %}{% if items.size > 0 %}Your {{ items.first.name | default: 'new gear' }} should have arrived a few days ago.{% else %}Your order should have arrived a few days ago.{% endif %} This is usually when fit questions come up, so here is the short version: if it is not right, exchanges are free for 60 days.

Care and fit guide: https://shop.northfield.example/care

Not useful? {{${set_user_to_unsubscribed_url}}}
```

Klaviyo (flow triggered by the `order_completed` metric; Shopify's own Placed Order event uses
different property names, so check the event's preview data before mapping):

```django klaviyo
Hi {{ first_name|default:'there' }},

{% if event.items %}Your {{ event.items.0.name|default:'new gear' }} should have arrived a few days ago.{% else %}Your order should have arrived a few days ago.{% endif %} This is usually when fit questions come up, so here is the short version: if it is not right, exchanges are free for 60 days.

{% for item in event.items|slice:':3' %}- {{ item.name|default:'item' }}
{% endfor %}
Care and fit guide: https://shop.northfield.example/care

{% unsubscribe 'Unsubscribe' %}
{{ organization.name }}, {{ organization.full_address }}
```

HubSpot (marketing email from a workflow; marketing emails cannot loop over order line items, so the
workflow first copies the item name to a contact property):

```hubl
Hi {{ personalization_token("contact.firstname", "there") }},

{% if contact.last_order_item %}Your {{ contact.last_order_item }} should have arrived a few days ago.{% else %}Your order should have arrived a few days ago.{% endif %} This is usually when fit questions come up, so here is the short version: if it is not right, exchanges are free for 60 days.

Care and fit guide: https://shop.northfield.example/care

<a href="{{ unsubscribe_link }}">Unsubscribe</a>
{{ site_settings.company_name }}, {{ site_settings.company_street_address_1 }}, {{ site_settings.company_city }}, {{ site_settings.company_state }}
```

Salesforce Marketing Cloud (journey email; the item name is a field on the entry data extension):

```ampscript
%%[
VAR @fn, @item
SET @fn = AttributeValue("FirstName")
IF Empty(@fn) THEN SET @fn = "there" ENDIF
SET @item = AttributeValue("LastOrderItem")
]%%
Hi %%=v(@fn)=%%,

%%[ IF NOT Empty(@item) THEN ]%%Your %%=v(@item)=%% should have arrived a few days ago.%%[ ELSE ]%%Your order should have arrived a few days ago.%%[ ENDIF ]%% This is usually when fit questions come up, so here is the short version: if it is not right, exchanges are free for 60 days.

Care and fit guide: https://shop.northfield.example/care

<a href="%%unsub_center_url%%">Unsubscribe</a>
%%member_busname%%, %%member_addr%%, %%member_city%%, %%member_state%% %%member_postalcode%%
```

Token fallbacks: first name to "there"; item name to "new gear" or the no-item sentence; loop items
to "item". The HubL `contact.last_order_item` is guarded by the `if`. Technical notes: one link
besides unsubscribe; no images in this message; plain-text part identical. Measurement: second-order
rate within the repeat window against the flow holdout; the subject test reads on clicks after 1,000
sends per arm.
