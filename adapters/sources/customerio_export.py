"""Customer.io's people export and its Deliveries (message activity) export into users, consent_events and message_events.

What it reads
  people CSV      People, Export (any of displayed, chosen or all attributes). Customer.io documents that an export
                  includes the audience's identifiers, shown in its examples as id, cio_id and email, and that a
                  displayed-attributes export adds id, city, state and created_at
                  (https://docs.customer.io/messaging/profiles/manage/exporting-users.md). Recognised by a cio_id
                  column, by id, email and unsubscribed columns (without a CDP warehouse's received_at, uuid_ts or
                  context_ columns), or by id and email beside a deliveries export. Subfolders are searched.
  deliveries CSV  Deliveries & Drafts (message activity), Sent tab, Export. Its columns are documented at
                  https://docs.customer.io/messaging/channels/message-activity/ and
                  https://docs.customer.io/journeys/channels/deliveries-drafts-data/: id, campaign_id, campaign_name,
                  newsletter_id, newsletter_name, template_id (the message), template_name, channel, subject,
                  customer_id, recipient (the To field), email, failure_message, and for each state a plain column and
                  an _RFC3339 column (created, sent, delivered, opened, clicked, converted, bounced, spammed,
                  unsubscribed, suppressed, failed, drafted, topic_unsubscribed). Recognised by customer_id and
                  template_id with _RFC3339 columns.

What it writes
  users           user_id = the person's id (your product's id); a profile identified only by email falls back to
                  cio_id (ASSUMED, counted). email, created_at (the created_at attribute, a Unix timestamp in
                  Customer.io), plan, country (ISO code or common name), timezone (IANA), phone (E.164 only),
                  lifecycle_stage and account_id when those attributes exist.
  consent_events  one revoked email row (source esp_unsubscribe) per person whose unsubscribed attribute is true.
  message_events  one row per state a delivery reached: delivered, opened, clicked, bounced, spammed (as
                  complained), unsubscribed and topic_unsubscribed (as unsubscribed). Ids are cio:<delivery>:<state>.
                  message_id is template_id (the message, shared by its recipients), campaign is the campaign,
                  newsletter or transactional message name, recipient_domain the recipient's domain, and a bounce
                  carries failure_message as diagnostic and its enhanced status code (5.1.1) as smtp_status.

What it assumes, and why
  * The _RFC3339 columns are ISO 8601 UTC ("2023-05-09T18:20:45Z", per the docs) and are preferred. The plain
    state columns are not described by the docs; they are read as Unix timestamps or ISO strings when the
    _RFC3339 twin is empty (ASSUMED).
  * Unsubscribe time: the people export says that a person is unsubscribed, not when. The opt-out row is dated at
    the person's latest unsubscribed time in the deliveries export, else at the newest time anywhere in the
    exports; the later date is the conservative one (a later opt-out cannot be lifted by an earlier opt-in).
  * unsubscribed = false is not an opt-in (Customer.io subscribes people by default), so no consent is granted and
    email_consent_at stays empty.
  * States without a canonical event name (sent, converted, suppressed, failed, drafted) are counted in the
    report, not written. Channels other than email, sms (twilio) and push (webhook, slack, in_app) are skipped
    and counted. Customer.io's export does not flag machine (privacy proxy) opens, so machine_open is empty.
  * created_at missing on a profile: the person's first delivery time is used as first seen (ASSUMED, counted);
    with neither, the person is left out (created_at is required).

    from adapters.sources import customerio_export
    report = customerio_export.map(Path("exports/customerio"), Path("data"))
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from adapters.sources._mapping import (Sheet, TimeParser, country_iso2, csv_files, csv_header, email, norm, phone_e164,
                                       rel, truthy, write_table)
from adapters.sources.report import MappingReport

NAME = "customerio"
DESCRIPTION = "Customer.io people export and Deliveries (message activity) export into users, consent_events and message_events"

STATE_TO_EVENT = {"delivered": "delivered", "opened": "opened", "clicked": "clicked", "bounced": "bounced",
                  "spammed": "complained", "unsubscribed": "unsubscribed", "topic_unsubscribed": "unsubscribed"}
UNMAPPED_STATES = ("sent", "converted", "suppressed", "failed", "drafted")
CHANNELS = {"email": "email", "push": "push", "sms": "sms", "twilio": "sms"}
SMTP = re.compile(r"\b([245]\.\d{1,3}\.\d{1,3})\b")


WAREHOUSE_HINTS = {"receivedat", "uuidts", "uuid", "loadedat", "anonymousid"}


def _kind(header: List[str], beside_deliveries: bool = False) -> Optional[str]:
    """'deliveries', 'people' or None. A people export is recognised by cio_id, or by id, email and unsubscribed
    without a CDP warehouse's columns, or by id and email when it sits beside a deliveries export."""
    keys = {norm(h) for h in header}
    if {"customerid", "templateid"} <= keys and any(h.lower().endswith("_rfc3339") for h in header):
        return "deliveries"
    if "cioid" in keys:
        return "people"
    if {"id", "email"} <= keys and not keys & WAREHOUSE_HINTS and not any(k.startswith("context") for k in keys):
        if "unsubscribed" in keys or beside_deliveries:
            return "people"
    return None


def _sheets(folder: Path) -> Tuple[List[Path], List[Path]]:
    """(people files, deliveries files) under `folder`, searched recursively."""
    files = csv_files(folder)
    headers = {p: csv_header(p) for p in files}
    deliveries = [p for p in files if _kind(headers[p]) == "deliveries"]
    dirs = {p.parent for p in deliveries}
    people = [p for p in files if p not in deliveries and _kind(headers[p], beside_deliveries=p.parent in dirs) == "people"]
    return people, deliveries


def detect(folder: Path) -> bool:
    """True when the folder (or a subfolder) holds a Customer.io people export or Deliveries export."""
    people, deliveries = _sheets(Path(folder))
    return bool(people or deliveries)


def _domain(addr: Optional[str]) -> Optional[str]:
    a = email(addr)
    return a.split("@", 1)[1] if a else None


def map(folder: Path, out_dir: Path) -> MappingReport:  # noqa: A001
    """Read Customer.io exports in `folder`; write users, consent_events and message_events CSVs to `out_dir`."""
    folder, out_dir = Path(folder), Path(out_dir)
    report = MappingReport(source=NAME)
    tp = TimeParser()
    people_files, delivery_files = _sheets(folder)
    people_sheets = [Sheet.read(p) for p in people_files]
    delivery_sheets = [Sheet.read(p) for p in delivery_files]
    if not people_sheets and not delivery_sheets:
        report.warnings.append(f"no Customer.io people or deliveries export in {folder}")
        return report

    # deliveries first: they date unsubscribes and give first-seen times
    messages: List[Dict[str, Any]] = []
    newest: Optional[datetime] = None
    first_seen: Dict[str, datetime] = {}
    last_unsub: Dict[str, datetime] = {}
    skipped_channels: Dict[str, int] = {}
    unmapped_states: Dict[str, int] = {}
    no_customer = plain_ts = 0
    for sheet in delivery_sheets:
        states = {s.lower() for s in list(STATE_TO_EVENT) + list(UNMAPPED_STATES) + ["created"]}
        for h in sheet.headers:   # each state has a plain column and an _RFC3339 twin; reading either uses both
            if h.lower().endswith("_rfc3339") or h.lower() in states:
                sheet.use(h)
        for row in sheet.rows:
            did = sheet.get(row, "id")
            uid = sheet.get(row, "customer_id")
            stamps: Dict[str, datetime] = {}
            for state in list(STATE_TO_EVENT) + list(UNMAPPED_STATES) + ["created"]:
                at = tp.parse(sheet.get(row, f"{state}_RFC3339"))
                if at is None:
                    at = tp.parse(sheet.get(row, state))
                    plain_ts += 1 if at else 0
                if at:
                    stamps[state] = at
                    newest = at if newest is None or at > newest else newest
            if not did:
                continue
            if not uid:
                no_customer += 1
                continue
            seen = min(stamps.values()) if stamps else None
            if seen and (uid not in first_seen or seen < first_seen[uid]):
                first_seen[uid] = seen
            for state in UNMAPPED_STATES:
                if state in stamps:
                    unmapped_states[state] = unmapped_states.get(state, 0) + 1
            raw_channel = (sheet.get(row, "channel") or "email").lower()
            channel = CHANNELS.get(raw_channel)
            if channel is None:
                skipped_channels[raw_channel] = skipped_channels.get(raw_channel, 0) + 1
                continue
            failure = sheet.get(row, "failure_message")
            base = {"user_id": uid, "channel": channel, "message_id": sheet.get(row, "template_id") or did,
                    "campaign": sheet.get(row, "campaign_name", "newsletter_name", "transactional_message_name"),
                    "recipient_domain": (_domain(sheet.get(row, "recipient")) or _domain(sheet.get(row, "email"))) if channel == "email" else None}
            for state, name in STATE_TO_EVENT.items():
                if state not in stamps:
                    continue
                ev = dict(base, event_id=f"cio:{did}:{state}", event_name=name, event_at=stamps[state])
                if name == "bounced" and failure:
                    m = SMTP.search(failure)
                    ev.update(diagnostic=failure, smtp_status=m.group(1) if m else None)
                messages.append(ev)
                if name == "unsubscribed" and (uid not in last_unsub or stamps[state] > last_unsub[uid]):
                    last_unsub[uid] = stamps[state]
        report.unmapped_columns[rel(sheet.path, folder)] = sheet.unused()

    # people
    users: Dict[str, Dict[str, Any]] = {}
    unsubscribed: List[str] = []
    by_cio_id = no_email = no_created = from_first_seen = 0
    unknown_countries: Dict[str, int] = {}
    for sheet in people_sheets:
        for row in sheet.rows:
            created = tp.parse(sheet.get(row, "created_at", "created"))
            if created:
                newest = created if newest is None or created > newest else newest
            uid = sheet.get(row, "id", "customer_id")
            if not uid:
                uid = sheet.get(row, "cio_id")
                by_cio_id += 1 if uid else 0
            else:
                sheet.column("cio_id")
            if not uid:
                continue
            addr = email(sheet.get(row, "email"))
            if not addr:
                no_email += 1
                continue
            if created is None and uid in first_seen:
                created = first_seen[uid]
                from_first_seen += 1
            if created is None:
                no_created += 1
                continue
            raw_country = sheet.get(row, "country", "country_code")
            country = country_iso2(raw_country)
            if raw_country and not country:
                unknown_countries[raw_country] = unknown_countries.get(raw_country, 0) + 1
            tz = sheet.get(row, "timezone", "time_zone")
            if uid in users:
                report.warnings.append(f"person {uid} appears twice in the people export; the first row was kept")
                continue
            users[uid] = {"user_id": uid, "email": addr, "created_at": created, "country": country,
                          "plan": sheet.get(row, "plan", "plan_name"), "lifecycle_stage": sheet.get(row, "lifecycle_stage"),
                          "timezone": tz if tz and "/" in tz else None, "phone": phone_e164(sheet.get(row, "phone", "phone_number")),
                          "account_id": sheet.get(row, "account_id", "company_id")}
            if truthy(sheet.get(row, "unsubscribed")) is True:
                unsubscribed.append(uid)
        report.unmapped_columns[rel(sheet.path, folder)] = sheet.unused()

    consent = [{"user_id": u, "channel": "email", "status": "revoked", "purpose": "marketing", "source": "esp_unsubscribe",
                "occurred_at": last_unsub.get(u) or newest} for u in unsubscribed]
    dated_by_export = sum(1 for u in unsubscribed if u not in last_unsub)

    if people_sheets:
        report.assumptions.append("No consent was inferred: unsubscribed = false is Customer.io's default, not an opt-in, so "
                                  "email_consent_at is empty.")
    if by_cio_id:
        report.assumptions.append(f"ASSUMED: {by_cio_id} profile(s) have no id (identified by email only); cio_id is their user_id.")
    if from_first_seen:
        report.assumptions.append(f"ASSUMED: {from_first_seen} profile(s) have no created_at; their first delivery time is used.")
    if unsubscribed:
        report.assumptions.append(f"ASSUMED: {len(unsubscribed)} unsubscribed person(s): the people export carries no unsubscribe "
                                  "time, so each is dated at their latest unsubscribed delivery event"
                                  + (f" ({dated_by_export} without one at the newest time in the exports)" if dated_by_export else "")
                                  + "; the later date is the conservative one.")
    if plain_ts:
        report.assumptions.append(f"ASSUMED: {plain_ts} delivery timestamp(s) had no _RFC3339 value and were read from the plain "
                                  "column (Unix seconds or ISO).")
    if unmapped_states:
        listed = ", ".join(f"{k} ({v})" for k, v in sorted(unmapped_states.items()))
        report.assumptions.append(f"delivery states with no canonical event name were not written: {listed}.")
    if skipped_channels:
        listed = ", ".join(f"{k} ({v})" for k, v in sorted(skipped_channels.items()))
        report.warnings.append(f"deliveries on channels the canonical message_events does not carry were skipped: {listed}.")
    if no_customer:
        report.warnings.append(f"{no_customer} delivery row(s) without a customer_id (deleted people) were skipped.")
    if no_email:
        report.warnings.append(f"{no_email} profile(s) without a valid email were left out of users.")
    if no_created:
        report.warnings.append(f"{no_created} profile(s) without created_at or any delivery were left out of users.")
    if unknown_countries:
        listed = ", ".join(f"{k} ({v})" for k, v in sorted(unknown_countries.items()))
        report.warnings.append(f"country left empty for values that are not an ISO code or a known country name: {listed}")
    report.assumptions += tp.notes("Customer.io")
    report.warnings += tp.warnings("Customer.io")

    if users:
        report.tables["users"] = write_table(out_dir, "users", list(users.values()), ["user_id"])
    if consent:
        report.tables["consent_events"] = write_table(out_dir, "consent_events", consent, ["user_id", "occurred_at"])
    if messages:
        report.tables["message_events"] = write_table(out_dir, "message_events", messages, ["event_at", "event_id"])
    return report
