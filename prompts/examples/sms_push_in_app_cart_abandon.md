# Worked example: checkout abandonment across email, push and SMS on Braze (fictional retailer)

A model answer for `prompts/jobs/copy/sms_push_in_app.md`. Read it only when a model answer would help.

Checkout abandonment for a fictional outdoor retailer on Braze. Trigger: `checkout_started` with no
`order_completed` within an hour. Goal: `order_completed` (exits every channel). Consent: email 86%,
SMS 22% (web-form checkbox, 10DLC registered), push permission on 41% of app users.

| Touch | Timing | Channel | Condition | Why this channel |
|---|---|---|---|---|
| 1 | +1 hour | email | email consent | least interruptive; can show the item |
| 2 | +4 hours, inside the push window | push | app user with permission, no order | they shop in the app; a deep link returns them to the cart |
| 3 | +20 hours, inside the SMS window | SMS | SMS consent, cart value 75 or more, has clicked an SMS before | high intent, expected channel for this person, time-sensitive stock |
| 4 | +48 hours | email | still no order | last touch; then stop |

Escalation stops at the first channel that converts: an order exits all four. Nobody receives both
push and SMS for the same cart within the same day.

Email touch 1 (Braze Liquid):

```liquid braze
Hi {{ ${first_name} | default: 'there' }},

{% assign items = event_properties.${items} %}{% if items.size > 0 %}The {{ items.first.name | default: 'gear' }} is still in your cart.{% else %}Your cart is still saved.{% endif %} Sizes move fast in the fall, and exchanges are free for 60 days if the fit is off.

Back to your cart: https://shop.northfield.example/cart

Not useful? {{${set_user_to_unsubscribed_url}}}
```

Push touch 2:

```liquid braze
{% assign items = event_properties.${items} %}Still deciding on the {{ items.first.name | default: 'gear' | truncate: 24 }}?
```

Body: "Free exchanges for 60 days. Tap to pick up where you left off." (deep link: the cart screen).

SMS touch 3, written for the longest rendering:

```liquid braze
Northfield: your {{ event_properties.${items}.first.name | default: 'order' | truncate: 32 }} is still in your cart. Free 60-day exchanges. https://nf.example.com/c/{{ ${user_id} }} Reply STOP to opt out
```

Encoding report (`engine/render/sms.py` on the worst-case rendering): the longest name `truncate: 32` can
produce (32 characters, e.g. "Ridgeline Packable Rain Shell...") and a 16-character `user_id`. The
id length is an assumption: confirm the maximum in the warehouse (`MAX(LENGTH(user_id))`) before
launch, because every character past 17 pushes the worst case into a second segment.

| Version | Encoding | Units | Segments |
|---|---|---|---|
| as written, GSM-7 only | GSM-7 | 159 | 1 |
| same text with one emoji | UCS-2 | 162 | 3 |

One character triples the cost of every send. The template has no emoji and no curly punctuation.

Permitted send windows (recipient local time): SMS 09:00 to 20:00 (the workspace default, the narrowest US state
window the rules file tracks), push 09:00 to 21:00, email 08:00 to 21:00. Caps: one push and one SMS per person per day
across all campaigns.

Measurement: one persistent holdout for the whole abandonment Canvas (Experiment Paths with a control
arm), read on orders within 7 days of `checkout_started`. Guardrails: SMS opt-out rate per send,
push disable rate, email complaints.
