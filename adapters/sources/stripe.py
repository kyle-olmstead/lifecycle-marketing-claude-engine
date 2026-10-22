"""Stripe billing exports into users, accounts, subscriptions, invoices and the billing moments in track_events.

What it reads
  A folder holding any of these, as exported from the Stripe Dashboard (Customers, Subscriptions or Invoices
  list, Export, CSV) or as saved from the API (a list response, {"object": "list", "data": [...]}, or a bare
  JSON array of objects):

    customers*.csv | customers*.json        one row per Stripe customer (cus_...)
    subscriptions*.csv | subscriptions*.json one row per subscription (sub_...)
    invoices*.csv | invoices*.json           one row per invoice (in_...)

  The folder is searched recursively, so `lcm import auto exports/` finds exports/stripe/. A file is recognised by
  name first, then by the "object" field (JSON) or tell-tale columns (CSV), and is read only when its ids carry
  Stripe's prefix for that kind (cus_, sub_, in_), so another vendor's customers.csv is never mistaken for Stripe's.
  Several files of one kind (invoices split by month) are read together. Column names are matched without regard to
  case, spaces, punctuation or a "(UTC)" suffix, so "Current Period End (UTC)", "current_period_end" and
  "Current Period End UTC" are the same column. Metadata columns ("user_id (metadata)") are read as metadata.

What it writes
  users          one per customer with an email. user_id is the customer's metadata user id (keys user_id,
                 userId, app_user_id, external_id, uid) so it joins your product data; without one it is the
                 Stripe customer id (cus_...), and the report says how many fell back.
  accounts       only when a customer carries an account id in metadata (account_id, workspace_id,
                 company_id, organization_id, org_id, team_id) or a business name (business_name field, a
                 "Business Name" column, or metadata company_name / company / business_name). With a business
                 name but no account id, the customer id is the account id (one Stripe customer = one buying
                 unit), labelled ASSUMED. Otherwise no accounts table, and the report says so.
  subscriptions  status, start, trial end, current period end, cancel time and MRR.
  invoices       status, amounts, attempt_count, next_payment_attempt, paid time.
  track_events   trial_started, subscription_started (the first paid period), payment_failed,
                 payment_succeeded and subscription_cancelled, with ids stripe:<event>:<object id>.

How MRR is computed
  Stripe counts MRR from active and past_due subscriptions (https://docs.stripe.com/data/analytics/supported-metrics,
  revenue.mrr), so every other status has mrr 0.00. For those two statuses: unit price x quantity, brought to a month
  (year / 12, week x 52 / 12, day x 365 / 12, divided by the interval count). Metered prices have no fixed amount,
  so mrr is left empty. Discounts: Stripe calls subtracting them the more conservative choice
  (https://docs.stripe.com/billing/subscriptions/analytics, Discounts), so a percent or amount coupon that recurs
  (forever or repeating) is subtracted when the API JSON carries it expanded; when an export only names a coupon
  (the CSV "Coupon" column, or an unexpanded discount id) its value is unknown, so mrr is left empty rather than
  overstated, labelled ASSUMED.

Amounts
  API JSON amounts are integers in the currency's smallest unit (https://docs.stripe.com/api/invoices/object.md,
  amount_paid "in the smallest currency unit"); they are divided by 100, except for zero-decimal currencies
  (divided by 1) and three-decimal ones (divided by 1000) per https://docs.stripe.com/currencies (ISK and UGX are
  represented as two-decimal in the API, per the same page). Dashboard CSV amounts are read as already in major
  units ("49.00"): ASSUMED, recorded in the report.

Billing moments, and what each one assumes
  trial_started            at trial_start; when only trial_end is exported, at the subscription start (ASSUMED).
  subscription_started     the first paid invoice of the subscription (amount_paid > 0, paid time known). When the
                           export holds no invoice for that subscription: at trial_end if the trial ended without
                           a cancellation, else at the start date for a subscription that was not incomplete
                           (ASSUMED, counted in the report).
  payment_failed           one per invoice whose payment failed at least once: an unpaid invoice with
                           attempt_count >= 1, or a paid one with attempt_count >= 2. Neither the Dashboard export
                           nor the invoice object carries the time of each attempt, so the event is dated at the
                           invoice's finalization (else creation) time, the earliest a failure can have happened,
                           with properties.attempt_count and properties.dated_at = "invoice_finalized" or
                           "invoice_created" (ASSUMED). attempt_count semantics:
                           https://docs.stripe.com/api/invoices/object.md (attempt_count).
  payment_succeeded        each paid invoice with amount_paid > 0, at status_transitions.paid_at ("Paid At").
                           A paid invoice without a paid time gets no event and no paid_at (counted).
  subscription_cancelled   at canceled_at. Stripe sets canceled_at when cancellation was requested, so for
                           cancel_at_period_end it is the request time; properties.effective_at is ended_at, else
                           cancel_at, else the current period end. reason maps cancellation_details.feedback
                           (too_expensive -> price, missing_features -> missing_feature, switched_service ->
                           switched_competitor, unused -> not_using, others -> other, none -> unknown); values from
                           https://stripe.dev/stripe-java/com/stripe/model/Subscription.CancellationDetails.html.
                           properties.cancellation_reason carries Stripe's own reason (cancellation_requested,
                           payment_failed, payment_disputed), which separates voluntary from involuntary churn.

API versions
  Since API version 2025-03-31.basil, current_period_end lives on subscription items and an invoice's subscription
  on parent.subscription_details.subscription
  (https://docs.stripe.com/changelog/basil/2025-03-31/deprecate-subscription-current-period-start-and-end,
  https://docs.stripe.com/changelog/basil/2025-03-31/adds-new-parent-field-to-invoicing-objects). Both the old
  and the new locations are read; the subscription's period end is the earliest item period end.

Column names verified, and not
  Verified from Stripe's own documentation: the subscriptions export columns id, Customer ID, Quantity,
  Start Date (UTC) and Current Period End (UTC) (https://docs.stripe.com/billing/subscriptions/import-subscriptions-toolkit,
  Stripe-to-Stripe migration table) and the customers export columns ID and Created (UTC) (cited by a Stripe
  partner, https://docs.cello.so/guides/attribution/stripe-reports). Stripe does not publish the full Dashboard
  column list (the export lets you pick columns), so the other names (Status, Trial End (UTC), Canceled At (UTC),
  Amount Due, Attempt Count, Paid At (UTC), Next Payment Attempt (UTC) and so on) are matched by tolerant aliases
  of the API field names, not from a verified list.

    from adapters.sources import stripe
    report = stripe.map(Path("exports/stripe"), Path("data"))
    print(report.to_dict())
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from adapters.sources._mapping import (CENT, Sheet, TimeParser, as_int, country_iso2, csv_files, csv_header, email,
                                       files_under, iso, money, norm, phone_e164, rel, truthy, write_table)
from adapters.sources.report import MappingReport

NAME = "stripe"
DESCRIPTION = ("Stripe Dashboard CSV exports or API JSON lists (customers, subscriptions, invoices) into users, accounts, "
               "subscriptions, invoices and billing events (trial_started, subscription_started, payment_failed, "
               "payment_succeeded, subscription_cancelled)")

ZERO_DECIMAL = {"bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg", "rwf", "vnd", "vuv", "xaf", "xof", "xpf"}
THREE_DECIMAL = {"bhd", "jod", "kwd", "omr", "tnd"}
MRR_STATUSES = {"active", "past_due"}
STATUS_RANK = {"active": 0, "past_due": 1, "trialing": 2, "unpaid": 3, "paused": 4, "incomplete": 5,
               "canceled": 6, "incomplete_expired": 7}
USER_KEYS = ("user_id", "userid", "app_user_id", "external_id", "externalid", "uid")
ACCOUNT_KEYS = ("account_id", "accountid", "workspace_id", "company_id", "organization_id", "organisation_id", "org_id", "team_id")
ACCOUNT_NAME_KEYS = ("account_name", "company_name", "company", "business_name", "workspace_name", "organization_name", "organization")
FEEDBACK_TO_REASON = {"too_expensive": "price", "missing_features": "missing_feature", "switched_service": "switched_competitor",
                      "unused": "not_using", "customer_service": "other", "low_quality": "other", "too_complex": "other",
                      "other": "other"}
KINDS = ("customer", "subscription", "invoice")
ID_PREFIX = {"customer": "cus_", "subscription": "sub_", "invoice": "in_"}


# ----------------------------------------------------------------------------------------------
# normalized records
# ----------------------------------------------------------------------------------------------

@dataclass
class Customer:
    id: str
    email: Optional[str] = None
    created: Optional[datetime] = None
    country: Optional[str] = None
    phone: Optional[str] = None
    business_name: Optional[str] = None
    metadata: Dict[str, str] = field(default_factory=dict)


@dataclass
class Subscription:
    id: str
    customer_id: Optional[str] = None
    customer_email: Optional[str] = None
    status: Optional[str] = None
    start: Optional[datetime] = None
    created: Optional[datetime] = None
    trial_start: Optional[datetime] = None
    trial_end: Optional[datetime] = None
    current_period_end: Optional[datetime] = None
    canceled_at: Optional[datetime] = None
    cancel_at: Optional[datetime] = None
    cancel_at_period_end: Optional[bool] = None
    ended_at: Optional[datetime] = None
    plan: Optional[str] = None
    interval: Optional[str] = None
    list_mrr: Optional[Decimal] = None          # price x quantity per month, before discounts
    mrr_unknown: Optional[str] = None           # why the list MRR could not be computed
    discount_factor: Optional[Decimal] = None   # multiply list MRR by this (percent coupons)
    discount_monthly: Decimal = Decimal(0)      # subtract this (amount coupons, per month)
    discount_unknown: bool = False              # a coupon is named but its value is not in the export
    currency: Optional[str] = None
    feedback: Optional[str] = None
    reason: Optional[str] = None
    metadata: Dict[str, str] = field(default_factory=dict)


@dataclass
class Invoice:
    id: str
    customer_id: Optional[str] = None
    customer_email: Optional[str] = None
    subscription_id: Optional[str] = None
    created: Optional[datetime] = None
    finalized: Optional[datetime] = None
    status: Optional[str] = None
    amount_due: Optional[Decimal] = None
    amount_paid: Optional[Decimal] = None
    currency: Optional[str] = None
    attempt_count: Optional[int] = None
    next_attempt: Optional[datetime] = None
    paid_at: Optional[datetime] = None


# ----------------------------------------------------------------------------------------------
# reading
# ----------------------------------------------------------------------------------------------

def _kind_from_name(path: Path) -> Optional[str]:
    n = path.name.lower()
    for k in KINDS:
        if k in n:
            return k
    return None


def _kind_from_header(header: List[str]) -> Optional[str]:
    keys = {norm(h) for h in header}
    if keys & {"attemptcount", "amountdue", "amountpaid"}:
        return "invoice"
    if keys & {"currentperiodend", "trialend", "cancelatperiodend", "startdate"}:
        return "subscription"
    if "email" in keys and ("id" in keys or "customerid" in keys):
        return "customer"
    return None


def _json_records(path: Path) -> Optional[List[Dict[str, Any]]]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if isinstance(doc, dict) and isinstance(doc.get("data"), list):
        doc = doc["data"]
    if isinstance(doc, list) and all(isinstance(r, dict) for r in doc):
        return doc
    return None


def _peek_id(path: Path, kind: str) -> Optional[str]:
    """The object id in the first data row of a CSV, read without loading the whole file."""
    try:
        with Path(path).open(newline="", encoding="utf-8-sig") as fh:
            first = next(csv.DictReader(fh), None)
    except (OSError, UnicodeDecodeError, csv.Error):
        return None
    if first is None:
        return None
    by = {norm(k): (v or "").strip() for k, v in first.items() if k is not None}
    for alias in ("id", f"{kind}id"):
        if by.get(alias):
            return by[alias]
    return None


def _classify(folder: Path, load: bool) -> Dict[str, List[Tuple[Path, Any]]]:
    """kind -> [(path, Sheet or list of JSON objects)] for every Stripe file under `folder` (searched recursively).

    A file counts only when its ids carry Stripe's prefix for its kind (cus_, sub_, in_) or its JSON objects say
    "object": "customer" | "subscription" | "invoice", so another vendor's customers.csv is never read as Stripe's.
    With load=False the CSVs are only peeked at (for detect)."""
    out: Dict[str, List[Tuple[Path, Any]]] = {k: [] for k in KINDS}
    for path in csv_files(folder):
        kind = _kind_from_name(path) or _kind_from_header(csv_header(path))
        if kind and (_peek_id(path, kind) or "").startswith(ID_PREFIX[kind]):
            out[kind].append((path, Sheet.read(path) if load else None))
    for path in files_under(folder, (".json",)):
        recs = _json_records(path)
        if not recs:
            continue
        objects = {str(r.get("object", "")) for r in recs if r.get("object")}
        kind = next((k for k in KINDS if objects == {k}), None)
        if kind is None:
            k = _kind_from_name(path)
            kind = k if k and str(recs[0].get("id", "")).startswith(ID_PREFIX[k]) else None
        if kind:
            out[kind].append((path, recs))
    return out


def detect(folder: Path) -> bool:
    """True when the folder (or a subfolder) holds a Stripe customers, subscriptions or invoices export."""
    return any(_classify(Path(folder), load=False).values())


def _metadata_from_sheet(sheet: Sheet, row: Dict[str, str]) -> Dict[str, str]:
    out = {}
    for h in sheet.headers:
        m = re.match(r"^\s*(.+?)\s*\(metadata\)\s*$", h, re.I) or re.match(r"^\s*metadata[.\[]\s*([^\]]+?)\s*\]?\s*$", h, re.I)
        if m:
            sheet.use(h)
            v = (row.get(h) or "").strip()
            if v:
                out[m.group(1).strip()] = v
    return out


def _meta(meta: Dict[str, Any], keys: Iterable[str]) -> Optional[str]:
    by = {norm(k): v for k, v in (meta or {}).items()}
    for k in keys:
        v = by.get(norm(k))
        if v not in (None, ""):
            return str(v).strip()
    return None


def _status(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    s = re.sub(r"[\s\-]+", "_", value.strip().lower())
    return {"cancelled": "canceled", "pastdue": "past_due"}.get(s, s)


def _minor(amount: Any, currency: Optional[str]) -> Optional[Decimal]:
    d = money(amount)
    if d is None:
        return None
    cur = (currency or "").lower()
    div = 1 if cur in ZERO_DECIMAL else 1000 if cur in THREE_DECIMAL else 100
    return d / div


def _months(interval: Optional[str], count: Optional[int]) -> Optional[Decimal]:
    """How many months one billing interval lasts."""
    n = Decimal(count or 1)
    per = {"month": Decimal(1), "year": Decimal(12), "week": Decimal(12) / Decimal(52), "day": Decimal(12) / Decimal(365)}
    base = per.get((interval or "").lower().rstrip("s").replace("monthly", "month").replace("yearly", "year"))
    return base * n if base is not None else None


def _plan_name(*candidates: Optional[str]) -> Optional[str]:
    ids = [c for c in candidates if c]
    for c in ids:
        if not re.match(r"^(price|plan|prod)_", c):
            return c
    return ids[0] if ids else None


def _customer_csv(sheet: Sheet, row: Dict[str, str], tp: TimeParser) -> Optional[Customer]:
    cid = sheet.get(row, "id", "customer id")
    if not cid:
        return None
    meta = _metadata_from_sheet(sheet, row)
    return Customer(id=cid, email=email(sheet.get(row, "email")),
                    created=tp.parse(sheet.get(row, "created", "created at", "created date")),
                    country=sheet.get(row, "address country", "address_country", "country", "billing country"),
                    phone=sheet.get(row, "phone"),
                    business_name=sheet.get(row, "business name") or _meta(meta, ("business_name", "company_name", "company")),
                    metadata=meta)


def _customer_json(r: Dict[str, Any], tp: TimeParser) -> Optional[Customer]:
    if not r.get("id"):
        return None
    meta = {str(k): str(v) for k, v in (r.get("metadata") or {}).items() if v not in (None, "")}
    addr = r.get("address") or {}
    return Customer(id=str(r["id"]), email=email(r.get("email")), created=tp.parse(r.get("created")),
                    country=addr.get("country") if isinstance(addr, dict) else None, phone=r.get("phone"),
                    business_name=r.get("business_name") or _meta(meta, ("business_name", "company_name", "company")),
                    metadata=meta)


def _subscription_csv(sheet: Sheet, row: Dict[str, str], tp: TimeParser) -> Optional[Subscription]:
    sid = sheet.get(row, "id", "subscription id")
    if not sid:
        return None
    currency = (sheet.get(row, "currency", "plan currency", "price currency") or "").lower() or None
    s = Subscription(
        id=sid, customer_id=sheet.get(row, "customer id", "customer"), customer_email=email(sheet.get(row, "customer email")),
        status=_status(sheet.get(row, "status")),
        start=tp.parse(sheet.get(row, "start date", "start", "started at", "start_date")),
        created=tp.parse(sheet.get(row, "created", "created at")),
        trial_start=tp.parse(sheet.get(row, "trial start", "trial start date")),
        trial_end=tp.parse(sheet.get(row, "trial end", "trial end date")),
        current_period_end=tp.parse(sheet.get(row, "current period end")),
        canceled_at=tp.parse(sheet.get(row, "canceled at", "cancelled at", "canceled date")),
        cancel_at=tp.parse(sheet.get(row, "cancel at")),
        cancel_at_period_end=truthy(sheet.get(row, "cancel at period end")),
        ended_at=tp.parse(sheet.get(row, "ended at", "ended", "ended date")),
        plan=_plan_name(sheet.get(row, "plan name", "price nickname", "plan nickname", "nickname"), sheet.get(row, "plan", "price"),
                        sheet.get(row, "product name", "product"), sheet.get(row, "plan id", "price id")),
        interval=(sheet.get(row, "interval", "plan interval", "price interval", "billing interval") or "").lower() or None,
        currency=currency,
        feedback=(sheet.get(row, "cancellation feedback", "feedback") or "").lower() or None,
        reason=(sheet.get(row, "cancellation reason") or "").lower() or None,
        metadata=_metadata_from_sheet(sheet, row))
    unit = money(sheet.get(row, "unit amount", "price amount", "plan amount", "amount"))
    qty = as_int(sheet.get(row, "quantity"))
    months = _months(s.interval, as_int(sheet.get(row, "interval count", "plan interval count", "price interval count")))
    if unit is None:
        s.mrr_unknown = "no price amount in the export"
    elif months is None:
        s.mrr_unknown = f"unknown billing interval {s.interval!r}"
    else:
        s.list_mrr = unit * Decimal(qty if qty is not None else 1) / months
    if sheet.get(row, "coupon", "coupon id", "discount", "discounts", "promotion code"):
        s.discount_unknown = True
    return s


def _subscription_json(r: Dict[str, Any], tp: TimeParser) -> Optional[Subscription]:
    if not r.get("id"):
        return None
    items = ((r.get("items") or {}).get("data") or []) if isinstance(r.get("items"), dict) else []
    currency = (r.get("currency") or "").lower() or None
    cust = r.get("customer")
    cdet = r.get("cancellation_details") or {}
    s = Subscription(
        id=str(r["id"]), customer_id=cust.get("id") if isinstance(cust, dict) else cust,
        customer_email=email(cust.get("email")) if isinstance(cust, dict) else None,
        status=_status(r.get("status")), start=tp.parse(r.get("start_date")), created=tp.parse(r.get("created")),
        trial_start=tp.parse(r.get("trial_start")), trial_end=tp.parse(r.get("trial_end")),
        canceled_at=tp.parse(r.get("canceled_at")), cancel_at=tp.parse(r.get("cancel_at")),
        cancel_at_period_end=r.get("cancel_at_period_end"), ended_at=tp.parse(r.get("ended_at")), currency=currency,
        feedback=(cdet.get("feedback") or None), reason=(cdet.get("reason") or None),
        metadata={str(k): str(v) for k, v in (r.get("metadata") or {}).items() if v not in (None, "")})
    period_ends = [tp.parse(i.get("current_period_end")) for i in items if i.get("current_period_end")]
    s.current_period_end = tp.parse(r.get("current_period_end")) or (min(period_ends) if period_ends else None)
    total = Decimal(0)
    names: List[Optional[str]] = []
    for i in items:
        price = i.get("price") or i.get("plan") or {}
        rec = price.get("recurring") or {}
        interval = rec.get("interval") or price.get("interval")
        count = rec.get("interval_count") or price.get("interval_count")
        s.interval = s.interval or interval
        product = price.get("product")
        names += [price.get("nickname"), product.get("name") if isinstance(product, dict) else product, price.get("id")]
        cur = (price.get("currency") or currency or "").lower()
        s.currency = s.currency or cur or None
        if (rec.get("usage_type") or price.get("usage_type")) == "metered":
            s.mrr_unknown = "a metered price has no fixed amount"
            continue
        unit = price.get("unit_amount") if price.get("unit_amount") is not None else price.get("amount")
        if unit is None and price.get("unit_amount_decimal") is not None:
            unit = price.get("unit_amount_decimal")
        months = _months(interval, count)
        if unit is None or months is None:
            s.mrr_unknown = s.mrr_unknown or "a price without a fixed amount or interval (tiered or custom)"
            continue
        total += _minor(unit, cur) * Decimal(i.get("quantity") or 1) / months
    if items and not s.mrr_unknown:
        s.list_mrr = total
    elif not items:
        s.mrr_unknown = "no items in the subscription object"
    s.plan = _plan_name(*names)
    discounts = list(r.get("discounts") or [])
    if r.get("discount"):
        discounts.append(r["discount"])
    for d in discounts:
        coupon = (d.get("coupon") if isinstance(d, dict) else None) or ((d.get("source") or {}).get("coupon") if isinstance(d, dict) else None)
        if not isinstance(coupon, dict):
            s.discount_unknown = True
            continue
        if coupon.get("duration") == "once":
            continue
        if coupon.get("percent_off") is not None:
            s.discount_factor = (s.discount_factor or Decimal(1)) * (1 - Decimal(str(coupon["percent_off"])) / 100)
        elif coupon.get("amount_off") is not None:
            months = _months(s.interval, 1) or Decimal(1)
            s.discount_monthly += _minor(coupon["amount_off"], coupon.get("currency") or s.currency) / months
        else:
            s.discount_unknown = True
    return s


def _invoice_csv(sheet: Sheet, row: Dict[str, str], tp: TimeParser) -> Optional[Invoice]:
    iid = sheet.get(row, "id", "invoice id")
    if not iid:
        return None
    status = _status(sheet.get(row, "status"))
    if not status and truthy(sheet.get(row, "paid")):
        status = "paid"
    return Invoice(id=iid, customer_id=sheet.get(row, "customer id", "customer"), customer_email=email(sheet.get(row, "customer email")),
                   subscription_id=sheet.get(row, "subscription", "subscription id"),
                   created=tp.parse(sheet.get(row, "date", "created", "invoice date", "created at")),
                   finalized=tp.parse(sheet.get(row, "finalized at", "finalized")), status=status,
                   amount_due=money(sheet.get(row, "amount due")), amount_paid=money(sheet.get(row, "amount paid")),
                   currency=(sheet.get(row, "currency") or "").lower() or None,
                   attempt_count=as_int(sheet.get(row, "attempt count")),
                   next_attempt=tp.parse(sheet.get(row, "next payment attempt")),
                   paid_at=tp.parse(sheet.get(row, "paid at", "paid date")))


def _invoice_json(r: Dict[str, Any], tp: TimeParser) -> Optional[Invoice]:
    if not r.get("id"):
        return None
    cur = (r.get("currency") or "").lower() or None
    sub = r.get("subscription")
    if not sub:
        parent = r.get("parent") or {}
        sub = ((parent.get("subscription_details") or {}).get("subscription")) if isinstance(parent, dict) else None
    sub = sub.get("id") if isinstance(sub, dict) else sub
    cust = r.get("customer")
    trans = r.get("status_transitions") or {}
    return Invoice(id=str(r["id"]), customer_id=cust.get("id") if isinstance(cust, dict) else cust,
                   customer_email=email(r.get("customer_email")), subscription_id=sub, created=tp.parse(r.get("created")),
                   finalized=tp.parse(trans.get("finalized_at")), status=_status(r.get("status")),
                   amount_due=_minor(r.get("amount_due"), cur), amount_paid=_minor(r.get("amount_paid"), cur), currency=cur,
                   attempt_count=as_int(r.get("attempt_count")), next_attempt=tp.parse(r.get("next_payment_attempt")),
                   paid_at=tp.parse(trans.get("paid_at")))


# ----------------------------------------------------------------------------------------------
# mapping
# ----------------------------------------------------------------------------------------------

def _mrr(s: Subscription) -> Tuple[Optional[Decimal], Optional[str]]:
    """(mrr, reason it is empty)."""
    if s.status not in MRR_STATUSES:
        return Decimal(0), None
    if s.list_mrr is None:
        return None, s.mrr_unknown or "unknown price"
    if s.discount_unknown:
        return None, "coupon"
    v = s.list_mrr * (s.discount_factor if s.discount_factor is not None else Decimal(1)) - s.discount_monthly
    return max(v, Decimal(0)).quantize(CENT, rounding=ROUND_HALF_UP), None


def _event(name: str, obj_id: str, user_id: str, at: datetime, props: Dict[str, Any]) -> Dict[str, Any]:
    return {"event_id": f"stripe:{name}:{obj_id}", "user_id": user_id, "anonymous_id": None, "event_name": name,
            "event_at": at, "properties": {k: v for k, v in props.items() if v is not None}}


def map(folder: Path, out_dir: Path) -> MappingReport:  # noqa: A001  (the shared mapper interface names it map)
    """Read the Stripe exports in `folder`, write canonical CSVs to `out_dir`, and say what was assumed."""
    folder, out_dir = Path(folder), Path(out_dir)
    report = MappingReport(source=NAME)
    tp = TimeParser()
    files = _classify(folder, load=True)
    customers: Dict[str, Customer] = {}
    subs: Dict[str, Subscription] = {}
    invoices: Dict[str, Invoice] = {}
    csv_seen = json_seen = False
    for kind, items in files.items():
        for path, payload in items:
            if isinstance(payload, Sheet):
                csv_seen = True
                for row in payload.rows:
                    rec = {"customer": _customer_csv, "subscription": _subscription_csv, "invoice": _invoice_csv}[kind](payload, row, tp)
                    if rec is not None:
                        {"customer": customers, "subscription": subs, "invoice": invoices}[kind].setdefault(rec.id, rec)
                report.unmapped_columns[rel(path, folder)] = payload.unused()
            else:
                json_seen = True
                for r in payload:
                    rec = {"customer": _customer_json, "subscription": _subscription_json, "invoice": _invoice_json}[kind](r, tp)
                    if rec is not None:
                        {"customer": customers, "subscription": subs, "invoice": invoices}[kind].setdefault(rec.id, rec)
    if not (customers or subs or invoices):
        report.warnings.append(f"no Stripe customers, subscriptions or invoices export found in {folder}")
        return report
    if csv_seen:
        report.assumptions.append("ASSUMED: Dashboard CSV amounts are in major currency units (49.00 means 49.00), and a "
                                  "subscription's Amount is the price of one unit, multiplied by Quantity for MRR.")
    if json_seen:
        report.assumptions.append("API JSON amounts are in the currency's smallest unit and were divided accordingly "
                                  "(100, or 1 for zero-decimal and 1000 for three-decimal currencies).")

    # identity: Stripe customer id -> the product's user id
    fallback_ids = 0
    uid_of: Dict[str, str] = {}
    for c in customers.values():
        meta_uid = _meta(c.metadata, USER_KEYS)
        uid_of[c.id] = meta_uid or c.id
        fallback_ids += 0 if meta_uid else 1
    if customers:
        if fallback_ids:
            report.assumptions.append(f"ASSUMED: {fallback_ids} of {len(customers)} customer(s) carry no user id in metadata "
                                      "(user_id, app_user_id, external_id); their Stripe customer id (cus_...) is the user_id, "
                                      "which will not join product events keyed on your own ids.")
        else:
            report.assumptions.append(f"user_id comes from customer metadata for all {len(customers)} customer(s).")

    def uid(customer_id: Optional[str]) -> Optional[str]:
        if not customer_id:
            return None
        return uid_of.get(customer_id, customer_id)

    orphan = sorted({s.customer_id for s in subs.values() if s.customer_id and s.customer_id not in customers} |
                    {i.customer_id for i in invoices.values() if i.customer_id and i.customer_id not in customers})
    if orphan and customers:
        report.warnings.append(f"{len(orphan)} customer id(s) appear on subscriptions or invoices but not in the customers export; "
                               "their Stripe customer id is used as user_id.")

    # accounts
    acct_of: Dict[str, str] = {}
    acct_name: Dict[str, str] = {}
    acct_created: Dict[str, datetime] = {}
    name_only = 0
    for c in sorted(customers.values(), key=lambda c: (iso(c.created), c.id)):
        aid = _meta(c.metadata, ACCOUNT_KEYS)
        name = c.business_name or _meta(c.metadata, ACCOUNT_NAME_KEYS)
        if not aid and name:
            aid = c.id
            name_only += 1
        if not aid:
            continue
        acct_of[c.id] = aid
        if name and aid not in acct_name:
            acct_name[aid] = name
        if c.created and (aid not in acct_created or c.created < acct_created[aid]):
            acct_created[aid] = c.created
    if name_only:
        report.assumptions.append(f"ASSUMED: {name_only} customer(s) have a business name but no account id in metadata; "
                                  "the customer id is used as the account id (one Stripe customer = one account).")

    def best(ss: List[Subscription]) -> Optional[Subscription]:
        """The subscription that describes the customer now: best status first, then the most recent start."""
        def key(s: Subscription) -> Tuple[int, float, str]:
            t = s.start or s.created
            return (STATUS_RANK.get(s.status or "", 9), -(t.timestamp() if t else 0.0), s.id)
        return min(ss, key=key) if ss else None

    subs_by_cust: Dict[str, List[Subscription]] = {}
    for s in subs.values():
        if s.customer_id:
            subs_by_cust.setdefault(s.customer_id, []).append(s)

    # users
    users: List[Dict[str, Any]] = []
    no_email = no_created = 0
    seen_uids: set = set()
    if customers:
        for c in sorted(customers.values(), key=lambda c: c.id):
            if not c.email:
                no_email += 1
                continue
            created = c.created
            if created is None:
                starts = [s.start or s.created for s in subs_by_cust.get(c.id, []) if (s.start or s.created)]
                created = min(starts) if starts else None
                if created is None:
                    no_created += 1
                    continue
            b = best(subs_by_cust.get(c.id, []))
            u = uid_of[c.id]
            if u in seen_uids:
                report.warnings.append(f"two Stripe customers map to user_id {u}; the first (by customer id) was kept")
                continue
            seen_uids.add(u)
            users.append({"user_id": u, "email": c.email, "created_at": created, "country": country_iso2(c.country),
                          "plan": b.plan if b else None, "account_id": acct_of.get(c.id), "phone": phone_e164(c.phone)})
    else:
        firsts: Dict[str, Subscription] = {}
        for s in sorted(subs.values(), key=lambda s: (iso(s.start or s.created), s.id)):
            if s.customer_id and s.customer_email and s.customer_id not in firsts:
                firsts[s.customer_id] = s
        for cid, s in sorted(firsts.items()):
            b = best(subs_by_cust.get(cid, []))
            users.append({"user_id": cid, "email": s.customer_email, "created_at": s.start or s.created,
                          "plan": b.plan if b else None})
        if users:
            report.assumptions.append(f"ASSUMED: no customers export; {len(users)} user(s) were built from the subscriptions "
                                      "export (Customer Email), keyed on the Stripe customer id, created at their first "
                                      "subscription start.")
    if no_email:
        report.warnings.append(f"{no_email} customer(s) without an email were left out of users (email is required).")
    if no_created:
        report.warnings.append(f"{no_created} customer(s) without a created time were left out of users.")

    # accounts rows
    accounts: List[Dict[str, Any]] = []
    for aid in sorted(set(acct_of.values())):
        members = [cid for cid, a in acct_of.items() if a == aid]
        b = best([s for cid in members for s in subs_by_cust.get(cid, [])])
        accounts.append({"account_id": aid, "name": acct_name.get(aid), "created_at": acct_created.get(aid),
                         "plan": b.plan if b else None, "status": b.status if b else None})
    if customers and not accounts:
        report.warnings.append("no accounts table: no customer carries an account id in metadata or a business name.")

    # subscriptions rows
    sub_rows: List[Dict[str, Any]] = []
    empty_mrr: Dict[str, int] = {}
    no_start = 0
    for s in sorted(subs.values(), key=lambda s: s.id):
        started = s.start or s.created
        if not started or not s.status:
            no_start += 1
            continue
        mrr, why = _mrr(s)
        if why:
            empty_mrr[why] = empty_mrr.get(why, 0) + 1
        sub_rows.append({"subscription_id": s.id, "user_id": uid(s.customer_id), "account_id": acct_of.get(s.customer_id or ""),
                         "plan": s.plan, "status": s.status, "started_at": started, "trial_end_at": s.trial_end,
                         "current_period_end_at": s.current_period_end, "canceled_at": s.canceled_at, "mrr": mrr,
                         "currency": (s.currency or "").upper() or None})
    if no_start:
        report.warnings.append(f"{no_start} subscription(s) without a start date or status were left out.")
    for why, n in sorted(empty_mrr.items()):
        if why == "coupon":
            report.assumptions.append(f"ASSUMED: {n} active or past_due subscription(s) carry a coupon whose value is not in the "
                                      "export; mrr was left empty rather than overstated.")
        else:
            report.warnings.append(f"{n} active or past_due subscription(s) have no computable MRR ({why}); mrr left empty.")
    if sub_rows:
        report.assumptions.append("mrr is 0.00 for every status except active and past_due, which is how Stripe counts MRR.")

    # invoices rows
    inv_rows: List[Dict[str, Any]] = []
    no_inv_created = paid_no_time = 0
    for i in sorted(invoices.values(), key=lambda i: i.id):
        if not i.created or not i.status:
            no_inv_created += 1
            continue
        if i.status == "paid" and i.paid_at is None:
            paid_no_time += 1
        inv_rows.append({"invoice_id": i.id, "subscription_id": i.subscription_id, "user_id": uid(i.customer_id),
                         "account_id": acct_of.get(i.customer_id or ""), "created_at": i.created, "status": i.status,
                         "amount_due": i.amount_due, "amount_paid": i.amount_paid, "currency": (i.currency or "").upper() or None,
                         "attempt_count": i.attempt_count, "next_payment_attempt_at": i.next_attempt, "paid_at": i.paid_at})
    if no_inv_created:
        report.warnings.append(f"{no_inv_created} invoice(s) without a created date or status were left out.")
    if paid_no_time:
        report.warnings.append(f"{paid_no_time} paid invoice(s) carry no paid time; paid_at is empty and no payment_succeeded "
                               "event was written for them.")

    # billing moments
    events: List[Dict[str, Any]] = []
    inv_by_sub: Dict[str, List[Invoice]] = {}
    for i in invoices.values():
        if i.subscription_id:
            inv_by_sub.setdefault(i.subscription_id, []).append(i)
    trial_from_start = started_fallback = 0
    for s in sorted(subs.values(), key=lambda s: s.id):
        u = uid(s.customer_id)
        if not u:
            continue
        mrr_at_start = None   # the price the subscription started on, discounted; unknown when a coupon's value is not exported
        if s.list_mrr is not None and not s.discount_unknown:
            v = s.list_mrr * (s.discount_factor if s.discount_factor is not None else Decimal(1)) - s.discount_monthly
            mrr_at_start = max(v, Decimal(0)).quantize(CENT, rounding=ROUND_HALF_UP)
        if s.trial_start or s.trial_end:
            at = s.trial_start or s.start or s.created
            if at:
                trial_from_start += 0 if s.trial_start else 1
                events.append(_event("trial_started", s.id, u, at, {"subscription_id": s.id, "plan": s.plan,
                                                                     "trial_end": iso(s.trial_end) or None}))
        paid = sorted((i for i in inv_by_sub.get(s.id, []) if i.status == "paid" and (i.amount_paid or 0) > 0 and i.paid_at),
                      key=lambda i: (i.paid_at, i.id))
        started_at = None
        if paid:
            started_at = paid[0].paid_at
        elif s.id not in inv_by_sub and s.status not in ("incomplete", "incomplete_expired"):
            if s.trial_end:
                ended_in_trial = s.ended_at is not None and s.ended_at <= s.trial_end
                if s.status != "trialing" and not ended_in_trial:
                    started_at = s.trial_end
            else:
                started_at = s.start or s.created
            started_fallback += 1 if started_at else 0
        if started_at:
            events.append(_event("subscription_started", s.id, u, started_at,
                                 {"subscription_id": s.id, "plan": s.plan, "interval": s.interval,
                                  "mrr": float(mrr_at_start) if mrr_at_start is not None else None,
                                  "currency": (s.currency or "").upper() or None, "trial": bool(s.trial_start or s.trial_end)}))
        if s.canceled_at:
            effective = s.ended_at or s.cancel_at or (s.current_period_end if s.cancel_at_period_end else None) or s.canceled_at
            events.append(_event("subscription_cancelled", s.id, u, s.canceled_at,
                                 {"subscription_id": s.id, "reason": FEEDBACK_TO_REASON.get(s.feedback or "", "unknown"),
                                  "effective_at": iso(effective), "cancellation_reason": s.reason,
                                  "cancel_at_period_end": s.cancel_at_period_end}))
    failed_dated = 0
    for i in sorted(invoices.values(), key=lambda i: i.id):
        u = uid(i.customer_id)
        if not u:
            continue
        attempts = i.attempt_count or 0
        failures = attempts - 1 if i.status == "paid" else attempts
        if failures >= 1 and i.status in ("paid", "open", "uncollectible", "void") and (i.amount_due or 0) > 0:
            at = i.finalized or i.created
            if at:
                failed_dated += 1
                events.append(_event("payment_failed", i.id, u, at, {
                    "invoice_id": i.id, "subscription_id": i.subscription_id, "amount_due": float(i.amount_due) if i.amount_due is not None else None,
                    "currency": (i.currency or "").upper() or None, "attempt_count": attempts,
                    "next_payment_attempt": iso(i.next_attempt) or None, "invoice_status": i.status,
                    "dated_at": "invoice_finalized" if i.finalized else "invoice_created"}))
        if i.status == "paid" and (i.amount_paid or 0) > 0 and i.paid_at:
            events.append(_event("payment_succeeded", i.id, u, i.paid_at, {
                "invoice_id": i.id, "subscription_id": i.subscription_id, "amount_paid": float(i.amount_paid),
                "currency": (i.currency or "").upper() or None, "attempt_count": attempts}))
    if trial_from_start:
        report.assumptions.append(f"ASSUMED: {trial_from_start} trial(s) have a trial end but no trial start; trial_started is "
                                  "dated at the subscription start.")
    if started_fallback:
        report.assumptions.append(f"ASSUMED: {started_fallback} subscription(s) have no invoice in the export; "
                                  "subscription_started is dated at the trial end (trial converted) or the start date.")
    if failed_dated:
        report.assumptions.append(f"ASSUMED: {failed_dated} payment_failed event(s): Stripe does not export the time of each "
                                  "attempt, so each is dated at its invoice's finalization (else creation) time, the earliest "
                                  "a failure can have happened; properties.attempt_count says how many attempts were made.")
    if subs and not invoices:
        report.warnings.append("no invoices export: payment_failed and payment_succeeded cannot be derived, and "
                               "subscription_started falls back to subscription dates.")

    report.assumptions += tp.notes("Stripe", "UTC (Dashboard columns are labelled UTC)")
    report.warnings += tp.warnings("Stripe")
    for table, rows, key in (("users", users, ["user_id"]), ("accounts", accounts, ["account_id"]),
                             ("subscriptions", sub_rows, ["subscription_id"]), ("invoices", inv_rows, ["invoice_id"]),
                             ("track_events", events, ["event_at", "event_id"])):
        if rows:
            report.tables[table] = write_table(out_dir, table, rows, key)
    return report
