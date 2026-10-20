"""Campaign Architect: the multi-channel logic tree, as a flow spec the platform can be built from.

Given the brief and the cohort, it decides each touch's day, channel, job and conditions, and writes:

  flow_spec   valid against schemas/flow_spec.schema.json: trigger, entry and every-step filters,
              suppressions, frequency, steps with waits and consent requirements, exit, data
              dependencies (the holdout and measurement blocks are attached by the experimentation agent)
  simulation  machine-readable rules for every condition and branch, so scripts/campaign_simulator.py
              can walk synthetic people through the flow before anything is built
  mermaid     the logic tree as a diagram for the plan
  build_steps the build, step by step, in the platform's own vocabulary

Design rules it never breaks:
  * Exit before content: the core action ends the journey at every step, not only at the next send.
  * The holdout split is the first step, before any message, so both arms are the same population.
  * Email is the backbone. SMS and push touches carry an email fallback for people without that
    consent; at most one SMS per flow. In-app messages wait for the next session and expire.
  * No two touches closer than the frequency cap allows, at the recipient's local send hour.
  * Win-back ends with an honest re-permission ask and a sunset, never an endless drip.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents import guardrails  # noqa: E402
from agents.models import PLATFORM_LABEL, Brief, CohortDefinition, FlowPlan, Touch, parse_quiet_hours  # noqa: E402

ROLES: Dict[str, Dict[str, str]] = {
    "value_reminder": {"job": "Remind them of the one outcome {product} gave them, tied to what they last did, with one link that lets them {core_do} again. No offer.",
                       "framework": "BAB: before, after, bridge", "goal": "{core}"},
    "friction_removal": {"job": "Name the most common reason people stop at this point and remove it: a template, a two-minute path, or a reply to a person.",
                         "framework": "PAS: problem, agitation kept factual, solution", "goal": "{core}"},
    "whats_new": {"job": "One change in {product} since they were last active that makes it easier to {core_do}. Show it; do not list features.",
                  "framework": "AIDA: attention, interest, desire, action", "goal": "{core}"},
    "social_proof": {"job": "How a team like theirs uses {product} to {core_do} each week: one specific, true example with a number.",
                     "framework": "Social proof through one concrete case", "goal": "{core}"},
    "human_help": {"job": "A short plain-text note from a named person offering a 15-minute call or a reply. One link at most.",
                   "framework": "Reciprocity: an offer of help, not a pitch", "goal": "reply or {core}"},
    "use_case": {"job": "A second way to use {product} they have not tried, chosen for their role, with the first step spelled out.",
                 "framework": "Open loop closed in the body: the subject asks, the email answers", "goal": "{core}"},
    "check_in": {"job": "Ask one honest question: what would make {product} worth coming back to? Invite a one-line reply.",
                 "framework": "Question-led; no call to action beyond replying", "goal": "reply"},
    "repermission": {"job": "Ask plainly whether they still want these messages, with one button to stay. Say what happens if they do nothing.",
                     "framework": "Plain re-permission; no guilt, no countdown", "goal": "click to stay subscribed"},
    "first_step": {"job": "Welcome them with the single next step ({core_do}) and how long it takes.",
                   "framework": "BAB: before, after, bridge", "goal": "{core}"},
    "unblock": {"job": "Remove the blocker at the step where most new users stall (the leak map), with the shortest path past it.",
                "framework": "PAS: problem, agitation kept factual, solution", "goal": "{core}"},
    "show_outcome": {"job": "Show what a team like theirs gets when they {core_do}, with a real example.",
                     "framework": "AIDA: attention, interest, desire, action", "goal": "{core}"},
    "last_nudge": {"job": "A short last reminder of the one next step, and where to get help. No pressure, no deadline.",
                   "framework": "Plain reminder", "goal": "{core}"},
}
WINBACK_ORDER = {1: ["value_reminder"], 2: ["value_reminder", "repermission"],
                 3: ["value_reminder", "friction_removal", "repermission"],
                 4: ["value_reminder", "friction_removal", "whats_new", "repermission"],
                 5: ["value_reminder", "friction_removal", "whats_new", "social_proof", "repermission"],
                 6: ["value_reminder", "friction_removal", "whats_new", "social_proof", "human_help", "repermission"],
                 7: ["value_reminder", "friction_removal", "whats_new", "use_case", "social_proof", "human_help", "repermission"],
                 8: ["value_reminder", "friction_removal", "whats_new", "use_case", "social_proof", "human_help", "check_in", "repermission"]}
ACTIVATION_ORDER = ["first_step", "unblock", "show_outcome", "human_help", "social_proof", "use_case", "check_in", "last_nudge"]
WINBACK_DAYS = [0, 3, 7, 12, 18, 25, 32, 40]
ACTIVATION_DAYS = [0, 1, 3, 6, 10, 15, 21, 28]
# preferred non-email channel by role, when the brief allows it
ROLE_CHANNEL = {"friction_removal": ["push"], "whats_new": ["in_app", "push"], "unblock": ["in_app", "push"],
                "use_case": ["in_app"], "check_in": ["sms"], "last_nudge": ["push", "sms"], "show_outcome": ["in_app"]}
IN_APP_DISPLAY_DAYS = 3

VOCAB = {
    "klaviyo": {"container": "a flow", "trigger": "Metric trigger", "delay": "Time delay", "split": "Conditional split",
                "every_step": "Flow filter", "draft": "set every message to Manual status until it is approved", "freq": "Smart Sending"},
    "hubspot": {"container": "a contact-based workflow", "trigger": "Enrollment trigger", "delay": "Delay", "split": "If/then branch",
                "every_step": "Unenrollment and goal criteria", "draft": "leave the workflow off until it is approved",
                "freq": "the send-frequency cap in marketing email settings"},
    "braze": {"container": "a Canvas", "trigger": "Action-based entry", "delay": "Delay step", "split": "Audience Paths",
              "every_step": "Exit criteria", "draft": "save the Canvas as a draft until it is approved", "freq": "Global frequency capping"},
    "customerio": {"container": "an event-triggered campaign", "trigger": "Trigger", "delay": "Time delay", "split": "True/False branch",
                   "every_step": "Exit condition (conversion goal, exit on conversion)", "draft": "leave every message in Draft until it is approved",
                   "freq": "the workspace message limit"},
    "sfmc": {"container": "a journey", "trigger": "API Event entry source", "delay": "Wait by Duration", "split": "Decision Split",
             "every_step": "Goal and exit criteria", "draft": "keep the journey in draft until it is approved", "freq": "the send-frequency setting"},
}


def _space(days: List[int], cap: int) -> List[int]:
    """Push touches later until no 7-day window holds more than cap sends."""
    out: List[int] = []
    for d in days:
        d = max(d, (out[-1] + 1) if out else d)
        while sum(1 for p in out if d - 6 <= p <= d) >= cap:
            d += 1
        out.append(d)
    return out


def _channels_for(roles: List[str], brief: Brief) -> List[str]:
    allowed = set(brief.channels)
    used_sms = False
    out = []
    for i, role in enumerate(roles):
        ch = "email"
        if 0 < i < len(roles) - 1 or brief.flow_type == "activation" and i > 0:
            for pref in ROLE_CHANNEL.get(role, []):
                if pref in allowed and not (pref == "sms" and used_sms):
                    ch = pref
                    break
        if role == "repermission":
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


def _fmt(s: str, brief: Brief) -> str:
    c = brief.core_conversion_event
    return s.format(product=brief.product_name, core_label=c.label or c.event, core_do=c.do, core=c.event)


def roles_for(brief: Brief) -> List[str]:
    n = brief.touches
    roles = WINBACK_ORDER[n] if brief.flow_type == "winback" else ACTIVATION_ORDER[:n]
    if brief.flow_type == "activation" and n >= 2:
        roles = roles[:-1] + ["last_nudge"]
    return roles


def schedule_days(brief: Brief) -> List[int]:
    """The day of each touch after entry, spread to respect the weekly frequency cap."""
    base_days = (WINBACK_DAYS if brief.flow_type == "winback" else ACTIVATION_DAYS)[: brief.touches]
    return _space(base_days, brief.frequency_cap_per_week)


def outcome_window(brief: Brief) -> int:
    """Days each entrant is followed for the core action: as given, or the whole flow plus a week."""
    if brief.outcome_window_days:
        return brief.outcome_window_days
    last = max(schedule_days(brief))
    if brief.flow_type == "winback":
        return max(30, 2 * brief.churn_threshold_days, last + 7)
    return max(14, last + 7)


def design(brief: Brief, cohort: CohortDefinition) -> FlowPlan:
    pol = guardrails.gate_policy(brief.sending_gate)
    core = brief.core_conversion_event
    qual = brief.qualifying_event
    n = brief.touches
    roles = roles_for(brief)
    days = schedule_days(brief)
    channels = _channels_for(roles, brief)
    hh = f"{brief.send_hour_local:02d}:00"

    steps: List[Dict[str, Any]] = []
    sim: Dict[str, Dict[str, Any]] = {}
    touches: List[Touch] = []
    mer: List[str] = ["flowchart TD", f'  entry(["Enters: {cohort.trigger_event}"]) --> holdout_split{{"in_holdout?"}}',
                      '  holdout_split -- "yes (' + f"{brief.holdout_pct:g}" + '%)" --> exit_holdout(["Exit: holdout, no messages"])']
    prev_node = "holdout_split"
    prev_label = "no"

    hold_src = ("event property from the daily entry job" if brief.flow_type == "winback" else
                f"property on the {qual.event} trigger event, set by the system that sends it")
    steps.append({"id": "holdout_split", "wait": {"type": "none"}, "channel": "none", "message": "-", "goal": "-",
                  "branch": {"condition": f"in_holdout is true ({hold_src})", "yes": "exit (holdout)",
                             "no": f"t1_{channels[0]}"}})
    sim["holdout_split"] = {"branch": {"holdout": True}}

    last_day = 0
    for i, (role, ch, day) in enumerate(zip(roles, channels, days), 1):
        r = ROLES[role]
        gap = day - last_day
        wait = {"type": "until_time_local", "local_time": hh} if i == 1 else {"type": "fixed", "duration": f"P{gap}D"}
        job, goal = _fmt(r["job"], brief), _fmt(r["goal"], brief)
        step_id = f"t{i}_{ch}"
        msg = f"{role.replace('_', ' ')}: {job}"
        if ch == "in_app":
            msg += f" Queued for the next session; expires after {IN_APP_DISPLAY_DAYS} days."
        step: Dict[str, Any] = {"id": step_id, "wait": wait, "channel": ch, "message": msg, "goal": goal}
        req = {"email": ["email_consent"], "sms": ["sms_consent"], "push": ["push_permission"]}.get(ch)
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
        touches.append(Touch(step_id=step_id, index=i, day=day, channel=ch, role=role, job=job, framework=r["framework"], goal=goal,
                             requires=req or [], condition=cond_text))
        node = f'{step_id}["Day {day}: {ch.replace("_", "-")} ({role.replace("_", " ")})"]'
        if ch in ("sms", "push"):
            fb = f"t{i}_email_fallback"
            steps.append({"id": fb, "wait": {"type": "none"}, "channel": "email", "requires": ["email_consent"],
                          "condition": f"no {'SMS consent' if ch == 'sms' else 'push permission'} (fallback for {step_id})",
                          "message": f"{role.replace('_', ' ')} (email version): {job}", "goal": goal})
            sim[fb] = {"condition": {"not": {"consent": "sms" if ch == "sms" else "push"}}}
            touches.append(Touch(step_id=fb, index=i, day=day, channel="email", role=role, job=job, framework=r["framework"], goal=goal,
                                 requires=["email_consent"], condition=f"fallback for people without {ch} consent"))
            dec = f'c{i}{{"{ch} consent?"}}'
            mer.append(f'  {prev_node} -- "{prev_label}" --> {dec}')
            mer.append(f'  c{i} -- "yes" --> {node}')
            mer.append(f'  c{i} -- "no" --> {fb}["Day {day}: email fallback"]')
            mer.append(f"  {step_id} --> j{i}((\" \"))")
            mer.append(f"  {fb} --> j{i}")
            prev_node, prev_label = f"j{i}", "next"
        else:
            mer.append(f'  {prev_node} -- "{prev_label}" --> {node}')
            prev_node, prev_label = step_id, "next"
        last_day = day

    if brief.flow_type == "winback" and n >= 2:
        steps.append({"id": "sunset", "wait": {"type": "fixed", "duration": "P14D"}, "channel": "none",
                      "condition": f"no click on the re-permission email and no {core.event} since entering",
                      "message": "Set lcm_sunset = true: suppressed from marketing, record kept, lifted only by a fresh opt-in", "goal": "-"})
        sim["sunset"] = {"condition": {"all": [{"not": {"event_since_entry": "message_clicked"}}, {"not": {"event_since_entry": core.event}}]}}  # noqa: E501
        mer.append(f'  {prev_node} -- "{prev_label}" --> sunset["Day {last_day + 14}: sunset if no click"]')
        prev_node = "sunset"
    mer.append(f'  {prev_node} --> done(["Exit: flow complete"])')
    mer.append(f'  goal(["{core.detail()} at any step"]) -.->|"exits immediately"| exit_goal(["Exit: converted"])')

    if brief.flow_type == "winback":
        entry_filters = [{"condition": f"{qual.describe()} ({qual.detail()}) at least once", "evaluated": "entry_only"}]
    else:
        entry_filters = [{"condition": f"trigger filter: {k} = {v}", "evaluated": "entry_only"} for k, v in qual.where.items()]
        entry_filters.append({"condition": f"has not yet done {core.detail()}", "evaluated": "entry_only"})
    entry_filters += [{"condition": f, "evaluated": "entry_only"} for f in pol["entry_filters"]]
    ongoing = [{"condition": f"has not done {core.detail()} since entering", "evaluated": "every_step"},
               {"condition": "can receive email marketing (subscribed, not bounced, not sunset-suppressed)", "evaluated": "every_step"}]
    n_days = brief.churn_threshold_days
    if brief.flow_type == "winback":
        trigger = {"type": "absence",
                   "definition": f"{n_days} days without {core.describe()} ({core.detail()}), measured from each person's own last {core.event}; "
                                 f"the daily entry job pushes {cohort.trigger_event} once per lapse",
                   "source_object": f"warehouse track_events ({brief.warehouse_dialect}) via the cohort entry query, pushed to "
                                    f"{PLATFORM_LABEL[brief.platform]} as the custom event {cohort.trigger_event}",
                   "re_entry": f"allowed: after doing {core.event} again a person can lapse again and re-enter; the entry query fires once per lapse"}
    else:
        trigger = {"type": "event", "definition": f"{qual.detail()} occurred",
                   "source_object": f"{qual.event} streamed to {PLATFORM_LABEL[brief.platform]} in real time, carrying in_holdout",
                   "re_entry": "off: one activation journey per person"}
    flow_spec: Dict[str, Any] = {
        "flow": f"{'Win-back' if brief.flow_type == 'winback' else 'Activation'}: {brief.product_name}"
                + (f", {n_days} days since the core action ({core.label or core.event})" if brief.flow_type == "winback" else ""),
        "platform": brief.platform,
        "analytics": f"warehouse ({brief.warehouse_dialect}) and {PLATFORM_LABEL[brief.platform]}",
        "template_dialect": brief.template_dialect,
        "lifecycle_stage": "active to lapsed" if brief.flow_type == "winback" else "new to activated",
        "objective": brief.goal or (f"bring people back to {core.label or core.event}" if brief.flow_type == "winback"
                                    else f"get new users to {core.label or core.event}"),
        "clv_lever": "lifespan",
        "gate_required": "AMBER" if pol["effective_gate"] == "AMBER" else "GREEN",
        "trigger": trigger,
        "entry_filters": entry_filters,
        "ongoing_filters": ongoing,
        "suppressions": guardrails.mandatory_suppressions(brief.flow_type),
        "holdout": {"percent": brief.holdout_pct, "read": "not readable", "reason_if_none": "attached by the experimentation agent"},
        "frequency": {"cap": f"at most {brief.frequency_cap_per_week} lifecycle messages a person a week across flows ({VOCAB[brief.platform]['freq']})",
                      "quiet_hours": f"send at {hh} recipient local; nothing between {brief.quiet_hours}", "priority": 3},
        "steps": steps,
        "exit": {"goal": f"{core.describe()} ({core.detail()})", "unenroll_immediately": True,
                 "then": "normal lifecycle messaging" if brief.flow_type == "winback" else "the engaged-user program"},
        "measurement": {"primary_metric": "attached by the experimentation agent", "read_after": "-", "compared_against": "holdout"},
        "data_dependencies": [d.model_dump(exclude_none=True) for d in cohort.data_dependencies],
    }
    simulation = {"version": 1, "flow": flow_spec["flow"], "core_event": core.event, "goal_event": core.event, "holdout_pct": brief.holdout_pct,
                  "frequency_cap_per_week": brief.frequency_cap_per_week, "send_hour_local": brief.send_hour_local,
                  "quiet_hours_local": parse_quiet_hours(brief.quiet_hours), "in_app_display_days": IN_APP_DISPLAY_DAYS, "max_touches": n,
                  "engaged_only": pol["effective_gate"] == "AMBER", "steps": sim}
    return FlowPlan(flow_spec=flow_spec, simulation=simulation, touches=touches, mermaid="\n".join(mer),
                    build_steps=build_steps(brief, cohort, touches, flow_spec))


def build_steps(brief: Brief, cohort: CohortDefinition, touches: List[Touch], spec: Dict[str, Any]) -> List[str]:
    v = VOCAB[brief.platform]
    core = brief.core_conversion_event.detail()
    out = []
    if brief.flow_type == "winback":
        push = ("fire one Journey Builder API Event per row (entry source for the journey), carrying in_holdout" if brief.platform == "sfmc" else
                f"push one {cohort.trigger_event} event per row with tools/event_payloads.py ({brief.platform})")
        out.append(f"Schedule the cohort entry query ({brief.warehouse_dialect}) to run daily after the warehouse load, and {push}. "
                   f"Holdout members are pushed too.")
    else:
        q = brief.qualifying_event.event
        out.append(f"Confirm {q} reaches {PLATFORM_LABEL[brief.platform]} in real time; it is the trigger. Add two properties to it where it "
                   f"is sent: holdout_bucket = first 7 hex digits of md5(user_id + ':{brief.slug}') as an integer, mod 100, and "
                   f"in_holdout = holdout_bucket < {brief.holdout_pct:g}. The same formula is in agents/sql_dialects.py (Python and SQL), "
                   f"so the readout can recompute the arms in the warehouse.")
    out.append(f"Create {v['container']} and {v['draft']}. {v['trigger']}: {cohort.trigger_event}.")
    out.append(f"Entry filters (checked once at entry): " + "; ".join(f["condition"] for f in spec["entry_filters"]) + ".")
    out.append(f"Checked before every step ({v['every_step']}): " + "; ".join(f["condition"] for f in spec["ongoing_filters"]) + ".")
    out.append(f"First step, {v['split']}: in_holdout is true goes to exit; everyone else continues. Nothing sends before this split.")
    for t in touches:
        when = f"day {t.day} at {brief.send_hour_local:02d}:00 recipient local"
        cond = f" only if {t.condition}" if t.condition else ""
        out.append(f"{v['delay']} to {when}, then {t.channel.replace('_', '-')} message {t.step_id}{cond}: {t.role.replace('_', ' ')}.")
    if any(s["id"] == "sunset" for s in spec["steps"]):
        out.append(f"{v['delay']} 14 days, then: if no click on the re-permission email, update the person: lcm_sunset = true. "
                   f"Make lcm_sunset a suppression in every marketing send.")
    out.append(f"Frequency: {spec['frequency']['cap']}; {spec['frequency']['quiet_hours']}.")
    out.append(f"Before enabling: run scripts/campaign_simulator.py on flow_spec.json, send every message to a seed list, and get approval. "
               f"Exit on {core} must be live before the first send.")
    return out
