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

from agents import campaign_architect as arch
from agents.models import Brief, CohortDefinition, Decision, PlannedTouch, Strategy

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
    roles = arch.roles_for(brief)
    days = arch.schedule_days(brief)
    nominal = (arch.WINBACK_DAYS if brief.flow_type == "winback" else arch.ACTIVATION_DAYS)[: brief.touches]
    if days != nominal:
        decisions.append(Decision(stage="strategist", decision=f"touch days {days}",
                                  why=f"spread from the default {nominal} so no seven days hold more than {brief.frequency_cap_per_week} messages"))
    picked = arch._channels_for(roles, brief.model_copy(update={"channels": channels}))
    touches = [PlannedTouch(index=i, day=d, role=r, channel=c, why=f"{ROLE_WHY[r]}; {CHANNEL_WHY[c]}")
               for i, (r, d, c) in enumerate(zip(roles, days, picked), 1)]
    if brief.flow_type == "winback" and brief.touches >= 2:
        decisions.append(Decision(stage="strategist", decision="end with re-permission and a sunset",
                                  why="people who ignore a whole win-back flow are the ones who complain; asking and then stopping protects the sender"))
    offer = ("no offer anywhere in the flow (offer policy)" if _no_offer(brief.offer_policy) else
             f"offers only as the policy allows ({brief.offer_policy}); never in the first touch, and tested against a no-offer arm")
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


def _no_offer(policy: str) -> bool:
    from agents.guardrails import _policy_allows_offers
    return not _policy_allows_offers(policy)
