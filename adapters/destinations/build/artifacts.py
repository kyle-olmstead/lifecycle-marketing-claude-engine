"""The files a build pack writes: importable create-API requests where the platform has one, a build sheet where it has not.

  Klaviyo  POST /api/templates (one CODE template per email, from the rendered copy), POST /api/segments (native absence
           flows), then POST /api/flows: the definition with the trigger, profile filter, time-delay, trigger-split,
           conditional-split, send-email, send-sms, send-mobile-push and update-profile actions, every action in draft.
           Shapes from Klaviyo's official OpenAPI spec (revision 2026-10-15). The definition cannot be edited through the
           API after creation: review it first.
  HubSpot  POST /automation/v4/flows (beta), off (isEnabled: false): delays (0-1), list branches, send email (0-4) and edit
           record (0-5). Emails must already exist as automated marketing emails (their content ids are inputs).
  SFMC     POST /interaction/v1/interactions, created as Draft: Wait and EMAILV2 activities; the Decision Split and its
           Filter XML are UNVERIFIED.
  Braze    no create API: a build sheet, plus an optional POST /canvas/duplicate from a template Canvas you made (UNVERIFIED).
  Customer.io, Iterable: no create API: a build sheet.

Ids the pack cannot know are placeholders, <<kind:key>>: metric ids (lcm build --create looks them up), template and
segment ids (filled from the create responses), and ids a person supplies (HubSpot event type and email content ids,
Marketing Cloud email ids and the event definition key). Every artifact is validated against
config/esp_artifact_schemas.json, and the fields that schema marks UNVERIFIED are listed on the artifact.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from adapters.destinations.build.context import Ctx
from adapters.destinations.build.model import Artifact, Step, TriggerConfig

PLACEHOLDER = re.compile(r"<<([a-z_]+)(?::([^<>]*))?>>")
KLAVIYO_REVISION = "2026-10-15"
K_HEADERS = {"Authorization": "Klaviyo-API-Key ${KLAVIYO_PRIVATE_KEY}", "revision": KLAVIYO_REVISION,
             "Content-Type": "application/vnd.api+json", "accept": "application/vnd.api+json"}
H_HEADERS = {"Authorization": "Bearer ${HUBSPOT_PRIVATE_APP_TOKEN}", "Content-Type": "application/json"}
S_HEADERS = {"Authorization": "Bearer ${SFMC_ACCESS_TOKEN}", "Content-Type": "application/json"}
B_HEADERS = {"Authorization": "Bearer ${BRAZE_REST_API_KEY}", "Content-Type": "application/json"}

PLACEHOLDER_HELP = {
    "klaviyo_metric": "the Klaviyo metric id of '{key}' (lcm build --create finds it by name with GET /api/metrics)",
    "klaviyo_template": "the Klaviyo template id for {key} (lcm build --create creates it from the rendered copy first)",
    "klaviyo_segment": "the Klaviyo segment id (lcm build --create creates the segment first)",
    "from_email": "the sender address for created messages: set esp.options.from_email in lmm.yaml, or --id from_email=...",
    "from_label": "the sender name for created messages: esp.options.from_label",
    "hubspot_event_type": "the HubSpot event type id (6-...) of the custom event pe<hub id>_{key}: Data Management > Custom events",
    "hubspot_email": "the content id of the automated marketing email built from the copy for {key} (create it in HubSpot first)",
    "sfmc_event_key": "the event definition key of the journey's entry source ({key}): create it in Journey Builder first",
    "sfmc_email": "the Content Builder email id built from the copy for {key}",
    "braze_template_canvas": "the id of a template Canvas you built in the Braze dashboard",
}


def placeholders(obj: Any) -> List[str]:
    return sorted({m.group(0) for m in PLACEHOLDER.finditer(json.dumps(obj))})


def describe(ph: str) -> str:
    m = PLACEHOLDER.fullmatch(ph)
    kind, key = (m.group(1), m.group(2) or "") if m else (ph, "")
    return PLACEHOLDER_HELP.get(kind, "an id to supply with --id").format(key=key)


def fill(obj: Any, values: Dict[str, Any]) -> Any:
    """Replace placeholders with known values: a string that is exactly one placeholder takes the value as given."""
    if isinstance(obj, dict):
        return {k: fill(v, values) for k, v in obj.items()}
    if isinstance(obj, list):
        return [fill(v, values) for v in obj]
    if isinstance(obj, str):
        if obj in values:
            return values[obj]
        return PLACEHOLDER.sub(lambda m: str(values[m.group(0)]) if m.group(0) in values else m.group(0), obj)
    return obj


def known_values(ctx: Ctx) -> Dict[str, Any]:
    """Placeholder values the account already gives: esp.options.ids plus the sender."""
    out: Dict[str, Any] = {}
    for k, v in (ctx.options.get("ids") or {}).items():
        out[k if k.startswith("<<") else f"<<{k}>>"] = v
    for k in ("from_email", "from_label"):
        if ctx.options.get(k):
            out[f"<<{k}>>"] = ctx.options[k]
    if "<<from_email>>" not in out or "<<from_label>>" not in out:
        try:                          # the brand kit's marketing sender (brand.yaml senders.marketing)
            from engine.agents.copywriter import brand_for
            snd = brand_for(ctx.brief).sender("marketing")
        except Exception:  # noqa: BLE001 - no kit: the sender stays an input
            snd = None
        if snd:
            out.setdefault("<<from_email>>", snd.from_address)
            out.setdefault("<<from_label>>", snd.from_name)
    return out


# ----------------------------------------------------------------------------------------------
# Klaviyo
# ----------------------------------------------------------------------------------------------

def _k_delay(tid: str, wait: Any) -> Dict[str, Any]:
    secs = wait.seconds
    if wait.until_local_time:
        return {"temporary_id": tid, "type": "time-delay", "links": {"next": None},
                "data": {"unit": "days", "value": secs // 86400, "timezone": "profile", "delay_until_time": f"{wait.until_local_time}:00"}}
    unit, value = (("days", secs // 86400) if secs % 86400 == 0 else ("hours", secs // 3600) if secs % 3600 == 0 else ("minutes", secs // 60))
    return {"temporary_id": tid, "type": "time-delay", "links": {"next": None}, "data": {"unit": unit, "value": int(value), "timezone": "profile"}}


def _k_consent(channel: str, has: bool) -> Dict[str, Any]:
    if channel == "sms":
        status = {"subscription": "subscribed"} if has else {"subscription": "any"}
    else:
        status = {"subscription": "any"}
    return {"condition_groups": [{"conditions": [{"type": "profile-marketing-consent", "consent": {
        "channel": channel, "can_receive_marketing": has, "consent_status": status}}]}]}


def _k_since_start(metric: str) -> Dict[str, Any]:
    return {"type": "profile-metric", "metric_id": f"<<klaviyo_metric:{metric}>>", "measurement": "count",
            "measurement_filter": {"type": "numeric", "operator": "equals", "value": 0}, "timeframe_filter": {"type": "date", "operator": "flow-start"}}


def klaviyo(ctx: Ctx, mode: str, trig: TriggerConfig, steps: List[Step]) -> List[Artifact]:
    arts: List[Artifact] = []
    transactional = ctx.brief.flow.message_class == "transactional"
    for s in steps:
        if s.kind == "send" and s.channel == "email" and s.content and s.content.chosen:
            html = ctx.rendered(s.content.step_id, ".html") or ""
            text = ctx.rendered(s.content.step_id, ".txt")
            body = {"data": {"type": "template", "attributes": {"name": f"{ctx.slug} {s.id} {s.content.chosen}", "editor_type": "CODE",
                                                                "html": html, "text": text}}}
            arts.append(Artifact(name=f"klaviyo_template_{s.id}", kind="create_request", path=f"artifacts/klaviyo_template_{s.id}.json",
                                 schema_ref="klaviyo_template_create", method="POST", url="https://a.klaviyo.com/api/templates", headers=dict(K_HEADERS),
                                 body=body, order=1, produces=f"<<klaviyo_template:{s.id}>>", creates_status="a CODE template (no status)"))
    if trig.segment and trig.segment.platform_json and mode == "native_segment":
        arts.append(Artifact(name="klaviyo_segment", kind="create_request", path="artifacts/klaviyo_segment.json", schema_ref="klaviyo_segment_create",
                             method="POST", url="https://a.klaviyo.com/api/segments", headers=dict(K_HEADERS), body=trig.segment.platform_json,
                             order=2, produces=f"<<klaviyo_segment:{ctx.slug}>>", creates_status="a segment (membership builds in Klaviyo)"))
    # the trigger
    if mode == "native_segment":
        trigger: Dict[str, Any] = {"type": "segment", "id": f"<<klaviyo_segment:{ctx.slug}>>"}
        metric = None
    else:
        metric = trig.event or ctx.event_name(ctx.qualifying)
        trigger = {"type": "metric", "id": f"<<klaviyo_metric:{metric}>>"}
        where = ctx.brief.qualifying_event.where if mode != "warehouse_job" else {}
        if where:
            trigger["trigger_filter"] = {"condition_groups": [{"conditions": [
                {"type": "metric-property", "metric_id": f"<<klaviyo_metric:{metric}>>", "field": k,
                 "filter": {"type": "string", "operator": "equals", "value": str(v)}}]} for k, v in where.items()]}
    # checked before every action: the goal has not happened, nor any excluded event, since the flow started
    goal = ctx.event_name("message_clicked") if ctx.flow_type == "sunset" else ctx.event_name(ctx.core)
    groups = [{"conditions": [_k_since_start(goal)]}] + [{"conditions": [_k_since_start(ctx.event_name(x))]} for x in ctx.brief.flow.exclude_events]
    if ctx.flow_type == "post_purchase" and metric:
        groups.append({"conditions": [{"type": "profile-metric", "metric_id": f"<<klaviyo_metric:{metric}>>", "measurement": "count",
                                       "measurement_filter": {"type": "numeric", "operator": "equals", "value": 1},
                                       "timeframe_filter": {"type": "date", "operator": "alltime"}}]})
    actions: List[Dict[str, Any]] = []
    pending: List[tuple] = []          # (action, link key) waiting for the next action's id

    def link(a: Dict[str, Any], key: str = "next") -> None:
        for prev, k in pending:
            prev["links"][k] = a["temporary_id"]
        pending.clear()
        actions.append(a)
        pending.append((a, key))

    for s in steps:
        by_filter = s.kind == "split" and s.split is not None and s.split.kind == "consent"
        if s.wait and (by_filter or not s.manual):
            link(_k_delay(f"{s.id}_wait", s.wait))      # a consent split keeps its wait: its sends follow it
        if s.manual or by_filter:
            continue                            # in-app steps are left out; consent splits become message filters
        if s.kind == "split" and s.split.kind == "holdout":
            if s.split.source == "trigger_event_property" and metric:
                a = {"temporary_id": s.id, "type": "trigger-split", "links": {"next_if_true": None, "next_if_false": None},
                     "data": {"trigger_filter": {"condition_groups": [{"conditions": [
                         {"type": "metric-property", "metric_id": f"<<klaviyo_metric:{metric}>>", "field": "in_holdout",
                          "filter": {"type": "boolean", "operator": "equals", "value": True}}]}]},
                         "trigger_id": f"<<klaviyo_metric:{metric}>>", "trigger_type": "metric"}}
            else:
                a = {"temporary_id": s.id, "type": "conditional-split", "links": {"next_if_true": None, "next_if_false": None},
                     "data": {"profile_filter": {"condition_groups": [{"conditions": [
                         {"type": "profile-property", "property": ctx.holdout_property, "filter": {"type": "boolean", "operator": "equals", "value": True}}]}]}}}
            link(a, "next_if_false")
        elif s.kind == "split" and s.split.kind == "sunset":
            a = {"temporary_id": s.id, "type": "conditional-split", "links": {"next_if_true": None, "next_if_false": None},
                 "data": {"profile_filter": {"condition_groups": [{"conditions": [
                     {"type": "profile-metric", "metric_id": f"<<klaviyo_metric:{ctx.event_name('message_clicked')}>>", "measurement": "count",
                      "measurement_filter": {"type": "numeric", "operator": "greater-than-or-equal", "value": 1},
                      "timeframe_filter": {"type": "date", "operator": "flow-start"}}]}]}}}
            link(a, "next_if_false")
        elif s.kind == "update_profile":
            link({"temporary_id": s.id, "type": "update-profile", "links": {"next": None}, "data": {"status": "draft", "profile_operations": [
                {"operator": "update", "property_type": "boolean", "property_key": "lcm_sunset", "property_value": True}]}})
        elif s.kind == "send" and s.channel == "email":
            msg: Dict[str, Any] = {"from_email": "<<from_email>>", "from_label": "<<from_label>>",
                                   "subject_line": (s.content.subject if s.content and s.content.subject else ctx.slug),
                                   "preview_text": s.content.preheader if s.content else None, "template_id": f"<<klaviyo_template:{s.id}>>",
                                   "smart_sending_enabled": not transactional, "transactional": transactional, "add_tracking_params": True, "name": s.id}
            if s.condition and "fallback" in s.condition:
                ch = "sms" if "SMS" in s.condition else "push"
                msg["additional_filters"] = _k_consent(ch, False)
            link({"temporary_id": s.id, "type": "send-email", "links": {"next": None}, "data": {"status": "draft", "message": msg}})
        elif s.kind == "send" and s.channel == "sms":
            text = json.loads(ctx.rendered(s.id, ".json") or "{}").get("text") or "SMS"
            link({"temporary_id": s.id, "type": "send-sms", "links": {"next": None}, "data": {"status": "draft", "message": {
                "body": text, "smart_sending_enabled": not transactional, "sms_quiet_hours_enabled": True, "transactional": transactional,
                "add_tracking_params": True, "additional_filters": _k_consent("sms", True), "name": s.id}}})
        elif s.kind == "send" and s.channel == "push":
            r = json.loads(ctx.rendered(s.id, ".json") or "{}")
            link({"temporary_id": s.id, "type": "send-mobile-push", "links": {"next": None}, "data": {"status": "draft", "message": {
                "title": r.get("title"), "body": r.get("body") or "Open the app", "on_open": "home", "smart_sending_enabled": not transactional,
                "additional_filters": _k_consent("push", True), "name": s.id}}})
    definition: Dict[str, Any] = {"triggers": [trigger], "profile_filter": {"condition_groups": groups}, "actions": actions,
                                  "entry_action_id": actions[0]["temporary_id"] if actions else "none"}
    api = trig.re_entry.get("api")
    if api:
        definition["reentry_criteria"] = api
    body = {"data": {"type": "flow", "attributes": {"name": f"LCM {ctx.spec['flow']}"[:200], "definition": definition}}}
    arts.append(Artifact(name="klaviyo_flow", kind="create_request", path="artifacts/klaviyo_flow.json", schema_ref="klaviyo_flow_create",
                         method="POST", url="https://a.klaviyo.com/api/flows", headers=dict(K_HEADERS), body=body, order=3,
                         creates_status="Draft: the flow and every action in draft",
                         note="the definition cannot be edited through the API after creation: change it in the flow builder"))
    return arts


# ----------------------------------------------------------------------------------------------
# HubSpot
# ----------------------------------------------------------------------------------------------

def hubspot(ctx: Ctx, mode: str, trig: TriggerConfig, steps: List[Step]) -> List[Artifact]:
    actions: List[Dict[str, Any]] = []
    pending: List[tuple] = []
    counter = [0]

    def nid() -> str:
        counter[0] += 1
        return str(counter[0])

    def add(a: Dict[str, Any], attach: str = "connection") -> None:
        # the previous action (or a branch's default path) continues to this one
        for prev, how in pending:
            prev["connection" if how == "connection" else "defaultBranch"] = {"edgeType": "STANDARD", "nextActionId": a["actionId"]}
        pending.clear()
        actions.append(a)
        pending.append((a, attach))

    def delay(wait: Any) -> None:
        secs = wait.seconds
        unit, delta = ("DAYS", secs // 86400) if secs and secs % 86400 == 0 else ("MINUTES", secs // 60)
        if secs:
            add({"type": "SINGLE_CONNECTION", "actionId": nid(), "actionTypeId": "0-1", "actionTypeVersion": 0,
                 "fields": {"delta": str(delta), "time_unit": unit}})

    def branch(name: str, filt: Dict[str, Any], default_name: str) -> None:
        # the named branch has no connection: those contacts leave the workflow there
        add({"type": "LIST_BRANCH", "actionId": nid(), "listBranches": [{"branchName": name, "filterBranch": {
            "filterBranchType": "OR", "filters": [], "filterBranches": [{"filterBranchType": "AND", "filters": [filt], "filterBranches": []}]}}],
             "defaultBranchName": default_name}, attach="default")

    for s in steps:
        skip = s.manual or s.kind == "split" and s.split is not None and s.split.kind == "consent"
        if s.wait and (not skip or s.kind == "split"):
            delay(s.wait)                          # a consent split added by hand keeps its wait
        if skip:
            continue
        if s.kind == "split" and s.split.kind == "holdout":
            branch("In holdout: exit", {"filterType": "PROPERTY", "property": ctx.holdout_property,
                                        "operation": {"propertyType": "bool", "operator": "IS_EQUAL_TO", "value": True}}, "Everyone else")
        elif s.kind == "split" and s.split.kind == "sunset":
            days = max(1, (s.wait.seconds // 86400) if s.wait else 14)
            branch("Clicked: stays subscribed", {"filterType": "PROPERTY", "property": "hs_email_last_click_date",
                                                 "operation": {"propertyType": "datetime-rolling", "operator": "IS_LESS_THAN_X_DAYS_AGO",
                                                               "numberOfDays": days}}, "No click")
        elif s.kind == "update_profile":
            add({"type": "SINGLE_CONNECTION", "actionId": nid(), "actionTypeId": "0-5", "actionTypeVersion": 0,
                 "fields": {"property_name": "lcm_sunset", "value": {"staticValue": "true"}}})
        elif s.kind == "send" and s.channel == "email":
            add({"type": "SINGLE_CONNECTION", "actionId": nid(), "actionTypeId": "0-4", "actionTypeVersion": 0,
                 "fields": {"content_id": f"<<hubspot_email:{s.id}>>"}})
    # a branch with nothing after it ends every path there: it is not a split any more, so it is dropped
    actions = [a for a in actions if not (a["type"] == "LIST_BRANCH" and "defaultBranch" not in a)]
    ree = trig.re_entry.get("api", {}).get("shouldReEnroll", False)
    if mode == "native_segment" and trig.segment and trig.segment.platform_json:
        enroll: Dict[str, Any] = {"type": "LIST_BASED", "shouldReEnroll": ree, "listFilterBranch": trig.segment.platform_json["listFilterBranch"],
                                  "unEnrollObjectsNotMeetingCriteria": False, "reEnrollmentTriggersFilterBranches": []}
    else:
        event = ctx.plan.cohort.trigger_event if mode == "warehouse_job" else ctx.qualifying
        enroll = {"type": "EVENT_BASED", "shouldReEnroll": ree, "eventFilterBranches": [{
            "filterBranchType": "UNIFIED_EVENTS", "filterBranchOperator": "AND", "eventTypeId": f"<<hubspot_event_type:{event}>>",
            "operator": "HAS_COMPLETED", "filters": [], "filterBranches": []}], "listMembershipFilterBranches": []}
    body = {"isEnabled": False, "flowType": "WORKFLOW", "name": f"LCM {ctx.spec['flow']}"[:200], "type": "CONTACT_FLOW", "objectTypeId": "0-1",
            "startActionId": actions[0]["actionId"] if actions else "1", "nextAvailableActionId": str(counter[0] + 1), "actions": actions,
            "enrollmentCriteria": enroll, "timeWindows": [], "blockedDates": [], "suppressionListIds": [], "customProperties": {}}
    return [Artifact(name="hubspot_workflow", kind="create_request", path="artifacts/hubspot_workflow.json", schema_ref="hubspot_flow_create",
                     method="POST", url="https://api.hubapi.com/automation/v4/flows", headers=dict(H_HEADERS), body=body, order=1,
                     creates_status="off (isEnabled: false)",
                     note="automation v4 is beta; the pinned path is /automation/v4/flows. Set the goal, the time-of-day delays and any "
                          "time window in the workflow editor before turning it on")]


# ----------------------------------------------------------------------------------------------
# Salesforce Marketing Cloud
# ----------------------------------------------------------------------------------------------

def sfmc(ctx: Ctx, mode: str, trig: TriggerConfig, steps: List[Step]) -> List[Artifact]:
    acts: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []

    def add(a: Dict[str, Any], out_index: int = 0) -> None:
        for o in pending:
            o["next"] = a["key"]
        pending.clear()
        acts.append(a)
        pending.append(a["outcomes"][out_index])

    key = trig.event if trig.mode in ("api_event", "warehouse_job") and trig.event else f"lcm_{ctx.slug}"
    event_key = f"<<sfmc_event_key:{key}>>"
    for i, s in enumerate(steps, 1):
        # consent splits, SMS and push, and the sunset split and update are added by hand; a split's wait stays in the journey
        by_hand = s.manual or s.kind == "update_profile" or (s.kind == "split" and s.split is not None and s.split.kind in ("consent", "sunset"))
        if by_hand and not (s.wait and s.kind == "split"):
            continue
        if s.wait:
            secs = s.wait.seconds
            unit, dur = ("days", secs // 86400) if secs % 86400 == 0 else ("hours", secs // 3600) if secs % 3600 == 0 else ("minutes", secs // 60)
            cfg: Dict[str, Any] = {"waitDuration": str(dur), "waitUnit": unit}
            if s.wait.until_local_time:
                cfg["specifiedTime"] = s.wait.until_local_time
            add({"key": f"WAIT-{i}", "name": f"Wait {s.wait.label}"[:100], "type": "Wait", "outcomes": [{"key": f"WAIT-{i}-next", "next": None}],
                 "arguments": {}, "configurationArguments": cfg, "metaData": {}})
        if s.kind == "split" and s.split.kind == "holdout":
            field = (f"Event.{event_key}.in_holdout" if s.split.source != "profile_property" or mode == "scheduled_segment" else
                     f"Contact.Attribute.{ctx.holdout_property}")
            xml = (f'<FilterDefinition><ConditionSet Operator="AND" ConditionSetName="Individual Filter Grouping"><Condition Key="{field}" '
                   f'Operator="Equal"><Value><![CDATA[true]]></Value></Condition></ConditionSet></FilterDefinition>')
            add({"key": f"DECISION-{i}", "name": "Holdout split", "type": "MULTICRITERIADECISION",
                 "outcomes": [{"key": "holdout", "next": None, "metaData": {"label": "In holdout: exit"}},
                              {"key": "remainder", "next": None, "metaData": {"label": "Everyone else"}}],
                 "arguments": {}, "configurationArguments": {"criteria": {"holdout": xml}}, "metaData": {}}, out_index=1)
        elif s.kind == "send" and s.channel == "email":
            add({"key": f"EMAIL-{i}", "name": s.id, "type": "EMAILV2", "outcomes": [{"key": f"EMAIL-{i}-next", "next": None}], "arguments": {},
                 "configurationArguments": {"triggeredSend": {"emailId": f"<<sfmc_email:{s.id}>>",
                                                              "emailSubject": (s.content.subject if s.content and s.content.subject else s.id),
                                                              "isMultipart": True}},
                 "metaData": {"isConfigured": True}})
    trig_type = "EmailAudience" if mode == "scheduled_segment" else "Event"
    body = {"key": f"lcm-{ctx.slug}"[:200], "name": f"LCM {ctx.spec['flow']}"[:200], "description": f"Built by the LMM Engine from run {ctx.plan.run_id}",
            "workflowApiVersion": 1.0,
            "triggers": [{"key": "TRIGGER", "name": key, "type": trig_type, "eventDefinitionKey": event_key, "arguments": {},
                          "configurationArguments": {}, "metaData": {}}],
            "goals": [], "activities": acts}
    sub = ctx.options.get("sfmc_subdomain") or "${SFMC_SUBDOMAIN}"
    return [Artifact(name="sfmc_journey", kind="create_request", path="artifacts/sfmc_journey.json", schema_ref="sfmc_interaction_create",
                     method="POST", url=f"https://{sub}.rest.marketingcloudapis.com/interaction/v1/interactions", headers=dict(S_HEADERS),
                     body=body, order=1, creates_status="Draft (publishing is a separate call the pack never makes)",
                     note="set the goal and exit criteria, and add the sunset update and any SMS or push activity, in Journey Builder")]


# ----------------------------------------------------------------------------------------------
# Braze duplicate, and the build sheet
# ----------------------------------------------------------------------------------------------

def braze_duplicate(ctx: Ctx, template_canvas: Optional[str]) -> Optional[Artifact]:
    canvas = template_canvas or ctx.options.get("braze_template_canvas")
    if not canvas:
        return None
    inst = ctx.options.get("braze_instance") or "${BRAZE_INSTANCE}"
    return Artifact(name="braze_canvas_duplicate", kind="duplicate_request", path="artifacts/braze_canvas_duplicate.json",
                    schema_ref="braze_canvas_duplicate", method="POST", url=f"https://rest.{inst}.braze.com/canvas/duplicate",
                    headers=dict(B_HEADERS), body={"canvas_id": str(canvas), "name": f"LCM {ctx.spec['flow']}"[:200]}, order=1,
                    creates_status="a copy of your template Canvas (its status after the copy is not documented)",
                    note="UNVERIFIED body: sent only with --allow-unverified; then edit the copy to match the build sheet")


def build_sheet(ctx: Ctx, mode: str, trig: TriggerConfig, steps: List[Step], settings: Any, manual: List[str]) -> Artifact:
    rows = []
    for n, s in enumerate(steps, 1):
        files = None
        if s.content and s.content.chosen:
            files = [f"copy/{f}" for f in s.content.variants.get(s.content.chosen, [])]
        rows.append({"n": n, "id": s.id, "kind": s.kind, "action": s.platform_action,
                     "wait": ({"seconds": s.wait.seconds, "value": s.wait.value, "unit": s.wait.unit, "label": s.wait.label} if s.wait else None),
                     "channel": s.channel, "content": files, "condition": s.condition,
                     "split": s.split.model_dump(exclude_none=True) if s.split else None, "window": s.window, "manual": s.manual, "note": s.note})
    body = {"platform": ctx.platform, "flow": ctx.spec["flow"], "slug": ctx.slug, "mode": mode, "status_on_create": settings.status_on_create,
            "trigger": {"type": trig.type, "event": trig.event, "summary": trig.summary, "filters": trig.filters, "flow_filters": trig.flow_filters,
                        "exit_criteria": trig.exit_criteria, "re_entry": trig.re_entry, "holdout": trig.holdout,
                        "segment": trig.segment.model_dump(exclude_none=True) if trig.segment else None},
            "steps": rows, "settings": {"goal": settings.goal, "re_entry": settings.re_entry, "send_windows": settings.send_windows,
                                        "frequency": settings.frequency},
            "manual_steps": manual}
    return Artifact(name="build_sheet", kind="build_sheet", path="artifacts/build_sheet.json", schema_ref="build_sheet", body=body, order=0,
                    note="follow it click by click; BUILD.md has the same steps in prose")
