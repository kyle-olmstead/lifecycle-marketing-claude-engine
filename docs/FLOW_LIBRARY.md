# The flow library

The engine plans ten flows. Each one is a definition in `engine/flows/library.py`: what starts it, what ends it, how many
messages it sends and when, who may receive it when the sending gate is AMBER, how a person re-enters, and the holdout it
starts with. The tables on this page are generated from that file and from the workspace template (`python3 -m
engine.docs_gen --write`), and a test fails when they drift.

## The flows

<!-- generated: flows -->
| Flow | What it does | Trigger | Goal | Touches (default / max) | Schedule | Holdout default | Re-entry | Models |
|---|---|---|---|---|---|---|---|---|
| `welcome` Welcome | new subscribers or signups get the brand promise and one reason to buy or start | signed_up | order_completed | 4 / 5 | 0, day 2, day 4, day 7 | 10% | never | all |
| `activation` Activation | new users reach the one action that means they are getting value | signed_up | activation_reached | 4 / 8 | 0, day 1, day 3, day 6 | 15% | never | saas |
| `winback` Win-back | people who stopped doing the core action come back, or are asked honestly whether to keep mailing | activation_reached | feature_used | 5 / 8 | 0, day 3, day 7, day 12, day 18 | 15% | after_exit | all |
| `cart_abandon` Abandoned cart | people who started checkout and did not order get their cart back within the hour | checkout_started | order_completed | 3 / 4 | 0, 23h, 71h | 10% | cooldown P7D | ecommerce |
| `browse_abandon` Browse abandonment | identified visitors who viewed a product and left get one reminder of it | product_viewed | order_completed | 2 / 3 | 0, 22h | 10% | cooldown P14D | ecommerce |
| `post_purchase` Post-purchase | first-time buyers get the most from what they bought, then a reason to come back | order_completed | order_completed | 4 / 4 | day 1, day 7, day 14, day 30 | 10% | never | ecommerce |
| `replenishment` Replenishment | buyers of a consumable hear from you just before it runs out | order_completed | order_completed | 2 / 3 | 0, day 5 | 10% | after_exit | ecommerce |
| `dunning` Failed payment (dunning) | a failed renewal is recovered before the subscription lapses | payment_failed | payment_succeeded | 3 / 4 | 0, day 3, day 7 | 0% (billing notices are never withheld: a holdout would cause involuntary churn. Read the flow against the recovery rate before launch and test message variants inside it) | after_exit | all |
| `trial_convert` Trial conversion | trial users reach value and choose a plan before the trial ends | trial_started | subscription_started | 4 / 5 | 0, day 7, day 11, day 13 | 10% | never | saas |
| `sunset` Sunset (re-permission) | people who stopped engaging are asked once whether to stay, then stop being mailed | message_delivered | message_clicked | 2 / 2 | 0, day 7 | 0% (list hygiene: the outcome is fewer unengaged recipients and a healthier sending gate, read from the gate's unengaged share and complaint rate, not a lift) | never | all |

Anything else (a referral program, a birthday series, a price-drop alert, a loyalty tier change) is refused by name rather than planned as the nearest flow. Hours-level waits are honored only by the abandoned cart and browse flows.
<!-- end generated -->

The schedule is when each touch sends after entry: `0` is at entry (after the abandonment wait for cart and browse flows),
`23h` is 23 hours later, `day 3` is three days later. Revise the number of touches with `lcm revise --set touches=N`, or
by saying it: `lcm "make it 4 touches"`.

## Permitted send windows and caps a new workspace starts with

<!-- generated: windows -->
| Channel | Permitted send window (recipient local time) |
|---|---|
| email | 08:00 to 21:00 |
| sms | 09:00 to 20:00 |
| push | 09:00 to 21:00 |

Caps across every flow: at most 3 email/sms/push per P7D; at most 1 sms per P1D. Collision rule: higher priority preempts. Priority: dunning, cart_abandon, browse_abandon, post_purchase, replenishment, welcome, trial_convert, activation, winback, sunset.

The SMS window is the narrowest state window config/compliance_rules.json tracks (09:00 to 20:00, rule state texting hours (strictest tracked)); a send hour outside a channel's window is refused when the brief is read, not after the plan is written.
<!-- end generated -->

Change them in `lmm.yaml` under `frequency`. `lcm simulate --program` checks every live flow together against them.

## Platforms

<!-- generated: platforms -->
| Platform | Template language | Built as | Frequency limit at send time | Unsubscribe link the emails carry |
|---|---|---|---|---|
| Klaviyo | Klaviyo template language (Django-style) | a flow | Smart Sending | `{% unsubscribe 'Unsubscribe' %}` |
| HubSpot | HubL | a contact-based workflow | the send-frequency cap in marketing email settings | `{{ unsubscribe_link }}` |
| Braze | Braze Liquid | a Canvas | Global frequency capping | `{{${set_user_to_unsubscribed_url}}}` |
| Customer.io | Customer.io Liquid | an event-triggered campaign | the workspace message limit | `{% unsubscribe_url %}` |
| Salesforce Marketing Cloud | AMPscript | a journey | the send-frequency setting | `%%unsub_center_url%%` |
| Iterable | Handlebars | a journey | Frequency Management in project settings (off by default) | `{{unsubscribeUrl}}` |
<!-- end generated -->
