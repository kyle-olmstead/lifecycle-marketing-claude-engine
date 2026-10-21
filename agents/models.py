"""Typed contracts between the teammate's agents (Pydantic v2).

Every object an agent hands to the next one is defined here, so a malformed hand-off fails loudly
at the boundary instead of producing a plausible-looking plan. Provenance travels with the data:
each brief field the marketer did not supply is recorded as an Assumption, and every number in the
plan carries an evidence label (SUPPLIED, LIVE PULL, ESTIMATE, ASSUMED, UNKNOWN).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Platform = Literal["klaviyo", "hubspot", "braze", "customerio", "sfmc"]
Channel = Literal["email", "sms", "push", "in_app"]
FlowType = Literal["winback", "activation"]
Gate = Literal["GREEN", "AMBER", "RED", "UNKNOWN"]
SqlDialect = Literal["postgres", "snowflake", "bigquery"]
BusinessModel = Literal["saas", "subscription", "ecommerce", "marketplace", "services", "sales_led_b2b"]
Label = Literal["SUPPLIED", "LIVE PULL", "ESTIMATE", "ASSUMED", "UNKNOWN", "DEMO DATA"]
CopyMode = Literal["auto", "claude", "handoff", "offline"]

# The template dialect each platform renders. Customer.io defaults to its latest Liquid.
PLATFORM_DIALECT: Dict[str, str] = {"klaviyo": "klaviyo", "hubspot": "hubl", "braze": "braze", "customerio": "customerio", "sfmc": "ampscript"}
PLATFORM_LABEL: Dict[str, str] = {"klaviyo": "Klaviyo", "hubspot": "HubSpot", "braze": "Braze", "customerio": "Customer.io",
                                  "sfmc": "Salesforce Marketing Cloud"}
EVENT_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
PropertyValue = str | int | float | bool

IRREGULAR = {"made": "make", "ran": "run", "built": "build", "bought": "buy", "sent": "send", "wrote": "write", "set": "set",
             "paid": "pay", "chose": "choose", "took": "take", "read": "read", "got": "get", "gave": "give", "did": "do",
             "began": "begin", "found": "find", "held": "hold", "kept": "keep", "left": "leave", "met": "meet", "put": "put",
             "sold": "sell", "spent": "spend", "won": "win", "invited": "invite", "liked": "like", "synced": "sync",
             "focused": "focus", "canceled": "cancel", "cancelled": "cancel", "labeled": "label", "labelled": "label",
             "monitored": "monitor", "visited": "visit", "edited": "edit", "exited": "exit", "credited": "credit",
             "opened": "open", "listened": "listen", "happened": "happen", "agreed": "agree", "freed": "free",
             "traveled": "travel", "travelled": "travel", "modeled": "model", "modelled": "model", "totaled": "total",
             "leveled": "level", "fueled": "fuel", "signaled": "signal", "treated": "treat", "repeated": "repeat",
             "heated": "heat", "seated": "seat", "defeated": "defeat", "cheated": "cheat", "favorited": "favorite",
             "favourited": "favourite"}
CONSONANTS = set("bcdfghjklmnpqrstvwxyz")
VOWELS = set("aeiou")


def _past_to_base(w: str) -> str:
    """Base form of a regular past-tense verb, by the spelling rules that cover product actions."""
    if w in IRREGULAR:
        return IRREGULAR[w]
    if not w.endswith("ed") or len(w) < 4:
        return w
    if w.endswith("ied") and len(w) > 4:
        return w[:-3] + "y"                         # applied, copied
    s = w[:-2]                                      # created -> creat, booked -> book
    if len(s) >= 3 and s[-1] == s[-2] and s[-1] in "bgmnprt":
        return s[:-1]                               # shipped, planned, submitted, logged, preferred
    last, prev = s[-1], s[-2]
    if last in "wyhx" or s.endswith(("ss", "ll", "zz", "ff", "dd")):
        return s                                    # viewed, played, reached, fixed, passed, called, added
    if last == "u":
        return s + "e"                              # continued, queued
    if last in "vz":
        return s + "e"                              # saved, approved, optimized
    if last in "cg":
        return s + "e"                              # placed, reduced, changed, managed
    if last == "s":
        return s + "e"                              # used, closed, purchased, paused
    if last == "l":
        if prev in CONSONANTS:
            return s + "e"                          # enabled, handled
        return s + "e" if len(s) >= 3 and s[-3] in CONSONANTS else s     # scheduled, filed / failed, pooled
    if last == "r":
        return s + "e" if prev in "aiuo" else s     # shared, configured, required, stored / entered, ordered
    if last in "td":
        if prev in CONSONANTS:
            return s                                # connected, started, printed, exported, sounded
        if s.endswith("eat"):
            return s + "e"                          # created, recreated (treated and the like are listed above)
        if prev in VOWELS and len(s) >= 3 and s[-3] in VOWELS:
            return s                                # loaded, needed, waited
        return s + "e" if prev in "aeou" or (prev == "i" and last == "d") else s   # created, completed, voted, decided / visited
    if last in "kpmnb":
        if prev in "aiouy" and len(s) >= 3 and s[-3] in CONSONANTS:
            return s + "e"                          # invoked, typed, named, tuned, subscribed
        return s                                    # booked, clicked, helped, signed, opened
    return s


def base_form(label: str) -> str:
    """'created a report' to 'create a report', for sentences that need the infinitive."""
    words = label.split()
    if not words:
        return label
    return " ".join([_past_to_base(words[0].lower())] + words[1:])


QUIET = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(?:to|-|until)\s*(\d{1,2})(?::(\d{2}))?")


def parse_quiet_hours(text: str) -> Optional[List[float]]:
    """'21:00 to 08:00 recipient local time' to [21.0, 8.0]; None if it cannot be read."""
    m = QUIET.search(text or "")
    if not m:
        return None
    a, b = int(m.group(1)) + int(m.group(2) or 0) / 60, int(m.group(3)) + int(m.group(4) or 0) / 60
    return [a, b] if 0 <= a < 24 and 0 <= b < 24 else None


def in_quiet_hours(hour: float, quiet: List[float]) -> bool:
    start, end = quiet
    return (start <= hour or hour < end) if start > end else (start <= hour < end)


class Assumption(BaseModel):
    """A brief field the marketer did not supply, the value used instead, and why."""
    model_config = ConfigDict(extra="forbid")
    field: str
    value: Any
    why: str


class EventMatch(BaseModel):
    """One event, optionally narrowed by property equality (compared as text in every dialect)."""
    model_config = ConfigDict(extra="forbid")
    event: str
    where: Dict[str, PropertyValue] = Field(default_factory=dict)
    label: Optional[str] = Field(None, description="how a marketer says it, e.g. 'created a report'")

    @field_validator("event")
    @classmethod
    def _snake(cls, v: str) -> str:
        if not EVENT_NAME.match(v):
            raise ValueError(f"event names are object_action in snake_case (got {v!r}); see docs/SQL_SCHEMA_CONVENTIONS.md")
        return v

    @field_validator("where")
    @classmethod
    def _keys(cls, v: Dict[str, PropertyValue]) -> Dict[str, PropertyValue]:
        bad = [k for k in v if not EVENT_NAME.match(k)]
        if bad:
            raise ValueError(f"property keys must be snake_case: {bad}")
        return v

    @property
    def do(self) -> str:
        """The action in the base form, for sentences: 'created a report' becomes 'create a report'."""
        return base_form(self.label) if self.label else f"do {self.event}"

    def detail(self) -> str:
        cond = ", ".join(f"{k} = {val}" for k, val in self.where.items())
        return f"{self.event}" + (f" where {cond}" if cond else "")

    def describe(self) -> str:
        if self.label:
            return self.label
        cond = ", ".join(f"{k} = {val}" for k, val in self.where.items())
        return f"{self.event}" + (f" ({cond})" if cond else "")


class Brief(BaseModel):
    """What a marketer fills in (briefs/brief_template.yaml) or says in one sentence."""
    model_config = ConfigDict(extra="forbid")

    product_name: str = Field(min_length=1)
    flow_type: FlowType = "winback"
    goal: Optional[str] = None
    platform: Platform = "customerio"
    business_model: BusinessModel = "saas"

    qualifying_event: EventMatch = Field(default_factory=lambda: EventMatch(event="activation_reached", label="reached activation"))
    core_conversion_event: EventMatch = Field(default_factory=lambda: EventMatch(event="feature_used", label="used the product"))
    churn_threshold_days: int = Field(14, ge=3, le=365, description="days without the core event before someone enters (winback)")
    touches: int = Field(5, ge=1, le=8)
    channels: List[Channel] = Field(default_factory=lambda: ["email"])

    holdout_pct: float = Field(15, ge=0, le=50)
    randomization_unit: Literal["user", "account"] = "user"
    monthly_entrants: Optional[int] = Field(None, ge=0)
    baseline_conversion_rate: Optional[float] = Field(None, gt=0, lt=1)
    min_detectable_lift: float = Field(0.20, gt=0, le=5, description="relative lift worth acting on, 0.20 = 20%")
    outcome_window_days: Optional[int] = Field(None, ge=1, le=365,
                                               description="default: the whole flow plus a week (win-back: at least 30 days or twice the threshold)")

    sending_gate: Gate = "UNKNOWN"
    warehouse_dialect: SqlDialect = "postgres"
    sender_name: Optional[str] = None
    brand_voice: Optional[str] = None
    postal_address: Optional[str] = None
    cta_url: Optional[str] = Field(None, description="where the main button goes: a deep link to the core action, on your own domain")
    recipient_countries: List[str] = Field(default_factory=list, description="ISO country codes of the audience, when there is no data to measure it")
    offer_policy: str = "no discounts or offers"
    frequency_cap_per_week: int = Field(3, ge=1, le=14)
    send_hour_local: int = Field(10, ge=6, le=20)
    quiet_hours: str = "21:00 to 08:00 recipient local time"
    copy_variants: int = Field(2, ge=1, le=4)
    assumptions: List[Assumption] = Field(default_factory=list)

    @field_validator("channels")
    @classmethod
    def _channels(cls, v: List[str]) -> List[str]:
        seen: List[str] = []
        for c in v:
            if c not in seen:
                seen.append(c)
        if "email" not in seen:
            raise ValueError("email must be one of the channels: it carries the unsubscribe, the re-permission ask and the fallback")
        return seen

    @model_validator(mode="after")
    def _distinct_events(self) -> "Brief":
        # a win-back may qualify and lapse on the same event (first order, then no repeat order); an activation flow cannot
        same = (self.qualifying_event.event, self.qualifying_event.where) == (self.core_conversion_event.event, self.core_conversion_event.where)
        if same and self.flow_type == "activation":
            raise ValueError(f"an activation flow's qualifying event and core action are the same ({self.core_conversion_event.detail()}): "
                             f"nobody could qualify without already having done the core action. Make them different events")
        return self

    @model_validator(mode="after")
    def _quiet(self) -> "Brief":
        quiet = parse_quiet_hours(self.quiet_hours)
        if quiet is None:
            raise ValueError(f"quiet_hours must read like '21:00 to 08:00' (got {self.quiet_hours!r})")
        if in_quiet_hours(self.send_hour_local, quiet):
            raise ValueError(f"send_hour_local {self.send_hour_local}:00 falls inside quiet hours ({self.quiet_hours}); pick an hour outside them")
        return self

    @property
    def template_dialect(self) -> str:
        return PLATFORM_DIALECT[self.platform]

    @property
    def slug(self) -> str:
        base = f"{self.flow_type}-{self.churn_threshold_days}d" if self.flow_type == "winback" else self.flow_type
        name = re.sub(r"[^a-z0-9]+", "_", self.product_name.lower()).strip("_")[:24] or "product"
        return f"{name}_{base}".replace("-", "_")

    def assumed(self, field: str) -> bool:
        return any(a.field == field for a in self.assumptions)


class DataDependency(BaseModel):
    """Matches the data_dependencies item of schemas/flow_spec.schema.json."""
    model_config = ConfigDict(extra="forbid")
    name: str
    kind: Literal["event", "attribute", "object"]
    status: Literal["verified", "blocked", "unknown"]
    fill_rate: Optional[float] = Field(None, ge=0, le=1)
    note: Optional[str] = None


class Estimate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Optional[float]
    label: Label
    basis: str


class CohortDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    plain_english: str
    trigger_event: str = Field(description="event the daily warehouse job pushes to the ESP when someone enters")
    entry_rules: List[str]
    exclusions: List[str]
    exit_rule: str
    platform_pattern: str
    sql: Dict[str, str] = Field(description="entry query by dialect, plus 'duckdb' for runs on files")
    history_sql: Dict[str, str]
    job_sql: Dict[str, Dict[str, str]] = Field(default_factory=dict,
                                               description="win-back daily job by dialect: ledger (once), queue, pending, mark")
    sql_checks: Dict[str, str]
    data_dependencies: List[DataDependency]
    monthly_entrants: Estimate
    baseline_conversion_rate: Estimate
    entrants_on_as_of: Optional[int] = None
    leak: Optional[str] = Field(None, description="onboarding step with the largest drop, when measured")
    countries: Dict[str, float] = Field(default_factory=dict, description="share of qualified people by users.country, when measured")
    as_of: Optional[str] = None
    trigger_payload_example: Dict[str, Any] = Field(default_factory=dict)


class Touch(BaseModel):
    """One sending step, with what the copywriter needs to know about its job."""
    model_config = ConfigDict(extra="forbid")
    step_id: str
    index: int
    day: int
    channel: Channel
    role: str
    job: str
    framework: str
    goal: str
    requires: List[str]
    condition: Optional[str] = None


class Decision(BaseModel):
    """One decision an agent made, and why: the plan's memory of its own reasoning."""
    model_config = ConfigDict(extra="forbid")
    stage: str
    decision: str
    why: str


class PlannedTouch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int
    day: int
    role: str
    channel: Channel
    why: str


class Strategy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    touches: List[PlannedTouch]
    channels_requested: List[str]
    channels_used: List[str]
    offer: str
    leak_step: Optional[str] = None
    decisions: List[Decision] = Field(default_factory=list)


class ComplianceFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule: str
    jurisdiction: str
    status: Literal["pass", "warn", "fail"]
    detail: str
    source: Optional[str] = None


class ComplianceReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["pass", "warn", "fail"]
    jurisdictions: List[str]
    findings: List[ComplianceFinding]
    note: str = ("Rules engine, not legal advice: it checks the plan against the rules in config/compliance_rules.json, "
                 "each with its source. Confirm anything marked warn with counsel.")


class FlowPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    flow_spec: Dict[str, Any]
    simulation: Dict[str, Any]
    touches: List[Touch]
    mermaid: str
    build_steps: List[str]


class CopyVariant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    variant_id: str
    framework: str
    hypothesis: str
    fields: Dict[str, Any] = Field(description="channel fields: subject, preheader, paragraphs, cta_label / text / title, body")
    rendered: Dict[str, str] = Field(default_factory=dict, description="file name to rendered template text")
    errors: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class TouchCopy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_id: str
    channel: Channel
    mode: Literal["claude", "offline", "handoff", "supplied"]
    copy_brief: str
    variants: List[CopyVariant] = Field(default_factory=list)
    note: Optional[str] = None


class ExperimentDesign(BaseModel):
    model_config = ConfigDict(extra="forbid")
    holdout_pct: float
    unit: Literal["user", "account"]
    salt: str
    assignment_sql: Dict[str, str]
    assignment_rule: str
    primary_metric: str
    outcome_window_days: int
    read: Literal["causal", "causal, slow", "directional only", "not readable"]
    sizing: str
    read_after: str
    feasibility: Optional[Dict[str, Any]] = None
    mde_by_horizon: List[Dict[str, Any]] = Field(default_factory=list)
    sensitivity: List[Dict[str, Any]] = Field(default_factory=list)
    decision_rule: str
    srm_rule: str
    peeking_policy: str
    guardrail_metrics: List[str]
    variant_tests: List[str] = Field(default_factory=list)


class Check(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    status: Literal["pass", "warn", "fail"]
    detail: str


class QAReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["READY", "READY WITH ASSUMPTIONS", "BLOCKED"]
    checks: List[Check]

    @classmethod
    def from_checks(cls, checks: List[Check], assumptions: int) -> "QAReport":
        if any(c.status == "fail" for c in checks):
            status = "BLOCKED"
        elif assumptions or any(c.status == "warn" for c in checks):
            status = "READY WITH ASSUMPTIONS"
        else:
            status = "READY"
        return cls(status=status, checks=checks)


class ExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).replace(microsecond=0).isoformat())
    engine_version: str
    brief: Brief
    gate_policy: Dict[str, Any]
    cohort: CohortDefinition
    flow: FlowPlan
    copy_plan: List[TouchCopy]
    experiment: ExperimentDesign
    qa: QAReport
    strategy: Optional[Strategy] = None
    compliance: Optional[ComplianceReport] = None
    decisions: List[Decision] = Field(default_factory=list)
    parent_run_id: Optional[str] = None
    approvals: Dict[str, str] = Field(default_factory=dict, description="step id to the approved variant id")
