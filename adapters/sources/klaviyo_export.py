"""Klaviyo exports into the canonical tables: users, consent_events, track_events and message_events.

Which files (put them in one folder; names do not matter, the mapper reads the headers):

  Profiles           a list or segment export: Audience > Lists & segments > open the list or segment > Manage list
                     (or Manage segment) > Export list (segment) to CSV; the file appears under Settings > Other >
                     Downloads. To export everyone, build a segment that holds all profiles and export it
                     (https://help.klaviyo.com/hc/en-us/articles/115005078687). Include the consent fields and the
                     profile id. Klaviyo documents the consent columns (https://help.klaviyo.com/hc/en-us/articles/115005078967):
                     "Email Marketing Consent", "Email Marketing Consent Timestamp", "SMS Marketing Consent", with values
                     Subscribed, Unsubscribed, Never subscribed (also read: SUBSCRIBED, NEVER_SUBSCRIBED, SUPPRESSED).
                     The other headers read here (Klaviyo ID, Profile Created On or Date Added, Country, Timezone, Phone
                     Number, External ID) are matched by several known spellings; the help center does not list them.
  Events             the list and segment export cannot include events (same article), so events come from Klaviyo's
                     SFTP data export: once SFTP data transfer is set up, its page's Data export tab > Create export >
                     Events, as .csv.gz or .jsonl.gz with the columns id, metric_id, profile_id, event_properties,
                     datetime, uuid (https://developers.klaviyo.com/en/docs/export_event_data_sftp; Owner, Admin or
                     Manager role). Gzipped files are read as they are.
                     Also read: Get Events API output (https://developers.klaviyo.com/en/reference/get_events), saved as
                     JSON with include=metric, or as JSON lines; and any CSV with a metric name column (metric, metric_name,
                     Metric Name) such as a warehouse sync table.
  Metric names       the SFTP export names the metric by id only. Add the Get Metrics API response saved as metrics.json
                     ({"data": [{"type": "metric", "id": ..., "attributes": {"name": "Placed Order"}}]}), or a two-column
                     CSV (metric_id,name). Events whose metric cannot be named are skipped and counted, never guessed.

What maps to what (metric names verified: email metrics at https://help.klaviyo.com/hc/en-us/articles/360036974872,
Shopify and onsite metrics at https://help.klaviyo.com/hc/en-us/articles/115005080447):

  Placed Order                      track_events order_completed (order_id from $event_id or $extra.id, net_amount from
                                    $value, items from $extra.line_items or Items)
  Checkout Started, Started Checkout  track_events checkout_started (checkout_id from $event_id)
  Viewed Product                    track_events product_viewed (product_id, category, price)
  Added to Cart                     track_events product_added
  Active on Site                    track_events session_started (platform web)
  Received Email                    message_events delivered
  Opened Email                      message_events opened; machine_open from the Apple Privacy Open property (True when
                                    the open may come from Apple Mail Privacy Protection,
                                    https://help.klaviyo.com/hc/en-us/articles/4416791883163)
  Clicked Email                     message_events clicked
  Bounced Email                     message_events bounced
  Marked Email as Spam              message_events complained, and an email consent revoke
  Unsubscribed (also "Unsubscribed from Email", "Unsubscribed from Email Marketing")
                                    message_events unsubscribed, and an email consent revoke (esp_unsubscribe)
  Unsubscribed from List            message_events unsubscribed (one list only: consent is not revoked)
  Subscribed to List, Subscribed to Email Marketing   email consent granted at the event time (signup_form)
  Received SMS, Clicked SMS, Subscribed to / Unsubscribed from SMS Marketing   the same for SMS (names not verified)
  anything else (Fulfilled Order, Ordered Product, Dropped Email ...)   skipped and counted in the report

product_id is normalized to the SKU wherever an order or checkout in the same export pairs Klaviyo's ProductID with a
SKU, so product views join the Shopify line items (whose product_id is the SKU).

What it cannot know (recorded as ASSUMED lines in the mapping report when they apply):
  * consent captured without a timestamp: a Subscribed profile with no Email Marketing Consent Timestamp is dated at the
    profile's first known activity (source import_undated, early so any real later unsubscribe wins); an Unsubscribed or
    Suppressed one with no timestamp is dated at the end of the export (late, so it outranks earlier grants). An SMS
    Subscribed with no timestamp is not recorded as consent (SMS needs a dated record).
  * the consent timestamp is read as the time the current status was set.
  * timestamps without an offset are read as UTC.
  * Suppressed (hard bounce or spam suppression) is read as a revoke: the person cannot be emailed either way.
  * $value is read as the order or checkout value Klaviyo received from the store; its tax and shipping treatment
    depends on the integration, so Shopify's orders table stays the revenue source of truth when both are imported.

    python3 adapters/sources/cli.py klaviyo exports/klaviyo --out data/
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from adapters.sources.export_io import (UNDATED_SOURCE, Columns, TimeParser, apply_consent, email_norm, email_user_id,
                                        fmt_money, fmt_ts, iter_files, jdump, money, norm, num, phone_e164, read_headers,
                                        read_jsonl, read_rows, relative, slug, strip_id, write_tables)
from adapters.sources.report import MappingReport
from engine.data.iso import country_code

NAME: str = "klaviyo"
DESCRIPTION: str = "Klaviyo profile export (list or segment CSV) and event export (SFTP .csv.gz or .jsonl.gz, or API JSON)"

TRACK = {"placedorder": "order_completed", "checkoutstarted": "checkout_started", "startedcheckout": "checkout_started",
         "viewedproduct": "product_viewed", "addedtocart": "product_added", "activeonsite": "session_started"}
MESSAGE = {"receivedemail": ("email", "delivered"), "openedemail": ("email", "opened"), "clickedemail": ("email", "clicked"),
           "bouncedemail": ("email", "bounced"), "markedemailasspam": ("email", "complained"),
           "unsubscribed": ("email", "unsubscribed"), "unsubscribedfromemail": ("email", "unsubscribed"),
           "unsubscribedfromemailmarketing": ("email", "unsubscribed"), "unsubscribedfromlist": ("email", "unsubscribed"),
           "receivedsms": ("sms", "delivered"), "clickedsms": ("sms", "clicked"),
           "unsubscribedfromsms": ("sms", "unsubscribed"), "unsubscribedfromsmsmarketing": ("sms", "unsubscribed")}
# metric -> (channel, status, source) consent record it implies
CONSENT = {"markedemailasspam": ("email", "revoked", "esp_complaint"), "unsubscribed": ("email", "revoked", "esp_unsubscribe"),
           "unsubscribedfromemail": ("email", "revoked", "esp_unsubscribe"),
           "unsubscribedfromemailmarketing": ("email", "revoked", "esp_unsubscribe"),
           "subscribedtolist": ("email", "granted", "signup_form"), "subscribedtoemailmarketing": ("email", "granted", "signup_form"),
           "subscribedtosmsmarketing": ("sms", "granted", "signup_form"), "subscribedtosms": ("sms", "granted", "signup_form"),
           "unsubscribedfromsms": ("sms", "revoked", "esp_unsubscribe"),
           "unsubscribedfromsmsmarketing": ("sms", "revoked", "esp_unsubscribe")}

PROFILE_ID = ("Klaviyo ID", "Profile ID", "Klaviyo Profile ID", "profile_id", "$id", "id")
EXTERNAL_ID = ("External ID", "external_id", "$external_id")
CREATED = ("Profile Created On", "Created", "Created At", "created", "Date Added", "First Active")
TIMEZONE = ("Timezone", "Time Zone", "$timezone", "timezone")
COUNTRY = ("Country", "$country", "country")
PHONE = ("Phone Number", "phone_number", "$phone_number", "Phone")
EMAIL_CONSENT = ("Email Marketing Consent", "email_marketing_consent")
EMAIL_CONSENT_AT = ("Email Marketing Consent Timestamp", "email_marketing_consent_timestamp")
SMS_CONSENT = ("SMS Marketing Consent", "sms_marketing_consent", "SMS Consent")
SMS_CONSENT_AT = ("SMS Marketing Consent Timestamp", "sms_marketing_consent_timestamp", "SMS Consent Timestamp")
EVENT_ID = ("id", "event_id", "Event ID")
EVENT_TIME = ("datetime", "timestamp", "Timestamp", "Event Time", "time")
EVENT_PROPS = ("event_properties", "properties", "Event Properties")
METRIC_ID = ("metric_id", "Metric ID", "statistic_id")
METRIC_NAME = ("metric_name", "Metric Name", "metric", "Metric", "event_name", "Event Name")
EVENT_PROFILE = ("profile_id", "Profile ID", "person_id", "Klaviyo ID")
EVENT_EMAIL = ("email", "Email", "profile_email")


def _consent_value(value: str) -> Optional[str]:
    v = norm(value)
    if v in ("subscribed", "true", "yes"):
        return "subscribed"
    if v in ("unsubscribed", "false"):
        return "unsubscribed"
    if v == "suppressed":
        return "suppressed"
    if v in ("neversubscribed", "", "no"):
        return None
    return "unknown"


def _classify(path: Path) -> Optional[str]:
    name = path.name.lower()
    if name.endswith((".jsonl", ".jsonl.gz")):
        return "events"
    if name.endswith(".json"):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
        data = doc.get("data") if isinstance(doc, dict) else None
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return {"metric": "metrics", "event": "events"}.get(str(data[0].get("type")))
        return None
    n = {norm(h) for h in read_headers(path)}
    if not n:
        return None
    if "eventproperties" in n and ({"metricid", "metricname", "metric", "statisticid"} & n):
        return "events"
    if ("metricid" in n or "id" in n) and "name" in n and "metric" in name and "email" not in n:
        return "metrics"
    if "email" in n and ({"emailmarketingconsent", "klaviyoid", "smsmarketingconsent"} & n
                         or ({"phonenumber", "stateregion"} <= n)):
        return "profiles"
    return None


def _files(folder: Path) -> Dict[str, List[Path]]:
    out: Dict[str, List[Path]] = defaultdict(list)
    for p in iter_files(folder, (".csv", ".csv.gz", ".json", ".jsonl", ".jsonl.gz")):
        kind = _classify(p)
        if kind:
            out[kind].append(p)
    return out


def detect(folder: Path) -> bool:
    """True when the folder holds a Klaviyo profile export or a Klaviyo event export."""
    files = _files(Path(folder))
    return bool(files.get("profiles") or files.get("events"))


def _props(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        obj = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _metric_names(paths: List[Path]) -> Dict[str, str]:
    names: Dict[str, str] = {}
    for p in paths:
        if p.name.lower().endswith(".json"):
            doc = json.loads(p.read_text(encoding="utf-8"))
            for m in doc.get("data", []):
                if isinstance(m, dict) and m.get("type") == "metric":
                    names[str(m.get("id"))] = str((m.get("attributes") or {}).get("name", ""))
        else:
            headers, rows = read_rows(p)
            cols = Columns(headers)
            for r in rows:
                mid = cols.get(r, "metric_id", "id")
                name = cols.get(r, "name", "metric_name", "Metric Name")
                if mid and name:
                    names[mid] = name
    return names


def _api_events(doc: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    """Get Events API objects (with include=metric/profile) flattened to the SFTP field names."""
    included = {(i.get("type"), i.get("id")): i for i in doc.get("included", []) if isinstance(i, dict)}
    for e in doc.get("data", []):
        if not isinstance(e, dict):
            continue
        yield _flatten_api_event(e, included)


def _flatten_api_event(e: Dict[str, Any], included: Dict[Tuple[Any, Any], Dict[str, Any]]) -> Dict[str, Any]:
    if "attributes" not in e:
        return e   # already flat (SFTP JSON lines)
    attrs = e.get("attributes") or {}
    rel = e.get("relationships") or {}
    metric = ((rel.get("metric") or {}).get("data") or {}).get("id")
    profile = ((rel.get("profile") or {}).get("data") or {}).get("id")
    flat = {"id": e.get("id"), "datetime": attrs.get("datetime") or attrs.get("timestamp"),
            "event_properties": attrs.get("event_properties") or {}, "metric_id": metric, "profile_id": profile}
    m = included.get(("metric", metric))
    if m:
        flat["metric_name"] = (m.get("attributes") or {}).get("name")
    pr = included.get(("profile", profile))
    if pr:
        flat["email"] = (pr.get("attributes") or {}).get("email")
    return flat


def _read_events(paths: List[Path], folder: Path, report: MappingReport) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for p in paths:
        name = p.name.lower()
        if name.endswith(".json"):
            events.extend(_api_events(json.loads(p.read_text(encoding="utf-8"))))
        elif name.endswith((".jsonl", ".jsonl.gz")):
            events.extend(_flatten_api_event(o, {}) for o in read_jsonl(p))
        else:
            headers, rows = read_rows(p)
            cols = Columns(headers)
            for r in rows:
                events.append({"id": cols.get(r, *EVENT_ID), "datetime": cols.get(r, *EVENT_TIME),
                               "event_properties": cols.get(r, *EVENT_PROPS), "metric_id": cols.get(r, *METRIC_ID),
                               "metric_name": cols.get(r, *METRIC_NAME), "profile_id": cols.get(r, *EVENT_PROFILE),
                               "email": cols.get(r, *EVENT_EMAIL)})
            cols.mark("uuid")
            report.unmapped_columns[relative(p, folder)] = cols.unmapped()
    return events


def _items(props: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Line items from a Placed Order or Checkout Started payload: $extra.line_items (Shopify), Items as objects
    (custom integrations), or Items as names only."""
    extra = props.get("$extra") if isinstance(props.get("$extra"), dict) else {}
    out = []
    for li in extra.get("line_items") or []:
        if isinstance(li, dict):
            out.append({"product_id": str(li.get("sku") or li.get("product_id") or slug(li.get("title") or li.get("name"))),
                        "klaviyo_product_id": str(li.get("product_id") or ""), "sku": str(li.get("sku") or ""),
                        "name": li.get("name") or li.get("title"), "quantity": _qty(li.get("quantity")),
                        "price": num(money(li.get("price")))})
    if out:
        return out
    for it in props.get("Items") or []:
        if isinstance(it, dict):
            pid = it.get("ProductID") or it.get("product_id")
            sku = it.get("SKU") or it.get("sku")
            out.append({"product_id": str(sku or pid or slug(it.get("ProductName") or it.get("Name"))),
                        "klaviyo_product_id": str(pid or ""), "sku": str(sku or ""),
                        "name": it.get("ProductName") or it.get("Name"), "quantity": _qty(it.get("Quantity")),
                        "price": num(money(it.get("ItemPrice") or it.get("Price")))})
        elif isinstance(it, str):
            out.append({"product_id": slug(it), "klaviyo_product_id": "", "sku": "", "name": it, "quantity": None, "price": None})
    return out


def _qty(value: Any) -> Any:
    """A quantity as a whole number when it is one ('2', 2.0 -> 2); anything else unchanged."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return value
    return int(f) if f.is_integer() else value


def _currency(props: Dict[str, Any]) -> str:
    extra = props.get("$extra") if isinstance(props.get("$extra"), dict) else {}
    for v in (props.get("$currency_code"), props.get("$currency"), props.get("Currency"), extra.get("currency"),
              extra.get("presentment_currency")):
        if v:
            return str(v).upper()
    return ""


def _first_category(value: Any) -> Optional[str]:
    if isinstance(value, list):
        return str(value[0]) if value else None
    return str(value) if value else None


def map(folder: Path, out_dir: Path) -> MappingReport:  # noqa: A001 (the mapper interface name)
    """Read the Klaviyo exports under `folder` and write canonical CSVs into `out_dir`."""
    folder, out_dir = Path(folder), Path(out_dir)
    report = MappingReport(NAME)
    files = _files(folder)
    ts = TimeParser()

    # ---- profiles ---------------------------------------------------------------------------------------------------
    users: Dict[str, Dict[str, Any]] = {}
    by_profile: Dict[str, str] = {}
    by_email: Dict[str, str] = {}
    profile_consent: List[Tuple[str, str, Optional[str], Optional[datetime]]] = []   # (uid, channel, value, at)
    skipped_no_email = 0
    unknown_consent: Counter = Counter()
    for path in files.get("profiles", []):
        headers, rows = read_rows(path)
        cols = Columns(headers)
        for r in rows:
            email = email_norm(cols.get(r, "Email", "Email Address", "$email"))
            pid = strip_id(cols.get(r, *PROFILE_ID))
            external = strip_id(cols.get(r, *EXTERNAL_ID))
            if not email and not pid:
                skipped_no_email += 1
                continue
            uid = external or (email_user_id(email) if email else f"klaviyo_{pid}")
            country = country_code(cols.get(r, *COUNTRY))
            users[uid] = {"user_id": uid, "email": email, "created": ts(cols.get(r, *CREATED)), "country": country or "",
                          "timezone": cols.get(r, *TIMEZONE), "phone": phone_e164(cols.get(r, *PHONE), country),
                          "first_event": None}
            if pid:
                by_profile[pid] = uid
            if email:
                by_email[email] = uid
            for channel, vcol, tcol in (("email", EMAIL_CONSENT, EMAIL_CONSENT_AT), ("sms", SMS_CONSENT, SMS_CONSENT_AT)):
                raw = cols.get(r, *vcol)
                value = _consent_value(raw)
                if value == "unknown":
                    unknown_consent[raw] += 1
                    continue
                if value:
                    profile_consent.append((uid, channel, value, ts(cols.get(r, *tcol))))
        report.unmapped_columns[relative(path, folder)] = cols.unmapped()
    if files.get("profiles") and not by_profile:
        report.warnings.append("the profile export has no profile id column (Klaviyo ID); events can only be matched to "
                               "profiles by email, which the SFTP event export does not carry. Re-export with the id.")
    if unknown_consent:
        report.warnings.append("consent value(s) not recognized and ignored: " +
                               ", ".join(f"{v!r} ({n})" for v, n in unknown_consent.most_common(5)))

    # ---- events -----------------------------------------------------------------------------------------------------
    metric_names = _metric_names(files.get("metrics", []))
    raw_events = _read_events(files.get("events", []), folder, report)
    skipped: Counter = Counter()
    unnamed: Counter = Counter()
    placeholder_users = 0
    parsed: List[Dict[str, Any]] = []
    for e in raw_events:
        metric = e.get("metric_name") or metric_names.get(str(e.get("metric_id") or ""))
        if not metric:
            unnamed[str(e.get("metric_id") or "(none)")] += 1
            continue
        at = ts(e.get("datetime"))
        if at is None:
            skipped["(no readable datetime)"] += 1
            continue
        pid = strip_id(e.get("profile_id"))
        email = email_norm(e.get("email"))
        uid = by_profile.get(pid) or by_email.get(email)
        if uid is None:
            if not pid and not email:
                skipped["(no profile)"] += 1
                continue
            uid = email_user_id(email) if email else f"klaviyo_{pid}"
            if uid not in users:
                users[uid] = {"user_id": uid, "email": email, "created": None, "country": "", "timezone": "", "phone": "",
                              "first_event": None}
                placeholder_users += 1
            if pid:
                by_profile[pid] = uid
        u = users[uid]
        if u["first_event"] is None or at < u["first_event"]:
            u["first_event"] = at
        parsed.append({"id": str(e.get("id") or ""), "metric": metric, "key": norm(metric), "at": at, "uid": uid,
                       "props": _props(e.get("event_properties"))})
    if unnamed:
        report.warnings.append(f"{sum(unnamed.values()):,} event(s) skipped: their metric could not be named (metric ids "
                               f"{', '.join(list(unnamed)[:5])}). Add metrics.json (Get Metrics API) or a metric_id,name CSV.")
    if placeholder_users:
        report.warnings.append(f"{placeholder_users:,} profile(s) appear in events but not in the profile export; each got a "
                               "user without an email (klaviyo_<profile id>) so its events are kept. Export a segment of "
                               "all profiles to fill them in.")

    # ProductID -> SKU, from orders and checkouts that carry both
    crosswalk: Dict[str, str] = {}
    for p in parsed:
        if TRACK.get(p["key"]) in ("order_completed", "checkout_started"):
            for it in _items(p["props"]):
                if it["klaviyo_product_id"] and it["sku"]:
                    crosswalk.setdefault(it["klaviyo_product_id"], it["sku"])

    def product_id(props: Dict[str, Any]) -> str:
        pid = str(props.get("ProductID") or props.get("product_id") or "")
        sku = str(props.get("SKU") or props.get("Variant SKU") or props.get("sku") or "")
        return sku or crosswalk.get(pid) or pid or slug(props.get("Product") or props.get("ProductName") or props.get("Product Name"))

    products: Dict[str, Dict[str, Any]] = {}

    def catalog(pid: str, at: datetime, props: Dict[str, Any], title: Any) -> None:
        """Keep the newest name, category, link, image and price seen for each product on onsite events."""
        have = products.setdefault(pid, {"_at": at, "product_id": pid})
        newer = at >= have["_at"]
        seen = {"product_name": str(title or ""), "category": _first_category(props.get("Categories")) or "",
                "url": str(props.get("URL") or ""), "image_url": str(props.get("ImageURL") or ""),
                "price": fmt_money(money(props.get("Price")))}
        for k, v in seen.items():
            if v and (newer or not have.get(k)):
                have[k] = v
        have["_at"] = max(have["_at"], at)

    track_rows: List[Dict[str, str]] = []
    message_rows: List[Dict[str, str]] = []
    consent: List[Dict[str, str]] = []
    no_id = 0
    first_order: Dict[str, Tuple[datetime, str]] = {}
    for n, p in enumerate(sorted(parsed, key=lambda x: (x["at"], x["id"]))):
        event_id = p["id"] or f"klaviyo-{n:07d}"
        no_id += 0 if p["id"] else 1
        props, key, at, uid = p["props"], p["key"], p["at"], p["uid"]
        name = TRACK.get(key)
        if name:
            extra = props.get("$extra") if isinstance(props.get("$extra"), dict) else {}
            if name in ("order_completed", "checkout_started"):
                ref = str(props.get("$event_id") or extra.get("id") or props.get("OrderId") or props.get("CheckoutID")
                          or extra.get("token") or event_id)
                items = [{"product_id": i["product_id"], "quantity": i["quantity"], "price": i["price"]} for i in _items(props)]
                value = num(money(props.get("$value")))
                if name == "order_completed":
                    out = {"order_id": ref, "net_amount": value, "currency": _currency(props), "items": items}
                    codes = props.get("Discount Codes") or props.get("DiscountCodes")
                    if codes:
                        out["discount_codes"] = codes if isinstance(codes, list) else [codes]
                    k = (at, ref)
                    if uid not in first_order or k < first_order[uid]:
                        first_order[uid] = k
                else:
                    out = {"checkout_id": ref, "value": value, "currency": _currency(props), "items": items}
            elif name == "product_viewed":
                out = {"product_id": product_id(props), "category": _first_category(props.get("Categories")),
                       "price": num(money(props.get("Price")))}
                if props.get("$currency"):
                    out["currency"] = str(props["$currency"]).upper()
                catalog(out["product_id"], at, props, props.get("Product") or props.get("ProductName"))
            elif name == "product_added":
                out = {"product_id": product_id(props), "quantity": _qty(props.get("Quantity")),
                       "price": num(money(props.get("Price") or props.get("AddedItemPrice"))),
                       "value": num(money(props.get("$value"))), "currency": str(props.get("$currency") or "").upper() or None}
                catalog(out["product_id"], at, props, props.get("Product Name") or props.get("AddedItemProductName"))
            else:   # session_started
                out = {"platform": "web"}
            out = {k: v for k, v in out.items() if v is not None and v != ""}
            track_rows.append({"event_id": event_id, "user_id": uid, "event_name": name, "event_at": fmt_ts(at),
                               "properties": out, "_ref": out.get("order_id")})
        msg = MESSAGE.get(key)
        if msg:
            channel, event_name = msg
            email = users[uid]["email"]
            apple = props.get("Apple Privacy Open")
            row = {"event_id": event_id, "user_id": uid, "channel": channel, "event_name": event_name, "event_at": fmt_ts(at),
                   "message_id": str(props.get("$message") or props.get("Message ID") or ""),
                   "campaign": str(props.get("Campaign Name") or props.get("Flow Name") or ""),
                   "recipient_domain": (email.split("@", 1)[1] if channel == "email" and "@" in email else ""),
                   "machine_open": "", "diagnostic": ""}
            if event_name == "opened" and apple is not None:
                row["machine_open"] = "true" if str(apple).strip().lower() in ("true", "1", "yes") else "false"
            if event_name == "bounced":
                bits = [f"{k}: {props[k]}" for k in ("Bounce Type", "Reason", "Bounce Reason") if props.get(k)]
                row["diagnostic"] = "; ".join(bits)
            message_rows.append(row)
        cons = CONSENT.get(key)
        if cons:
            channel, status, source = cons
            consent.append({"user_id": uid, "channel": channel, "status": status, "purpose": "marketing",
                            "source": source, "occurred_at": fmt_ts(at)})
        if not (name or msg or cons):
            skipped[p["metric"]] += 1
    for row in track_rows:
        if row["event_name"] == "order_completed":
            row["properties"]["is_first_order"] = first_order.get(row["user_id"], (None, None))[1] == row["_ref"]
        row["properties"] = jdump(row.pop("properties"))
        row.pop("_ref", None)
    if skipped:
        report.assumptions.append("metrics with no canonical meaning were skipped: " +
                                  ", ".join(f"{m} ({c:,})" for m, c in sorted(skipped.items(), key=lambda x: (-x[1], x[0]))))
    if no_id:
        report.warnings.append(f"{no_id:,} event(s) had no id; they got klaviyo-<n> ids that change if the export changes.")
    if any(r["event_name"] == "order_completed" for r in track_rows):
        report.assumptions.append("order_completed.net_amount is Placed Order's $value, which may include shipping and tax "
                                  "depending on the store integration; import the store's own orders (Shopify) for revenue.")
    if crosswalk:
        report.assumptions.append(f"product_id is the SKU: {len(crosswalk):,} Klaviyo ProductID(s) were matched to a SKU "
                                  "through orders and checkouts that carry both, so product events join Shopify line items.")
    opens = [r for r in message_rows if r["event_name"] == "opened"]
    if opens and not any(r["machine_open"] for r in opens):
        report.warnings.append("no Opened Email event carries Apple Privacy Open; machine_open is empty, so privacy opens "
                               "cannot be told apart from human opens.")

    # ---- dates and consent from profiles --------------------------------------------------------------------------
    times = [u["created"] for u in users.values()] + [p["at"] for p in parsed] + [at for _u, _c, _v, at in profile_consent]
    start = min((t for t in times if t), default=None)
    end = max((t for t in times if t), default=None)
    no_created = 0
    placeholder_created: List[str] = []
    for u in users.values():
        if u["created"] is None:
            no_created += 1
            u["created"] = u["first_event"] or start
            if u["first_event"] is None:
                placeholder_created.append(u["user_id"])
    report.assumed_values = {"users.created_at": sorted(placeholder_created)}  # type: ignore[attr-defined]
    if no_created:
        report.assumptions.append(f"ASSUMED: {no_created:,} profile(s) have no creation date; created_at is their first event, "
                                  f"else the start of the export window ({fmt_ts(start)}).")
    undated_grant = undated_revoke = sms_undated = 0
    for uid, channel, value, at in profile_consent:
        if value == "subscribed":
            if at is not None:
                consent.append({"user_id": uid, "channel": channel, "status": "granted", "purpose": "marketing",
                                "source": "import", "occurred_at": fmt_ts(at)})
            elif channel == "sms":
                sms_undated += 1
            elif users[uid]["created"] is not None:
                consent.append({"user_id": uid, "channel": channel, "status": "granted", "purpose": "marketing",
                                "source": UNDATED_SOURCE, "occurred_at": fmt_ts(users[uid]["created"])})
                undated_grant += 1
        else:   # unsubscribed or suppressed
            when = at or end
            if when is None:
                continue
            consent.append({"user_id": uid, "channel": channel, "status": "revoked", "purpose": "marketing",
                            "source": "import" if at else UNDATED_SOURCE, "occurred_at": fmt_ts(when)})
            undated_revoke += 0 if at else 1
    if profile_consent:
        report.assumptions.append("Each profile's Marketing Consent Timestamp is read as the time its current consent status "
                                  "was set (source import). Suppressed is read as revoked: the address cannot be emailed.")
    if undated_grant:
        report.assumptions.append(f"ASSUMED: {undated_grant:,} profile(s) are Subscribed with no consent timestamp; consent is "
                                  "dated at the profile's creation (source import_undated), early, so any real later "
                                  "unsubscribe wins.")
    if undated_revoke:
        report.assumptions.append(f"ASSUMED: {undated_revoke:,} profile(s) are Unsubscribed or Suppressed with no timestamp; "
                                  f"the revoke is dated at the end of the export ({fmt_ts(end)}), late, so it outranks "
                                  "earlier grants.")
    if sms_undated:
        report.warnings.append(f"{sms_undated:,} profile(s) are SMS Subscribed with no SMS consent timestamp; SMS needs a dated "
                               "record (TCPA, 10DLC), so they were NOT marked SMS-consented.")
    if skipped_no_email:
        report.warnings.append(f"{skipped_no_email:,} profile row(s) had neither an email nor a profile id and were left out.")
    if ts.naive:
        report.assumptions.append(f"ASSUMED: {ts.naive:,} timestamp(s) carried no UTC offset and were read as UTC.")
    if ts.failed:
        report.warnings.append(f"{ts.failed:,} timestamp(s) could not be read (for example {', '.join(ts.failed_examples)}).")
    if not files.get("events"):
        report.warnings.append("no Klaviyo event export found: only users and consent were written.")
    if not files.get("profiles"):
        report.warnings.append("no Klaviyo profile export found: users have no email, country or consent from Klaviyo.")

    user_rows = [{"user_id": u["user_id"], "email": u["email"], "created_at": fmt_ts(u["created"]), "country": u["country"],
                  "timezone": u["timezone"], "phone": u["phone"]} for u in users.values()]
    apply_consent(user_rows, consent)
    product_rows = [{k: v for k, v in p.items() if k != "_at"} for p in products.values() if p.get("product_name")]
    if product_rows:
        report.assumptions.append(f"products: {len(product_rows):,} product(s) described from Viewed Product and Added to Cart "
                                  "(newest name, category, URL, image and price seen); merged with a store's own catalog, "
                                  "they only fill blanks.")
    report.tables = write_tables(out_dir, {"users": user_rows, "consent_events": consent, "track_events": track_rows,
                                           "message_events": message_rows, "products": product_rows})
    report.unmapped_columns = {k: v for k, v in report.unmapped_columns.items() if v}
    return report
