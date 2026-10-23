"""Campaign Architect: the multi-channel logic tree, as a flow spec the platform can be built from.

Given the brief, the cohort and the Strategist's decisions, it writes:

  flow_spec   valid against schemas/flow_spec.schema.json: the typed trigger, entry and every-step filters,
              suppressions, the typed frequency policy and send windows, steps with waits (in hours where the flow
              is timed in hours) and consent requirements, exit, data dependencies (the holdout and measurement
              blocks are attached by the experimentation agent)
  simulation  machine-readable rules for every condition and branch, so engine/policy/simulator.py can walk
              synthetic people through the flow before anything is built
  mermaid     the logic tree as a diagram for the plan
  build_steps the build, step by step, in the platform's own vocabulary

Every flow type comes from engine/flows/library.py: its roles, its schedule, its trigger, its re-entry rule.

Design rules it never breaks:
  * Exit before content: the goal ends the journey at every step, not only at the next send.
  * The holdout split is the first step, before any message, so both arms are the same population.
  * Email is the backbone. SMS and push touches carry an email fallback for people without that
    consent; at most one SMS per flow. In-app messages wait for the next session and expire.
  * No cap in the frequency policy is broken, and every send lands inside its channel's permitted window.
  * Win-back ends with an honest re-permission ask and a sunset, never an endless drip.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.flows import library  # noqa: E402
from engine.models import PLATFORM_LABEL, Brief, CohortDefinition, FlowPlan, Strategy, Touch  # noqa: E402
from engine.policy import guardrails  # noqa: E402
from engine.policy.contracts import hours_to_duration  # noqa: E402

ROLES: Dict[str, Dict[str, str]] = {
    # win-back and activation
    "value_reminder": {"job": "Remind them of the one outcome {product} gave them, tied to what they last did, with one link that lets them {core_do} again. No offer.",
                       "framework": "BAB: before, after, bridge", "goal": "{core}"},
    "friction_removal": {"job": "Name the most common reason people stop at this point and remove it: a template, a two-minute path, or a reply to a person.",
                         "framework": "PAS: problem, agitation kept factual, solution", "goal": "{core}"},
    "whats_new": {"job": "One change in {product} since they were last active that makes it easier to {core_do}. Show it; do not list features.",
                  "framework": "AIDA: attention, interest, desire, action", "goal": "{core}"},
    "social_proof": {"job": "How a customer like them uses {product} to {core_do}: one specific, true example.",
                     "framework": "Social proof through one concrete case", "goal": "{core}"},
    "human_help": {"job": "A short plain-text note from a named person offering a 15-minute call or a reply. One link at most.",
                   "framework": "Reciprocity: an offer of help, not a pitch", "goal": "reply or {core}"},
    "use_case": {"job": "A second way to use {product} they have not tried, chosen for their role, with the first step spelled out.",
                 "framework": "Open loop closed in the body: the subject asks, the email answers", "goal": "{core}"},
    "check_in": {"job": "Ask one honest question: what would make {product} worth coming back to? Invite a one-line reply.",
                 "framework": "Question-led; no call to action beyond replying", "goal": "reply"},
    "repermission": {"job": "Ask plainly whether they still want these messages, with one button to stay. Say what happens if they do nothing.",
                     "framework": "Plain re-permission; no guilt, no countdown", "goal": "click to stay subscribed"},
    "repermission_reminder": {"job": "A short reminder of the re-permission ask: one button to stay, and the date mail stops if they do nothing.",
                              "framework": "Plain reminder with a stated consequence", "goal": "click to stay subscribed"},
    "first_step": {"job": "Welcome them with the single next step ({core_do}) and how long it takes.",
                   "framework": "BAB: before, after, bridge", "goal": "{core}"},
    "unblock": {"job": "Remove the blocker at the step where most new users stall (the leak map), with the shortest path past it.",
                "framework": "PAS: problem, agitation kept factual, solution", "goal": "{core}"},
    "show_outcome": {"job": "Show what a team like theirs gets when they {core_do}, with a real example.",
                     "framework": "AIDA: attention, interest, desire, action", "goal": "{core}"},
    "last_nudge": {"job": "A short last reminder of the one next step, and where to get help. No pressure, no deadline.",
                   "framework": "Plain reminder", "goal": "{core}"},
    # welcome
    "welcome_promise": {"job": "Thank them for joining and state what {product} promises them in one line, then the single best place to start.",
                        "framework": "Promise and proof", "goal": "{core}"},
    "best_sellers": {"job": "The two or three products most people start with, and why, with the product block.",
                     "framework": "Curated choice: reduce options to the safe first pick", "goal": "{core}"},
    "how_it_works": {"job": "How {product} works in three plain steps: what they get, when, and what happens if it is not right.",
                     "framework": "Risk reversal through clarity", "goal": "{core}"},
    "first_order_nudge": {"job": "A low-pressure reminder to place the first order; the offer only if the offer policy allows one.",
                          "framework": "Reminder; offer per policy, never invented", "goal": "{core}"},
    # cart and browse
    "cart_reminder": {"job": "Their cart, exactly as they left it (the product block), with one button back to checkout. No offer.",
                      "framework": "Plain reminder: show the cart", "goal": "{core}"},
    "cart_objection": {"job": "Answer the most common reason people stop at checkout (shipping cost, delivery time, returns), with the cart below.",
                       "framework": "Objection handling: the specific answer, not reassurance", "goal": "{core}"},
    "cart_last_call": {"job": "A last, short reminder that the cart is saved; the offer only if the offer policy allows one, and a deadline only if the offer has one.",
                       "framework": "Plain last reminder; offer per policy", "goal": "{core}"},
    "browse_reminder": {"job": "The product they looked at (the product block), with what makes it worth a second look in one line.",
                        "framework": "Relevance: their own choice, shown back", "goal": "{core}"},
    "browse_alternatives": {"job": "Help them choose: the product they viewed beside one or two alternatives most people compare it with.",
                            "framework": "Comparison that ends in a recommendation", "goal": "{core}"},
    # post-purchase and replenishment
    "order_thanks": {"job": "Thank them for the first order and tell them what to expect next and how to get the most from it on day one.",
                     "framework": "Reassurance and expectation setting", "goal": "{core}"},
    "how_to_use": {"job": "One practical tip that makes what they bought better, specific to the product.",
                   "framework": "Education that increases value delivered", "goal": "{core}"},
    "review_request": {"job": "Ask for an honest review of what they bought, with one button; no incentive unless the policy allows one.",
                       "framework": "Reciprocity, honestly asked", "goal": "review or {core}"},
    "cross_sell": {"job": "The one product that pairs best with what they bought, and why, with the product block.",
                   "framework": "Complement, not upsell", "goal": "{core}"},
    "replenish_reminder": {"job": "Their last order is probably running low: the product block and one button to reorder.",
                           "framework": "Timely reminder anchored on their own usage", "goal": "{core}"},
    "replenish_nudge": {"job": "A short second reminder to reorder before they run out, plus the option to subscribe if the product offers it.",
                        "framework": "Convenience framing", "goal": "{core}"},
    "replenish_last": {"job": "A last reminder to reorder; the offer only if the offer policy allows one.",
                       "framework": "Plain last reminder; offer per policy", "goal": "{core}"},
    # billing
    "payment_failed": {"job": "Their payment did not go through: what happened, the one button to update payment, and that nothing is lost yet.",
                       "framework": "Plain notice: what, why, what to do", "goal": "{core}"},
    "payment_retry": {"job": "A reminder that the payment still needs updating, with the date the next retry or the pause happens.",
                      "framework": "Plain notice with a real date", "goal": "{core}"},
    "payment_final": {"job": "Final notice before access pauses: the exact date, and the one button to keep the account active.",
                      "framework": "Plain final notice; a real deadline from the billing system", "goal": "{core}"},
    "access_paused": {"job": "Access is paused: how to restore it in one step, and that their data is kept.",
                      "framework": "Plain notice with a clear way back", "goal": "{core}"},
    # trial
    "trial_welcome": {"job": "Welcome to the trial: the single first step that shows the value fastest, and how long the trial lasts.",
                      "framework": "BAB: before, after, bridge", "goal": "{core}"},
    "trial_value": {"job": "Halfway through the trial: what they have done so far and the one feature that makes the paid plan worth it.",
                    "framework": "Progress plus next best action", "goal": "{core}"},
    "trial_ending": {"job": "The trial ends in three days: the exact date, what happens then, and the plan that fits how they use it.",
                     "framework": "Plain notice with a real date", "goal": "{core}"},
    "trial_last_day": {"job": "The trial ends tomorrow: one line on what they keep by choosing a plan, and the button.",
                       "framework": "Plain reminder with a real date", "goal": "{core}"},
    "trial_ended": {"job": "The trial has ended: how to pick a plan, and an extension only if the offer policy allows one.",
                    "framework": "Plain notice; offer per policy", "goal": "{core}"},
}
# preferred non-email channel by role, when the brief allows it
ROLE_CHANNEL = {"friction_removal": ["push"], "whats_new": ["in_app", "push"], "unblock": ["in_app", "push"],
                "use_case": ["in_app"], "check_in": ["sms"], "last_nudge": ["push", "sms"], "show_outcome": ["in_app"],
                "cart_objection": ["sms"], "replenish_nudge": ["sms", "push"], "payment_retry": ["sms"], "trial_value": ["in_app"],
                "trial_last_day": ["push", "sms"], "how_to_use": ["in_app"], "browse_alternatives": ["push"]}
IN_APP_DISPLAY_DAYS = 3
OFFER_ROLES = {"first_order_nudge", "cart_last_call", "replenish_last", "trial_ended", "review_request"}

VOCAB = {
    "klaviyo": {"container": "a flow", "trigger": "Metric trigger", "delay": "Time delay", "split": "Conditional split",
                "every_step": "Flow filter", "draft": "set every message to Manual status until it is approved", "freq": "Smart Sending",
                "window": "the time delay's 'delay until a specific time' window (or Smart Send Time)"},
    "hubspot": {"container": "a contact-based workflow", "trigger": "Enrollment trigger", "delay": "Delay", "split": "If/then branch",
                "every_step": "Unenrollment and goal criteria", "draft": "leave the workflow off until it is approved",
                "freq": "the send-frequency cap in marketing email settings", "window": "a delay until a time of day, on chosen days"},
    "braze": {"container": "a Canvas", "trigger": "Action-based entry", "delay": "Delay step", "split": "Audience Paths",
              "every_step": "Exit criteria", "draft": "save the Canvas as a draft until it is approved", "freq": "Global frequency capping",
              "window": "Quiet Hours on the Canvas"},
    "customerio": {"container": "an event-triggered campaign", "trigger": "Trigger", "delay": "Time delay", "split": "True/False branch",
                   "every_step": "Exit condition (conversion goal, exit on conversion)", "draft": "leave every message in Draft until it is approved",
                   "freq": "the workspace message limit", "window": "a Time Window step before the message"},
    "sfmc": {"container": "a journey", "trigger": "API Event entry source", "delay": "Wait by Duration", "split": "Decision Split",
             "every_step": "Goal and exit criteria", "draft": "keep the journey in draft until it is approved", "freq": "the send-frequency setting",
             "window": "Send Time Optimization off and a Wait Until time of day"},
    # Iterable journeys (support.iterable.com/hc/articles/15934993720468 entry, /8120791290260 exit rules, /14319818758036 Delay
    # tile, /15342990564372 Frequency Management, /16928857601940 Quiet Hours)
    "iterable": {"container": "a journey", "trigger": "Journey entry source (Event occurs)", "delay": "Delay tile", "split": "Yes/No Split",
                 "every_step": "Journey exit rules", "draft": "leave the journey unpublished until it is approved",
                 "freq": "Frequency Management in project settings (off by default)",
                 "window": "Quiet Hours on each send tile (Delivery and Compliance)"},
}


def _channels_for(roles: List[str], brief: Brief) -> List[str]:
    allowed = set(brief.channels)
    used_sms = False
    out = []
    first_and_middle = brief.flow_type in ("activation", "trial_convert", "dunning")
    for i, role in enumerate(roles):
        ch = "email"
        if 0 < i < len(roles) - 1 or first_and_middle and i > 0:
            for pref in ROLE_CHANNEL.get(role, []):
                if pref in allowed and not (pref == "sms" and used_sms):
                    ch = pref
                    break
        if role in ("repermission", "repermission_reminder"):
            ch = "email"
        used_sms = used_sms or ch == "sms"
        out.append(ch)
    # a channel the brief asked for should be used at least once if there is a middle touch to carry it
    for want in ("push", "sms", "in_app"):
        if want in allowed and want not in out:
            middles = [i for i in range(1, len(roles) - 1) if out[i] == "email" and roles[i] not in ("human_help", "repermission")]
            if middles:
                out[middles[-1]] = want
    return out


# a software welcome series: the welcome roles' jobs, written about the first step in the product, not a first order
SAAS_WELCOME_JOBS: Dict[str, str] = {
    "best_sellers": "The one step most new users take first ({core_do}), why it comes first, and how long it takes.",
    "how_it_works": "How {product} works in three plain steps: what to do first ({core_do}), that their work is saved, and where to get help.",
    "first_order_nudge": "A low-pressure reminder of the first step ({core_do}) and where to get help; the offer only if the offer policy "
                         "allows one.",
    "social_proof": "What people who stay with {product} do in their first week ({core_do}): one specific, true example.",
}


def role_label(role: str, brief: Brief) -> str:
    from engine.agents.copy_saas import role_label as _label
    return _label(role, brief)


def role_job(role: str, brief: Brief) -> str:
    """A role's job for this brief: the software wording for a software welcome series, else the library's."""
    from engine.agents.copy_saas import applies
    job = SAAS_WELCOME_JOBS.get(role) if applies(brief) else None
    return _fmt(job or ROLES[role]["job"], brief)


def _fmt(s: str, brief: Brief) -> str:
    from engine.agents.copy_saas import first_step
    c = brief.core_conversion_event
    return s.format(product=brief.product_name, core_label=c.label or c.event, core_do=first_step(brief), core=c.event)


def roles_for(brief: Brief) -> List[str]:
    return list(brief.flow.roles_for(brief.touches))


def _space_hours(hours: List[float], caps: List[Any], step: float) -> List[float]:
    """Push touches later until no cap's window holds more than its maximum. step is 24 for flows sent at a daily hour."""
    out: List[float] = []
    for h in hours:
        h = max(h, (out[-1] + step) if out else h)
        while any(c.max > 0 and sum(1 for p in out if h - c.hours < p <= h) >= c.max for c in caps):
            h += step
        out.append(h)
    return out


def schedule_hours(brief: Brief) -> List[float]:
    """Hours from the trigger to each touch, spread so no cap covering email is broken. Flows timed in hours (cart, browse)
    count from the trigger event, so the first touch lands after the abandonment wait."""
    fd = brief.flow
    base = library.schedule_hours(fd, brief.touches, brief.trial_days)
    if fd.hour_level:
        base = [h + (brief.abandon_after_hours or 0) for h in base]
    caps = [c for c in brief.freq.caps if "email" in c.channels]
    return _space_hours(base, caps, 1.0 if fd.hour_level else 24.0)


def schedule_days(brief: Brief) -> List[int]:
    """The day of each touch after entry (hours rounded down to whole days)."""
    return [int(h // 24) for h in schedule_hours(brief)]


def outcome_window(brief: Brief) -> int:
    """Days each entrant is followed for the goal: as given, or the whole flow plus the flow type's follow-up."""
    if brief.outcome_window_days:
        return brief.outcome_window_days
    last = math.ceil(max(schedule_hours(brief)) / 24)
    if brief.flow_type == "winback":
        return max(30, 2 * brief.churn_threshold_days, last + 7)
    if brief.flow_type == "activation":
        return max(14, last + 7)
    return max(7, last + brief.flow.outcome_days)


def _when(hours: float, hour_level: bool) -> str:
    if hour_level:
        return f"hour {hours:g}" if hours < 48 else f"day {hours / 24:.0f}"
    return f"day {int(hours // 24)}"


def entry_filters(brief: Brief, pol: Dict[str, Any]) -> List[Dict[str, str]]:
    q, core = brief.qualifying_event, brief.core_conversion_event
    ft = brief.flow_type
    out: List[Dict[str, str]] = []
    if ft == "winback":
        out.append({"condition": f"{q.describe()} ({q.detail()}) at least once", "evaluated": "entry_only"})
    elif ft in ("cart_abandon", "browse_abandon"):
        out.append({"condition": f"is identified (has an email address on the profile); anonymous {q.event} events cannot enter", "evaluated": "entry_only"})
        out.append({"condition": f"has not done {core.detail()} in the {brief.abandon_after_hours:g} hour(s) after {q.event}", "evaluated": "entry_only"})
        if ft == "browse_abandon":
            out.append({"condition": "has not been in the abandoned cart flow in the last 7 days (the cart flow outranks this one)", "evaluated": "entry_only"})
    elif ft == "post_purchase":
        out.append({"condition": f"this {q.event} is the person's first (completed orders over all time = 1)", "evaluated": "entry_only"})
    elif ft == "replenishment":
        days = brief.replenish_after_days
        out.append({"condition": (f"bought the product {days - brief.replenish_lead_days} days ago ({days}-day supply, reminded "
                                  f"{brief.replenish_lead_days} days early)" if days else
                                  "bought the product one measured supply interval ago, less the lead days"), "evaluated": "entry_only"})
        out.append({"condition": "has not bought that product again since", "evaluated": "entry_only"})
    elif ft == "sunset":
        out.append({"condition": f"received at least 5 marketing emails and clicked or replied to none in {brief.churn_threshold_days} days",
                    "evaluated": "entry_only"})
        out.append({"condition": "is not already sunset-suppressed (lcm_sunset is not true)", "evaluated": "entry_only"})
    elif ft == "dunning":
        out.append({"condition": "the subscription is still active or past due (not canceled)", "evaluated": "entry_only"})
    else:
        out += [{"condition": f"trigger filter: {k} = {v}", "evaluated": "entry_only"} for k, v in q.where.items()]
        out.append({"condition": f"has not yet done {core.detail()}", "evaluated": "entry_only"})
    if brief.global_holdout_pct > 0:
        out.append({"condition": f"not in the program-wide global holdout (bucket of md5(user_id || ':lmm_global') under "
                                 f"{brief.global_holdout_pct:g})", "evaluated": "entry_only"})
    out += [{"condition": f, "evaluated": "entry_only"} for f in pol["entry_filters"]]
    return out


def _trigger_block(brief: Brief, cohort: CohortDefinition) -> Dict[str, Any]:
    q, core = brief.qualifying_event, brief.core_conversion_event
    td = brief.trigger_def
    platform = PLATFORM_LABEL[brief.platform]
    re_entry = {"mode": td.re_entry.mode, **({"cooldown": td.re_entry.cooldown} if td.re_entry.cooldown else {}),
                "description": td.re_entry.describe()}
    ft = brief.flow_type
    if ft == "winback":
        n = brief.churn_threshold_days
        definition = (f"{n} days without {core.describe()} ({core.detail()}), measured from each person's own last {core.event}; "
                      f"the daily entry job pushes {cohort.trigger_event} once per lapse")
        source = (f"warehouse track_events ({brief.warehouse_dialect}) via the cohort entry query, pushed to {platform} as the custom "
                  f"event {cohort.trigger_event}")
        kind = "absence"
    elif ft == "replenishment":
        definition = f"a product's supply interval, less {brief.replenish_lead_days} days, since the person last bought it; no repeat since"
        source = (f"{platform}'s own order events when it has them (native), else the warehouse job pushes {cohort.trigger_event} once "
                  f"per purchase cycle")
        kind = "absence"
    elif ft == "sunset":
        definition = f"{brief.churn_threshold_days} days without a click or reply across at least 5 marketing emails"
        source = f"message_events (engagement tiers), pushed to {platform} as {cohort.trigger_event} once per person"
        kind = "absence"
    else:
        wait = f", then {brief.abandon_after_hours:g} hour(s) without {core.event}" if brief.abandon_after_hours and brief.flow.hour_level else ""
        definition = f"{q.detail()} occurred{wait}"
        source = f"{q.event} sent to {platform} in real time" + (", carrying in_holdout" if brief.holdout_pct > 0 else "")
        kind = "event"
    return {"type": kind, "definition": definition, "source_object": source, "re_entry": re_entry,
            "typed": td.model_dump(mode="json", exclude_none=True)}


def design(brief: Brief, cohort: CohortDefinition, strategy: Optional[Strategy] = None) -> FlowPlan:
    """Compile a Strategy into a flow spec. Without one (older callers), the default strategy is used."""
    fd = brief.flow
    pol = guardrails.gate_policy(brief.sending_gate, brief.flow_type)
    core = brief.core_conversion_event
    qual = brief.qualifying_event
    if strategy is None:
        from engine.agents.strategist import decide
        strategy = decide(brief, cohort)
    roles = [t.role for t in strategy.touches]
    hours = [t.hours if t.hours is not None else t.day * 24 for t in strategy.touches]
    channels = [t.channel for t in strategy.touches]
    leak = strategy.leak_step
    hh = f"{brief.send_hour_local:02d}:00"
    freq = brief.freq
    transactional = fd.message_class == "transactional"
    consent_req = "transactional_basis" if transactional else "email_consent"

    steps: List[Dict[str, Any]] = []
    sim: Dict[str, Dict[str, Any]] = {}
    touches: List[Touch] = []
    mer: List[str] = ["flowchart TD", f'  entry(["Enters: {cohort.trigger_event}"])']
    if brief.holdout_pct > 0:
        mer += ['  entry --> holdout_split{"in_holdout?"}',
                '  holdout_split -- "yes (' + f"{brief.holdout_pct:g}" + '%)" --> exit_holdout(["Exit: holdout, no messages"])']
        prev_node, prev_label = "holdout_split", "no"
        hold_src = ("event property from the daily entry job" if cohort.trigger_source == "warehouse_job" else
                    f"property on the {qual.event} trigger event, set by the system that sends it")
        steps.append({"id": "holdout_split", "wait": {"type": "none"}, "channel": "none", "message": "-", "goal": "-",
                      "branch": {"condition": f"in_holdout is true ({hold_src})", "yes": "exit (holdout)", "no": f"t1_{channels[0]}"}})
        sim["holdout_split"] = {"branch": {"holdout": True}}
    else:
        prev_node, prev_label = "entry", "next"

    last_h = 0.0
    for i, (role, ch, h) in enumerate(zip(roles, channels, hours), 1):
        r = ROLES[role]
        gap = h - last_h
        if fd.hour_level:
            wait: Dict[str, Any] = {"type": "fixed", "duration": hours_to_duration(gap)} if gap > 0 else {"type": "none"}
        elif i == 1:
            wait = {"type": "until_time_local", "local_time": hh}
            if h >= 24:
                wait["after"] = hours_to_duration(math.floor(h / 24) * 24)
        else:
            wait = {"type": "fixed", "duration": hours_to_duration(round(gap / 24) * 24)}
        job, goal = role_job(role, brief), _fmt(r["goal"], brief)
        if role == "unblock" and leak:
            job = job.replace("the step where most new users stall (the leak map)", f"{leak}, where most new users stall")
        step_id = f"t{i}_{ch}"
        msg = f"{role_label(role, brief)}: {job}"
        if ch == "in_app":
            msg += f" Queued for the next session; expires after {IN_APP_DISPLAY_DAYS} days."
        step: Dict[str, Any] = {"id": step_id, "wait": wait, "channel": ch, "message": msg, "goal": goal}
        if fd.hour_level and ch in ("email", "sms", "push"):
            step["respect_send_window"] = True
        req = {"email": [consent_req], "sms": ["sms_consent"], "push": ["push_permission"]}.get(ch)
        cond_text, cond_rule = None, None
        if ch in ("sms", "push"):
            consent = "sms" if ch == "sms" else "push"
            cond_text = "has SMS consent" if ch == "sms" else "has push permission (latest push_permission_changed is granted)"
            cond_rule = {"consent": consent}
        if req:
            step["requires"] = req
        if cond_text:
            step["condition"] = cond_text
        steps.append(step)
        sim[step_id] = {"condition": cond_rule}
        day = int(h // 24)
        touches.append(Touch(step_id=step_id, index=i, day=day, hours=h if fd.hour_level else None, channel=ch, role=role, job=job,
                             framework=r["framework"], goal=goal, requires=req or [], condition=cond_text))
        when = _when(h, fd.hour_level).capitalize()
        node = f'{step_id}["{when}: {ch.replace("_", "-")} ({role_label(role, brief)})"]'
        if ch in ("sms", "push"):
            fb = f"t{i}_email_fallback"
            fb_step = {"id": fb, "wait": {"type": "none"}, "channel": "email", "requires": [consent_req],
                       "condition": f"no {'SMS consent' if ch == 'sms' else 'push permission'} (fallback for {step_id})",
                       "message": f"{role_label(role, brief)} (email version): {job}", "goal": goal}
            if fd.hour_level:
                fb_step["respect_send_window"] = True
            steps.append(fb_step)
            sim[fb] = {"condition": {"not": {"consent": "sms" if ch == "sms" else "push"}}}
            touches.append(Touch(step_id=fb, index=i, day=day, hours=h if fd.hour_level else None, channel="email", role=role, job=job,
                                 framework=r["framework"], goal=goal, requires=[consent_req], condition=f"fallback for people without {ch} consent"))
            dec = f'c{i}{{"{ch} consent?"}}'
            mer.append(f'  {prev_node} -- "{prev_label}" --> {dec}')
            mer.append(f'  c{i} -- "yes" --> {node}')
            mer.append(f'  c{i} -- "no" --> {fb}["{when}: email fallback"]')
            mer.append(f"  {step_id} --> j{i}((\" \"))")
            mer.append(f"  {fb} --> j{i}")
            prev_node, prev_label = f"j{i}", "next"
        else:
            mer.append(f'  {prev_node} -- "{prev_label}" --> {node}')
            prev_node, prev_label = step_id, "next"
        last_h = h

    if (brief.flow_type == "winback" and brief.touches >= 2) or brief.flow_type == "sunset":
        steps.append({"id": "sunset", "wait": {"type": "fixed", "duration": "P14D"}, "channel": "none",
                      "condition": f"no click on the re-permission email and no {core.event} since entering",
                      "message": "Set lcm_sunset = true: suppressed from marketing, record kept, lifted only by a fresh opt-in", "goal": "-"})
        sim["sunset"] = {"condition": {"all": [{"not": {"event_since_entry": "message_clicked"}}, {"not": {"event_since_entry": core.event}}]}}  # noqa: E501
        mer.append(f'  {prev_node} -- "{prev_label}" --> sunset["Day {int(last_h // 24) + 14}: sunset if no click"]')
        prev_node = "sunset"
    mer.append(f'  {prev_node} --> done(["Exit: flow complete"])')
    mer.append(f'  goal(["{core.detail()} at any step"]) -.->|"exits immediately"| exit_goal(["Exit: converted"])')

    ongoing = [{"condition": f"has not done {core.detail()} since entering", "evaluated": "every_step"}]
    for ex in fd.exclude_events:
        ongoing.append({"condition": f"has not done {ex} since entering", "evaluated": "every_step"})
    ongoing.append({"condition": ("has a deliverable email address (not hard bounced); billing notices do not depend on marketing consent"
                                  if transactional else "can receive email marketing (subscribed, not bounced, not sunset-suppressed)"),
                    "evaluated": "every_step"})
    trig = _trigger_block(brief, cohort)
    priority = freq.flow_priority.index(brief.flow_type) + 1 if brief.flow_type in freq.flow_priority else len(freq.flow_priority) + 1
    windows = [w for w in freq.send_windows if w.channel in {t.channel for t in touches} | {"email"}]
    label = fd.label
    flow_spec: Dict[str, Any] = {
        "flow": f"{label}: {brief.product_name}" + (f", {brief.churn_threshold_days} days since the core action ({core.label or core.event})"
                                                     if brief.flow_type == "winback" else ""),
        "flow_type": brief.flow_type,
        "message_class": fd.message_class,
        "platform": brief.platform,
        "analytics": f"warehouse ({brief.warehouse_dialect}) and {PLATFORM_LABEL[brief.platform]}",
        "template_dialect": brief.template_dialect,
        "lifecycle_stage": fd.lifecycle_stage,
        "objective": brief.goal or f"{fd.summary}",
        "clv_lever": fd.clv_lever,
        "gate_required": "AMBER" if pol["effective_gate"] == "AMBER" else ("RED" if pol["effective_gate"] == "RED" else "GREEN"),
        "trigger": trig,
        "entry_filters": entry_filters(brief, pol),
        "ongoing_filters": ongoing,
        "suppressions": guardrails.mandatory_suppressions(brief.flow_type),
        "holdout": {"percent": brief.holdout_pct, "read": "not readable", "reason_if_none": "attached by the experimentation agent"},
        "frequency": {"cap": f"{freq.describe()} across flows ({VOCAB[brief.platform]['freq']})",
                      "caps": [c.model_dump(mode="json") for c in freq.caps],
                      "send_windows": [w.model_dump(mode="json", exclude_none=True) for w in windows],
                      "send_hour_local": None if fd.hour_level else hh,
                      "priority": priority, "collision_rule": freq.collision_rule},
        "steps": steps,
        "exit": {"goal": f"{core.describe()} ({core.detail()})", "unenroll_immediately": True,
                 "then": "normal lifecycle messaging" if brief.flow_type != "activation" else "the engaged-user program"},
        "measurement": {"primary_metric": "attached by the experimentation agent", "read_after": "-", "compared_against": "holdout"},
        "data_dependencies": [d.model_dump(exclude_none=True) for d in cohort.data_dependencies],
    }
    simulation = {"version": 2, "flow": flow_spec["flow"], "flow_type": brief.flow_type, "core_event": core.event, "goal_event": core.event,
                  "holdout_pct": brief.holdout_pct, "caps": [c.model_dump(mode="json") for c in freq.caps],
                  "frequency_cap_per_week": freq.weekly_cap("email"),
                  "send_hour_local": None if fd.hour_level else brief.send_hour_local,
                  "send_windows": {w.channel: [float(w.allowed_from[:2]) + int(w.allowed_from[3:]) / 60,
                                               float(w.allowed_until[:2]) + int(w.allowed_until[3:]) / 60] for w in freq.send_windows},
                  "hour_level": fd.hour_level, "in_app_display_days": IN_APP_DISPLAY_DAYS, "max_touches": brief.touches,
                  "engaged_only": pol["effective_gate"] == "AMBER", "steps": sim}
    return FlowPlan(flow_spec=flow_spec, simulation=simulation, touches=touches, mermaid="\n".join(mer),
                    build_steps=build_steps(brief, cohort, touches, flow_spec))


def build_steps(brief: Brief, cohort: CohortDefinition, touches: List[Touch], spec: Dict[str, Any]) -> List[str]:
    v = VOCAB[brief.platform]
    fd = brief.flow
    core = brief.core_conversion_event.detail()
    out = []
    if cohort.trigger_source == "warehouse_job":
        d = brief.warehouse_dialect
        push = ("fire one Journey Builder API Event per row (the journey's entry source), carrying in_holdout" if brief.platform == "sfmc" else
                f"send one {cohort.trigger_event} event per row with adapters/destinations/event_payloads.py ({brief.platform}), or "
                f"lcm send --approve")
        out.append(f"Create the ledger once (cohort/job/ledger_{d}.sql). Then schedule the daily job after the warehouse load: "
                   f"run cohort/job/queue_{d}.sql; read the rows from cohort/job/pending_{d}.sql and {push}, holdout members "
                   f"included; after each request succeeds, run cohort/job/mark_{d}.sql for every row it carried. Rows whose request "
                   f"failed stay pending and go out on the next run. Do not push from cohort/entry_{d}.sql: it does not write the "
                   f"ledger, so the same lapse would go out on several days.")
        if brief.flow_type in ("winback", "replenishment") and cohort.platform_pattern and "natively" in cohort.platform_pattern:
            out.append(f"Or build it natively instead, with no warehouse job: {cohort.platform_pattern}")
        out.append(f"Or skip the job: `lcm build --run <this plan's folder>` writes the {PLATFORM_LABEL[brief.platform]} build pack with a "
                   f"native trigger (a segment or a daily scheduled audience), the segment to create and its holdout property; keep the job "
                   f"when {brief.core_conversion_event.event} does not reach {PLATFORM_LABEL[brief.platform]} (docs/ESP_BUILD_PACKS.md)")
    else:
        q = brief.qualifying_event.event
        hold = (f" Add two properties to it where it is sent: holdout_bucket = first 7 hex digits of md5({'account_id' if brief.randomization_unit == 'account' else 'user_id'}"
                f" + ':{brief.slug}') as an integer, mod 100, and in_holdout = holdout_bucket < {brief.holdout_pct:g}. The same formula is in "
                f"engine/sql/dialects.py (Python and SQL), so the readout can recompute the arms in the warehouse." if brief.holdout_pct > 0 else "")
        out.append(f"Confirm {q} reaches {PLATFORM_LABEL[brief.platform]} in real time; it is the trigger.{hold}")
        out.append(f"Native trigger: {cohort.platform_pattern}")
    out.append(f"Create {v['container']} and {v['draft']}. {v['trigger']}: {cohort.trigger_event}.")
    out.append("Entry filters (checked once at entry): " + "; ".join(f["condition"] for f in spec["entry_filters"]) + ".")
    out.append(f"Checked before every step ({v['every_step']}): " + "; ".join(f["condition"] for f in spec["ongoing_filters"]) + ".")
    if brief.holdout_pct > 0:
        out.append(f"First step, {v['split']}: in_holdout is true goes to exit; everyone else continues. Nothing sends before this split.")
    re_entry = spec["trigger"]["re_entry"]
    out.append(f"Re-entry: {re_entry['description']}.")
    for t in touches:
        if fd.hour_level:
            when = f"{t.hours:g} hours after {brief.qualifying_event.event}, held to the {t.channel} send window ({v['window']})"
        else:
            when = f"day {t.day} at {brief.send_hour_local:02d}:00 recipient local"
        cond = f" only if {t.condition}" if t.condition else ""
        out.append(f"{v['delay']} to {when}, then {t.channel.replace('_', '-')} message {t.step_id}{cond}: {role_label(t.role, brief)}.")
    if any(s["id"] == "sunset" for s in spec["steps"]):
        out.append(f"{v['delay']} 14 days, then: if no click on the re-permission email, update the person: lcm_sunset = true. "
                   f"Make lcm_sunset a suppression in every marketing send.")
    windows = "; ".join(f"{w['channel']} {w['allowed_from']} to {w['allowed_until']}" for w in spec["frequency"]["send_windows"])
    out.append(f"Frequency: {spec['frequency']['cap']}. Permitted send windows, recipient local: {windows}. Priority {spec['frequency']['priority']} "
               f"of the program's flows ({spec['frequency']['collision_rule'].replace('_', ' ')}).")
    out.append(f"Before enabling: run lcm simulate on flow_spec.json (engine/policy/simulator.py), send every message to a seed list, and get "
               f"approval. Exit on {core} must be live before the first send.")
    return out
