"""Each flow step as the platform's own action, with its wait in the platform's smallest delay unit, the explicit splits,
and the goal, exit, send-window and frequency settings in the platform's words.

Action names and units come from the 2026-10-21 research (config/esp_capabilities.json): Klaviyo's flow action types
from its official OpenAPI spec (time-delay, trigger-split, conditional-split, send-email, send-sms, send-mobile-push,
update-profile), HubSpot's action type ids from the automation v4 reference (0-1 delay, 0-4 send email, 0-5 edit
record), Braze's Canvas steps, Customer.io's workflow actions, Iterable's journey tiles, and Marketing Cloud's Wait and
EMAILV2 activities. Where a platform has no verified action for a channel (SMS or push on HubSpot and Marketing Cloud,
in-app on Klaviyo, HubSpot and Marketing Cloud), the step is marked manual and the build sheet says what to add by hand.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from adapters.destinations.build.context import Ctx, Node, wait_in_unit
from adapters.destinations.build.model import Settings, Split, Step, Wait

ACTIONS: Dict[str, Dict[str, Optional[str]]] = {
    "klaviyo": {"delay": "time-delay", "email": "send-email", "sms": "send-sms", "push": "send-mobile-push", "in_app": None,
                "update": "update-profile", "holdout_event": "trigger-split", "holdout_profile": "conditional-split",
                "consent": "conditional-split realized as message filters: send-{ch} for people with {ch} consent, then send-email for everyone else "
                           "(Klaviyo branches never rejoin, so the flow stays one path)",
                "sunset": "conditional-split on Clicked Email since the flow started"},
    "hubspot": {"delay": "0-1 Delay for a set amount of time", "email": "0-4 Send email", "sms": None, "push": None, "in_app": None,
                "update": "0-5 Edit record", "holdout_event": "If/then branch (list branch)", "holdout_profile": "If/then branch (list branch)",
                "consent": "If/then branch on {ch} consent (add by hand)", "sunset": "If/then branch on the re-permission email click"},
    "braze": {"delay": "Delay step", "email": "Message step (email)", "sms": "Message step (SMS)", "push": "Message step (push)",
              "in_app": "Message step (in-app message)", "update": "User Update step (LIKELY)", "holdout_event": "Audience Paths",
              "holdout_profile": "Audience Paths", "consent": "Audience Paths: {ch} subscribed, everyone else",
              "sunset": "Audience Paths on the re-permission email click"},
    "customerio": {"delay": "Time Delay", "email": "Send email", "sms": "Send SMS", "push": "Send push", "in_app": "Send in-app message",
                   "update": "Create or update person (LIKELY)", "holdout_event": "True/False branch", "holdout_profile": "True/False branch",
                   "consent": "True/False branch on {ch} consent", "sunset": "True/False branch on the re-permission email click"},
    "iterable": {"delay": "Delay tile", "email": "Send Email tile", "sms": "Send SMS tile", "push": "Send Push tile", "in_app": "Send In-App tile",
                 "update": "update the user profile (tile name UNVERIFIED)", "holdout_event": "Yes/No Split", "holdout_profile": "Yes/No Split",
                 "consent": "Yes/No Split on {ch} subscription", "sunset": "Yes/No Split on the re-permission email click"},
    "sfmc": {"delay": "Wait (Wait by Duration)", "email": "Email (EMAILV2)", "sms": None, "push": None, "in_app": None,
             "update": "Update Contact (LIKELY)", "holdout_event": "Decision Split", "holdout_profile": "Decision Split",
             "consent": "Decision Split on {ch} consent (add by hand)", "sunset": "Decision Split on the re-permission email click"},
}


def window_text(ctx: Ctx, channel: str) -> Optional[str]:
    """How this platform holds a step to the channel's permitted window."""
    w = next((x for x in ctx.spec.get("frequency", {}).get("send_windows", []) if x["channel"] == channel), None)
    if not w:
        return None
    a, b = w["allowed_from"], w["allowed_until"]
    return {"klaviyo": (f"sms_quiet_hours_enabled: true (Klaviyo's SMS quiet hours; the plan permits {a} to {b})" if channel == "sms" else
                        f"sends when the delay ends: Klaviyo has no hour window on an hours delay (the plan permits {a} to {b})"),
            "hubspot": f"workflow time window: run actions {a} to {b} every day (account time zone, not the recipient's)",
            "braze": f"Canvas quiet hours outside {a} to {b} user local time, Send at next available time",
            "customerio": f"a Time Window delay just before the message: {a} to {b}, in the person's timezone attribute",
            "iterable": f"Quiet Hours on the tile outside {a} to {b} (user timeZone field), send at next eligible time",
            "sfmc": f"a Wait Until (or Contact Scheduling) that releases contacts only between {a} and {b} (LIKELY)"}[ctx.platform]


def _wait(ctx: Ctx, node: Node, extra_seconds: int = 0) -> Optional[Wait]:
    secs = node.wait_seconds + extra_seconds
    if not secs and not node.until_local_time:
        return None
    w = wait_in_unit(secs, ctx.platform)
    if node.until_local_time:
        w.until_local_time = node.until_local_time
        w.label = (f"{w.label}, then " if secs else "") + f"until {node.until_local_time} recipient local time"
    return w


def steps(ctx: Ctx, mode: str) -> List[Step]:
    from adapters.destinations.build.triggers import supply_days
    act = ACTIONS[ctx.platform]
    out: List[Step] = []
    extra = 0
    if ctx.flow_type == "replenishment" and mode == "native_event":
        days, _ = supply_days(ctx)
        extra = max(0, days - ctx.brief.replenish_lead_days) * 86400
    profile_holdout = mode in ("native_segment", "scheduled_segment") or ctx.platform == "hubspot"
    for node in ctx.nodes:
        add = 0
        if extra and node.kind in ("send", "consent"):
            add, extra = extra, 0
        wait = _wait(ctx, node, add)
        if node.kind == "holdout":
            if mode == "scheduled_segment" and ctx.platform == "sfmc":
                cond, src = "the data extension's in_holdout column is true", "profile_property"
            elif profile_holdout:
                cond, src = f"{ctx.holdout_property} is true", "profile_property"
            else:
                cond, src = "the trigger event's in_holdout is true", "trigger_event_property"
            action = act["holdout_profile" if src == "profile_property" else "holdout_event"]
            out.append(Step(id=node.id, kind="split", platform_action=action, wait=wait,
                            split=Split(kind="holdout", condition=cond, platform_action=action, yes="exit (holdout: no messages)", no="", source=src)))
        elif node.kind == "send":
            a = act.get(node.channel)
            st = Step(id=node.id, kind="send", platform_action=a or f"no {node.channel} action on {ctx.label}", wait=wait, channel=node.channel,
                      content=ctx.content(node.content_step), respect_send_window=node.respect_send_window, condition=node.condition)
            if node.respect_send_window:
                st.window = window_text(ctx, node.channel)
            if a is None:
                st.manual = True
                st.note = f"{ctx.label} has no verified {node.channel.replace('_', '-')} action in a flow: deliver it from your product, or leave it out"
            out.append(st)
        elif node.kind == "consent":
            ch = node.consent
            a = act.get(ch)
            split_action = (act["consent"] or "").format(ch=ch)
            manual = a is None
            out.append(Step(id=f"{node.id}_consent", kind="split", platform_action=split_action, wait=wait, manual=manual,
                            split=Split(kind="consent", condition=f"has {ch} consent" if ch == "sms" else "has push permission", platform_action=split_action,
                                        yes=node.id, no=node.fallback_step or "", source="consent",
                                        note=("added by hand: the artifact sends the email version to everyone at this step" if manual else None))))
            sms = Step(id=node.id, kind="send", platform_action=a or f"no verified {ch} action on {ctx.label}", channel=ch, content=ctx.content(node.content_step),
                       respect_send_window=node.respect_send_window, condition=node.condition, manual=manual)
            if node.respect_send_window:
                sms.window = window_text(ctx, ch)
            out.append(sms)
            if node.fallback_step:
                fb = Step(id=node.fallback_step, kind="send", platform_action=act["email"] or "email", channel="email",
                          content=ctx.content(node.fallback_step), respect_send_window=node.respect_send_window,
                          condition=f"no {'SMS consent' if ch == 'sms' else 'push permission'} (fallback for {node.id})")
                if manual:
                    fb.note = f"in the artifact this email goes to everyone until a {ch} action is added by hand"
                if node.respect_send_window:
                    fb.window = window_text(ctx, "email")
                out.append(fb)
        elif node.kind == "sunset":
            split_action = act["sunset"] or "split"
            out.append(Step(id=node.id, kind="split", platform_action=split_action, wait=wait,
                            split=Split(kind="sunset", condition="clicked the re-permission email since entering", platform_action=split_action,
                                        yes="exit (stays subscribed)", no=f"{node.id}_update", source="engagement")))
            out.append(Step(id=f"{node.id}_update", kind="update_profile", platform_action=act["update"] or "update profile",
                            note="set lcm_sunset = true: suppressed from marketing, record kept, lifted only by a fresh opt-in"))
    # each split's 'no' goes to the next step on the main path
    ids = [s.id for s in out]
    for i, s in enumerate(out):
        if s.split and s.split.kind == "holdout":
            s.split.no = ids[i + 1] if i + 1 < len(ids) else "exit"
    return out


FREQ_PERIOD = {168: "Weekly", 24: "Daily", 336: "Every two weeks", 720: "Monthly"}


def settings(ctx: Ctx, mode: str, trigger: Any) -> Settings:
    p = ctx.platform
    spec = ctx.spec
    caps = spec.get("frequency", {}).get("caps", [])
    windows = spec.get("frequency", {}).get("send_windows", [])
    from engine.policy.contracts import duration_hours
    freq: List[str] = []
    for c in caps:
        h = duration_hours(c["window"])
        chans = "/".join(c["channels"])
        if p == "klaviyo":
            freq.append(f"plan cap: at most {c['max']} {chans} per {c['window']}. Klaviyo: Smart Sending on for every message (smart_sending_enabled: true; "
                        "email window 16 hours by default): a window, not a count, so the flow's own spacing keeps the cap")
        elif p == "hubspot":
            freq.append(f"Email frequency safeguard (Marketing Hub Enterprise): at most {c['max']} marketing emails {FREQ_PERIOD.get(int(h), 'Weekly').lower()}, "
                        "and 'Apply send frequency cap for this send' ticked on each email")
        elif p == "braze":
            freq.append(f"Global frequency capping rule: at most {c['max']} {chans} per {int(h // 24) or 1} day(s) (30 days at most), and this Canvas respects global caps")
        elif p == "customerio":
            freq.append(f"Message limit: {c['max']} messages across {chans} per {int(h // 24)} day(s), with this campaign opted in to message limits")
        elif p == "iterable":
            freq.append(f"Frequency Management: {c['max']} per {'week' if h == 168 else 'day' if h == 24 else 'month'} for {chans}; leave Ignore Frequency Cap off on these tiles")
        else:
            freq.append(f"plan cap: at most {c['max']} {chans} per {c['window']}. Marketing Cloud has no hard Journey Builder cap (UNVERIFIED): spacing inside this journey only")
    send_windows = []
    for w in windows:
        send_windows.append(f"{w['channel']}: {w['allowed_from']} to {w['allowed_until']} recipient local ({w.get('source') or 'plan'}); "
                            f"{window_text(ctx, w['channel'])}")
    status = {"klaviyo": "Draft (the flow and every action in draft; nothing sends until you set it live)",
              "hubspot": "off (isEnabled: false)", "braze": "a draft Canvas you build and launch yourself",
              "customerio": "Draft", "iterable": "unpublished (draft)", "sfmc": "Draft (publishing is a separate call the pack never makes)"}[p]
    goal = spec["exit"]["goal"]
    return Settings(goal=f"{goal}; " + "; ".join(trigger.exit_criteria), exit="leaves immediately when the goal happens, at any step",
                    re_entry=trigger.re_entry.get("setting", trigger.re_entry.get("plan", "")), send_windows=send_windows, frequency=freq,
                    status_on_create=status)
