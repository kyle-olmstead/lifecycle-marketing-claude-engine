"""The flow library: one definition per flow type, so every agent reads the same facts instead of branching on two.

v2 planned only win-back and activation, so a request for an abandoned-cart flow quietly became a blocked win-back
and "within 2 hours" was dropped. Each FlowDef below says how people enter (a typed trigger, with waits in hours where
the flow needs them), what ends the journey, the job of each touch and when it sends, whether a holdout is honest for
it, which tables it needs, and how to build it natively in each ESP. A request for a flow that is not here is refused
by name (UNSUPPORTED), never mapped to the nearest thing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional, Tuple

Kind = Literal["event", "absence", "state_change"]
GateClass = Literal["new_entrant", "behavior", "lapsed", "transactional", "remediation"]


@dataclass(frozen=True)
class FlowDef:
    key: str
    label: str
    summary: str
    kind: Kind
    gate_class: GateClass
    message_class: Literal["marketing", "transactional"]
    trigger_event: Dict[str, str]                 # business model ("ecommerce", "saas", "*") -> default trigger event
    goal_event: Dict[str, str]                    # business model -> default goal (core) event
    trigger_label: Dict[str, str]
    goal_label: Dict[str, str]
    roles: Dict[int, Tuple[str, ...]]             # touch count -> role per touch
    schedule_hours: Tuple[float, ...]             # nominal hours from entry, per touch position
    default_touches: int
    max_touches: int
    re_entry: Tuple[str, Optional[str]]           # (mode, cooldown)
    dedupe_key: str
    holdout_default: float
    holdout_reason_if_none: str = ""
    hour_level: bool = False                      # touches send when the wait ends (inside the send window), not at a daily hour
    abandon_after_hours: Optional[float] = None   # event flows: enter only if the goal has not happened this long after the trigger
    exclude_events: Tuple[str, ...] = ()          # events after the trigger that stop entry
    requires: Tuple[str, ...] = ("users", "track_events")
    models: Tuple[str, ...] = ("*",)              # business models it fits
    lifecycle_stage: str = ""
    clv_lever: Literal["acquisition", "transaction_value", "frequency", "lifespan"] = "lifespan"
    outcome_days: int = 14                        # default outcome window after the last touch, in days
    product_block: bool = False                   # touches can show the cart, viewed or purchased items
    native: Dict[str, str] = field(default_factory=dict)   # platform -> how to build the trigger natively

    def trigger_for(self, business_model: str) -> str:
        return self.trigger_event.get(_family(business_model), self.trigger_event.get("*", ""))

    def goal_for(self, business_model: str) -> str:
        return self.goal_event.get(_family(business_model), self.goal_event.get("*", ""))

    def roles_for(self, touches: int) -> Tuple[str, ...]:
        n = max(1, min(touches, self.max_touches))
        if n in self.roles:
            return self.roles[n]
        longest = self.roles[max(self.roles)]
        return longest[:n]

    def fits(self, business_model: str) -> bool:
        return "*" in self.models or _family(business_model) in self.models


def _family(business_model: str) -> str:
    return "ecommerce" if business_model in ("ecommerce", "marketplace") else "saas"


ESP_NATIVE_EVENT = {
    "klaviyo": "Metric-triggered flow on {trigger}{filter}. Flow filter (checked before every step): has not done {goal} since starting this flow.",
    "hubspot": "Contact-based workflow enrolled on the {trigger} event{filter} (custom event enrollment needs Professional or Enterprise "
               "since March 2026, knowledge.hubspot.com/events/create-custom-events; on Starter, sync the event to a contact property and "
               "enroll on the property). Goal criteria: {goal}.",
    "braze": "Canvas with action-based entry on {trigger}{filter}. Exception event / exit criteria: performs {goal}.",
    "customerio": "Event-triggered campaign on {trigger}{filter}. Conversion goal {goal} with exit on conversion.",
    "sfmc": "Journey Builder with an API Event entry on {trigger}{filter}. Goal and exit criteria: {goal}.",
    "iterable": "Journey with an event-triggered entry on {trigger}{filter}. Exit rule: performs {goal}.",
}

FLOWS: Dict[str, FlowDef] = {f.key: f for f in (
    FlowDef(
        key="welcome", label="Welcome", summary="new subscribers or signups get the brand promise and one reason to buy or start",
        kind="event", gate_class="new_entrant", message_class="marketing",
        trigger_event={"*": "signed_up"}, goal_event={"ecommerce": "order_completed", "saas": "activation_reached"},
        trigger_label={"*": "signed up"}, goal_label={"ecommerce": "placed a first order", "saas": "reached activation"},
        roles={1: ("welcome_promise",), 2: ("welcome_promise", "last_nudge"),
               3: ("welcome_promise", "best_sellers", "last_nudge"),
               4: ("welcome_promise", "best_sellers", "social_proof", "first_order_nudge"),
               5: ("welcome_promise", "best_sellers", "social_proof", "how_it_works", "first_order_nudge")},
        schedule_hours=(0, 48, 96, 168, 240), default_touches=4, max_touches=5,
        re_entry=("never", None), dedupe_key="user_id", holdout_default=10,
        lifecycle_stage="subscriber to first purchase", clv_lever="acquisition", outcome_days=14),
    FlowDef(
        key="activation", label="Activation", summary="new users reach the one action that means they are getting value",
        kind="event", gate_class="new_entrant", message_class="marketing",
        trigger_event={"*": "signed_up"}, goal_event={"*": "activation_reached"},
        trigger_label={"*": "signed up"}, goal_label={"*": "reached activation"},
        roles={n: tuple(r) for n, r in {
            1: ["first_step"], 2: ["first_step", "last_nudge"], 3: ["first_step", "unblock", "last_nudge"],
            4: ["first_step", "unblock", "show_outcome", "last_nudge"],
            5: ["first_step", "unblock", "show_outcome", "human_help", "last_nudge"],
            6: ["first_step", "unblock", "show_outcome", "human_help", "social_proof", "last_nudge"],
            7: ["first_step", "unblock", "show_outcome", "human_help", "social_proof", "use_case", "last_nudge"],
            8: ["first_step", "unblock", "show_outcome", "human_help", "social_proof", "use_case", "check_in", "last_nudge"]}.items()},
        schedule_hours=(0, 24, 72, 144, 240, 360, 504, 672), default_touches=4, max_touches=8,
        re_entry=("never", None), dedupe_key="user_id", holdout_default=15, models=("saas",),
        lifecycle_stage="new to activated", outcome_days=7),
    FlowDef(
        key="winback", label="Win-back", summary="people who stopped doing the core action come back, or are asked honestly whether to keep mailing",
        kind="absence", gate_class="lapsed", message_class="marketing",
        trigger_event={"*": "activation_reached", "ecommerce": "order_completed"}, goal_event={"*": "feature_used", "ecommerce": "order_completed"},
        trigger_label={"*": "reached activation", "ecommerce": "placed an order"}, goal_label={"*": "used the product", "ecommerce": "placed an order"},
        roles={n: tuple(r) for n, r in {
            1: ["value_reminder"], 2: ["value_reminder", "repermission"], 3: ["value_reminder", "friction_removal", "repermission"],
            4: ["value_reminder", "friction_removal", "whats_new", "repermission"],
            5: ["value_reminder", "friction_removal", "whats_new", "social_proof", "repermission"],
            6: ["value_reminder", "friction_removal", "whats_new", "social_proof", "human_help", "repermission"],
            7: ["value_reminder", "friction_removal", "whats_new", "use_case", "social_proof", "human_help", "repermission"],
            8: ["value_reminder", "friction_removal", "whats_new", "use_case", "social_proof", "human_help", "check_in", "repermission"]}.items()},
        schedule_hours=tuple(d * 24 for d in (0, 3, 7, 12, 18, 25, 32, 40)), default_touches=5, max_touches=8,
        re_entry=("after_exit", None), dedupe_key="user_id + anchor_date (one entry per lapse)", holdout_default=15,
        lifecycle_stage="active to lapsed", outcome_days=7),
    FlowDef(
        key="cart_abandon", label="Abandoned cart", summary="people who started checkout and did not order get their cart back within the hour",
        kind="event", gate_class="behavior", message_class="marketing",
        trigger_event={"*": "checkout_started"}, goal_event={"*": "order_completed"},
        trigger_label={"*": "started checkout"}, goal_label={"*": "placed an order"},
        roles={1: ("cart_reminder",), 2: ("cart_reminder", "cart_last_call"), 3: ("cart_reminder", "cart_objection", "cart_last_call"),
               4: ("cart_reminder", "cart_objection", "social_proof", "cart_last_call")},
        schedule_hours=(0, 23, 71, 167), default_touches=3, max_touches=4, hour_level=True, abandon_after_hours=1,
        re_entry=("cooldown", "P7D"), dedupe_key="user_id + checkout_id", holdout_default=10,
        requires=("users", "track_events", "orders"), models=("ecommerce",), product_block=True,
        lifecycle_stage="shopper to buyer", clv_lever="frequency", outcome_days=7,
        native={"klaviyo": "Metric-triggered flow on Started Checkout (Shopify's own event, or checkout_started sent by the engine). "
                           "First step: a time delay of {abandon}. Flow filter: Placed Order zero times since starting this flow. "
                           "Re-entry: once per 7 days (flow settings, or a filter 'has not been in this flow in the last 7 days')."}),
    FlowDef(
        key="browse_abandon", label="Browse abandonment", summary="identified visitors who viewed a product and left get one reminder of it",
        kind="event", gate_class="behavior", message_class="marketing",
        trigger_event={"*": "product_viewed"}, goal_event={"*": "order_completed"},
        trigger_label={"*": "viewed a product"}, goal_label={"*": "placed an order"},
        roles={1: ("browse_reminder",), 2: ("browse_reminder", "browse_alternatives"), 3: ("browse_reminder", "browse_alternatives", "social_proof")},
        schedule_hours=(0, 22, 70), default_touches=2, max_touches=3, hour_level=True, abandon_after_hours=2,
        exclude_events=("checkout_started", "product_added"),
        re_entry=("cooldown", "P14D"), dedupe_key="user_id + product_id + day", holdout_default=10,
        models=("ecommerce",), product_block=True, lifecycle_stage="browser to shopper", clv_lever="frequency", outcome_days=7,
        native={"klaviyo": "Metric-triggered flow on Viewed Product. Time delay {abandon}. Flow filters: Started Checkout zero times and "
                           "Placed Order zero times since starting this flow; has not been in the abandoned cart flow in the last 7 days; "
                           "has not been in this flow in the last 14 days."}),
    FlowDef(
        key="post_purchase", label="Post-purchase", summary="first-time buyers get the most from what they bought, then a reason to come back",
        kind="event", gate_class="behavior", message_class="marketing",
        trigger_event={"*": "order_completed"}, goal_event={"*": "order_completed"},
        trigger_label={"*": "placed a first order"}, goal_label={"*": "placed a second order"},
        roles={1: ("order_thanks",), 2: ("order_thanks", "cross_sell"), 3: ("order_thanks", "how_to_use", "cross_sell"),
               4: ("order_thanks", "how_to_use", "review_request", "cross_sell")},
        schedule_hours=(24, 168, 336, 720), default_touches=4, max_touches=4,
        re_entry=("never", None), dedupe_key="user_id (first order only)", holdout_default=10,
        requires=("users", "track_events", "orders"), models=("ecommerce",), product_block=True,
        lifecycle_stage="first purchase to repeat", clv_lever="frequency", outcome_days=30,
        native={"klaviyo": "Metric-triggered flow on Placed Order with the trigger filter 'Placed Order equals 1 over all time' (first order). "
                           "Flow filter: Placed Order zero times since starting this flow, for the goal."}),
    FlowDef(
        key="replenishment", label="Replenishment", summary="buyers of a consumable hear from you just before it runs out",
        kind="absence", gate_class="behavior", message_class="marketing",
        trigger_event={"*": "order_completed"}, goal_event={"*": "order_completed"},
        trigger_label={"*": "bought a consumable"}, goal_label={"*": "reordered"},
        roles={1: ("replenish_reminder",), 2: ("replenish_reminder", "replenish_last"), 3: ("replenish_reminder", "replenish_nudge", "replenish_last")},
        schedule_hours=(0, 120, 288), default_touches=2, max_touches=3,
        re_entry=("after_exit", None), dedupe_key="user_id + anchor_date (one entry per purchase cycle)", holdout_default=10,
        requires=("users", "track_events", "orders", "line_items"), models=("ecommerce",), product_block=True,
        lifecycle_stage="repeat buyer to habit", clv_lever="frequency", outcome_days=14,
        native={"klaviyo": "Metric-triggered flow on Ordered Product with a trigger filter for the product. Time delay of {lead} days. "
                           "Flow filter: Ordered Product (that product) zero times since starting this flow."}),
    FlowDef(
        key="dunning", label="Failed payment (dunning)", summary="a failed renewal is recovered before the subscription lapses",
        kind="event", gate_class="transactional", message_class="transactional",
        trigger_event={"*": "payment_failed"}, goal_event={"*": "payment_succeeded"},
        trigger_label={"*": "had a payment fail"}, goal_label={"*": "updated payment and paid"},
        roles={1: ("payment_failed",), 2: ("payment_failed", "payment_final"), 3: ("payment_failed", "payment_retry", "payment_final"),
               4: ("payment_failed", "payment_retry", "payment_final", "access_paused")},
        schedule_hours=(0, 72, 168, 240), default_touches=3, max_touches=4,
        re_entry=("after_exit", None), dedupe_key="invoice_id", holdout_default=0,
        holdout_reason_if_none=("billing notices are never withheld: a holdout would cause involuntary churn. Read the flow against the "
                                "recovery rate before launch and test message variants inside it"),
        requires=("users", "track_events"), lifecycle_stage="at risk (billing)", clv_lever="lifespan", outcome_days=7,
        native={"customerio": "Event-triggered campaign on the billing system's payment_failed (Stripe invoice.payment_failed through the "
                              "integration or a webhook). Exit on payment_succeeded or subscription_cancelled."}),
    FlowDef(
        key="trial_convert", label="Trial conversion", summary="trial users reach value and choose a plan before the trial ends",
        kind="event", gate_class="new_entrant", message_class="marketing",
        trigger_event={"*": "trial_started"}, goal_event={"*": "subscription_started"},
        trigger_label={"*": "started a trial"}, goal_label={"*": "started a paid subscription"},
        roles={1: ("trial_ending",), 2: ("trial_welcome", "trial_ending"), 3: ("trial_welcome", "trial_value", "trial_ending"),
               4: ("trial_welcome", "trial_value", "trial_ending", "trial_last_day"),
               5: ("trial_welcome", "trial_value", "trial_ending", "trial_last_day", "trial_ended")},
        schedule_hours=(0, 168, 264, 312, 360), default_touches=4, max_touches=5,
        re_entry=("never", None), dedupe_key="user_id", holdout_default=10, models=("saas",),
        lifecycle_stage="trial to paid", clv_lever="acquisition", outcome_days=7),
    FlowDef(
        key="sunset", label="Sunset (re-permission)", summary="people who stopped engaging are asked once whether to stay, then stop being mailed",
        kind="absence", gate_class="remediation", message_class="marketing",
        trigger_event={"*": "message_delivered"}, goal_event={"*": "message_clicked"},
        trigger_label={"*": "kept receiving email"}, goal_label={"*": "clicked to stay subscribed"},
        roles={1: ("repermission",), 2: ("repermission", "repermission_reminder")},
        schedule_hours=(0, 168), default_touches=2, max_touches=2,
        re_entry=("never", None), dedupe_key="user_id", holdout_default=0,
        holdout_reason_if_none=("list hygiene: the outcome is fewer unengaged recipients and a healthier sending gate, read from the "
                                "gate's unengaged share and complaint rate, not a lift"),
        requires=("users", "track_events", "message_events"), lifecycle_stage="unengaged to re-permissioned or sunset", outcome_days=14),
)}

# Requests the engine recognizes but does not plan yet: refused by name, never mapped to the nearest flow type.
UNSUPPORTED: Dict[str, Tuple[str, str]] = {
    "birthday": (r"birthday|anniversary", "birthday and anniversary flows need a date attribute trigger, which is not built yet"),
    "back_in_stock": (r"back[- ]in[- ]stock|restock alert", "back-in-stock needs a catalog inventory feed and a waitlist event"),
    "price_drop": (r"price[- ]drop", "price-drop alerts need catalog price history"),
    "referral": (r"referral|refer[- ]a[- ]friend", "referral programs need a referral-tracking source"),
    "lead_nurture": (r"lead nurture|nurture leads|mql|sales[- ]qualified", "B2B lead nurture belongs in the CRM's sequences; the engine "
                                                                           "plans customer lifecycle flows"),
    "loyalty": (r"loyalty|\bvip\b|points balance", "loyalty and VIP flows need a loyalty program source"),
    "transactional": (r"order confirmation|shipping (notification|confirmation|update)|delivery (notification|update)|"
                      r"\breceipts?\b|password reset|order status|tracking (email|notification)",
                      "order confirmations, shipping updates, receipts and password resets are transactional messages your store or "
                      "platform sends itself; the engine plans lifecycle flows"),
    "campaign": (r"\bnewsletters?\b|black friday|cyber monday|holiday (campaign|sale|promo)|flash sale|\bpromo(tion)?s? (email|blast|campaign)|"
                 r"\bbroadcast\b|one-off (send|email|campaign)|launch announcement|\bcampaign\b",
                 "a newsletter or a one-off campaign is a send, not a flow; lcm blast compares a send with your flows "
                 "(lcm \"compare our monthly newsletter with the win-back trigger\")"),
    "survey": (r"\bnps\b|survey", "survey flows are not built yet; send them from your survey tool"),
    "wishlist": (r"wish ?lists?|saved for later|saved items|favou?rites list",
                 "wishlist reminders need a wishlist event (added_to_wishlist) from the store, which the tracking plan does not carry yet"),
    "cross_sell": (r"cross[- ]?sell\w*", "a cross-sell flow needs product affinity data the engine does not read yet; a post-purchase flow's "
                                        "last touch suggests the next product after an order (\"a 3-touch post-purchase flow\")"),
    "upsell": (r"\bup[- ]?sell\w*|\bupgrade (them |people |users |customers )?(to|into) (an? |the )?(annual|yearly|higher|bigger|premium|pro|"
               r"business|enterprise)\b|\bexpansion (flow|campaign|revenue)",
               "upsell and plan-upgrade flows need plan and usage data the engine does not read yet; a trial conversion flow covers "
               "free to paid"),
}
# checked before the library's own flow names, which would otherwise read these as a post-purchase or trial flow
UNSUPPORTED_FIRST = ("wishlist", "cross_sell", "upsell")
# refused only when the sentence names no flow of its own: "a win-back for people who bought on Black Friday" is a win-back
UNSUPPORTED_SOFT = ("campaign",)


def get(flow_type: str) -> FlowDef:
    if flow_type not in FLOWS:
        raise ValueError(f"unknown flow type {flow_type!r}; the library has: {', '.join(FLOWS)}")
    return FLOWS[flow_type]


def schedule_hours(fd: FlowDef, touches: int, trial_days: Optional[int] = None) -> List[float]:
    """Nominal hours from entry per touch. Trial flows are anchored to the trial's end, so they scale with its length."""
    n = max(1, min(touches, fd.max_touches))
    if fd.key == "trial_convert":
        t = trial_days or 14
        anchors = {"trial_welcome": 0, "trial_value": max(1, t // 2) * 24, "trial_ending": max(1, t - 3) * 24,
                   "trial_last_day": max(1, t - 1) * 24, "trial_ended": (t + 1) * 24}
        return sorted(anchors[r] for r in fd.roles_for(n))
    return list(fd.schedule_hours[:n])
