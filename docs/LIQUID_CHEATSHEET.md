# Personalization syntax cheat sheet (Liquid and the rest)

> **In plain English.** "Liquid" gets used as a word for all email personalization code. It isn't.
> Braze and Customer.io use Liquid. Klaviyo uses its own Django-style language, HubSpot uses HubL,
> and Salesforce Marketing Cloud uses AMPscript. Code written for one breaks in another, often
> silently, so the engine names the dialect on every block and lints it with
> `engine/render/template_lint.py`. Every code block on this page passes that linter in CI.

Platform facts were read from each vendor's documentation in October 2026. Platforms add tags and
filters; check their docs for anything not listed here.

## 1. Who speaks what

| Platform | Dialect | Fence info string | Linter dialect | Parser behind the linter |
|---|---|---|---|---|
| Customer.io | Liquid ("latest"; older workspaces may be on legacy) | `liquid customerio` | `customerio`, `customerio_legacy` | python-liquid |
| Braze | Liquid (up to Shopify Liquid 5) plus Braze attribute syntax | `liquid braze` | `braze` | python-liquid, with Braze tags checked by pattern |
| Klaviyo | Django-style template language with Klaviyo tags and filters | `django klaviyo` | `klaviyo` | Django's template engine |
| HubSpot | HubL (Jinja-derived) | `hubl` | `hubl` | Jinja2 |
| Salesforce Marketing Cloud | AMPscript | `ampscript` | `ampscript` | structural checks (no public parser) |

## 2. The same five jobs in every dialect

| Job | Customer.io | Braze | Klaviyo | HubSpot | SFMC |
|---|---|---|---|---|---|
| First name with fallback | `{{ customer.first_name \| default: "there" }}` | `{{ ${first_name} \| default: 'there' }}` | `{{ first_name\|default:'there' }}` | `{{ personalization_token("contact.firstname", "there") }}` | `IF Empty(@fn) THEN SET @fn = "there" ENDIF` |
| Custom profile field | `{{ customer.plan }}` | `{{ custom_attribute.${plan} }}` | `{{ person\|lookup:'Plan' }}` | `{{ contact.plan }}` | `AttributeValue("Plan")` |
| Trigger event property | `{{ event.plan }}` | `{{ event_properties.${plan} }}` | `{{ event\|lookup:'plan' }}` | (none in marketing email) | data extension field |
| Unsubscribe link | `{% unsubscribe_url %}` | `{{${set_user_to_unsubscribed_url}}}` | `{% unsubscribe %}` or `{% unsubscribe_link %}` | `{{ unsubscribe_link }}` | `%%unsub_center_url%%` |
| Postal address | literal or snippet | literal, or `{{${email_footer}}}` | `{{ organization.full_address }}` | `site_settings.company_street_address_1` and city, state | `%%member_addr%%` and siblings |

The backslash before each pipe in the table is Markdown escaping; in code the pipe is plain.

## 3. Customer.io

Objects, and when each exists:

| Object | Available in |
|---|---|
| `customer.<attribute>` | every message |
| `event.<property>` | event-triggered campaigns only, and only the triggering event's data |
| `trigger.<property>` | transactional messages, API-triggered broadcasts, webhook-triggered actions |
| `journey.<attribute>` | journey attributes set by webhook actions |

Customer.io fails a send when a referenced value is missing and has no fallback (it shows as Failed
in the message log), so fallbacks are not optional here.

```liquid customerio
Hi {{ customer.first_name | default: "there" }},

{% if event.plan == "growth" %}Your Growth plan includes scheduled reports.{% else %}Scheduled reports are one click away when you need them.{% endif %}

{% for item in event.items limit: 3 %}- {{ item.name | default: "item" }}
{% endfor %}
{% unsubscribe_url %}
```

Legacy Liquid has no `default` filter. Guard instead, or upgrade the workspace:

```liquid customerio_legacy
{% if customer.first_name != blank %}Hi {{ customer.first_name }},{% else %}Hi there,{% endif %}
```

Customer.io-only tags the linter knows: `unsubscribe`, `unsubscribe_url`,
`manage_subscription_preferences_url`, `tracking_consent_url`, `subscription_topic_name`,
`view_in_browser_url`, `cio_link`, `cio_link_id`, `render_liquid`, `countdown`, `generate_uuid`.

## 4. Braze

Braze wraps attribute names in `${...}`, which lets names contain spaces.

```liquid braze
Hi {{ ${first_name} | default: 'there' }},

{% if custom_attribute.${loyalty tier} == 'gold' %}Gold members get free returns.{% endif %}

{% connected_content https://api.example.com/recommendations/{{ ${user_id} }} :save recs %}
{% if recs.items.size < 3 %}{% abort_message('fewer than three recommendations') %}{% endif %}

{{${set_user_to_unsubscribed_url}}}
```

- `{{ canvas_entry_properties.${name} }}` reads Canvas entry properties;
  `{{ api_trigger_properties.${name} }}` reads API-trigger properties;
  `{{ content_blocks.${name} }}` inserts a Content Block.
- `abort_message('reason')` takes plain text only; Liquid inside the reason is not allowed.
  Aborted sends are not counted as sends.
- A custom footer must include `{{${set_user_to_unsubscribed_url}}}` and a postal address.

## 5. Klaviyo

A Django-style language: filters follow a pipe with arguments after a colon, in single quotes, and
**no space after the colon** (`default:'there'`, not `default: 'there'`; Django's parser rejects the
space, and the linter does too).

```django klaviyo
Hi {{ first_name|default:'there' }},

{% if person|lookup:'Loyalty Tier' == 'gold' %}Gold members get free returns.{% endif %}

Your favorite color is {{ person|lookup:'Favorite Color'|default:'still a mystery' }}.

{% for item in event.items|slice:':3' %}- {{ item.name|default:'item' }}
{% endfor %}
{% unsubscribe 'Unsubscribe' %} | {% manage_preferences 'Preferences' %}
{{ organization.name }}, {{ organization.full_address }}
```

- `person|lookup:'Name'` reads properties whose names contain spaces; `person.name` works for simple
  names. Tags and properties are case sensitive.
- Klaviyo supports most Django filters plus its own (`lookup`, `currency_format`, `floatformat`
  and others). An unknown filter is a warning from the linter, not an error, because Klaviyo adds
  filters over time.

## 6. HubSpot (HubL)

```hubl
Hi {{ personalization_token("contact.firstname", "there") }},

{% if contact.lifecyclestage == "customer" %}Thanks for being a customer.{% else %}Here is what customers use most.{% endif %}

<a href="{{ unsubscribe_link }}">Unsubscribe</a>
{{ site_settings.company_name }}, {{ site_settings.company_street_address_1 }}, {{ site_settings.company_city }}, {{ site_settings.company_state }}
```

- `personalization_token(expression, default)` returns the property or the default; prefer it to a
  bare `{{ contact.firstname }}`, which renders empty when the property is missing.
- Coded email templates must include `site_settings.company_name`, `company_street_address_1`,
  `company_city`, `company_state` and `unsubscribe_link`.
- Marketing emails cannot loop over a contact's orders; have the workflow copy what you need onto a
  contact property first.

## 7. Salesforce Marketing Cloud (AMPscript)

```ampscript
%%[
VAR @fn, @tier
SET @fn = AttributeValue("FirstName")
IF Empty(@fn) THEN SET @fn = "there" ENDIF
SET @tier = AttributeValue("LoyaltyTier")
]%%
Hi %%=v(@fn)=%%,
%%[ IF @tier == "gold" THEN ]%%Gold members get free returns.%%[ ENDIF ]%%
<a href="%%unsub_center_url%%">Unsubscribe</a>
%%member_busname%%, %%member_addr%%, %%member_city%%, %%member_state%% %%member_postalcode%%
```

- Code blocks sit between `%%[` and `]%%`; inline output between `%%=` and `=%%`.
- `AttributeValue("Name")` returns empty when the field is missing from the sendable data;
  referencing `[Name]` directly errors the send. Guard every output with `Empty()` or `IsNull()`.
- `IF ... ENDIF` and `FOR ... NEXT` must balance across blocks; the linter checks them across the
  whole template.

## 8. Pitfalls that pass a casual review

| Pitfall | Where | What happens | Fix |
|---|---|---|---|
| `default` replaces `false` | Liquid | `{{ vip \| default: 'yes' }}` prints "yes" when vip is false | compare explicitly (`{% if vip == false %}`), or use `allow_false: true` where the platform supports it |
| Integer division | Liquid | `{{ 7 \| divided_by: 2 }}` is 3 | divide by `2.0` |
| String comparison is case sensitive | every dialect | "Gold" is not "gold" | normalize upstream, or `\| downcase` (Liquid), `\|lower` (Klaviyo, HubL) |
| A space after the filter colon | Klaviyo | the template fails to save or render | `default:'x'` |
| Liquid habits in Klaviyo | Klaviyo | `{{ x \| upcase }}` is not a Klaviyo filter | Django names: `\|upper`, `\|title` |
| `event.*` in a segment-triggered campaign | Customer.io | the message fails: there is no triggering event | read `customer.*` or switch to an event trigger |
| Bare `[Field]` | AMPscript | the send errors when the field is absent | `AttributeValue("Field")` |
| A token with no fallback | every dialect | "Hi ," | a fallback on every token |

## 9. Check any template

```text
python3 engine/render/template_lint.py --dialect customerio email.html          # syntax and fallback rules
python3 engine/render/template_lint.py --dialect klaviyo --email email.html     # plus unsubscribe, address, alt text, size, dark mode
```
