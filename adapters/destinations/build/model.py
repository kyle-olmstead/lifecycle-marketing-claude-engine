"""The build pack's typed parts: what a person (or the create API) needs to stand a flow up natively in one ESP.

A BuildPack is platform-specific and complete on its own: the native trigger (exact trigger type, filters, exit criteria,
re-entry), every step as the platform's own action with its wait in the platform's smallest delay unit, the explicit
splits (holdout first, then each SMS or push consent split with its email fallback), the goal and exit, the send-window
and frequency settings, the copy files each message uses, the capability checks against the account, and the artifacts:
an importable create-API body where the platform has one, otherwise a click-by-click build sheet.

Every fact a pack relies on that is not VERIFIED is listed in `unverified`, with its source, so nothing is presented as
more certain than the research behind it (config/esp_capabilities.json).
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Confidence = Literal["VERIFIED", "LIKELY", "UNVERIFIED"]
CheckStatus = Literal["pass", "warn", "fail"]
PackStatus = Literal["READY", "READY WITH WARNINGS", "BLOCKED"]
TriggerMode = Literal["native_event", "native_segment", "scheduled_segment", "api_event", "warehouse_job"]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Fact(_M):
    """A vendor fact the pack depends on, with where it was read and how sure it is."""
    fact: str
    source: str
    confidence: Confidence


class CapabilityCheck(_M):
    id: str
    status: CheckStatus
    hard: bool = False
    requirement: str
    detail: str
    source: str
    confidence: Confidence


class Wait(_M):
    """A relative wait, expressed in the platform's smallest delay unit (Braze seconds; Klaviyo, HubSpot, Iterable and SFMC
    minutes; Customer.io hours, minutes only when a wait is not a whole hour)."""
    iso: str
    seconds: int
    value: int
    unit: str
    label: str
    until_local_time: Optional[str] = Field(None, description="then wait until this recipient-local time (HH:MM)")


class ContentRef(_M):
    """The rendered copy a message step uses: file names inside the plan's copy/ folder, per variant."""
    step_id: str
    channel: str
    variants: Dict[str, List[str]] = Field(default_factory=dict)
    chosen: Optional[str] = Field(None, description="the variant an importable artifact uses (the approved one, else A)")
    subject: Optional[str] = None
    preheader: Optional[str] = None


class Split(_M):
    kind: Literal["holdout", "consent", "sunset"]
    condition: str
    platform_action: str
    yes: str
    no: str
    source: Literal["trigger_event_property", "profile_property", "consent", "engagement"] = "consent"
    note: Optional[str] = None


class Step(_M):
    id: str
    kind: Literal["split", "send", "update_profile"]
    platform_action: str
    wait: Optional[Wait] = None
    channel: Optional[str] = None
    condition: Optional[str] = None
    content: Optional[ContentRef] = None
    split: Optional[Split] = None
    respect_send_window: bool = False
    window: Optional[str] = Field(None, description="how this platform holds the step to the channel's permitted window")
    manual: bool = Field(False, description="not in the importable artifact: a person adds it by hand")
    note: Optional[str] = None


class SegmentDefinition(_M):
    """The segment (or list, or data extension) a native absence trigger reads: what to create, in the platform's words."""
    name: str
    kind: str
    conditions: List[str]
    refresh: str
    data_prerequisites: List[str] = Field(default_factory=list)
    platform_json: Optional[Dict[str, Any]] = None
    confidence: Confidence = "LIKELY"


class TriggerConfig(_M):
    mode: TriggerMode
    type: str = Field(description="the platform's exact trigger or entry type")
    event: Optional[str] = Field(None, description="the event or metric name as the platform sees it")
    summary: str
    filters: List[str] = Field(default_factory=list, description="checked once, at entry")
    flow_filters: List[str] = Field(default_factory=list, description="checked before every step, or the platform's exit criteria")
    exit_criteria: List[str] = Field(default_factory=list)
    re_entry: Dict[str, Any] = Field(default_factory=dict)
    segment: Optional[SegmentDefinition] = None
    holdout: str = Field(description="where the holdout split reads in_holdout from")
    warehouse_alternative: Optional[str] = None
    native_alternative: Optional[str] = None
    confidence: Confidence = "VERIFIED"


class Settings(_M):
    goal: str
    exit: str
    re_entry: str
    send_windows: List[str] = Field(default_factory=list)
    frequency: List[str] = Field(default_factory=list)
    status_on_create: str


class Artifact(_M):
    """One file the pack writes: a create-API request, a duplicate request, or a build sheet."""
    name: str
    kind: Literal["create_request", "duplicate_request", "build_sheet", "holdout_sync_sql"]
    path: str
    schema_ref: Optional[str] = Field(None, description="the $defs entry in config/esp_artifact_schemas.json it validates against")
    method: Optional[str] = None
    url: Optional[str] = None
    headers: Dict[str, str] = Field(default_factory=dict)
    body: Optional[Any] = None
    order: int = 0
    produces: Optional[str] = Field(None, description="placeholder the response's data.id fills for later artifacts")
    creates_status: Optional[str] = None
    valid: Optional[bool] = None
    errors: List[str] = Field(default_factory=list)
    unverified_fields: List[str] = Field(default_factory=list)
    placeholders: List[str] = Field(default_factory=list)
    note: Optional[str] = None


class BuildPack(_M):
    platform: str
    platform_label: str
    flow_type: str
    flow: str
    slug: str
    run_id: Optional[str] = None
    status: PackStatus
    blocked_reasons: List[str] = Field(default_factory=list)
    mode: TriggerMode
    trigger: TriggerConfig
    steps: List[Step]
    settings: Settings
    content: List[ContentRef]
    checks: List[CapabilityCheck]
    artifacts: List[Artifact] = Field(default_factory=list)
    inputs_required: Dict[str, str] = Field(default_factory=dict, description="placeholder to what it is and where to find it")
    manual_steps: List[str] = Field(default_factory=list)
    unverified: List[Fact] = Field(default_factory=list)
    account: Dict[str, Any] = Field(default_factory=dict)
    automatic: str = Field(description="what the pack can create through the API, in one sentence")

    @property
    def ok(self) -> bool:
        return self.status != "BLOCKED"
