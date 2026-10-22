"""Everything the platform builders read from a plan, in one place: the plan itself, the account, the flow's steps in a
platform-neutral shape with waits in seconds, the copy each step uses, and the event names the platform sees.

A plan is an ExecutionPlan, its JSON (a dict), a plan.json path, or the folder `engine.pipeline.orchestrator plan` wrote
(it holds plan.json); a run folder (<flow>/runs/<id>) is resolved to its flow folder.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from engine.models import PLATFORM_DIALECT, PLATFORM_LABEL, ExecutionPlan
from engine.policy.contracts import duration_hours

from adapters.destinations.build.model import ContentRef, Wait

PLATFORMS = ("klaviyo", "hubspot", "braze", "customerio", "iterable", "sfmc")
CAPABILITIES_PATH = Path(__file__).resolve().parents[3] / "config" / "esp_capabilities.json"
UNIT_SECONDS = {"seconds": 1, "minutes": 60, "hours": 3600}
_CAPS: Dict[str, Any] = {}


def capabilities() -> Dict[str, Any]:
    """config/esp_capabilities.json, read once."""
    if not _CAPS:
        _CAPS.update(json.loads(CAPABILITIES_PATH.read_text(encoding="utf-8")))
    return _CAPS


def plan_folder(path: Union[str, Path]) -> Path:
    """The folder that holds plan.json: the path itself, its parent for a plan.json path, or the flow folder of a run."""
    p = Path(path)
    if p.is_file():
        return p.parent
    if (p / "plan.json").is_file():
        return p
    if (p / "state.json").is_file() and (p.parents[1] / "plan.json").is_file():
        return p.parents[1]
    raise FileNotFoundError(f"{p}: no plan.json here (pass the folder `orchestrator plan` wrote, or its plan.json)")


def load_plan(plan: Union[ExecutionPlan, Dict[str, Any], str, Path]) -> ExecutionPlan:
    if isinstance(plan, ExecutionPlan):
        return plan
    if isinstance(plan, dict):
        return ExecutionPlan.model_validate(plan)
    folder = plan_folder(plan)
    return ExecutionPlan.model_validate_json((folder / "plan.json").read_text(encoding="utf-8"))


def normalize_account(account: Optional[Dict[str, Any]], platform: str) -> Dict[str, Any]:
    """{platform, tier, options}. An account written for another platform keeps its options but not its tier."""
    acc = dict(account or {})
    out = {"platform": acc.get("platform") or platform, "tier": acc.get("tier"), "options": dict(acc.get("options") or {})}
    if out["tier"] is not None:
        out["tier"] = str(out["tier"]).strip().lower()
    if out["platform"] != platform:
        out["note"] = f"the account describes {out['platform']}, not {platform}: its tier is ignored"
        out["tier"] = None
    return out


def wait_in_unit(seconds: int, platform: str) -> Wait:
    """A wait in the platform's smallest delay unit. Customer.io uses hours (verified) unless the wait is not a whole hour,
    then minutes (LIKELY: config/esp_capabilities.json customerio.minutes_delay)."""
    caps = capabilities()["platforms"][platform]
    unit = caps["min_delay"]["value"]["unit"]
    if platform == "customerio" and seconds % 3600:
        unit = "minutes"
    size = UNIT_SECONDS[unit]
    value = seconds // size if seconds % size == 0 else round(seconds / size)
    return Wait(iso=_iso(seconds), seconds=seconds, value=int(value), unit=unit, label=_label(seconds))


def _iso(seconds: int) -> str:
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    out = "P" + (f"{d}D" if d else "")
    if h or m or s:
        out += "T" + (f"{h}H" if h else "") + (f"{m}M" if m else "") + (f"{s}S" if s else "")
    return out if out != "P" else "PT0S"


def _label(seconds: int) -> str:
    if seconds % 86400 == 0 and seconds:
        n = seconds // 86400
        return f"{n} day" + ("s" if n != 1 else "")
    if seconds % 3600 == 0 and seconds:
        n = seconds // 3600
        return f"{n} hour" + ("s" if n != 1 else "")
    n = seconds // 60
    return f"{n} minute" + ("s" if n != 1 else "")


@dataclass
class Node:
    """One step of the flow spec, platform-neutral: a split, a send or a profile update, after a wait."""
    id: str
    kind: str                          # holdout | send | consent | sunset
    wait_seconds: int = 0
    until_local_time: Optional[str] = None
    channel: Optional[str] = None
    content_step: Optional[str] = None
    fallback_step: Optional[str] = None     # consent: the email fallback step id
    consent: Optional[str] = None           # consent: sms | push
    respect_send_window: bool = False
    message: str = ""
    condition: Optional[str] = None


@dataclass
class Ctx:
    plan: ExecutionPlan
    platform: str
    account: Dict[str, Any]
    native: bool = True
    folder: Optional[Path] = None
    nodes: List[Node] = field(default_factory=list)

    @property
    def brief(self):
        return self.plan.brief

    @property
    def spec(self) -> Dict[str, Any]:
        return self.plan.flow.flow_spec

    @property
    def label(self) -> str:
        return PLATFORM_LABEL[self.platform]

    @property
    def caps(self) -> Dict[str, Any]:
        return capabilities()["platforms"][self.platform]

    @property
    def options(self) -> Dict[str, Any]:
        return self.account.get("options") or {}

    @property
    def slug(self) -> str:
        return self.brief.slug

    @property
    def flow_type(self) -> str:
        return self.brief.flow_type

    @property
    def core(self) -> str:
        return self.brief.core_conversion_event.event

    @property
    def qualifying(self) -> str:
        return self.brief.qualifying_event.event

    @property
    def shopify(self) -> bool:
        """Klaviyo with Shopify's own events: esp.options.shopify when set, else whatever the rendered copy reads (the product
        block reads Shopify's event.extra.line_items for an ecommerce cart or post-purchase flow on Klaviyo)."""
        if self.options.get("shopify") is not None:
            return bool(self.options["shopify"])
        if self.platform != "klaviyo" or self.brief.platform != "klaviyo":
            return False
        from engine.render.product_blocks import source
        return source(self.brief).startswith("shopify")

    @property
    def holdout_property(self) -> str:
        """The per-person, per-flow property a native segment trigger splits on (no trigger event carries in_holdout)."""
        return f"lcm_holdout_{self.slug}"[:100]

    def event_name(self, event: str) -> str:
        """How the platform names an engine event: an explicit mapping, Shopify's Klaviyo metric names, HubSpot's fully
        qualified pe<hub id>_<name>, or the engine's own name."""
        mapped = (self.options.get("event_names") or {}).get(event)
        if mapped:
            return str(mapped)
        if self.platform == "klaviyo" and event in self.caps["built_in_metric_names"]["value"]:
            return self.caps["built_in_metric_names"]["value"][event]       # Klaviyo's own email metrics (names LIKELY)
        if self.platform == "klaviyo" and self.shopify:
            return self.caps["shopify_metric_names"]["value"].get(event, event)
        if self.platform == "hubspot":
            hub = str(self.options.get("hub_id") or "<hub id>")
            return f"pe{hub}_{event}"
        return event

    def content(self, step_id: str) -> Optional[ContentRef]:
        tc = next((c for c in self.plan.copy_plan if c.step_id == step_id), None)
        if tc is None:
            return None
        variants = {v.variant_id: sorted(v.rendered) for v in tc.variants if v.ok}
        chosen = self.plan.approvals.get(step_id) or (sorted(variants)[0] if variants else None)
        subject = preheader = None
        if chosen and tc.channel == "email":
            v = next(v for v in tc.variants if v.variant_id == chosen)
            subj = v.rendered.get(f"{step_id}_{chosen}.subject.txt", "")
            m = re.search(r"^Subject: (.*)$", subj, re.M)
            subject = m.group(1).strip() if m else None
            m = re.search(r"^Preview text: (.*)$", subj, re.M)
            preheader = m.group(1).strip() if m else None
        return ContentRef(step_id=step_id, channel=tc.channel, variants=variants, chosen=chosen, subject=subject, preheader=preheader)

    def rendered(self, step_id: str, name_suffix: str) -> Optional[str]:
        """The rendered text of the chosen variant's file ending in name_suffix (".html", ".txt", ".json")."""
        ref = self.content(step_id)
        if not ref or not ref.chosen:
            return None
        tc = next(c for c in self.plan.copy_plan if c.step_id == step_id)
        v = next(v for v in tc.variants if v.variant_id == ref.chosen)
        return v.rendered.get(f"{step_id}_{ref.chosen}{name_suffix}")

    def has(self, channel: str) -> bool:
        return any(n.channel == channel or n.consent == channel for n in self.nodes)

    @property
    def dialect_ok(self) -> bool:
        return self.spec.get("template_dialect") == PLATFORM_DIALECT[self.platform]


def _wait_seconds(wait: Dict[str, Any]) -> tuple[int, Optional[str]]:
    t = wait.get("type", "none")
    if t == "fixed":
        return int(round(duration_hours(wait["duration"]) * 3600)), None
    if t == "until_time_local":
        after = int(round(duration_hours(wait["after"]) * 3600)) if wait.get("after") else 0
        return after, wait.get("local_time")
    if t == "until_event":
        return int(round(duration_hours(wait["timeout"]) * 3600)), None
    return 0, None


def nodes_from_spec(spec: Dict[str, Any]) -> List[Node]:
    """The flow spec's steps as nodes. An SMS or push step and its email fallback become one consent node."""
    steps = spec.get("steps", [])
    by_id = {s["id"]: s for s in steps}
    out: List[Node] = []
    for s in steps:
        sid = s["id"]
        secs, local = _wait_seconds(s.get("wait") or {})
        if sid.endswith("_email_fallback"):
            continue
        if sid == "holdout_split":
            out.append(Node(id=sid, kind="holdout", wait_seconds=secs, condition=s.get("branch", {}).get("condition")))
        elif sid == "sunset":
            out.append(Node(id=sid, kind="sunset", wait_seconds=secs, until_local_time=local, condition=s.get("condition"), message=s.get("message", "")))
        elif s.get("channel") in ("sms", "push"):
            fb = f"{sid.split('_')[0]}_email_fallback"
            out.append(Node(id=sid, kind="consent", wait_seconds=secs, until_local_time=local, channel=s["channel"], content_step=sid,
                            fallback_step=fb if fb in by_id else None, consent=s["channel"], respect_send_window=bool(s.get("respect_send_window")),
                            message=s.get("message", ""), condition=s.get("condition")))
        elif s.get("channel") in ("email", "in_app"):
            out.append(Node(id=sid, kind="send", wait_seconds=secs, until_local_time=local, channel=s["channel"], content_step=sid,
                            respect_send_window=bool(s.get("respect_send_window")), message=s.get("message", ""), condition=s.get("condition")))
    return out


def make_ctx(plan: Union[ExecutionPlan, Dict[str, Any], str, Path], platform: Optional[str], native: bool,
             account: Optional[Dict[str, Any]]) -> Ctx:
    folder = None
    if isinstance(plan, (str, Path)):
        folder = plan_folder(plan)
    p = load_plan(plan)
    platform = platform or p.brief.platform
    if platform not in PLATFORMS:
        raise ValueError(f"platform must be one of {', '.join(PLATFORMS)} (got {platform!r})")
    ctx = Ctx(plan=p, platform=platform, account=normalize_account(account, platform), native=native, folder=folder)
    ctx.nodes = nodes_from_spec(p.flow.flow_spec)
    return ctx
