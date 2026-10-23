"""Typed policy contracts: the rules that protect a program, as data code evaluates instead of prose a regex reads.

v2 kept the sending gate as a bare word, the frequency cap and the offer policy as free text, and re-entry and send
hours as strings. The audit showed what that costs: "at most 1 lifecycle email per day" was not enforced at all,
"15% off the second order; never stack codes" read as "no offers" because of the word "never", "ends when stock runs
out" read as a deadline and licensed urgency copy, and a GREEN gate nobody observed opened full-volume sending.

Each contract here has a JSON Schema twin in schemas/ (sending_gate, trigger, frequency_policy, offer_policy,
consent_record) and tests/contracts/ proves both accept the same good documents and reject the same bad ones.
Free text is still accepted where a marketer writes it, but it is parsed once, here, into the typed form, and every
reading that involved a judgment is returned as a note the plan prints under "Confirm before you build".
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

Channel = Literal["email", "sms", "push", "in_app"]
DURATION = re.compile(r"^P(?!$)(?:(\d+)D)?(?:T(?=\d)(?:(\d+)H)?(?:(\d+)M)?)?$")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
GATE_MAX_AGE_DAYS = 30


def duration_hours(iso: str) -> float:
    """'P3D' -> 72.0, 'PT2H' -> 2.0, 'P1DT6H' -> 30.0. Raises ValueError for anything else."""
    m = DURATION.match(iso or "")
    if not m:
        raise ValueError(f"not an ISO 8601 duration in days, hours or minutes: {iso!r}")
    d, h, mi = (int(x) if x else 0 for x in m.groups())
    return d * 24 + h + mi / 60


def hours_to_duration(hours: float) -> str:
    """72 -> 'P3D', 2 -> 'PT2H', 30 -> 'P1DT6H', 0.5 -> 'PT30M'."""
    total_min = int(round(hours * 60))
    d, rem = divmod(total_min, 24 * 60)
    h, mi = divmod(rem, 60)
    out = "P" + (f"{d}D" if d else "")
    if h or mi:
        out += "T" + (f"{h}H" if h else "") + (f"{mi}M" if mi else "")
    return out if out != "P" else "PT0M"


def _duration(v: Optional[str]) -> Optional[str]:
    if v is not None:
        duration_hours(v)
    return v


def _hhmm(v: str) -> float:
    if not HHMM.match(v or ""):
        raise ValueError(f"times are 24-hour HH:MM (got {v!r})")
    h, m = v.split(":")
    return int(h) + int(m) / 60


# ----------------------------------------------------------------------------------------------
# the sending gate, as evidence rather than a word
# ----------------------------------------------------------------------------------------------

class SendingGateRecord(BaseModel):
    """What the deliverability check concluded, when, and on what evidence (schemas/sending_gate.schema.json)."""
    model_config = ConfigDict(extra="forbid")
    verdict: Literal["GREEN", "AMBER", "RED"]
    assessed_at: datetime
    auth_label: Literal["LIVE PULL", "SUPPLIED", "UNKNOWN"]
    domain: Optional[str] = None
    source: Optional[str] = Field(None, description="what produced it, e.g. 'lcm deliverability example.com'")
    signals: Dict[str, Any] = Field(default_factory=dict)
    critical_findings: List[str] = Field(default_factory=list)
    recorded_verdict: Optional[Literal["GREEN", "AMBER", "RED"]] = Field(
        None, description="the verdict the file claimed, when the engine read it lower (a GREEN without observed evidence)")

    @model_validator(mode="after")
    def _green_needs_evidence(self) -> "SendingGateRecord":
        if self.verdict == "GREEN" and self.auth_label != "LIVE PULL":
            raise ValueError("a GREEN gate needs observed authentication (auth_label LIVE PULL); a supplied or unknown one is AMBER at best")
        if self.verdict == "GREEN" and self.critical_findings:
            raise ValueError(f"a GREEN gate cannot carry critical findings: {self.critical_findings}")
        return self


GREEN_SIGNALS = ("complaint_rate", "hard_bounce_rate")
# the GREEN and AMBER ceilings engine/deliverability/analyze.py sending_gate issues a gate on, as shares
GREEN_BANDS = {"complaint_rate": (0.001, 0.003), "hard_bounce_rate": (0.01, 0.02)}
GATE_WRITER = "lcm deliverability"      # the source every record engine/deliverability/analyze.py gate_record writes begins with


def read_gate(doc: Any) -> Any:
    """A sending gate as the engine reads it. A word stays a word. A record is validated; one that claims GREEN without the
    evidence a GREEN needs (observed authentication, no critical finding) is read as AMBER, keeping the claim in
    recorded_verdict, rather than failing the plan: an unobserved claim can lower the gate, never raise it."""
    if not isinstance(doc, dict):
        return doc
    try:
        return SendingGateRecord.model_validate(doc)
    except ValidationError as e:
        if doc.get("verdict") == "GREEN":
            try:
                return SendingGateRecord.model_validate({**doc, "verdict": "AMBER", "recorded_verdict": "GREEN"})
            except ValidationError:
                pass
        problems = "; ".join(f"{'.'.join(str(x) for x in err['loc']) or 'record'}: {err['msg']}" for err in e.errors())
        raise ValueError(f"the sending gate record is not valid ({problems}). Re-run lcm deliverability check DOMAIN "
                         f"--write-gate gate.json") from None


def effective_gate(gate: Any, today: Optional[date] = None) -> Tuple[str, str]:
    """(GREEN | AMBER | RED, the reason). A bare word is a claim: RED and AMBER stand, GREEN reads AMBER. A record older
    than 30 days reads AMBER too: reputation moves in weeks, and a stale GREEN is how a program mails into a problem. So
    does a GREEN dated in the future, one recorded without observed evidence, one not written by lcm deliverability check,
    one missing the signals a GREEN is issued on (observed authentication, complaint and hard bounce rates), and one whose
    own signals are above a GREEN's ceilings (it reads AMBER or RED, by its worst signal). The engine cannot prove a file
    was not edited by hand; these rules make a hand-typed GREEN fail loudly instead of passing quietly."""
    today = today or datetime.now(timezone.utc).date()
    gate = read_gate(gate)
    if isinstance(gate, SendingGateRecord):
        age = (today - gate.assessed_at.date()).days
        when = gate.assessed_at.date()
        rerun = "re-run lcm deliverability check DOMAIN --write-gate gate.json"
        if gate.recorded_verdict == "GREEN" and gate.verdict != "GREEN":
            why = (f"it lists critical findings ({'; '.join(gate.critical_findings[:2])})" if gate.critical_findings else
                   f"its authentication was {gate.auth_label}, not observed (LIVE PULL)")
            return gate.verdict, f"the gate on record says GREEN, but {why}, so it reads {gate.verdict}; {rerun}"
        if gate.verdict == "GREEN":
            if age < -1:
                return "AMBER", f"the GREEN gate on record is dated {when}, in the future, so no check observed it; {rerun}"
            if age > GATE_MAX_AGE_DAYS:
                return "AMBER", (f"the GREEN gate on record is {age} days old (from {when}); {rerun}: a gate older than "
                                 f"{GATE_MAX_AGE_DAYS} days reads AMBER")
            if not str(gate.source or "").startswith(GATE_WRITER):
                return "AMBER", (f"the gate on record says GREEN, but it was not written by {GATE_WRITER} (its source is "
                                 f"{gate.source or 'not given'!r}), so it reads AMBER; {rerun}")
            missing = [k for k in GREEN_SIGNALS if gate.signals.get(k) is None]
            if str(gate.signals.get("authentication_state", "")).lower() != "verified":
                missing.insert(0, "authentication_state verified")
            if missing:
                return "AMBER", (f"the GREEN gate on record lacks the signals a GREEN is issued on ({', '.join(missing)}), so it "
                                 f"reads AMBER; {rerun}")
            worst, why = "GREEN", []
            for name, (green, amber) in GREEN_BANDS.items():
                try:
                    v = float(gate.signals[name])
                except (TypeError, ValueError):
                    return "AMBER", f"the GREEN gate on record has a {name} that is not a number ({gate.signals[name]!r}); {rerun}"
                band = "GREEN" if v < green else ("AMBER" if v <= amber else "RED")
                if band != "GREEN":
                    why.append(f"{name} {v:.2%} (a GREEN needs under {green:.1%})")
                    worst = "RED" if "RED" in (band, worst) else "AMBER"
            if why:
                return worst, f"the gate on record says GREEN, but its own signals do not: {'; '.join(why)}, so it reads {worst}; {rerun}"
        where = f" for {gate.domain}" if gate.domain else ""
        return gate.verdict, f"{gate.verdict} gate{where} assessed {when} ({gate.auth_label})"
    word = (gate or "UNKNOWN").upper()
    if word == "GREEN":
        return "AMBER", ("GREEN was supplied as a word, with no record of what was observed or when, so the plan runs as AMBER. "
                         "Attach the gate record lcm deliverability writes (gate.json) to lift it")
    if word in ("AMBER", "RED"):
        return word, f"{word} gate as supplied"
    return "AMBER", ("no sending gate on record, so the plan runs as AMBER. Run the deliverability check "
                     "(lcm \"check deliverability for <your domain>\") to lift this")


# ----------------------------------------------------------------------------------------------
# triggers
# ----------------------------------------------------------------------------------------------

class EventRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: str
    where: Dict[str, Any] = Field(default_factory=dict)


class AbsenceRule(BaseModel):
    """Enter when `missing` has not happened for `after` since the last `anchor` (anchor defaults to missing itself)."""
    model_config = ConfigDict(extra="forbid")
    anchor: EventRef
    missing: EventRef
    after: str

    _d = field_validator("after")(lambda cls, v: _duration(v))


class StateRule(BaseModel):
    """Enter when a record's field changes to a value: invoices.status to open with a failed attempt, a trial nearing its end."""
    model_config = ConfigDict(extra="forbid")
    object: str
    field: str
    to: str
    offset: Optional[str] = Field(None, description="enter this long before (negative) or after a timestamp field, e.g. -P3D before trial_end_at")
    relative_to: Optional[str] = None


class ReEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["never", "after_exit", "cooldown"]
    cooldown: Optional[str] = None

    @model_validator(mode="after")
    def _cooldown(self) -> "ReEntry":
        if self.mode == "cooldown":
            if not self.cooldown:
                raise ValueError("re_entry mode cooldown needs a cooldown duration, e.g. P7D")
            duration_hours(self.cooldown)
        return self

    def describe(self) -> str:
        return {"never": "never: one journey per person", "after_exit": "allowed after the person leaves the flow",
                "cooldown": f"allowed again {self.cooldown} after entering"}[self.mode]


class TriggerDefinition(BaseModel):
    """How a person enters a flow (schemas/trigger.schema.json). Waits can be hours: a 2-hour browse abandonment is
    PT2H in `abandon_after`."""
    model_config = ConfigDict(extra="forbid")
    flow_id: str = Field(pattern=r"^[a-z0-9_]+$")
    type: Literal["event", "absence", "state_change", "schedule"]
    event: Optional[EventRef] = None
    absence: Optional[AbsenceRule] = None
    state: Optional[StateRule] = None
    schedule: Optional[str] = Field(None, description="cron, for schedule triggers")
    abandon_after: Optional[str] = Field(None, description="event triggers: enter only if the goal has not happened this long after the event")
    exclude_if: List[EventRef] = Field(default_factory=list, description="events after the trigger that stop entry (a checkout ends a browse journey)")
    re_entry: ReEntry
    dedupe_key: str
    source: Literal["esp_native", "warehouse_job", "stream"] = "esp_native"
    holdout_experiment_id: Optional[str] = None

    _d = field_validator("abandon_after")(lambda cls, v: _duration(v))

    @model_validator(mode="after")
    def _shape(self) -> "TriggerDefinition":
        need = {"event": self.event, "absence": self.absence, "state_change": self.state, "schedule": self.schedule}[self.type]
        if need is None:
            raise ValueError(f"a {self.type} trigger needs its '{self.type if self.type != 'state_change' else 'state'}' block")
        return self


# ----------------------------------------------------------------------------------------------
# frequency, send windows and priority across every flow
# ----------------------------------------------------------------------------------------------

class Cap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channels: List[Channel] = Field(min_length=1)
    max: int = Field(ge=0)
    window: str

    _d = field_validator("window")(lambda cls, v: _duration(v))

    @property
    def hours(self) -> float:
        return duration_hours(self.window)

    def describe(self) -> str:
        h = self.hours
        span = (f"{int(h // 24)} days" if h % 24 == 0 and h >= 48 else "day" if h == 24 else "7 days" if h == 168 else f"{h:g} hours")
        span = "week" if h == 168 else span
        return f"at most {self.max} {'/'.join(self.channels)} message(s) per {span}"


class SendWindow(BaseModel):
    """When a channel may send, in the recipient's local time. A permitted window, never a 'quiet hours' range."""
    model_config = ConfigDict(extra="forbid")
    channel: Channel
    allowed_from: str
    allowed_until: str
    basis: Literal["recipient_local"] = "recipient_local"
    source: Optional[str] = Field(None, description="the rule that sets it, e.g. a compliance_rules.json id")

    @model_validator(mode="after")
    def _order(self) -> "SendWindow":
        if _hhmm(self.allowed_from) >= _hhmm(self.allowed_until):
            raise ValueError(f"{self.channel}: allowed_from {self.allowed_from} must be before allowed_until {self.allowed_until}")
        return self

    def allows(self, hour: float) -> bool:
        return _hhmm(self.allowed_from) <= hour < _hhmm(self.allowed_until)

    def describe(self) -> str:
        return f"{self.channel} {self.allowed_from} to {self.allowed_until} recipient local"


DEFAULT_PRIORITY = ["dunning", "cart_abandon", "browse_abandon", "post_purchase", "replenishment", "welcome", "trial_convert",
                    "activation", "winback", "sunset"]


class FrequencyPolicy(BaseModel):
    """One policy across every flow (schemas/frequency_policy.schema.json)."""
    model_config = ConfigDict(extra="forbid")
    caps: List[Cap] = Field(min_length=1)
    send_windows: List[SendWindow] = Field(default_factory=list)
    flow_priority: List[str] = Field(default_factory=lambda: list(DEFAULT_PRIORITY))
    collision_rule: Literal["higher_priority_preempts", "queue_lower_priority", "drop_lower_priority"] = "higher_priority_preempts"

    @field_validator("flow_priority")
    @classmethod
    def _unique(cls, v: List[str]) -> List[str]:
        if len(set(v)) != len(v):
            raise ValueError("flow_priority lists each flow once")
        return v

    def window(self, channel: str) -> Optional[SendWindow]:
        return next((w for w in self.send_windows if w.channel == channel), None)

    def weekly_cap(self, channel: str = "email") -> Optional[int]:
        """The tightest cap covering `channel`, scaled to 7 days (a per-day cap of 1 is 7 a week, but also 1 a day)."""
        caps = [c for c in self.caps if channel in c.channels]
        return min((c.max for c in caps if c.hours == 168), default=None)

    def describe(self) -> str:
        return "; ".join(c.describe() for c in self.caps)


_N = r"\d+|one|two|three|four|five|six|seven|eight|nine|ten|once|twice|thrice"
WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
         "once": 1, "twice": 2, "thrice": 3}
WINDOW_FOR = {"day": "P1D", "24 hours": "P1D", "week": "P7D", "7 days": "P7D", "fortnight": "P14D", "two weeks": "P14D",
              "month": "P30D", "30 days": "P30D", "daily": "P1D", "weekly": "P7D", "monthly": "P30D"}
# the period a cap is counted over: "a week", "per day", "each month", "daily", "every 3 days", "every other day"
CAP_PERIOD = re.compile(r"\b(?:(?:per|a|an|each|every)\s+(day|24 hours|week|7 days|month|30 days|fortnight|two weeks)|"
                        rf"(?:every|per|in any)\s+({_N}|other)\s+(days?|weeks?|hours?)|(daily|weekly|monthly))\b")
# "in 60 days" alone is a lapse ("have not ordered in 60 days"), never a cap; "in any 7 days" is a cap
CAP_CHANNEL = (r"e-?mails?|sms(?:es)?|texts?|text messages?|push(?:es)?(?: notifications?)?|notifications?|messages?|touch(?:es)?|"
               r"sends?|times")
# one "N things" item before a period: "2 emails", "1 lifecycle email", "twice"; the list "2 emails and 1 text" is two items
CAP_ITEM = re.compile(rf"\b({_N})\s+(?:(?!(?:{_N})\b)[a-z/-]+\s+){{0,3}}?({CAP_CHANNEL})\b|\b({_N})\s*$")
# where the words that state a cap start: everything before them in the clause is about something else
CAP_LEAD = re.compile(r"[,;:.(]|\b(?:with|but|max(?:imum)?|at most|no more than|not more than|up to|cap(?:ped)?(?: at| of)?|"
                      r"limit(?:ed)?(?: to| of)?|only|never more than)\b")
CAP_DAILY = re.compile(r"\b(daily|weekly|monthly)\b")     # kept for callers; CAP_PERIOD reads these too


def _qty(token: str) -> int:
    return int(token) if token.isdigit() else WORDS[token]


def cap_phrases(text: str) -> List[Tuple[int, int, List[Tuple[int, Optional[str]]], str]]:
    """Each cap a sentence states: (start, end, [(max, channel word or None)], window). One period can carry several items:
    '2 emails and 1 text a day' is two caps, one per channel."""
    low = (text or "").lower()
    out = []
    prev = 0
    for m in CAP_PERIOD.finditer(low):
        if m.group(1) or m.group(4):
            window = WINDOW_FOR[m.group(1) or m.group(4)]
        else:
            k = 2 if m.group(2) == "other" else _qty(m.group(2))
            unit = m.group(3)
            window = (hours_to_duration(k) if unit.startswith("hour") else f"P{k * (7 if unit.startswith('week') else 1)}D")
        seg_start = prev
        for lead in CAP_LEAD.finditer(low, prev, m.start()):
            if lead.group(0) in ",;:.(":
                seg_start = lead.end()
            elif re.search(rf"\b({_N})\b", low[lead.end():m.start()]):
                seg_start = lead.start()       # "max 2 emails": the cap starts here; "2 emails max" leaves the start alone
        segment = low[seg_start:m.start()]
        items = []
        for it in CAP_ITEM.finditer(segment):
            if it.group(3):
                items.append((_qty(it.group(3)), None))
            else:
                items.append((_qty(it.group(1)), it.group(2)))
        if items:
            first = seg_start + next(CAP_ITEM.finditer(segment)).start()
            out.append((first, m.end(), items, window))
        prev = m.end()
    return out


def parse_cap_text(text: str, channels: Optional[List[str]] = None) -> List[Cap]:
    """'at most 1 lifecycle email per day' -> [Cap(email, 1, P1D)]; 'three messages a week' -> [Cap(email,sms,push, 3, P7D)];
    '2 emails and 1 text a day' -> [Cap(email, 2, P1D), Cap(sms, 1, P1D)]; 'one email every 3 days' -> [Cap(email, 1, P3D)];
    'twice a week' and '2 SMS daily' too. A number with no channel word takes `channels`, else the sentence's own channel
    when it names only emails, else every capped channel."""
    low = (text or "").lower()
    default = channels or (["email"] if re.search(r"\bemails?\b", low) and not re.search(r"\b(messages?|texts?|sms|push)\b", low)
                           else ["email", "sms", "push"])
    out: List[Cap] = []
    for _s, _e, items, window in cap_phrases(text):
        for n, word in items:
            chans = channels or (_cap_channels(word) if word else None) or default
            out.append(Cap(channels=chans, max=n, window=window))
    return out


def _cap_channels(phrase: str) -> List[str]:
    """The channels one cap item names: '2 texts' is SMS, '6 emails' email, '4 push notifications' push, '3 messages' all three."""
    if re.search(r"\bpush\b", phrase):
        return ["push"]
    if re.search(r"\b(messages?|touch(es)?|sends?|notifications?|times)\b", phrase):
        return ["email", "sms", "push"]
    found = [ch for ch, pat in (("email", r"\be-?mails?\b"), ("sms", r"\b(sms(es)?|texts?|text messages?)\b"))
             if re.search(pat, phrase)]
    return found


# ----------------------------------------------------------------------------------------------
# offers
# ----------------------------------------------------------------------------------------------

class Offer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    offer_id: str
    kind: Literal["percent_off", "amount_off", "free_shipping", "gift", "credit", "trial_extension"]
    value: Optional[float] = Field(None, ge=0)
    eligible: Optional[str] = Field(None, description="who or which order it applies to, in words: 'second order'")
    stackable: bool = False
    expires_after: Optional[str] = Field(None, description="a real deadline, as a duration from send (P3D). Absent means no deadline: no urgency copy")

    _d = field_validator("expires_after")(lambda cls, v: _duration(v))

    def describe(self) -> str:
        v = f"{self.value:g}" if self.value is not None else ""
        what = {"percent_off": f"{v}% off", "amount_off": f"{v} off", "free_shipping": "free shipping", "gift": "a gift",
                "credit": f"{v} credit" if v else "a credit",
                "trial_extension": f"a {v}-day trial extension" if v else "a trial extension"}[self.kind]
        return (what + (f" on the {self.eligible}" if self.eligible else "") + ("" if self.stackable else ", not stackable")
                + (f", expires {self.expires_after} after send" if self.expires_after else ", no deadline"))


class OfferLimit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int = Field(ge=0)
    window: str

    _d = field_validator("window")(lambda cls, v: _duration(v))


class OfferPolicy(BaseModel):
    """What the copy may offer (schemas/offer_policy.schema.json). No offers listed means none may be offered."""
    model_config = ConfigDict(extra="forbid")
    offers: List[Offer] = Field(default_factory=list)
    max_offers_per_person: OfferLimit = Field(default_factory=lambda: OfferLimit(count=1, window="P30D"))
    min_days_between_offers: int = Field(30, ge=0)
    never_in_first_touch: bool = True
    exclude_if: List[Literal["discount_share_over_50pct_last_3_orders", "redeemed_offer_last_30d", "segment_champions",
                             "segment_cannot_lose"]] = Field(default_factory=lambda: ["discount_share_over_50pct_last_3_orders"])
    text: Optional[str] = Field(None, description="the marketer's own words, when the policy was parsed from text")

    @property
    def allows_offers(self) -> bool:
        return bool(self.offers)

    @property
    def has_deadline(self) -> bool:
        return any(o.expires_after for o in self.offers)

    def describe(self) -> str:
        if not self.offers:
            return "no offers"
        return "; ".join(o.describe() for o in self.offers)


NO_OFFER = re.compile(r"\bno (discounts?|offers?|promotions?|coupons?|incentives?)\b|^\s*(none|no)\s*$|\bnever (discount|offer)|"
                      r"\bfull[- ]price only\b|\bdo not discount\b|\bdon't discount\b")
STOCK_END = re.compile(r"while (supplies|stocks?) last|(until|when) (stock|supplies|inventory) (runs? out|lasts?|is gone)|"
                       r"stock runs out|until sold out|limited stock")
DEADLINE = re.compile(r"(?:expires?|ends?|valid|good)\s+(?:in|for|after|within)\s+(\d+)\s*(hour|day|week)s?|(\d+)[- ](hour|day|week)\s+(?:window|deadline|offer)")
NOT_STACK = re.compile(r"(never|no|not|don't|do not|cannot|can't)\s+(be\s+)?stack|non[- ]stackable|one code per order|single[- ]use")


def parse_offer_text(text: str) -> Tuple[OfferPolicy, List[str]]:
    """Read a free-text offer policy into an OfferPolicy, with a note for every judgment the reading made."""
    raw = (text or "").strip()
    low = raw.lower()
    notes: List[str] = []
    if not raw or NO_OFFER.search(low):
        return OfferPolicy(text=raw or None), notes
    offers: List[Offer] = []
    eligible = None
    m = re.search(r"(first|second|next|third|1st|2nd|3rd) (order|purchase)", low)
    if m:
        ordinal = {"1st": "first", "2nd": "second", "3rd": "third"}.get(m.group(1), m.group(1))
        eligible = f"{ordinal} {m.group(2)}"
    stackable = not bool(NOT_STACK.search(low)) and bool(re.search(r"\bstack(able|s)?\b", low))
    expires = None
    d = DEADLINE.search(low)
    if d:
        n = int(d.group(1) or d.group(3))
        unit = d.group(2) or d.group(4)
        expires = hours_to_duration(n * {"hour": 1, "day": 24, "week": 168}[unit])
    if STOCK_END.search(low):
        notes.append("the offer ends when stock runs out, which is not a deadline the reader can act on: urgency copy stays blocked")
    for i, pm in enumerate(re.finditer(r"(\d{1,2}(?:\.\d+)?)\s*%\s*off", low)):
        offers.append(Offer(offer_id=f"pct_{pm.group(1)}", kind="percent_off", value=float(pm.group(1)), eligible=eligible,
                            stackable=stackable, expires_after=expires))
    for pm in re.finditer(r"[$£€](\d+(?:\.\d+)?)\s*off", low):
        offers.append(Offer(offer_id=f"amt_{pm.group(1)}", kind="amount_off", value=float(pm.group(1)), eligible=eligible,
                            stackable=stackable, expires_after=expires))
    if re.search(r"free (shipping|delivery)", low):
        offers.append(Offer(offer_id="free_shipping", kind="free_shipping", eligible=eligible, stackable=stackable, expires_after=expires))
    if re.search(r"free (gift|sample)", low):
        offers.append(Offer(offer_id="gift", kind="gift", eligible=eligible, stackable=stackable, expires_after=expires))
    tm = re.search(r"(\d+)[- ]day (trial )?extension|extend(ed)? (the )?trial (by )?(\d+)", low)
    if tm:
        days = float(tm.group(1) or tm.group(6))
        offers.append(Offer(offer_id="trial_extension", kind="trial_extension", value=days, stackable=False, expires_after=expires))
    if not offers and re.search(r"discount|offer|coupon|promo|credit|% off|sale", low):
        notes.append(f"could not read an exact offer in {raw!r}, so none is allowed: write it as a typed offer policy "
                     f"(kind, value, eligible, expires_after) to use one")
    elif offers:
        notes.append("offer policy read as: " + "; ".join(o.describe() for o in offers))
    return OfferPolicy(offers=offers, text=raw), notes


# ----------------------------------------------------------------------------------------------
# consent
# ----------------------------------------------------------------------------------------------

class ConsentRecord(BaseModel):
    """One consent change for one person and channel (schemas/consent_record.schema.json); the newest record wins."""
    model_config = ConfigDict(extra="forbid")
    user_id: str
    channel: Literal["email", "sms", "push"]
    status: Literal["granted", "revoked"]
    purpose: Literal["marketing", "transactional"] = "marketing"
    source: str
    occurred_at: datetime


def current_consent(records: List[ConsentRecord], channel: str, purpose: str = "marketing") -> Optional[ConsentRecord]:
    """The record that decides whether a person may receive `channel` now: the newest one. A later opt-in lifts an earlier
    unsubscribe (v2 kept one timestamp per channel, so someone who unsubscribed and opted back in stayed suppressed)."""
    relevant = [r for r in records if r.channel == channel and r.purpose == purpose]
    return max(relevant, key=lambda r: r.occurred_at, default=None)


def since(td: timedelta) -> str:
    return hours_to_duration(td.total_seconds() / 3600)
