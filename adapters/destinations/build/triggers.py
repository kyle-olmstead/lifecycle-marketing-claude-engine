"""The native trigger for each flow type on each platform: exact trigger type, filters, exit criteria, re-entry, and for
absence flows the segment to create, so win-back, replenishment and sunset need no warehouse job where the platform can
see the absence itself.

Mode by flow kind and platform (config/esp_capabilities.json has each platform's trigger types and sources):

  event flows (welcome, activation, cart, browse, post-purchase, dunning, trial)
      native_event on every platform; on Salesforce Marketing Cloud the native entry is an API Event (api_event)
  win-back and sunset
      native_segment  Klaviyo (segment-triggered flow, VERIFIED), HubSpot (list-based enrollment, VERIFIED),
                      Customer.io (segment trigger, Every re-match, VERIFIED)
      scheduled_segment  Braze (daily Scheduled Canvas on a segment: no 'enters segment' entry), Iterable (Schedule entry on
                      a dynamic list), Salesforce Marketing Cloud (scheduled Data Extension entry from a SQL Query Activity)
  replenishment
      native_event: the order event starts the flow, then a delay of the supply less the lead days, with an exit when the
      product is ordered again (Klaviyo Ordered Product, HubSpot custom event, Braze action-based, Customer.io event,
      Iterable Purchase); Marketing Cloud uses a scheduled Data Extension
  warehouse_job (--warehouse-job, native=False): the daily job pushes one event per entrant, as before. Keep it when the
      core event does not live in the ESP.

The segment definitions are LIKELY: the trigger types are verified on each vendor's documentation, the recipes are not.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from engine.policy.contracts import duration_hours

from adapters.destinations.build.context import Ctx
from adapters.destinations.build.model import SegmentDefinition, TriggerConfig

SEGMENT_PLATFORMS = {"klaviyo": "native_segment", "hubspot": "native_segment", "customerio": "native_segment",
                     "braze": "scheduled_segment", "iterable": "scheduled_segment", "sfmc": "scheduled_segment"}


def decide_mode(ctx: Ctx) -> str:
    if ctx.brief.flow.kind != "absence":
        return "api_event" if ctx.platform == "sfmc" else "native_event"
    if not ctx.native:
        return "warehouse_job"
    if ctx.flow_type == "replenishment" and ctx.platform != "sfmc":
        return "native_event"
    return SEGMENT_PLATFORMS[ctx.platform]


def supply_days(ctx: Ctx) -> Tuple[int, str]:
    """Days one unit lasts: the brief's, else the cohort analyst's measured or assumed value, else 30 (ASSUMED)."""
    if ctx.brief.replenish_after_days:
        return int(ctx.brief.replenish_after_days), "SUPPLIED"
    for d in ctx.plan.cohort.data_dependencies:
        if d.name == "replenish_after_days" and d.note:
            m = re.search(r"supply interval (\d+) days \((\w+)", d.note)
            if m:
                return int(m.group(1)), m.group(2)
    return 30, "ASSUMED"


def _cooldown_parts(cooldown: Optional[str]) -> Tuple[int, str]:
    hours = duration_hours(cooldown) if cooldown else 0
    if hours and hours % 168 == 0:
        return int(hours // 168), "week"
    if hours and hours % 24 == 0:
        return int(hours // 24), "day"
    return int(hours), "hour"


def re_entry(ctx: Ctx, mode: str) -> Dict[str, Any]:
    """The plan's re-entry rule in the platform's own setting (and API value where there is one)."""
    r = ctx.spec["trigger"]["re_entry"]
    m, cd = r["mode"], r.get("cooldown")
    n, unit = _cooldown_parts(cd)
    days = int(duration_hours(cd) // 24) if cd else 0
    seconds = int(duration_hours(cd) * 3600) if cd else 0
    p = ctx.platform
    out: Dict[str, Any] = {"plan": r["description"], "mode": m}
    if p == "klaviyo":
        out["setting"] = {"never": "Enter only once", "after_exit": "Re-enter whenever they re-qualify",
                          "cooldown": f"Re-enter after a minimum time period: {n} {unit}(s)"}[m]
        out["api"] = ({"duration": 1, "unit": "alltime"} if m == "never" else {"duration": 0, "unit": "alltime"} if m == "after_exit"
                      else {"duration": n, "unit": unit})
    elif p == "hubspot":
        out["setting"] = ("Re-enrollment off (shouldReEnroll: false)" if m == "never" else
                          "Re-enroll every time the trigger occurs (shouldReEnroll: true)" +
                          (f"; add the enrollment filter 'not enrolled in this workflow in the last {days} days' (LIKELY)" if m == "cooldown" else ""))
        out["api"] = {"shouldReEnroll": m != "never"}
    elif p == "braze":
        out["setting"] = ("Re-eligibility off" if m == "never" else
                          "Allow users to re-enter this Canvas after the maximum Canvas duration" if m == "after_exit" else
                          f"Allow users to re-enter this Canvas after {seconds} seconds ({days} days)")
        out["seconds"] = None if m != "cooldown" else seconds
    elif p == "customerio":
        if mode in ("native_segment",):
            out["setting"] = "Every re-match" + (" (with the entry filter that keeps people out after their first journey)" if m == "never" else "")
        else:
            out["setting"] = {"never": "Frequency: One time", "after_exit": "Frequency: On every event (people already in the campaign are not entered again: LIKELY)",
                              "cooldown": f"Frequency: Once within a time period of {days} days"}[m]
    elif p == "iterable":
        out["setting"] = {"never": "Maximum entries: 1", "after_exit": "Maximum entries: Unlimited; simultaneous entries off (people finish before re-entering)",
                          "cooldown": f"Maximum entries: Unlimited, plus a Yes/No Split first: entered this journey in the last {days} days goes to exit (LIKELY)"}[m]
    elif p == "sfmc":
        out["setting"] = {"never": "Contact entry: No re-entry", "after_exit": "Contact entry: Re-entry only after exiting",
                          "cooldown": f"Contact entry: Re-entry anytime, plus a Decision Split on the last entry date ({days} days, LIKELY)"}[m]
    return out


def _where(ctx: Ctx) -> List[str]:
    return [f"{k} = {v}" for k, v in ctx.brief.qualifying_event.where.items()]


def _sql_ident(s: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", s.lower())


def segment(ctx: Ctx, mode: str) -> Optional[SegmentDefinition]:
    """The segment a native absence trigger reads, in the platform's words (and JSON where the shape is verified)."""
    ft, p = ctx.flow_type, ctx.platform
    if mode not in ("native_segment", "scheduled_segment") or ft not in ("winback", "sunset", "replenishment"):
        return None
    n = ctx.brief.churn_threshold_days
    q, c = ctx.qualifying, ctx.core
    qn, cn = ctx.event_name(q), ctx.event_name(c)
    name = f"LCM {ctx.brief.flow.label}: {ctx.slug}"
    prereq: List[str] = []
    crossing = mode == "scheduled_segment"
    if ft == "winback":
        if p == "klaviyo":
            conds = [f"What someone has done: {qn} at least once over all time"]
            if q != c:
                conds.append(f"What someone has done: {qn} zero times in the last {n} days (qualified more than {n} days ago)")
            conds.append(f"What someone has done: {cn} zero times in the last {n} days")
            groups = [{"conditions": [_k_metric(qn, "greater-than-or-equal", 1, {"type": "date", "operator": "alltime"})]}]
            if q != c:
                groups.append({"conditions": [_k_metric(qn, "equals", 0, {"type": "date", "operator": "in-the-last", "unit": "day", "quantity": n})]})
            groups.append({"conditions": [_k_metric(cn, "equals", 0, {"type": "date", "operator": "in-the-last", "unit": "day", "quantity": n})]})
            body = {"data": {"type": "segment", "attributes": {"name": name, "definition": {"condition_groups": groups}}}}
            return SegmentDefinition(name=name, kind="Klaviyo segment", conditions=conds, refresh="Klaviyo updates segment membership itself",
                                     platform_json=body, data_prerequisites=[f"{qn} and {cn} are Klaviyo metrics (Shopify's own, or sent by the engine)"])
        if p == "hubspot":
            props = [f"lcm_last_{_sql_ident(c)}_at"] + ([f"lcm_last_{_sql_ident(q)}_at"] if q != c else [])
            conds = [f"Contact property {props[0]} is more than {n} days ago"]
            if q != c:
                conds.append(f"Contact property {props[1]} is known and more than {n} days ago (qualified before the window)")
            conds.append(f"or, in the segment builder: behavioral event {cn} has not been completed in the last {n} days and {qn} has been completed (LIKELY)")
            filters = [{"filterType": "PROPERTY", "property": pr, "operation": {"propertyType": "datetime-rolling", "operator": "IS_MORE_THAN_X_DAYS_AGO",
                                                                                "numberOfDays": n}} for pr in props]
            branch = {"filterBranchType": "OR", "filters": [], "filterBranches": [{"filterBranchType": "AND", "filters": filters, "filterBranches": []}]}
            prereq = [f"contact datetime properties {', '.join(props)}, set whenever the event happens (write them with the event, or from a "
                      f"workflow enrolled on the custom event)"]
            return SegmentDefinition(name=name, kind="HubSpot active list (segment) used as list-based enrollment", conditions=conds,
                                     refresh="HubSpot re-evaluates list-based enrollment as properties change", data_prerequisites=prereq,
                                     platform_json={"listFilterBranch": branch})
        if p == "customerio":
            conds = [f"has performed {qn} at least once"] + ([f"has not performed {qn} within the past {n} days"] if q != c else []) + \
                    [f"has not performed {cn} within the past {n} days"]
            return SegmentDefinition(name=name, kind="Customer.io data-driven segment", conditions=conds,
                                     refresh="people enter the campaign when they match (Every re-match)",
                                     data_prerequisites=[f"{qn} and {cn} are tracked as Customer.io events"])
        if p == "braze":
            conds = [f"Last did custom event {cn} more than {n} days ago and less than {n + 1} days ago (only on the day they cross)",
                     f"or: has never done {cn}, and first did {qn} between {n} and {n + 1} days ago"]
            return SegmentDefinition(name=name, kind="Braze segment (entry audience of a daily Scheduled Canvas)", conditions=conds,
                                     refresh="the Scheduled Canvas evaluates the segment once a day", data_prerequisites=[f"{qn} and {cn} are Braze custom events"])
        if p == "iterable":
            conds = [f"{qn} event count at least 1", f"most recent {cn} event between {n} and {n + 1} days ago, or no {cn} event and "
                     f"{qn} between {n} and {n + 1} days ago (only on the day they cross)"]
            return SegmentDefinition(name=name, kind="Iterable dynamic list (Schedule entry, daily)", conditions=conds,
                                     refresh="the journey's Schedule entry reads the list once a day", data_prerequisites=[f"{qn} and {cn} are Iterable custom events"])
        if p == "sfmc":
            conds = [f"a data extension LCM_{ctx.slug} refreshed daily by an Automation Studio SQL Query Activity: contacts whose last {c} is "
                     f"{n} days before today (or who qualified {n} days ago and never did it)"]
            return SegmentDefinition(name=name, kind="Data Extension entry source (scheduled daily)", conditions=conds,
                                     refresh="Automation Studio runs the query daily, then the journey's scheduled entry reads the data extension",
                                     data_prerequisites=[f"{q} and {c} history in Marketing Cloud (a synchronized data extension)", "a column in_holdout from the holdout sync"])
    if ft == "sunset":
        rec, clk = "Received Email", "Clicked Email"
        if p == "klaviyo":
            conds = [f"What someone has done: {rec} at least 5 times in the last {n} days", f"What someone has done: {clk} zero times in the last {n} days"]
            groups = [{"conditions": [_k_metric(rec, "greater-than-or-equal", 5, {"type": "date", "operator": "in-the-last", "unit": "day", "quantity": n})]},
                      {"conditions": [_k_metric(clk, "equals", 0, {"type": "date", "operator": "in-the-last", "unit": "day", "quantity": n})]}]
            body = {"data": {"type": "segment", "attributes": {"name": name, "definition": {"condition_groups": groups}}}}
            return SegmentDefinition(name=name, kind="Klaviyo segment", conditions=conds, refresh="Klaviyo updates segment membership itself",
                                     platform_json=body, data_prerequisites=["Klaviyo's built-in email metrics (Received Email, Clicked Email: names LIKELY)"])
        if p == "hubspot":
            conds = ["Contact property lcm_engagement_tier is equal to unengaged", "Contact property lcm_sunset is not true"]
            filt = {"filterType": "PROPERTY", "property": "lcm_engagement_tier",
                    "operation": {"operationType": "MULTISTRING", "operator": "IS_EQUAL_TO", "values": ["unengaged"]}}
            branch = {"filterBranchType": "OR", "filters": [], "filterBranches": [{"filterBranchType": "AND", "filters": [filt], "filterBranches": []}]}
            return SegmentDefinition(name=name, kind="HubSpot active list (segment) used as list-based enrollment", conditions=conds,
                                     refresh="re-evaluated as lcm_engagement_tier changes",
                                     data_prerequisites=["lcm_engagement_tier written by the engagement sync (python3 -m engine.agents.engagement --platform hubspot, "
                                                         "then lcm send --profile-updates)"], platform_json={"listFilterBranch": branch})
        words = {"customerio": ("Customer.io data-driven segment", [f"has been sent at least 5 emails in the past {n} days", f"has not clicked an email in the past {n} days", "lcm_sunset is not true"]),
                 "braze": ("Braze segment (entry audience of a daily Scheduled Canvas)", [f"received email at least 5 times in the last {n} days", f"last clicked email more than {n} days ago, or never", "lcm_sunset is not true"]),
                 "iterable": ("Iterable dynamic list (Schedule entry, daily)", [f"emailSend events at least 5 in the last {n} days", f"no emailClick event in the last {n} days", "lcm_sunset is not true"]),
                 "sfmc": ("Data Extension entry source (scheduled daily)", [f"a data extension LCM_{ctx.slug} refreshed daily by a SQL Query Activity over the _Sent and _Click data views: at least 5 sends and no click in {n} days"])}
        kind, conds = words[p]
        return SegmentDefinition(name=name, kind=kind, conditions=conds, refresh="daily", data_prerequisites=[
            "or segment on lcm_engagement_tier = unengaged, written by the engagement sync (python3 -m engine.agents.engagement --platform <platform>)"])
    if ft == "replenishment" and p == "sfmc":
        days, _ = supply_days(ctx)
        lead = ctx.brief.replenish_lead_days
        return SegmentDefinition(name=name, kind="Data Extension entry source (scheduled daily)",
                                 conditions=[f"contacts whose last purchase of a product in scope was {days - lead} days ago, with no purchase of it since"],
                                 refresh="Automation Studio runs the query daily", data_prerequisites=["order line items in Marketing Cloud (a synchronized data extension)"])
    return None


def _k_metric(metric: str, op: str, value: int, timeframe: Dict[str, Any]) -> Dict[str, Any]:
    """A Klaviyo profile-metric condition (SegmentsProfileMetricCondition in the OpenAPI spec, revision 2026-10-15)."""
    return {"type": "profile-metric", "metric_id": f"<<klaviyo_metric:{metric}>>", "measurement": "count",
            "measurement_filter": {"type": "numeric", "operator": op, "value": value}, "timeframe_filter": timeframe}


def trigger(ctx: Ctx, mode: str) -> TriggerConfig:
    p, ft = ctx.platform, ctx.flow_type
    q, c = ctx.qualifying, ctx.core
    qn, cn = ctx.event_name(q), ctx.event_name(c)
    excl = [ctx.event_name(x) for x in ctx.brief.flow.exclude_events]
    where = _where(ctx)
    seg = segment(ctx, mode)
    ree = re_entry(ctx, mode)
    if mode == "warehouse_job":
        qn = ctx.event_name(ctx.plan.cohort.trigger_event)
    hold_event = {"klaviyo": "Trigger split on the trigger event's in_holdout property (true goes to exit)",
                  "braze": "Audience Paths on the in_holdout entry property, read as the Context variable {{context.${in_holdout}}} (LIKELY)",
                  "customerio": "True/False branch on the trigger event's attribute in_holdout (event.in_holdout, LIKELY)",
                  "iterable": "Yes/No Split on this journey's triggering event, field in_holdout",
                  "sfmc": "Decision Split on the entry event's in_holdout field",
                  "hubspot": f"If/then branch on the contact property {ctx.holdout_property} (HubSpot branches are verified on record "
                             f"properties, not on event data: write it with the holdout sync)"}[p]
    hold_profile = (f"split on the person property {ctx.holdout_property} (true goes to exit), written once and then weekly by the holdout sync "
                    f"(build/holdout_sync_<dialect>.sql, then lcm send --profile-updates): a segment trigger carries no event to read in_holdout from")
    holdout = (hold_profile if mode in ("native_segment", "scheduled_segment") and p != "sfmc" else
               "Decision Split on the data extension's in_holdout column" if mode == "scheduled_segment" else hold_event)
    if ctx.brief.holdout_pct <= 0:
        holdout = "none: this flow has no holdout (" + (ctx.spec.get("holdout", {}).get("reason_if_none") or "see the plan") + ")"
    flow_filters = [f"{cn} zero times since starting this flow" if p == "klaviyo" else f"has not done {cn} since entering"]
    flow_filters += [(f"{x} zero times since starting this flow" if p == "klaviyo" else f"has not done {x} since entering") for x in excl]
    exit_words = {"klaviyo": f"flow filter: {cn} zero times since starting this flow (checked before every action)",
                  "hubspot": f"goal: the contact completes {cn} (goal contacts leave the workflow)",
                  "braze": f"exit criteria: performs custom event {cn}" + "".join(f", or {x}" for x in excl),
                  "customerio": f"conversion goal: performs {cn}; exit when the person converts",
                  "iterable": f"journey exit rule: performs {cn}" + "".join(f", or {x}" for x in excl),
                  "sfmc": f"goal: {c} recorded for the contact, with exit on goal (evaluated nightly and when a wait ends)"}
    exits = [exit_words[p]]
    filters: List[str] = []
    native_alt = warehouse_alt = None
    conf = "VERIFIED"
    days, label = supply_days(ctx) if ft == "replenishment" else (None, None)
    lead = ctx.brief.replenish_lead_days

    if mode == "native_event":
        if p == "klaviyo":
            ttype = "metric"
            if ft == "replenishment" and ctx.options.get("shopify"):
                qn = ctx.caps["shopify_metric_names"]["value"]["product_ordered"]
            summary = f"Metric-triggered flow on {qn}"
            filters = [f"trigger filter: {w}" for w in where]
            if ft == "post_purchase":
                filters.append(f"profile filter: {qn} equals 1 over all time (the first order)")
            if ft in ("cart_abandon", "browse_abandon"):
                filters.append("Klaviyo tracks events on known profiles only, so anonymous sessions cannot enter")
        elif p == "hubspot":
            ttype = "EVENT_BASED: Custom event occurs"
            summary = f"Contact-based workflow enrolled when the custom event {qn} occurs (enrolls existing contacts only)"
            filters = [f"event property filter (checked at event time only): {w}" for w in where]
            if ft == "welcome":
                filters.append("or enroll on the signup form submission or list membership instead of the event")
        elif p == "braze":
            ttype = "Action-Based"
            summary = f"Canvas with action-based entry: performs custom event {qn}"
            filters = [f"property filter on the trigger: {w}" for w in where]
            if ft in ("cart_abandon", "browse_abandon", "post_purchase"):
                filters.append("Braze's recommended eCommerce events work too (ecommerce.checkout_started, ecommerce.product_viewed, "
                               "ecommerce.order_placed) with the Start checkout, View product and Place Order triggers")
        elif p == "customerio":
            ttype = "Event"
            summary = f"Event-triggered campaign on {qn}"
            filters = [f"event data filter: {w}" for w in where]
        else:  # iterable
            ttype = "Event occurs"
            iter_entry = {"cart_abandon": "Shopping Cart Update (Shopify) or the custom event", "post_purchase": "Purchase, with the entry rule update = false",
                          "browse_abandon": "Custom Event (Shopify sends productView)"}.get(ft, "Custom Event")
            summary = f"Journey with an Event occurs entry: {iter_entry} {qn}"
            filters = [f"entry rule: {w}" for w in where]
        if ft == "replenishment":
            summary += f", then a delay of {days - lead} days ({days}-day supply, {label}, less {lead} lead days) before the first touch"
            flow_filters = [f"{qn} for that product zero times since starting this flow" if p == "klaviyo" else f"has not ordered that product again since entering"]
            exits = [exit_words[p].replace(cn, f"{cn} (that product)")]
            conf = "LIKELY"
            warehouse_alt = ("Or keep the warehouse job (--warehouse-job): it measures each product's supply interval from repeat orders "
                             "and pushes replenishment_due once per purchase cycle")
    elif mode == "api_event":
        ttype = "API Event entry source"
        key = f"lcm_{q}"
        summary = (f"Journey Builder journey whose entry is the API Event {key}: the system that records {q} calls POST /interaction/v1/events "
                   f"with EventDefinitionKey {key} and the contact's data (create the event definition in Journey Builder first)")
        filters = [f"entry filter on event data: {w}" for w in where]
        qn = key
    elif mode in ("native_segment", "scheduled_segment"):
        conf = "LIKELY"
        if p == "klaviyo":
            ttype = "segment"
            summary = f"Segment-triggered flow on '{seg.name}': people enter when they newly qualify"
        elif p == "hubspot":
            ttype = "LIST_BASED"
            summary = f"Contact-based workflow with list-based enrollment on the criteria of '{seg.name}', re-enrolling when contacts qualify again"
        elif p == "customerio":
            ttype = "Attribute or Segment"
            summary = f"Segment-triggered campaign on '{seg.name}' (Every re-match)"
        elif p == "braze":
            ttype = "Scheduled (daily)"
            summary = (f"Canvas scheduled daily at {ctx.brief.send_hour_local:02d}:00 in users' local time; entry audience: the segment '{seg.name}', "
                       "which holds people only on the day they cross the line")
        elif p == "iterable":
            ttype = "Schedule (daily)"
            summary = f"Journey with a daily Schedule entry on the dynamic list '{seg.name}', which holds people only on the day they cross the line"
        else:
            ttype = "Data Extension entry (scheduled)"
            summary = f"Journey Builder journey with a scheduled Data Extension entry, '{seg.name}', refreshed daily by a SQL Query Activity"
        qn = None
        if ft == "sunset":
            flow_filters = [f"{ctx.event_name('message_clicked')} zero times since starting this flow" if p == "klaviyo" else "has not clicked since entering"]
        warehouse_alt = (f"Or keep the warehouse job (--warehouse-job): run cohort/job/queue_{ctx.brief.warehouse_dialect}.sql daily and push "
                         f"{ctx.plan.cohort.trigger_event} with lcm send. Use it when {c} does not reach {ctx.label}, or when the lapse must be "
                         f"measured from the warehouse's own event history")
    else:  # warehouse_job
        ttype = {"klaviyo": "metric", "hubspot": "EVENT_BASED: Custom event occurs", "braze": "Action-Based", "customerio": "Event",
                 "iterable": "Event occurs", "sfmc": "API Event entry source"}[p]
        summary = (f"Triggered by {qn}, pushed once per entrant by the daily warehouse job (cohort/job/: queue, pending, then lcm send "
                   f"--rows <pending results> --approve, then mark)")
        if p == "sfmc":
            summary = f"Journey Builder API Event entry fed by the daily warehouse job (one POST /interaction/v1/events per pending row)"
        mode_native = decide_mode(Ctx(plan=ctx.plan, platform=p, account=ctx.account, native=True))
        native_alt = f"Native alternative with no job: build without --warehouse-job ({mode_native.replace('_', ' ')})"
    return TriggerConfig(mode=mode, type=ttype, event=qn, summary=summary, filters=filters, flow_filters=flow_filters, exit_criteria=exits,
                         re_entry=ree, segment=seg, holdout=holdout, warehouse_alternative=warehouse_alt, native_alternative=native_alt,
                         confidence=conf)
