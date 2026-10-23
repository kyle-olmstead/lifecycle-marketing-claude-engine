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

import json

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from pydantic import ValidationError

from engine.models import Assumption, Brief, EventMatch

NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
PLATFORMS = (("customer.io", "customerio"), ("customerio", "customerio"), ("klaviyo", "klaviyo"), ("hubspot", "hubspot"),
             ("braze", "braze"), ("marketing cloud", "sfmc"), ("sfmc", "sfmc"), ("iterable", "iterable"))
WINBACK_WORDS = ("win-back", "winback", "win back", "win them back", "bring back", "bring them back", "come back", "re-engage",
                 "reengage", "re-engagement", "reactivat", "lapsed", "lapse", "inactive", "stopped", "dormant", "churn", "gone quiet")
ACTIVATION_WORDS = ("onboarding", "activation", "welcome", "new users", "new signups", "new sign-ups",
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
                r"involuntary churn|past[- ]due|update (their|your) (card|payment)|cards? (is |are )?expir\w*|expir\w* cards?"),
    ("cart_abandon", r"abandon\w* (carts?|checkouts?|baskets?)|(cart|checkout|basket) abandon\w*|"
                     r"left (items |something |it )?in (their|the|a) (cart|basket)|(cart|basket|checkout) recovery|"
                     r"recover\w* (abandoned |their |lost )?(carts?|baskets?|checkouts?)|started (a )?checkout but|"
                     r"\b(cart|basket) (flow|series|emails?|sequence|reminders?|messages?)\b|"
                     r"added (items |something |it |products? )?to (their |the |a )?(cart|basket)"),
    ("browse_abandon", r"browse abandon\w*|abandon\w* brows\w*|"
                       r"(viewed|looked at|browsed) (a |the )?products? (but|and (didn't|did not|never|left))|"
                       r"(viewed|looked at|browsed) (a |the )?products? (without|then left)|"
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
    from engine.flows.library import UNSUPPORTED_FIRST
    for key in UNSUPPORTED_FIRST:       # names a library flow would otherwise swallow ("a cross-sell flow after their order")
        pattern, why = UNSUPPORTED[key]
        if key == "cross_sell" and re.search(r"post[- ]purchase", low):
            continue                    # a post-purchase flow's own cross-sell touch
        if re.search(pattern, low):
            return None, why
    for ft, pattern in FLOW_PATTERNS:
        if re.search(pattern, low):
            if ft == "welcome" and re.search(r"connect\w* (a |your |their )?data|invit\w* (a )?(teammate|colleague)|report|log(ged)? ?in", low):
                return "activation", None     # a SaaS onboarding sentence that happens to say "welcome"
            return ft, None
    from engine.flows.library import UNSUPPORTED_SOFT
    names_a_flow = any(w in low for w in WINBACK_WORDS + ACTIVATION_WORDS) or re.search(r"(haven't|have not|hasn't|has not)", low)
    for key, (pattern, why) in UNSUPPORTED.items():
        if key in UNSUPPORTED_SOFT and names_a_flow:
            continue
        if re.search(pattern, low):
            return None, why
    return "", None


def _num(token: str) -> Optional[int]:
    token = token.lower()
    return int(token) if token.isdigit() else NUMBER_WORDS.get(token)


def parse_text(text: str, product_name: Optional[str] = None, overrides: Optional[Dict[str, Any]] = None,
               defaults: Optional[Dict[str, Any]] = None) -> Brief:
    """Rule-based parse of a one-sentence brief. The sentence wins, then the workspace's lmm.yaml (`defaults`, read from the
    workspace when None), then the engine's defaults; values that came from none of these become recorded assumptions."""
    t = " ".join(text.split())
    low = t.lower().replace("’", "'")
    from engine.policy.contracts import cap_phrases, parse_cap_text
    stated_caps = parse_cap_text(low)
    # a cap is not a touch count or a lapse: "no more than 2 emails a week" is not a 2-touch flow, "every 3 days" no threshold
    bare = low
    for s, e, _items, _w in cap_phrases(low):
        bare = bare[:s] + " " * (e - s) + bare[e:]
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
        # nothing in the sentence says which flow: refuse by name rather than plan a win-back nobody asked for
        raise ValueError("could not tell which flow this is. Name one: a welcome series, an activation flow, a win-back, an abandoned "
                         "cart or browse abandonment flow, a post-purchase, replenishment, failed payment (dunning), trial conversion "
                         "or sunset flow; or describe who it is for (\"users who haven't logged in for 14 days\")")

    # touches
    from engine.flows.library import FLOWS
    fd = FLOWS[data.get("flow_type", "winback")]
    m = re.search(r"\b(\d+|one|two|three|four|five|six|seven|eight)[- ](touch|step|message|email|text|part)", bare)
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
    hm = HOURS.search(bare)
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
    m = re.search(r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s*(day|week|month)s?\b", bare)
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

    saas_words = (r"\b(data sources?|teammates?|reports?|dashboards?|workspaces?|log(ged)? ?in|apps?|trials?|seats?|plans?|users?|saas|"
                  r"software|b2b)\b")
    events = [x.event for x in (data.get("qualifying_event"), data.get("core_conversion_event")) if isinstance(x, EventMatch)]
    software = r"\b(saas|software|b2b|apps?|platform|subscriptions?|users?|log(ged)? ?in)\b"
    if re.search(r"\b(shop|store|ecommerce|e-commerce|dtc|d2c|shopify)\b", low):
        data["business_model"] = "ecommerce"          # stated: the sentence says it is a store
    elif re.search(r"\b(saas|software|b2b)\b", low) and FLOWS[data.get("flow_type", "winback")].fits("saas"):
        data["business_model"] = "saas"               # stated: the sentence says it is software
    elif (re.search(r"\b(cart|checkout|orders?|ordered|purchases?|buyers?)\b", low)
          or (re.search(r"\bproducts?\b", low) and not re.search(software, low))) or \
            FLOWS[data.get("flow_type", "winback")].models == ("ecommerce",) or "order_completed" in events:
        # implied by the flow or its words: the workspace's stated business model wins over this reading
        assume("business_model", "ecommerce", "business model not stated; read as a store's because the flow is about carts, "
                                              "products or orders. Set brand.business_model in lmm.yaml to say otherwise")
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
    from engine import workspace
    filled = workspace.apply_defaults(data, {a.field for a in assumptions}, defaults)
    assumptions = [a for a in assumptions if a.field not in filled]
    for k, v in (overrides or {}).items():
        data[k] = v
        assumptions = [a for a in assumptions if a.field != k]
    if stated_caps:
        data["frequency"] = with_caps(data.get("frequency"), stated_caps)
    assumptions = fit_to_business_model(data, assumptions)
    from engine.flows.library import _family
    if (data.get("flow_type") == "activation" and _family(data.get("business_model") or "saas") == "ecommerce"
            and not re.search(r"\bactivat", low)):
        # a store's "new customers who have not ordered yet" is a welcome series aimed at the first order
        data["flow_type"] = "welcome"
        assumptions = [a for a in assumptions if a.field != "flow_type"] + [Assumption(
            field="flow_type", value="welcome", why="new people who have not done the goal yet, for a store: planned as a welcome series "
                                                    "aimed at the first order (an activation flow is for software)")]
        wmax = FLOWS["welcome"].max_touches
        if data.get("touches", 0) > wmax:
            data["touches"] = wmax
            assumptions.append(Assumption(field="touches", value=wmax, why=f"a welcome series has at most {wmax} touches in the library"))
    check_business_model(data)
    _standard_assumptions(data, assumptions)
    data["assumptions"] = assumptions
    return Brief(**data)


STORE_LAPSE_DAYS = 60


def with_caps(base: Any, caps: List[Any]) -> Any:
    """The frequency policy with the sentence's caps: each replaces a cap on the same channels and window, others stay. With
    no policy yet, the engine's default (3 a week across email, SMS and push, and the default send windows) is the base."""
    from engine.models import default_frequency
    from engine.policy.contracts import FrequencyPolicy
    policy = FrequencyPolicy.model_validate(base) if isinstance(base, dict) else (base or default_frequency())
    keep = [c for c in policy.caps if all((sorted(c.channels), c.window) != (sorted(n.channels), n.window) for n in caps)]
    return policy.model_copy(update={"caps": keep + list(caps)})


def fit_to_business_model(data: Dict[str, Any], assumptions: List[Assumption]) -> List[Assumption]:
    """A store's win-back is about orders: when who qualifies, the core action or the lapse were defaulted (not stated) and
    the business model is a store's, use the flow library's store events and a store-length lapse instead of software's."""
    from engine.flows.library import FLOWS, _family
    bm = data.get("business_model")
    if data.get("flow_type", "winback") != "winback" or not bm or _family(bm) != "ecommerce":
        return assumptions
    fd = FLOWS["winback"]
    assumed = {a.field for a in assumptions}
    out = list(assumptions)
    swaps = (("qualifying_event", EventMatch(event=fd.trigger_for(bm), label=fd.trigger_label["ecommerce"]),
              "who qualifies was not stated; for a store, defaulted to people who placed an order"),
             ("core_conversion_event", EventMatch(event=fd.goal_for(bm), label=fd.goal_label["ecommerce"]),
              "the core action was not named; for a store, a win-back's goal is another order (order_completed)"))
    for fieldname, match, why in swaps:
        if fieldname in assumed or fieldname not in data:
            data[fieldname] = match
            out = [a for a in out if a.field != fieldname] + [Assumption(field=fieldname, value=match.model_dump(), why=why)]
    if "churn_threshold_days" in assumed:
        data["churn_threshold_days"] = STORE_LAPSE_DAYS
        out = [a for a in out if a.field != "churn_threshold_days"] + [Assumption(
            field="churn_threshold_days", value=STORE_LAPSE_DAYS,
            why=f"inactivity threshold not stated; for a store, {STORE_LAPSE_DAYS} days since the last order. Set it to about one and "
                f"a half times your usual gap between orders")]
    return out


def _a(word: str) -> str:
    return "an" if word[:1] in "aeiou" else "a"


def check_business_model(data: Dict[str, Any]) -> None:
    """Refuse, by name, a flow the library does not build for this business model (the Models column of FLOW_LIBRARY.md)."""
    from engine.flows.library import FLOWS
    ft = data.get("flow_type", "winback")
    fd = FLOWS.get(ft)
    bm = data.get("business_model")
    if fd is None or not bm or fd.fits(bm):
        return
    label = fd.label.lower()
    if fd.models == ("saas",):
        instead = {"activation": 'for a store, a welcome series aims new subscribers at a first order ("a 4-touch welcome series")',
                   "trial_convert": "for a store, a welcome series or a post-purchase flow fits instead"}.get(ft, "")
        raise ValueError(f"not built for this business: {_a(label)} {label} flow is for software (business model saas or subscription), and "
                         f"this brief's business model is {bm}" + (f"; {instead}" if instead else ""))
    raise ValueError(f"not built for this business: {_a(label)} {label} flow is for stores (business model ecommerce or marketplace), and this "
                     f"brief's business model is {bm}. If you sell products with a cart and checkout, set brand.business_model: "
                     f"ecommerce in lmm.yaml")


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


def load_brief(path: Path, defaults: Optional[Dict[str, Any]] = None) -> Brief:
    """Load and validate a YAML brief, filling unstated fields from the workspace's lmm.yaml (`defaults`, read from the
    workspace when None). Raises ValueError with every problem listed, in plain words."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: a brief is a set of 'field: value' lines")
    raw = {k: v for k, v in raw.items() if v is not None and v != ""}
    gate = raw.get("sending_gate")
    if isinstance(gate, str) and gate.lower().endswith(".json"):        # a gate record written by lcm deliverability check
        gp = Path(gate) if Path(gate).is_absolute() else Path(path).parent / gate
        if not gp.exists():
            raise ValueError(f"{path}: sending_gate names {gate}, which does not exist (run lcm deliverability check DOMAIN --write-gate {gate})")
        raw["sending_gate"] = json.loads(gp.read_text(encoding="utf-8"))
    from engine import workspace
    workspace.apply_defaults(raw, set(), defaults)
    check_business_model(raw)
    ft = raw.get("flow_type", "winback")
    needed = {"winback": REQUIRED_YAML, "activation": ("product_name", "core_conversion_event")}.get(ft, ("product_name",))
    missing = [k for k in needed if k not in raw]
    if missing:
        raise ValueError(f"{path}: fill in {', '.join(missing)} (see the comments in templates/brief.yaml)")
    for k in ("qualifying_event", "core_conversion_event"):
        if k in raw:
            raw[k] = _event(raw[k])
    assumptions: List[Assumption] = []
    if "qualifying_event" not in raw and ft == "winback":
        from engine.flows.library import _family
        store = _family(raw.get("business_model") or "saas") == "ecommerce"
        assumptions.append(Assumption(field="qualifying_event", value={"event": "order_completed" if store else "activation_reached"},
                                      why="who qualifies was not stated; " + ("for a store, defaulted to people who placed an order"
                                                                              if store else "defaulted to users who reached activation")))
    if "platform" not in raw:
        assumptions.append(Assumption(field="platform", value="customerio", why="messaging platform not stated"))
    _standard_assumptions(raw, assumptions)
    raw["assumptions"] = assumptions + [Assumption(**a) for a in raw.get("assumptions", [])]
    try:
        return Brief(**raw)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors())
        raise ValueError(f"{path}: {problems}") from None


KEEP_IN_THIN = ("product_name", "flow_type", "goal", "platform", "touches", "channels", "qualifying_event", "core_conversion_event",
                "churn_threshold_days", "abandon_after_hours", "replenish_after_days", "trial_days", "holdout_pct")
IRRELEVANT = {"churn_threshold_days": ("winback", "sunset"), "abandon_after_hours": ("cart_abandon", "browse_abandon"),
              "replenish_after_days": ("replenishment",), "replenish_lead_days": ("replenishment",), "trial_days": ("trial_convert",)}


def to_yaml(brief: Brief, thin: bool = False, defaults: Optional[Dict[str, Any]] = None) -> str:
    """Write a brief back out for editing, with the assumptions listed as comments to confirm.

    thin=True writes only what this flow needs and what differs from the workspace (lmm.yaml) and the engine's defaults,
    so the brief keeps inheriting the workspace's platform, gate, policies and brand: what `lcm brief --out` writes."""
    data = brief.model_dump(exclude={"assumptions"}, exclude_none=True)
    data = {k: v for k, v in data.items() if brief.flow_type in IRRELEVANT.get(k, (brief.flow_type,))}
    if thin:
        from engine import workspace
        ws = workspace.brief_defaults() if defaults is None else defaults
        mine = brief.model_dump(mode="json", exclude={"assumptions"})
        try:     # the workspace's values as this brief would hold them (validated the same way, so 5 and 5.0 compare equal)
            theirs = Brief.model_validate({**brief.model_dump(exclude={"assumptions"}), **ws}).model_dump(mode="json")
        except ValidationError:
            theirs = {}
        model_defaults = Brief.model_construct().model_dump(mode="json")
        same = lambda k: json.dumps(mine.get(k), sort_keys=True) == json.dumps((theirs if k in ws else model_defaults).get(k), sort_keys=True)  # noqa: E731
        data = {k: v for k, v in data.items() if k in KEEP_IN_THIN or not same(k)}
        data.pop("brand_kit", None)
        if brief.frequency is not None or "frequency" in ws:
            data.pop("frequency_cap_per_week", None)
    head = ["# Generated by lcm brief. Edit, then run:", "#   lcm plan --brief <this file>", "#"]
    if thin:
        head += ["# Fields left out come from the workspace's lmm.yaml (platform, sending gate, frequency policy, offer policy,",
                 "# brand and measurement). Add one here to override it for this flow only.", "#"]
    if brief.assumptions:
        head.append("# Assumed, not stated. Confirm or change each one:")
        head += [f"#   {a.field}: {a.why}" for a in brief.assumptions]
    return "\n".join(head) + "\n" + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
