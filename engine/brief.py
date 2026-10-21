"""Turn what a marketer gives the teammate into a validated Brief.

Two ways in:

  YAML     templates/brief.yaml, filled in. Three fields are required (product_name,
           core_conversion_event, churn_threshold_days); everything else has a stated default.
  A sentence  "Build a 5-touch automated win-back flow for users who completed onboarding but
           haven't triggered a core action in 14 days". Parsed by rules, not by a model, so the
           same sentence always gives the same brief.

Either way, every value the marketer did not state is recorded as an Assumption with the reason,
and the plan prints those first under "Confirm before you build". Nothing is silently invented.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from pydantic import ValidationError

from engine.models import Assumption, Brief, EventMatch

NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
PLATFORMS = (("customer.io", "customerio"), ("customerio", "customerio"), ("klaviyo", "klaviyo"), ("hubspot", "hubspot"),
             ("braze", "braze"), ("marketing cloud", "sfmc"), ("sfmc", "sfmc"))
WINBACK_WORDS = ("win-back", "winback", "win back", "win them back", "bring back", "bring them back", "come back", "re-engage",
                 "reengage", "re-engagement", "reactivat", "lapsed", "lapse", "inactive", "stopped", "dormant", "churn", "gone quiet")
ACTIVATION_WORDS = ("onboarding flow", "onboarding series", "activation", "welcome", "new users", "new signups", "new sign-ups",
                    "new customers", "new accounts", "new members", "just signed up", "first week", "trial users", "free trial users")
SIGNUP = r"sign(ed|ing)? ?up|signups|sign-ups|new users|new customers|new accounts|new members|registered"
NEGATION = r"haven't|have not|hasn't|has not|without|\bbut\b"

# Phrases a marketer uses for the qualifying cohort and the core action, mapped to the reference
# tracking plan (config/tracking_plan.json).
QUALIFYING_PHRASES: List[Tuple[str, EventMatch, str]] = [
    (r"complet\w* (the )?onboarding|finished onboarding|onboarded",
     EventMatch(event="onboarding_step_completed", where={"step_index": 3}, label="completed onboarding"),
     "'completed onboarding' mapped to onboarding_step_completed with step_index 3, the final step in the reference tracking plan; "
     "set your own final step"),
    (r"activat\w*|reached (the )?aha",
     EventMatch(event="activation_reached", label="reached activation"), "'activated' mapped to activation_reached"),
    (r"signed up|new users|new signups|registered",
     EventMatch(event="signed_up", label="signed up"), "'signed up' mapped to signed_up"),
    (r"customers|bought|purchased|ordered|placed an order",
     EventMatch(event="order_completed", label="placed an order"), "'customers' mapped to order_completed"),
]
CORE_PHRASES: List[Tuple[str, EventMatch, str]] = [
    (r"connect\w* (a |your |their )?data|data source",
     EventMatch(event="onboarding_step_completed", where={"step": "connected_data_source"}, label="connected a data source"),
     "core action read as connecting a data source: onboarding_step_completed with step = connected_data_source"),
    (r"invit\w* (a )?(teammate|colleague|team)",
     EventMatch(event="onboarding_step_completed", where={"step": "invited_teammate"}, label="invited a teammate"),
     "core action read as inviting a teammate: onboarding_step_completed with step = invited_teammate"),
    (r"(purchas|order|bought|buy)\w*", EventMatch(event="order_completed", label="placed an order"),
     "core action read as a purchase: order_completed"),
    (r"log(ged)? ?in|session|opened the app|came back",
     EventMatch(event="session_started", label="started a session"), "core action read as a session: session_started"),
    (r"report", EventMatch(event="feature_used", where={"feature": "reports"}, label="used reports"),
     "core action read as using reports: feature_used with feature = reports"),
]


# Flow types beyond win-back and activation, recognized by name. Order matters: the first match wins.
FLOW_PATTERNS: List[Tuple[str, str]] = [
    ("dunning", r"dunning|failed (payment|charge|renewal)|payment (failure|failed|declined)|card (declined|expired|failed)|"
                r"involuntary churn|past[- ]due|update (their|your) (card|payment)"),
    ("cart_abandon", r"abandon\w* (cart|checkout)|(cart|checkout) abandon\w*|left (items |something |it )?in (their|the|a) cart|"
                     r"cart recovery|started (a )?checkout but"),
    ("browse_abandon", r"browse abandon\w*|abandon\w* brows\w*|viewed (a )?products? (but|and (didn't|did not|never))|"
                       r"looked at (a )?products?|product views? (but|without)"),
    ("replenishment", r"replenish\w*|re-?order reminder|run(ning)? (low|out)|refill|restock reminder|reorder"),
    ("post_purchase", r"post[- ]purchase|after (their|the|a) (first )?(order|purchase)|first[- ]time (buyers?|customers?|purchasers?)|"
                      r"thank[- ]you (flow|series|email)|order follow[- ]?up|review request"),
    ("trial_convert", r"trial (conversion|to paid|convert\w*|expir\w*|ending|ends)|convert\w* (free )?trial|"
                      r"(free )?trial (users|accounts) (to|into) paid|before (their|the) trial ends"),
    ("sunset", r"\bsunset\b|re-?permission|list hygiene|unengaged (subscribers|contacts|people)|clean (up )?(the|our) list|"
               r"stop (emailing|mailing) (people|subscribers) who"),
    ("welcome", r"welcome (flow|series|email|sequence)|new (newsletter )?subscribers?|newsletter sign[- ]?ups?|joined (the|our) (list|newsletter)"),
]
HOURS = re.compile(r"\b(\d+(?:\.\d+)?|one|two|three|four|five|six|twelve|24|48|72)\s*(?:-|\s)?\s*(hours?|hrs?|h\b|minutes?|mins?)")


def flow_type_of(text: str) -> Tuple[Optional[str], Optional[str]]:
    """(flow type, None), or (None, why) for a flow the engine recognizes but does not build. Rules, not a model."""
    from engine.flows.library import UNSUPPORTED
    low = " ".join(text.lower().replace("’", "'").split())
    for ft, pattern in FLOW_PATTERNS:
        if re.search(pattern, low):
            if ft == "welcome" and re.search(r"connect\w* (a |your |their )?data|invit\w* (a )?(teammate|colleague)|report|log(ged)? ?in", low):
                return "activation", None     # a SaaS onboarding sentence that happens to say "welcome"
            return ft, None
    for key, (pattern, why) in UNSUPPORTED.items():
        if re.search(pattern, low):
            return None, why
    return "", None


def _num(token: str) -> Optional[int]:
    token = token.lower()
    return int(token) if token.isdigit() else NUMBER_WORDS.get(token)


def parse_text(text: str, product_name: Optional[str] = None, overrides: Optional[Dict[str, Any]] = None) -> Brief:
    """Rule-based parse of a one-sentence brief. Unstated values become recorded assumptions."""
    t = " ".join(text.split())
    low = t.lower().replace("’", "'")
    data: Dict[str, Any] = {}
    assumptions: List[Assumption] = []

    def assume(field: str, value: Any, why: str) -> None:
        data[field] = value
        assumptions.append(Assumption(field=field, value=value if not isinstance(value, EventMatch) else value.model_dump(), why=why))

    # flow type: a named flow from the library first (cart, browse, post-purchase, replenishment, dunning, trial, sunset,
    # welcome); a recognized flow the engine does not build is refused by name; then win-back and activation as before:
    # explicit lapse words win; then "haven't ... since" (a lapse); then explicit new-user words; then "haven't ... yet"
    # (not done it for the first time); then any negation or "in N days" (a lapse)
    named, refusal = flow_type_of(t)
    if refusal:
        raise ValueError(f"not supported yet: {refusal}. The engine plans these flows: welcome, activation, win-back, abandoned cart, "
                         f"browse abandonment, post-purchase, replenishment, failed payment (dunning), trial conversion and sunset")
    neg = r"(haven't|have not|hasn't|has not)"
    repeat = r"second (order|purchase)|(order|ordered|buy|bought|purchase|purchased) again|repeat (order|purchase)"
    if named:
        data["flow_type"] = named
    elif any(w in low for w in WINBACK_WORDS) or re.search(neg + r".*\bsince\b", low) or re.search(repeat, low):
        data["flow_type"] = "winback"
    elif any(w in low for w in ACTIVATION_WORDS):
        data["flow_type"] = "activation"
    elif re.search(neg + r".*\byet\b", low):
        data["flow_type"] = "activation"
    elif re.search(neg + r"|in \d+ (day|week|month)", low):
        data["flow_type"] = "winback"
    else:
        assume("flow_type", "winback", "the brief does not say whether this is a win-back or an activation flow")

    # touches
    from engine.flows.library import FLOWS
    fd = FLOWS[data.get("flow_type", "winback")]
    m = re.search(r"\b(\d+|one|two|three|four|five|six|seven|eight)[- ](touch|step|message|email|text|part)", low)
    if m:
        n = _num(m.group(1)) or fd.default_touches
        if n > fd.max_touches:
            assume("touches", fd.max_touches, f"the sentence asked for {n} touches; a {fd.label.lower()} flow has at most {fd.max_touches} "
                                               f"in the library, so the plan uses {fd.max_touches}")
        else:
            data["touches"] = n
    else:
        assume("touches", fd.default_touches, "number of touches not stated")

    # hours: cart and browse flows wait in hours; any other flow cannot, and says so instead of dropping the number
    hm = HOURS.search(low)
    if hm:
        qty = _num(hm.group(1)) if not hm.group(1).replace(".", "").isdigit() else float(hm.group(1))
        hrs = (qty or 0) / (60 if hm.group(2).startswith("min") else 1)
        if fd.hour_level and hrs > 0:
            data["abandon_after_hours"] = min(max(hrs, 0.25), 72)
        elif hrs > 0:
            assumptions.append(Assumption(field="timing", value=f"{hm.group(0)}",
                                          why=f"the sentence asked for {hm.group(0)}, but a {fd.label.lower()} flow is timed in days, not hours; "
                                              f"the hours were not used. Abandoned cart and browse flows wait in hours"))

    # inactivity threshold
    m = re.search(r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s*(day|week|month)s?\b", low)
    ft = data.get("flow_type", "winback")
    if m and ft in ("winback", "sunset", "activation", "welcome"):
        n = _num(m.group(1)) or 14
        days = n * {"day": 1, "week": 7, "month": 30}[m.group(2)]
        if days < 3 and ft == "winback":
            assume("churn_threshold_days", 3, f"the sentence asked for {days} day(s); a win-back threshold is at least 3 days")
        elif ft in ("winback", "sunset"):
            data["churn_threshold_days"] = days
    elif m and ft == "replenishment":
        n = _num(m.group(1)) or 30
        data["replenish_after_days"] = n * {"day": 1, "week": 7, "month": 30}[m.group(2)]
    elif m and ft == "trial_convert":
        n = _num(m.group(1)) or 14
        data["trial_days"] = min(90, n * {"day": 1, "week": 7, "month": 30}[m.group(2)])
    elif ft == "winback":
        assume("churn_threshold_days", 14, "inactivity threshold not stated")

    # qualifying cohort: the clause before "but", or the whole sentence
    who = re.split(NEGATION, low, maxsplit=1)[0]
    for sep in (":", " for "):
        if sep in who:
            who = who.rsplit(sep, 1)[1]
    if data.get("flow_type") == "activation" and re.search(SIGNUP, low):
        who = "signed up"   # an activation flow starts at signup when the sentence mentions it anywhere
    phrase_flows = ("winback", "activation", "welcome")
    for pattern, match, why in (QUALIFYING_PHRASES if data.get("flow_type", "winback") in phrase_flows else []):
        if re.search(pattern, who):
            data["qualifying_event"] = match
            assumptions.append(Assumption(field="qualifying_event", value=match.model_dump(), why=why))
            break
    else:
        ft_now = data.get("flow_type", "winback")
        if ft_now == "activation":
            assume("qualifying_event", EventMatch(event="signed_up", label="signed up"), "who qualifies was not stated; defaulted to new signups")
        elif ft_now == "winback":
            assume("qualifying_event", EventMatch(event="activation_reached", label="reached activation"),
                   "who qualifies was not stated; defaulted to users who reached activation")

    # core action: the clause after "but" / "haven't"
    after = re.split(NEGATION, low, maxsplit=1)
    action = after[1] if len(after) > 1 else ""
    for pattern, match, why in (CORE_PHRASES if data.get("flow_type", "winback") in phrase_flows else []):
        if re.search(pattern, action):
            data["core_conversion_event"] = match
            assumptions.append(Assumption(field="core_conversion_event", value=match.model_dump(), why=why))
            break
    else:
        if data.get("flow_type", "winback") in ("winback", "activation"):
            assume("core_conversion_event", EventMatch(event="feature_used", label="used the core feature"),
                   "the core action was not named; defaulted to feature_used (any feature). Name the one action that means a user is "
                   "getting value, e.g. feature_used with feature = reports")

    # platform
    for needle, platform in PLATFORMS:
        if needle in low:
            data["platform"] = platform
            break
    else:
        assume("platform", "customerio", "messaging platform not stated")

    # channels
    channels = ["email"]
    if re.search(r"\bsms\b|\btext messages?\b|\btexts?\b", low):
        channels.append("sms")
    if re.search(r"\bpush\b", low):
        channels.append("push")
    if re.search(r"in[- ]app", low):
        channels.append("in_app")
    if len(channels) == 1 and "email" not in low:
        assume("channels", channels, "channels not stated; email only until consent for other channels is confirmed")
    else:
        data["channels"] = channels

    m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*hold-?out", low)
    if m:
        data["holdout_pct"] = float(m.group(1))

    saas_words = r"\b(data sources?|teammates?|reports?|dashboards?|workspaces?|log(ged)? ?in|the app|trials?|seats?|plans?)\b"
    events = [x.event for x in (data.get("qualifying_event"), data.get("core_conversion_event")) if isinstance(x, EventMatch)]
    if re.search(r"\b(shop|store|ecommerce|e-commerce|dtc|d2c|cart|checkout|orders?|purchases?|products?|buyers?)\b", low) or \
            FLOWS[data.get("flow_type", "winback")].models == ("ecommerce",) or "order_completed" in events:
        data["business_model"] = "ecommerce"
    elif data.get("flow_type") == "welcome" and not re.search(saas_words, low):
        assume("business_model", "ecommerce", "business model not stated; a welcome series is read as a store's, aiming at the first order. "
                                              "Set business_model: saas to aim it at activation instead")

    if product_name:
        data["product_name"] = product_name
    else:
        assume("product_name", "Your product", "product name not given; pass --product or edit the brief")

    q, c = data.get("qualifying_event"), data.get("core_conversion_event")
    if (data.get("flow_type") == "activation" and isinstance(q, EventMatch) and isinstance(c, EventMatch)
            and (q.event, q.where) == (c.event, c.where)):
        fallback = EventMatch(event="signed_up", label="signed up")
        data["qualifying_event"] = fallback
        assumptions[:] = [a for a in assumptions if a.field != "qualifying_event"]
        assumptions.append(Assumption(field="qualifying_event", value=fallback.model_dump(),
                                      why=f"the sentence named {c.event} for both who qualifies and the core action; qualifying set to "
                                          f"{fallback.event}. Confirm who should be eligible"))
    data["goal"] = t
    for k, v in (overrides or {}).items():
        data[k] = v
        assumptions = [a for a in assumptions if a.field != k]
    _standard_assumptions(data, assumptions)
    data["assumptions"] = assumptions
    return Brief(**data)


def _standard_assumptions(data: Dict[str, Any], assumptions: List[Assumption]) -> None:
    """Fields that shape the plan's honesty: unstated, they are recorded, never quietly defaulted."""
    have = {a.field for a in assumptions} | set(data)
    if "sending_gate" not in have:
        assumptions.append(Assumption(field="sending_gate", value="UNKNOWN",
                                      why="no sending gate on record; treated as AMBER (engaged recipients only) until the deliverability-gate "
                                          "skill or engine/deliverability/analyze.py issues one"))
    if "monthly_entrants" not in have:
        assumptions.append(Assumption(field="monthly_entrants", value=None,
                                      why="entry volume unknown; estimated from data when --data is given, otherwise the read date is not computed"))
    if "baseline_conversion_rate" not in have:
        assumptions.append(Assumption(field="baseline_conversion_rate", value=None,
                                      why="share of entrants who come back with no flow is unknown; estimated from data when --data is given"))
    if "postal_address" not in have:
        assumptions.append(Assumption(field="postal_address", value=None,
                                      why="postal address not given; templates use the platform's address token where one exists"))


REQUIRED_YAML = ("product_name", "core_conversion_event", "churn_threshold_days")


def _event(value: Any) -> Any:
    if isinstance(value, str):
        return {"event": value}
    return value


def load_brief(path: Path) -> Brief:
    """Load and validate a YAML brief. Raises ValueError with every problem listed, in plain words."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: a brief is a set of 'field: value' lines")
    raw = {k: v for k, v in raw.items() if v is not None and v != ""}
    missing = [k for k in REQUIRED_YAML if k not in raw and not (k == "churn_threshold_days" and raw.get("flow_type") == "activation")]
    if missing:
        raise ValueError(f"{path}: fill in {', '.join(missing)} (see the comments in templates/brief.yaml)")
    for k in ("qualifying_event", "core_conversion_event"):
        if k in raw:
            raw[k] = _event(raw[k])
    assumptions: List[Assumption] = []
    if "qualifying_event" not in raw:
        assumptions.append(Assumption(field="qualifying_event", value={"event": "activation_reached"},
                                      why="who qualifies was not stated; defaulted to users who reached activation"))
    if "platform" not in raw:
        assumptions.append(Assumption(field="platform", value="customerio", why="messaging platform not stated"))
    _standard_assumptions(raw, assumptions)
    raw["assumptions"] = assumptions + [Assumption(**a) for a in raw.get("assumptions", [])]
    try:
        return Brief(**raw)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors())
        raise ValueError(f"{path}: {problems}") from None


def to_yaml(brief: Brief) -> str:
    """Write a brief back out for editing, with the assumptions listed as comments to confirm."""
    data = brief.model_dump(exclude={"assumptions"}, exclude_none=True)
    head = ["# Generated by engine/pipeline/orchestrator.py brief. Edit, then run:",
            "#   python3 -m engine.pipeline.orchestrator plan --brief <this file>", "#"]
    if brief.assumptions:
        head.append("# Assumed, not stated. Confirm or change each one:")
        head += [f"#   {a.field}: {a.why}" for a in brief.assumptions]
    return "\n".join(head) + "\n" + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
