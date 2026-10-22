# Worked example: win-back flow for Northfield Outfitters (fictional)

A model answer for `prompts/jobs/flows/winback.md` on Klaviyo: branch map, flow spec, templates and measurement.
Read it only when a model answer would help; the method and rules are in the job prompt.

**Inputs.** Northfield Outfitters (fictional), outdoor apparel, Shopify plus Klaviyo. The RFM job
(prompts/jobs/sql/rfm_segmentation.md) scored the repeat window at 58 days and syncs `lmm_rfm_segment` weekly. About 2,500
customers cross the window each month; 4% of them repurchase within 60 days with no flow (60 days because the median inter-purchase interval for returning customers is 41 days). Gate:
GREEN. Offers available: free shipping (approved), percentage discounts (founder approval only).

**Branch map.**

| Branch | Who | First touch | Offer |
|---|---|---|---|
| holdout | `lmm_holdout_winback` true (20%) | none: exits | none |
| high value | `cannot_lose`, `champions` | plain-text note from the head of customer care | none |
| standard | everyone else past the window | what is new in their category | none in message 1; free-shipping test in message 2 |
| sunset | no click or order 21 days into the flow | re-permission ask | none |

Customers with one order (the `instant_churn` pattern) are routed out at the first split, after the
wait: at trigger time everyone has just ordered, so an entry filter on their RFM segment would catch
no one. They belong in the second-order flow, where the job is a first repeat, not a return. The
repeat split runs before the holdout split so the two arms hold the same population.

**Flow spec** (validates against `schemas/flow_spec.schema.json`):

```json flow_spec
{
  "flow": "Win-back: lapsed past own cycle",
  "platform": "klaviyo",
  "analytics": "klaviyo and warehouse",
  "template_dialect": "klaviyo",
  "lifecycle_stage": "customer to lapsed",
  "objective": "a next order from customers who have gone quiet past their normal repeat window",
  "clv_lever": "lifespan",
  "gate_required": "GREEN",
  "trigger": {
    "type": "absence",
    "definition": "metric trigger Placed Order, then wait the repeat window; the flow filter removes anyone who orders in the meantime",
    "source_object": "Shopify orders via the Klaviyo Placed Order metric; repeat window from the RFM job (58 days at the last scoring)",
    "re_entry": "every new Placed Order restarts the clock; Klaviyo flows let a profile re-enter on each trigger event, so the flow filter is what prevents a double journey"
  },
  "entry_filters": [
    {
      "condition": "trigger filter: Placed Order value is greater than 0 (excludes $0 replacement and warranty orders)",
      "evaluated": "entry_only"
    }
  ],
  "ongoing_filters": [
    {
      "condition": "Placed Order zero times since starting this flow",
      "evaluated": "every_step"
    },
    {
      "condition": "can receive email marketing",
      "evaluated": "every_step"
    }
  ],
  "suppressions": [
    "unsubscribed or hard bounced",
    "open support ticket tagged refund or complaint",
    "in the post-purchase flow",
    "received any campaign with a discount in the last 14 days"
  ],
  "holdout": {
    "percent": 20,
    "persistent": true,
    "unit": "user",
    "assignment": "warehouse: lmm_holdout_winback = (abs(hash(customer_id)) % 5 = 0), synced as a profile property; holdout profiles still enter, wait the window and pass the repeat split, then exit at holdout_split, so both arms are the same population",
    "sizing": "about 2,500 entrants a month, 4% 60-day repurchase among lapsed customers, 1.5-point lift: 2,039 holdout entrants at 20%, 4.1 months of entries plus the 60-day outcome window, so the read lands about 6.0 months after launch (at the 15% win-back default: about 7.1 months) (lcm size --baseline 0.04 --mde-abs 0.015 --holdout 0.20 --entries-per-month 2500 --outcome-window-days 60)",
    "read": "causal, slow"
  },
  "frequency": {
    "cap": "Smart Sending on (16 hours); at most 3 lifecycle emails a week across flows",
    "send_windows": [
      {"channel": "email", "allowed_from": "08:00", "allowed_until": "21:00", "basis": "recipient_local", "source": "engine default"}
    ],
    "send_hour_local": "10:00",
    "priority": 3
  },
  "steps": [
    {
      "id": "repeat_split",
      "wait": {
        "type": "fixed",
        "duration": "P58D"
      },
      "channel": "none",
      "message": "-",
      "goal": "-",
      "branch": {
        "condition": "Placed Order at least 2 times over all time",
        "yes": "holdout_split",
        "no": "exit (one order only: the second-order flow owns them; routed out before the holdout split so both arms hold the same population)"
      }
    },
    {
      "id": "holdout_split",
      "wait": {
        "type": "none"
      },
      "channel": "none",
      "message": "-",
      "goal": "-",
      "branch": {
        "condition": "lmm_holdout_winback is true",
        "yes": "exit (holdout)",
        "no": "value_split"
      }
    },
    {
      "id": "value_split",
      "wait": {
        "type": "none"
      },
      "channel": "none",
      "message": "-",
      "goal": "-",
      "branch": {
        "condition": "lmm_rfm_segment is cannot_lose or champions",
        "yes": "personal_note",
        "no": "whats_new"
      }
    },
    {
      "id": "personal_note",
      "wait": {
        "type": "until_time_local",
        "local_time": "10:00"
      },
      "channel": "email",
      "message": "Plain-text note from the head of customer care; no offer",
      "goal": "reply or order",
      "requires": [
        "email_consent"
      ]
    },
    {
      "id": "whats_new",
      "wait": {
        "type": "until_time_local",
        "local_time": "10:00"
      },
      "channel": "email",
      "message": "What is new in their category since their last order; no discount",
      "goal": "order",
      "requires": [
        "email_consent"
      ]
    },
    {
      "id": "offer_test",
      "wait": {
        "type": "fixed",
        "duration": "P7D"
      },
      "condition": "no order and no click since whats_new",
      "channel": "email",
      "message": "A/B at message level: free shipping vs no offer",
      "goal": "order",
      "requires": [
        "email_consent"
      ]
    },
    {
      "id": "repermission",
      "wait": {
        "type": "fixed",
        "duration": "P14D"
      },
      "condition": "no order and no click in the flow",
      "channel": "email",
      "message": "Do you still want these emails? One button to stay",
      "goal": "click to stay subscribed",
      "requires": [
        "email_consent"
      ]
    },
    {
      "id": "sunset",
      "wait": {
        "type": "fixed",
        "duration": "P30D"
      },
      "condition": "no click on the re-permission email",
      "channel": "none",
      "message": "Update profile property lmm_sunset = true (suppression segment excludes from marketing)",
      "goal": "-"
    }
  ],
  "exit": {
    "goal": "Placed Order",
    "unenroll_immediately": true,
    "then": "post-purchase flow"
  },
  "measurement": {
    "primary_metric": "60-day repurchase rate from the repeat split (where the arms diverge), treatment vs holdout, on the Shopify orders object",
    "read_after": "2,039 holdout entrants have each completed 60 days (about 6.0 months after launch)",
    "compared_against": "holdout",
    "guardrails": [
      "unsubscribe rate per send",
      "spam complaints under 0.1%",
      "discount cost per incremental order (offer test)"
    ]
  },
  "data_dependencies": [
    {
      "name": "Placed Order",
      "kind": "event",
      "status": "verified",
      "fill_rate": null
    },
    {
      "name": "lmm_rfm_segment",
      "kind": "attribute",
      "status": "verified",
      "fill_rate": 1.0,
      "note": "synced by the RFM job; scored weekly"
    },
    {
      "name": "lmm_holdout_winback",
      "kind": "attribute",
      "status": "unknown",
      "fill_rate": null,
      "note": "warehouse job to be scheduled before launch"
    },
    {
      "name": "Favorite Category",
      "kind": "attribute",
      "status": "unknown",
      "fill_rate": null,
      "note": "used with a fallback in the template; a research finding if under 60% filled"
    },
    {
      "name": "lmm_orders_lifetime",
      "kind": "attribute",
      "status": "blocked",
      "fill_rate": null,
      "note": "used in the high-value note with a fallback; add lifetime_orders to the RFM sync before launch"
    }
  ]
}
```

**Templates** (Klaviyo dialect; `organization` fields come from the account settings).

Standard path, message 1:

```django klaviyo
Hi {{ first_name|default:'there' }},

It has been a while since your last order{% if person|lookup:'lmm_rfm_segment' == 'at_risk' %}, longer than usual for you{% endif %}. Here is what is new in {{ person|lookup:'Favorite Category'|default:'the shop' }} since then.

The fall layering pieces are back in every size, and the rain shell you asked about last season now comes in tall.

See what is new: https://shop.northfield.example/new

{% unsubscribe 'Unsubscribe' %} | {% manage_preferences 'Email preferences' %}
{{ organization.name }}, {{ organization.full_address }}
```

High-value path (plain text, from a person):

```django klaviyo
Hi {{ first_name|default:'there' }},

I look after customer care at Northfield. You have ordered from us {{ person|lookup:'lmm_orders_lifetime'|default:'many' }} times, and it has been longer than usual since the last one, so I wanted to ask directly: did something go wrong, or did we just stop being useful?

If anything was off with a recent order, reply and I will sort it out myself.

Rae Lindqvist
Customer care, Northfield Outfitters

{% unsubscribe 'Unsubscribe' %}
{{ organization.name }}, {{ organization.full_address }}
```

Re-permission (sunset):

```django klaviyo
Hi {{ first_name|default:'there' }},

You have not opened or clicked anything from us in a while, so we would rather ask than keep filling your inbox.

If you still want our emails, click below and nothing changes: {% manage_preferences 'Keep me subscribed' %}

If we do not hear from you, we will stop emailing you in 30 days. You can always come back.

{% unsubscribe 'Unsubscribe now' %}
{{ organization.name }}, {{ organization.full_address }}
```

The subject for the re-permission email states the question plainly ("Still want emails from
Northfield?"). Expect 1 to 5% to click; that is a normal result, not a failure.

**Measurement.** Primary metric: 60-day repurchase from the repeat split (the point both arms
diverge), treatment versus holdout, on the Shopify orders object (not Klaviyo-attributed revenue).
Sizing from `lcm size --baseline 0.04 --mde-abs 0.015 --holdout 0.20 --entries-per-month 2500
--outcome-window-days 60`: a 20% holdout needs 2,039 holdout entrants to detect a 1.5-point lift on a 4%
baseline, which is 4.1 months of entries plus the 60-day window, so the read lands about 6.0 months after
launch. The 15% win-back default would take about 7.1 months, so the spec uses 20%. Report attributed,
incremental and the gap with `lcm readout` and `prompts/jobs/measurement/holdout_incrementality.md`. The
free-shipping test reports cost per incremental order.
