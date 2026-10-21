"""Strategist: decides the shape of the campaign, and says why, before anything is built.

The Strategist turns the brief and the Analyst's findings into a Strategy: which job each touch
does, on which day, on which channel, and what the copy may offer. Every decision is logged with its
reason, so the plan can explain itself and a later revision can see what changed. The Campaign
Architect then compiles the Strategy into a flow spec; it makes no strategic choices of its own.

Decisions that use data, when the Analyst had data to look at:
  * a channel the brief asked for is dropped when almost nobody can receive it (SMS consent under 5%,
    push permission or sessions not tracked), and its touches fall back to email
  * an activation flow's unblocking touch is aimed at the onboarding step where most people stall
Decisions that never change with data:
  * email opens and closes every flow; the re-permission ask is always email
  * at most one SMS per flow; push, SMS and in-app each carry an email fallback for people without consent
  * touches are spaced so no seven days hold more than the weekly cap
"""
from __future__ import annotations

from typing import Dict, List, Optional

from engine.agents import campaign_architect as arch
from engine.models import Brief, CohortDefinition, Decision, PlannedTouch, Strategy

MIN_SMS_CONSENT = 0.05

ROLE_WHY: Dict[str, str] = {
    "value_reminder": "opens on what they already got from the product: the cheapest reason to come back",
    "friction_removal": "the most common reason people stop is a specific obstacle; naming and removing it beats a generic nudge",
    "whats_new": "a lapsed user's picture of the product is out of date; one concrete change updates it",
    "social_proof": "a routine copied from a similar team lowers the effort of deciding what to do",
    "human_help": "late in the flow, a named person offering help converts people that automated copy has not",
    "use_case": "a second use case re-opens the door for people who finished their first one",
    "check_in": "asks what is missing; replies are product research even when they do not convert",
    "repermission": "ends honestly: asks whether to keep mailing, which protects the sender reputation every other flow depends on",
    "first_step": "a new user needs one next step, not a tour",
    "unblock": "aimed at the onboarding step where the most people stall",
    "show_outcome": "shows the finished result so the effort feels worth it",
    "last_nudge": "a low-pressure last reminder, then the flow stops",
    "repermission_reminder": "one reminder of the ask, with the date mail stops, then the sunset",
    "welcome_promise": "the first message is the most read one a brand ever sends: spend it on the promise and the first step",
    "best_sellers": "a new subscriber does not know the range; the safe first pick removes the hardest decision",
    "how_it_works": "uncertainty about delivery, fit and returns is the usual reason a first order waits",
    "first_order_nudge": "a plain reminder converts the undecided; an offer only where the policy allows one",
    "cart_reminder": "most recovered carts come from the first hour: show the cart, nothing else",
    "cart_objection": "the second touch answers why they stopped (cost, timing, returns) instead of repeating the first",
    "cart_last_call": "a final reminder while intent is still warm; the offer, if any, comes last so early buyers pay full price",
    "browse_reminder": "the product they chose is the most relevant thing you can show them",
    "browse_alternatives": "a viewer who did not add to cart is often still choosing; a comparison helps them decide",
    "order_thanks": "the moment after a first order is when expectations are set and returns are prevented",
    "how_to_use": "value delivered drives the second order more than any promotion",
    "review_request": "asked after delivery, a review request turns satisfaction into proof for the next buyer",
    "cross_sell": "the best second purchase complements the first",
    "replenish_reminder": "timed to their own usage, a reminder is service, not marketing",
    "replenish_nudge": "a second reminder catches people who meant to reorder and forgot",
    "replenish_last": "the last reminder before they buy elsewhere",
    "payment_failed": "most failed payments are expired cards, fixed in a minute when asked plainly and early",
    "payment_retry": "a dated reminder before the next retry",
    "payment_final": "the final notice carries the billing system's real date, so the urgency is true",
    "access_paused": "a clear way back keeps a paused customer from becoming a churned one",
    "trial_welcome": "the first session decides most trials; one step to the value",
    "trial_value": "halfway through, progress plus the feature that justifies paying",
    "trial_ending": "three days of notice, with the exact date, is honest and converts the decided",
    "trial_last_day": "the last day is when undecided users decide",
    "trial_ended": "after the trial, a plain way to pick a plan",
}
CHANNEL_WHY: Dict[str, str] = {
    "email": "email carries the long-form message, the unsubscribe and the fallback for every other channel",
    "sms": "SMS is used once, mid-flow, for a short prompt; it is the channel people tolerate least",
    "push": "push reaches app users for a one-line prompt; an email fallback covers anyone without permission",
    "in_app": "in-app waits for the next session, so it only reaches people already back, and expires after three days",
}


def _dep(cohort: CohortDefinition, name: str):
    return next((d for d in cohort.data_dependencies if d.name == name), None)


def _usable_channels(brief: Brief, cohort: CohortDefinition, decisions: List[Decision]) -> List[str]:
    channels = list(brief.channels)
    for ch, dep_name in (("sms", "sms_consent"), ("push", "push_permission_changed"), ("in_app", "session_started")):
        if ch not in channels:
            continue
        dep = _dep(cohort, dep_name)
        if dep is None:
            continue
        if dep.status == "blocked":
            channels.remove(ch)
            decisions.append(Decision(stage="strategist", decision=f"drop {ch}",
                                      why=f"{dep_name} is not in the data ({dep.note}); every {ch} touch would fall back to email"))
        elif ch == "sms" and dep.fill_rate is not None and dep.fill_rate < MIN_SMS_CONSENT:
            channels.remove(ch)
            decisions.append(Decision(stage="strategist", decision="drop sms",
                                      why=f"only {dep.fill_rate:.1%} of people have SMS consent, so an SMS touch would reach almost nobody"))
    return channels


def decide(brief: Brief, cohort: CohortDefinition) -> Strategy:
    decisions: List[Decision] = []
    channels = _usable_channels(brief, cohort, decisions)
    from engine.flows import library
    fd = brief.flow
    roles = arch.roles_for(brief)
    hours = arch.schedule_hours(brief)
    nominal = library.schedule_hours(fd, brief.touches, brief.trial_days)
    if fd.hour_level:
        nominal = [h + (brief.abandon_after_hours or 0) for h in nominal]
        decisions.append(Decision(stage="strategist", decision=f"touches at {', '.join(f'{h:g}h' for h in hours)} after {brief.qualifying_event.event}",
                                  why="intent fades in hours, not days; each send is held to its channel's permitted window"))
    if hours != nominal:
        unit = (lambda h: f"{h:g}h") if fd.hour_level else (lambda h: f"day {int(h // 24)}")  # noqa: E731
        decisions.append(Decision(stage="strategist", decision=f"touches at {', '.join(unit(h) for h in hours)}",
                                  why=f"spread from {', '.join(unit(h) for h in nominal)} so no cap is broken ({brief.freq.describe()})"))
    picked = arch._channels_for(roles, brief.model_copy(update={"channels": channels}))
    touches = [PlannedTouch(index=i, day=int(h // 24), hours=h if fd.hour_level else None, role=r, channel=c,
                            why=f"{ROLE_WHY[r]}; {CHANNEL_WHY[c]}")
               for i, (r, h, c) in enumerate(zip(roles, hours, picked), 1)]
    if brief.flow_type == "winback" and brief.touches >= 2:
        decisions.append(Decision(stage="strategist", decision="end with re-permission and a sunset",
                                  why="people who ignore a whole win-back flow are the ones who complain; asking and then stopping protects the sender"))
    policy = brief.offer
    offer_roles = [r for r in roles[1:] if r in arch.OFFER_ROLES] if policy.never_in_first_touch else [r for r in roles if r in arch.OFFER_ROLES]
    offer = ("no offer anywhere in the flow (offer policy)" if not policy.allows_offers else
             f"offers only as the policy allows ({policy.describe()}); only in {', '.join(r.replace('_', ' ') for r in offer_roles) or 'no touch'}"
             + ("; never in the first touch" if policy.never_in_first_touch else "")
             + (f"; excluded for {', '.join(x.replace('_', ' ') for x in policy.exclude_if)}" if policy.exclude_if else ""))
    decisions.append(Decision(stage="strategist", decision="offer stance", why=offer))
    leak: Optional[str] = cohort.leak if brief.flow_type == "activation" else None
    if leak and "unblock" in roles:
        decisions.append(Decision(stage="strategist", decision=f"aim the unblock touch at {leak}", why="the largest drop in the onboarding funnel"))
    unused = [c for c in channels if c not in picked]
    if unused:
        decisions.append(Decision(stage="strategist", decision=f"{', '.join(unused)} not used",
                                  why=f"{brief.touches} touch(es) leave no middle position for them; email opens and closes the flow"))
    return Strategy(touches=touches, channels_requested=list(brief.channels), channels_used=sorted(set(picked)), offer=offer,
                    leak_step=leak, decisions=decisions)


