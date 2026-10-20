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
             "logged": "log", "shipped": "ship", "stopped": "stop", "planned": "plan", "paid": "pay", "chose": "choose", "took": "take"}
STRIP_D = ("ated", "ized", "ised", "ured", "ored", "ided", "oted", "uted", "ited", "eted", "aced", "ared", "ined", "oved", "ased",
           "used", "aged", "uded", "ired", "ered", "oded", "iced", "ived", "aved")


def base_form(label: str) -> str:
    """'created a report' to 'create a report', for sentences that need the infinitive."""
    words = label.split()
    if not words:
        return label
    w = words[0].lower()
    if w in IRREGULAR:
        b = IRREGULAR[w]
    elif w.endswith("ied"):
        b = w[:-3] + "y"
    elif w.endswith(STRIP_D) and not w.endswith("ered") or w in ("used", "shared", "placed", "invited", "created", "completed"):
        b = w[:-1]
    elif w.endswith("ed"):
        b = w[:-2]
    else:
        b = w
    return " ".join([b] + words[1:])



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
    outcome_window_days: Optional[int] = Field(None, ge=1, le=365)

    sending_gate: Gate = "UNKNOWN"
    warehouse_dialect: SqlDialect = "postgres"
    sender_name: Optional[str] = None
    brand_voice: Optional[str] = None
    postal_address: Optional[str] = None
    cta_url: Optional[str] = Field(None, description="where the main button goes: a deep link to the core action, on your own domain")
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
    def _window(self) -> "Brief":
        if self.outcome_window_days is None:
            # long enough for the whole flow to run and a returner to act on the last touch
            self.outcome_window_days = max(30, 2 * self.churn_threshold_days)
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
    sql_checks: Dict[str, str]
    data_dependencies: List[DataDependency]
    monthly_entrants: Estimate
    baseline_conversion_rate: Estimate
    entrants_on_as_of: Optional[int] = None
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
