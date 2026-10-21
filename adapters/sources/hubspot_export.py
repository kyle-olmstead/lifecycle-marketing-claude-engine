"""HubSpot's contacts export (CSV) into users and the email opt-outs in consent_events.

What it reads
  The file HubSpot emails after Contacts, Export (CSV; unzip it first when HubSpot zipped it). Columns are the
  property labels: HubSpot names "Record ID" as the first column of an all-properties export
  (https://knowledge.hubspot.com/import-and-export/export-records), and the default contact properties used here
  are listed with those labels at https://knowledge.hubspot.com/properties/hubspots-default-contact-properties:
  Record ID, Email, Create Date, Lifecycle Stage, Country/Region, Marketing contact status, Unsubscribed from all
  email, "Opted out of email: <subscription type>", Time zone, Phone number. Internal names (hs_object_id,
  createdate, lifecyclestage, country, hs_marketable_status, hs_email_optout) are accepted as aliases, as is any
  column name differing only in case or punctuation. Any CSV with Record ID and Email columns in the folder, or in
  a subfolder of it, is read.

What it writes
  users           user_id, email, created_at (Create Date), lifecycle_stage, country, timezone, phone, account_id.
                  user_id is your product's user id when the export carries one ("User ID", "user_id",
                  "External ID", "App User ID", a custom property you add to the export), so HubSpot rows join your
                  event data; otherwise it is the HubSpot Record ID, and the report says so.
  consent_events  one revoked email row per contact who is "Unsubscribed from all email" or opted out of any
                  subscription type, source esp_unsubscribe, purpose marketing.

What it assumes, and why
  * Times: HubSpot exports datetimes in the account's time zone (HubSpot community answer,
    https://community.hubspot.com/t/select-time-zone-for-exported-reports/148282) without saying which, so they
    are read as UTC unless map(..., timezone="America/Chicago") names the portal's zone (ASSUMED, recorded).
  * Opt-out times: the export says that a contact opted out, not when. The opt-out row is dated at the contact's
    Last Modified Date (an opt-out changes the record, so it happened at or before then), else at the newest time
    found anywhere in the export. The later date is the conservative one: consent_events lets a newer opt-in lift
    an older opt-out, and a later opt-out cannot be lifted by an opt-in that came before it (ASSUMED).
  * Opting out of one subscription type ("Opted out of email: Newsletter") is written as a marketing revocation
    for all email, which suppresses more than HubSpot does; the canonical consent_events has no subscription-type
    column (ASSUMED, conservative).
  * Consent is never inferred. "Marketing contact status" is HubSpot's billing flag for who may receive marketing
    email, not a consent record, and "not opted out" is not an opt-in, so email_consent_at stays empty. The users
    table has no column for marketing contact status; the report counts non-marketing contacts instead.
  * Country/Region is free text in HubSpot. ISO codes and common English country names are converted; anything
    else is left empty and listed in the report, never guessed.
  * Lifecycle Stage labels are written in snake_case (Marketing Qualified Lead -> marketing_qualified_lead); the
    internal values (marketingqualifiedlead) map to the same names. Custom stages keep their exported value.

Not verified: the exact datetime format of the export (read tolerantly: ISO, "YYYY-MM-DD HH:MM", Unix epoch,
month-first slash dates) and whether opt-out columns export as true/false or Yes/No (both are read).

    from adapters.sources import hubspot_export
    report = hubspot_export.map(Path("exports/hubspot"), Path("data"), timezone="America/New_York")
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from adapters.sources._mapping import (Sheet, TimeParser, country_iso2, csv_files, csv_header, email, norm, phone_e164,
                                       rel, snake, truthy, write_table)
from adapters.sources.report import MappingReport

NAME = "hubspot"
DESCRIPTION = "HubSpot contacts export (CSV) into users and email opt-outs in consent_events"

RECORD_ID = ("record id", "hs_object_id", "contact id", "vid")
EMAIL = ("email", "email address")
CREATED = ("create date", "createdate", "created date", "created at")
MODIFIED = ("last modified date", "lastmodifieddate", "hs_lastmodifieddate", "last modified")
USER_ID = ("user id", "user_id", "userid", "external id", "external user id", "app user id", "app_user_id")
STAGE = ("lifecycle stage", "lifecyclestage")
COUNTRY = ("country/region", "country", "country region")
MARKETABLE = ("marketing contact status", "hs_marketable_status")
OPTOUT_ALL = ("unsubscribed from all email", "hs_email_optout", "opted out of all email")
TIMEZONE = ("time zone", "timezone", "hs_timezone")
PHONE = ("phone number", "phone")
COMPANY_ID = ("associated company id", "associated company ids", "primary associated company id", "associatedcompanyid", "company id")
STAGE_IDS = {"marketingqualifiedlead": "marketing_qualified_lead", "salesqualifiedlead": "sales_qualified_lead"}


def _is_contacts(header: List[str]) -> bool:
    keys = {norm(h) for h in header}
    return bool(keys & {norm(a) for a in RECORD_ID}) and bool(keys & {norm(a) for a in EMAIL})


def detect(folder: Path) -> bool:
    """True when the folder holds a CSV with HubSpot's Record ID and Email columns."""
    folder = Path(folder)
    return folder.is_dir() and any(_is_contacts(csv_header(p)) for p in csv_files(folder))


def _zone(name: Optional[str]):
    if not name:
        return None
    from zoneinfo import ZoneInfo
    return ZoneInfo(name)


def _iana(value: Optional[str]) -> Optional[str]:
    """An IANA zone name when HubSpot's value is one (America/Chicago); HubSpot's own labels are not guessed at."""
    if not value or "/" not in value:
        return None
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(value.strip())
        return value.strip()
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _stage(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    key = re.sub(r"[^a-z]", "", value.lower())
    if key in STAGE_IDS:
        return STAGE_IDS[key]
    if re.fullmatch(r"\d+", value.strip()):
        return value.strip()                      # a custom stage id: kept as exported
    return snake(value)


def map(folder: Path, out_dir: Path, timezone: Optional[str] = None) -> MappingReport:  # noqa: A001
    """Read HubSpot contacts exports in `folder`; write users.csv and consent_events.csv to `out_dir`.

    timezone: the HubSpot account's time zone (IANA name), used to read the export's zone-less datetimes.
    """
    folder, out_dir = Path(folder), Path(out_dir)
    report = MappingReport(source=NAME)
    zone = _zone(timezone)
    tp = TimeParser(zone) if zone else TimeParser()
    sheets = [Sheet.read(p) for p in csv_files(folder) if _is_contacts(csv_header(p))]
    if not sheets:
        report.warnings.append(f"no HubSpot contacts export (a CSV with Record ID and Email columns) in {folder}")
        return report

    users: Dict[str, Dict[str, Any]] = {}
    optouts: List[Dict[str, Any]] = []
    newest: Optional[datetime] = None
    by_record_id = no_email = no_created = non_marketing = all_optout = type_only = 0
    unknown_countries: Dict[str, int] = {}
    pending: List[Dict[str, Any]] = []
    for sheet in sheets:
        type_cols = [h for h in sheet.headers if re.match(r"^\s*opted out of email\s*:", h, re.I) or re.match(r"^hs_email_optout_\d+$", h)]
        for h in type_cols:
            sheet.use(h)
        for row in sheet.rows:
            created = tp.parse(sheet.get(row, *CREATED))
            modified = tp.parse(sheet.get(row, *MODIFIED))
            for t in (created, modified):
                if t and (newest is None or t > newest):
                    newest = t
            rid = sheet.get(row, *RECORD_ID)
            uid = sheet.get(row, *USER_ID)
            if not uid:
                uid = rid
                by_record_id += 1 if rid else 0
            if not uid:
                continue
            addr = email(sheet.get(row, *EMAIL))
            if not addr:
                no_email += 1
                continue
            if created is None:
                no_created += 1
                continue
            raw_country = sheet.get(row, *COUNTRY)
            country = country_iso2(raw_country)
            if raw_country and not country:
                unknown_countries[raw_country] = unknown_countries.get(raw_country, 0) + 1
            status = (sheet.get(row, *MARKETABLE) or "").lower()
            if status and ("non" in status or status in ("false", "no")):
                non_marketing += 1
            company = sheet.get(row, *COMPANY_ID)
            if uid in users:
                report.warnings.append(f"user_id {uid} appears twice in the HubSpot export; the first row was kept")
                continue
            users[uid] = {"user_id": uid, "email": addr, "created_at": created, "country": country,
                          "lifecycle_stage": _stage(sheet.get(row, *STAGE)), "timezone": _iana(sheet.get(row, *TIMEZONE)),
                          "phone": phone_e164(sheet.get(row, *PHONE)),
                          "account_id": company.split(";")[0].strip() if company else None}
            opted_all = truthy(sheet.get(row, *OPTOUT_ALL)) is True
            opted_type = any(truthy(row.get(h)) is True for h in type_cols)
            if opted_all or opted_type:
                all_optout += 1 if opted_all else 0
                type_only += 0 if opted_all else 1
                pending.append({"user_id": uid, "modified": modified})
        report.unmapped_columns[rel(sheet.path, folder)] = sheet.unused()

    for p in pending:
        optouts.append({"user_id": p["user_id"], "channel": "email", "status": "revoked", "purpose": "marketing",
                        "source": "esp_unsubscribe", "occurred_at": p["modified"] or newest})
    dated_by_export = sum(1 for p in pending if not p["modified"])

    if by_record_id:
        report.assumptions.append(f"ASSUMED: {by_record_id} contact(s) carry no product user id column (User ID, user_id, "
                                  "External ID); their HubSpot Record ID is the user_id, which will not join events keyed on "
                                  "your own ids.")
    else:
        report.assumptions.append("user_id comes from the export's user id column, so HubSpot contacts join your product data.")
    zone_name = timezone or "UTC"
    if not timezone:
        report.assumptions.append("ASSUMED: HubSpot exports datetimes in the account's time zone without naming it; they were "
                                  "read as UTC. Pass timezone=<the portal's IANA zone> to read them correctly.")
    report.assumptions += tp.notes("HubSpot", zone_name)
    report.warnings += tp.warnings("HubSpot")
    if pending:
        report.assumptions.append(f"ASSUMED: {len(pending)} opt-out(s) carry no opt-out time in the export; each is dated at "
                                  "the contact's Last Modified Date (an opt-out changes the record, so it happened by then)"
                                  + (f", and {dated_by_export} without one at the newest time in the export" if dated_by_export else "")
                                  + ". The later date is the conservative one.")
    if type_only:
        report.assumptions.append(f"ASSUMED: {type_only} contact(s) opted out of one or more subscription types but not all "
                                  "email; each is written as a revocation of all marketing email (conservative), since "
                                  "consent_events has no subscription-type column.")
    if all_optout:
        report.assumptions.append(f"{all_optout} contact(s) are unsubscribed from all email: revoked, source esp_unsubscribe.")
    report.assumptions.append("No consent was inferred: marketing contact status is a HubSpot billing flag and 'not opted "
                              "out' is not an opt-in, so email_consent_at is empty for every contact.")
    if non_marketing:
        report.warnings.append(f"{non_marketing} contact(s) are non-marketing contacts: HubSpot will not send them marketing "
                               "email, and the canonical users table has no column to carry that (schema gap).")
    if unknown_countries:
        listed = ", ".join(f"{k} ({v})" for k, v in sorted(unknown_countries.items()))
        report.warnings.append(f"country left empty for values that are not an ISO code or a known country name: {listed}")
    if no_email:
        report.warnings.append(f"{no_email} contact(s) without a valid email were left out (email is required).")
    if no_created:
        report.warnings.append(f"{no_created} contact(s) without a Create Date were left out (created_at is required).")

    if users:
        report.tables["users"] = write_table(out_dir, "users", list(users.values()), ["user_id"])
    if optouts:
        report.tables["consent_events"] = write_table(out_dir, "consent_events", optouts, ["user_id", "occurred_at"])
    return report
