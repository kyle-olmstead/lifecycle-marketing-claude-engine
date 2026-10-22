"""Shopify admin exports into the canonical tables: users, orders, line_items, products, discounts, refunds,
consent_events and track_events (order_completed, checkout_started).

Which files (all CSV, downloaded from the Shopify admin; put them in one folder, any file names):

  Customers export   Customers > Export > All customers > CSV. Columns verified against
                     https://help.shopify.com/en/manual/customers/import-export-customers : First Name, Last Name, Email,
                     Accepts Email Marketing, Default Address Company, Default Address Address1, Default Address Address2,
                     Default Address City, Default Address Province Code, Default Address Country Code, Default Address Zip,
                     Default Address Phone, Phone, Accepts SMS Marketing, Accepts WhatsApp Marketing, Total Spent,
                     Total Orders, Note, Tax Exempt, Tags. A "Customer ID" column, when an export has one, is used as the
                     user id (not listed on that page, so not relied on).
  Orders export      Orders > Export > Export orders > CSV. Columns verified against
                     https://help.shopify.com/en/manual/orders/export-orders : one row per line item; "many of the fields
                     are left blank to indicate that multiple items were purchased on the same order", so order-level
                     fields are read from the first row of each order that carries them. Rows of one order share Name.
  Abandoned checkouts (optional) Orders > Abandoned checkouts > Export, emailed as CSV. Shopify's help center does not
                     list its columns (a store owner describes the export at
                     https://community.shopify.com/t/how-can-i-extract-information-from-all-abandoned-checkouts/40958);
                     the mapper reads the order-export style columns it shares (Id, Email, Created at, Total, Currency,
                     Lineitem quantity, Lineitem name, Lineitem price, Lineitem SKU), so it is tolerant rather than exact.

What maps to what:
  users            one per customer and per email seen on an order or checkout. user_id is Customer ID when present,
                   else a stable hash of the email (export_io.email_user_id), the same id the other mappers use.
                   created_at is the first order or checkout (the customers export carries no dates).
  orders           order_id = Id (Name when Id is blank); order_at = Created at, converted to UTC (the export writes the
                   store's offset, '2026-03-15 14:22:10 -0500'); status from Financial Status and Canceled at;
                   gross = Subtotal + Discount Amount, net = Subtotal - Refunded Amount (shipping and taxes excluded),
                   once the data confirms Subtotal is after discounts (Subtotal + Shipping + Taxes = Total).
  line_items       one per line row; product_id = Lineitem SKU (the orders export has no product id), else the name.
  products         one per product_id seen on a line, with its latest name and price.
  discounts        one per code in Discount Code, with Discount Amount when there is one code.
  refunds          one per order with a Refunded Amount.
  consent_events   email consent from each order's Accepts Marketing (dated: the order time) and the customers export's
                   Accepts Email Marketing (current state, undated); see the assumptions below.
  track_events     order_completed per order, at the time it was placed (a later cancel or refund shows in orders.status
                   and refunds); checkout_started per abandoned checkout.

What it cannot know (each is recorded as an ASSUMED line in the mapping report when it applies):
  * when consent was captured: the customers export says yes or no, never when. An undated email yes is dated at the
    customer's earliest known activity (old, so any real later unsubscribe outranks it) with source import_undated; an
    undated no after an earlier yes is dated at the end of the export (late, so it outranks earlier grants). An
    undated SMS yes is not recorded as consent: SMS needs a dated record (TCPA, 10DLC).
  * when a refund happened: the orders export has the amount, not the date. Refunds are dated at Canceled at, else at
    the order time.
  * the discount type (percentage, fixed amount, free shipping): not in the export; left blank.
  * checkouts that completed without being abandoned: the abandoned checkouts export holds only abandoned ones, so
    checkout_started from Shopify alone undercounts started checkouts. Klaviyo's Checkout Started fills this in when
    both are imported together (lcm import auto).
  * product categories, URLs and images: not in these exports.

    python3 adapters/sources/cli.py shopify exports/shopify --out data/
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from adapters.sources.export_io import (UNDATED_SOURCE, Columns, TimeParser, apply_consent, email_norm, email_ok,
                                        email_user_id, fmt_money, fmt_ts, iter_files, jdump, money, norm, num,
                                        phone_e164, read_headers, read_rows, relative, slug, strip_id, write_tables, yes)
from adapters.sources.report import MappingReport
from engine.data.iso import country_code

NAME: str = "shopify"
DESCRIPTION: str = "Shopify admin exports: Customers CSV, Orders CSV (one row per line item), Abandoned checkouts CSV"

# Financial Status values (https://help.shopify.com/en/manual/orders/export-orders: "Whether the order has been paid,
# authorized, refunded, and so on") to the canonical status.
FINANCIAL_STATUS = {"paid": "completed", "partiallyrefunded": "completed", "partiallypaid": "completed",
                    "refunded": "refunded", "voided": "canceled", "expired": "canceled",
                    "pending": "pending", "authorized": "pending", "unpaid": "pending"}

ORDER_COLS = ("Name", "Id", "Email", "Phone", "Billing Phone", "Shipping Phone", "Financial Status", "Paid at",
              "Accepts Marketing", "Currency", "Subtotal", "Shipping", "Taxes", "Total", "Discount Code",
              "Discount Amount", "Created at", "Lineitem quantity", "Lineitem name", "Lineitem price", "Lineitem SKU",
              "Billing Name", "Billing Country", "Shipping Country", "Canceled at", "Cancelled at", "Refunded Amount",
              "Lineitem discount", "Vendor")


def _kind(path: Path, headers: List[str]) -> Optional[str]:
    n = {norm(h) for h in headers}
    has_lines = {"lineitemquantity", "lineitemname"} <= n
    if has_lines and (any("abandon" in h or "recover" in h for h in n) or "checkout" in path.name.lower()):
        return "checkouts"
    if has_lines and ("financialstatus" in n or "paidat" in n):
        return "orders"
    if has_lines:
        return "checkouts"
    if "email" in n and ("acceptsemailmarketing" in n or "defaultaddresscountrycode" in n
                         or {"acceptsmarketing", "totalorders"} <= n):
        return "customers"
    return None


def _files(folder: Path) -> Dict[str, List[Path]]:
    out: Dict[str, List[Path]] = defaultdict(list)
    for p in iter_files(folder, (".csv", ".csv.gz")):
        kind = _kind(p, read_headers(p))
        if kind:
            out[kind].append(p)
    return out


def detect(folder: Path) -> bool:
    """True when the folder holds a Shopify customers, orders or abandoned checkouts export."""
    return bool(_files(Path(folder)))


def _group(cols: Columns, rows: List[List[str]], key_aliases: Tuple[str, ...]) -> List[List[List[str]]]:
    """Rows grouped into orders (or checkouts): a new group starts when the key changes; a row with a blank key continues
    the previous group, which is how the export writes the extra line items of one order."""
    groups: List[List[List[str]]] = []
    last = None
    for r in rows:
        key = cols.get(r, *key_aliases)
        if groups and (not key or key == last):
            groups[-1].append(r)
        else:
            groups.append([r])
            last = key
    return groups


def _first(cols: Columns, group: List[List[str]], *aliases: str) -> str:
    for r in group:
        v = cols.get(r, *aliases)
        if v:
            return v
    return ""


def _items(cols: Columns, group: List[List[str]]) -> List[Dict[str, Any]]:
    items = []
    for r in group:
        name = cols.get(r, "Lineitem name")
        qty = cols.get(r, "Lineitem quantity")
        if not name and not qty:
            continue
        sku = cols.get(r, "Lineitem SKU", "Lineitem sku")
        try:
            quantity = int(Decimal(qty or "1"))
        except Exception:  # noqa: BLE001
            quantity = 1
        price = money(cols.get(r, "Lineitem price"))
        discount = money(cols.get(r, "Lineitem discount")) or Decimal("0.00")
        items.append({"product_id": sku or slug(name) or "unknown", "sku": sku, "name": name, "quantity": quantity,
                      "price": price, "discount": discount})
    return items


def map(folder: Path, out_dir: Path) -> MappingReport:  # noqa: A001 (the mapper interface name)
    """Read the Shopify exports under `folder` and write canonical CSVs into `out_dir`."""
    folder, out_dir = Path(folder), Path(out_dir)
    report = MappingReport(NAME)
    files = _files(folder)
    ts = TimeParser()

    # ---- orders -------------------------------------------------------------------------------------------------
    orders: List[Dict[str, Any]] = []
    for path in files.get("orders", []):
        headers, rows = read_rows(path)
        cols = Columns(headers)
        for group in _group(cols, rows, ("Name", "Id")):
            created = ts(_first(cols, group, "Created at"))
            email = email_norm(_first(cols, group, "Email"))
            order_id = strip_id(_first(cols, group, "Id")) or _first(cols, group, "Name")
            if not order_id:
                continue
            orders.append({
                "order_id": order_id, "name": _first(cols, group, "Name"), "email": email, "created": created,
                "financial": norm(_first(cols, group, "Financial Status")),
                "canceled": ts(_first(cols, group, "Canceled at", "Cancelled at")),
                "accepts": yes(_first(cols, group, "Accepts Marketing")),
                "currency": _first(cols, group, "Currency").upper(),
                "subtotal": money(_first(cols, group, "Subtotal")), "shipping": money(_first(cols, group, "Shipping")),
                "taxes": money(_first(cols, group, "Taxes")), "total": money(_first(cols, group, "Total")),
                "codes": [c.strip() for c in _first(cols, group, "Discount Code").split(",") if c.strip()],
                "discount": money(_first(cols, group, "Discount Amount")),
                "refunded": money(_first(cols, group, "Refunded Amount")),
                "phone": _first(cols, group, "Phone", "Billing Phone", "Shipping Phone"),
                "country": country_code(_first(cols, group, "Billing Country", "Shipping Country")),
                "items": _items(cols, group),
            })
        report.unmapped_columns[relative(path, folder)] = cols.unmapped()

    # Is Subtotal before or after discounts? Decide from the data, not from memory.
    after = before = 0
    for o in orders:
        if None in (o["subtotal"], o["total"]) or not o["discount"]:
            continue
        rest = (o["shipping"] or 0) + (o["taxes"] or 0)
        if abs(o["subtotal"] + rest - o["total"]) <= Decimal("0.02"):
            after += 1
        elif abs(o["subtotal"] - o["discount"] + rest - o["total"]) <= Decimal("0.02"):
            before += 1
    subtotal_after_discount = after >= before
    if after or before:
        report.assumptions.append(
            f"Subtotal read as {'after' if subtotal_after_discount else 'before'} discounts: on {max(after, before):,} of "
            f"{after + before:,} discounted orders Subtotal {'+' if subtotal_after_discount else '- Discount Amount +'} "
            f"Shipping + Taxes equals Total. gross_amount = Subtotal{' + Discount Amount' if subtotal_after_discount else ''}, "
            "net_amount = gross - discount - Refunded Amount (shipping and taxes excluded, floored at 0).")
    else:
        report.assumptions.append("Subtotal read as after discounts (no discounted order to confirm it on): gross_amount = "
                                  "Subtotal + Discount Amount, net_amount = Subtotal - Refunded Amount.")

    # ---- abandoned checkouts --------------------------------------------------------------------------------------
    checkouts: List[Dict[str, Any]] = []
    for path in files.get("checkouts", []):
        headers, rows = read_rows(path)
        cols = Columns(headers)
        for group in _group(cols, rows, ("Id", "Name", "Checkout ID")):
            checkout_id = strip_id(_first(cols, group, "Id", "Checkout ID")) or _first(cols, group, "Name")
            created = ts(_first(cols, group, "Created at", "Started at"))
            if not checkout_id or not created:
                continue
            checkouts.append({"checkout_id": checkout_id, "email": email_norm(_first(cols, group, "Email")),
                              "created": created, "currency": _first(cols, group, "Currency").upper(),
                              "total": money(_first(cols, group, "Total", "Subtotal")),
                              "phone": _first(cols, group, "Phone", "Billing Phone"),
                              "country": country_code(_first(cols, group, "Billing Country", "Shipping Country")),
                              "accepts": yes(_first(cols, group, "Accepts Marketing")), "items": _items(cols, group)})
        report.unmapped_columns[relative(path, folder)] = cols.unmapped()
    if checkouts:
        report.assumptions.append(
            "checkout_started comes from the abandoned checkouts export only, so it covers abandoned checkouts; a checkout "
            "that completed without being abandoned has no checkout_started from Shopify (import Klaviyo's Checkout Started "
            "with lcm import auto to cover them). The abandoned checkouts export columns are not documented by Shopify; "
            "they were read by their order-export names.")

    # ---- customers --------------------------------------------------------------------------------------------------
    customers: Dict[str, Dict[str, Any]] = {}
    no_email = 0
    for path in files.get("customers", []):
        headers, rows = read_rows(path)
        cols = Columns(headers)
        for r in rows:
            email = email_norm(cols.get(r, "Email"))
            if not email:
                no_email += 1
                continue
            country = country_code(cols.get(r, "Default Address Country Code", "Country Code", "Country"))
            customers[email] = {
                "customer_id": strip_id(cols.get(r, "Customer ID", "Id")),
                "country": country,
                "phone": phone_e164(cols.get(r, "Phone", "Default Address Phone"), country),
                "email_yes": yes(cols.get(r, "Accepts Email Marketing", "Accepts Marketing")),
                "sms_yes": yes(cols.get(r, "Accepts SMS Marketing")),
            }
        report.unmapped_columns[relative(path, folder)] = cols.unmapped()
    if no_email:
        report.warnings.append(f"{no_email:,} customer row(s) have no email and were left out (users.email is required).")
    if files.get("customers") and not any(c["customer_id"] for c in customers.values()):
        report.assumptions.append("The customers export has no Customer ID column, so user_id is a stable hash of the "
                                  "lower-cased email (em_...), the same id the other mappers make for that address.")
    if files.get("customers"):
        report.assumptions.append("First Name, Last Name and the address lines are not imported: the engine needs none of "
                                  "them, and leaving them out keeps personal data out of the canonical tables.")

    # ---- users --------------------------------------------------------------------------------------------------
    all_times = [o["created"] for o in orders] + [c["created"] for c in checkouts]
    start = min((t for t in all_times if t), default=None)
    end = max((t for t in all_times if t), default=None)
    users: Dict[str, Dict[str, Any]] = {}
    email_to_uid: Dict[str, str] = {}

    def uid_for(email: str) -> str:
        if email in email_to_uid:
            return email_to_uid[email]
        cust = customers.get(email) or {}
        uid = cust.get("customer_id") or email_user_id(email)
        email_to_uid[email] = uid
        users[uid] = {"user_id": uid, "email": email, "first": None, "country": cust.get("country") or "",
                      "phone": cust.get("phone") or "", "email_ok": email_ok(email)}
        return uid

    for email in sorted(customers):
        uid_for(email)
    guests = 0
    for rec in orders + checkouts:
        if rec["email"]:
            uid = uid_for(rec["email"])
        else:
            guests += 1
            uid = f"shopify_guest_{slug(rec.get('order_id') or rec.get('checkout_id'))}"
            users.setdefault(uid, {"user_id": uid, "email": "", "first": None, "country": "", "phone": "", "email_ok": False})
        rec["user_id"] = uid
        u = users[uid]
        if rec["created"] and (u["first"] is None or rec["created"] < u["first"]):
            u["first"] = rec["created"]
        if not u["country"] and rec.get("country"):
            u["country"] = rec["country"]
        if not u["phone"] and rec.get("phone"):
            u["phone"] = phone_e164(rec["phone"], u["country"] or None)
    if guests:
        report.warnings.append(f"{guests:,} order(s) or checkout(s) have no email; each got a user without an email "
                               "(shopify_guest_<id>) so its revenue is kept. check-data lists them.")
    undated_users = [u for u in users.values() if u["first"] is None]
    if undated_users:
        if start is not None:
            report.assumptions.append(
                f"ASSUMED: {len(undated_users):,} customer(s) with no order or checkout have no date in any export; their "
                f"created_at is the start of the export window ({fmt_ts(start)}), so none of them looks newly signed up.")
        else:
            report.warnings.append(f"{len(undated_users):,} customer(s) have no date anywhere (no orders export): "
                                   "created_at is empty; add the orders export.")
        for u in undated_users:
            u["first"] = start
        # an optional extension of the mapping report: values the export did not carry, so a merge prefers real ones
        report.assumed_values = {"users.created_at": sorted(u["user_id"] for u in undated_users)}  # type: ignore[attr-defined]

    # ---- consent ----------------------------------------------------------------------------------------------------
    consent: List[Dict[str, str]] = []
    dated_grants = undated_grants = late_grants = late_revokes = sms_undated = 0
    by_user_orders: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for rec in orders + checkouts:
        if rec["created"] and rec["accepts"] is not None:
            by_user_orders[rec["user_id"]].append(rec)
    state_from_orders: Dict[str, Optional[bool]] = {}
    for uid, its in by_user_orders.items():
        state = None
        for o in sorted(its, key=lambda x: (x["created"], x.get("order_id") or x.get("checkout_id"))):
            if o["accepts"] is True and state is not True:
                consent.append({"user_id": uid, "channel": "email", "status": "granted", "purpose": "marketing",
                                "source": "checkout", "occurred_at": fmt_ts(o["created"])})
                dated_grants += 1
                state = True
            elif o["accepts"] is False and state is True:
                consent.append({"user_id": uid, "channel": "email", "status": "revoked", "purpose": "marketing",
                                "source": "checkout", "occurred_at": fmt_ts(o["created"])})
                state = False
        state_from_orders[uid] = state
    for email, cust in customers.items():
        uid = email_to_uid[email]
        u = users[uid]
        now_yes = cust["email_yes"]
        seen = state_from_orders.get(uid)
        if now_yes is True and seen is None and u["first"]:
            consent.append({"user_id": uid, "channel": "email", "status": "granted", "purpose": "marketing",
                            "source": UNDATED_SOURCE, "occurred_at": fmt_ts(u["first"])})
            undated_grants += 1
        elif now_yes is True and seen is False and end:
            # opted out on a later order, subscribed again since, at an unknown time: dated when the export shows it
            consent.append({"user_id": uid, "channel": "email", "status": "granted", "purpose": "marketing",
                            "source": UNDATED_SOURCE, "occurred_at": fmt_ts(end)})
            late_grants += 1
        elif now_yes is False and seen is True and end:
            consent.append({"user_id": uid, "channel": "email", "status": "revoked", "purpose": "marketing",
                            "source": UNDATED_SOURCE, "occurred_at": fmt_ts(end)})
            late_revokes += 1
        if cust["sms_yes"] is True:
            sms_undated += 1
    if dated_grants:
        report.assumptions.append(f"{dated_grants:,} email consent grant(s) dated at the order or checkout whose Accepts "
                                  "Marketing was yes (source checkout), read as captured at that checkout: the earliest "
                                  "time the export shows the customer opted in. A later order with no is read as a revoke.")
    if undated_grants:
        report.assumptions.append(
            f"ASSUMED: {undated_grants:,} customer(s) have Accepts Email Marketing = yes with no dated evidence; consent is "
            "dated at their earliest known activity (source import_undated). Dating it early means any real, later "
            "unsubscribe from another source wins. Replace with real capture times when you have them.")
    if late_grants:
        report.assumptions.append(
            f"ASSUMED: {late_grants:,} customer(s) said no on a later order but have Accepts Email Marketing = yes now, so they "
            f"opted in again at an unknown time; that grant is dated at the end of the export ({fmt_ts(end)}, source "
            "import_undated), when the export shows it.")
    if late_revokes:
        report.assumptions.append(
            f"ASSUMED: {late_revokes:,} customer(s) accepted marketing on an order but have Accepts Email Marketing = no "
            f"now; the revoke is dated at the end of the export ({fmt_ts(end)}, source import_undated) so it outranks the "
            "earlier grant.")
    if sms_undated:
        report.warnings.append(
            f"{sms_undated:,} customer(s) have Accepts SMS Marketing = yes, but the export does not say when they opted in. "
            "SMS consent needs a dated record (TCPA, 10DLC), so they were NOT marked SMS-consented. Import SMS consent with "
            "timestamps (Klaviyo's SMS Marketing Consent Timestamp, or your SMS tool) to enable SMS for them.")

    # ---- canonical rows ---------------------------------------------------------------------------------------------
    order_rows, line_rows, discount_rows, refund_rows, event_rows = [], [], [], [], []
    products: Dict[str, Dict[str, Any]] = {}
    refund_undated = 0
    first_order: Dict[str, Tuple[datetime, str]] = {}
    for o in orders:
        status = "canceled" if o["canceled"] else FINANCIAL_STATUS.get(o["financial"], "pending")
        o["status"] = status
        if status not in ("canceled", "pending") and o["created"]:
            key = (o["created"], o["order_id"])
            if o["user_id"] not in first_order or key < first_order[o["user_id"]]:
                first_order[o["user_id"]] = key
    unknown_status = sorted({o["financial"] for o in orders if o["financial"] and o["financial"] not in FINANCIAL_STATUS})
    if unknown_status:
        report.warnings.append("Financial Status value(s) not recognized, read as pending (not revenue): " + ", ".join(unknown_status))
    for o in orders:
        if not o["created"]:
            report.warnings.append(f"order {o['order_id']} has no readable Created at and was left out")
            continue
        discount = o["discount"] or Decimal("0.00")
        subtotal = o["subtotal"] if o["subtotal"] is not None else sum((i["price"] or 0) * i["quantity"] for i in o["items"]) - discount
        gross = subtotal + discount if subtotal_after_discount else subtotal
        refunded = o["refunded"] or Decimal("0.00")
        net = max(gross - discount - refunded, Decimal("0.00"))
        order_rows.append({"order_id": o["order_id"], "user_id": o["user_id"], "order_at": fmt_ts(o["created"]),
                           "status": o["status"], "gross_amount": fmt_money(gross), "discount_amount": fmt_money(discount),
                           "net_amount": fmt_money(net), "currency": o["currency"]})
        for n, it in enumerate(o["items"], 1):
            line_amount = (it["price"] * it["quantity"] - it["discount"]) if it["price"] is not None else None
            line_rows.append({"order_id": o["order_id"], "line_id": f"{o['order_id']}-{n}", "product_id": it["product_id"],
                              "sku": it["sku"], "product_name": it["name"], "quantity": str(it["quantity"]),
                              "unit_price": fmt_money(it["price"]), "line_amount": fmt_money(line_amount)})
            p = products.setdefault(it["product_id"], {"product_id": it["product_id"], "at": None})
            if p["at"] is None or o["created"] >= p["at"]:
                p.update(at=o["created"], product_name=it["name"], price=fmt_money(it["price"]))
        for code in o["codes"]:
            discount_rows.append({"order_id": o["order_id"], "code": code,
                                  "amount": fmt_money(o["discount"]) if len(o["codes"]) == 1 else ""})
        if refunded > 0:
            refund_undated += 1
            refund_rows.append({"refund_id": f"{o['order_id']}-refund", "order_id": o["order_id"], "user_id": o["user_id"],
                                "refunded_at": fmt_ts(o["canceled"] or o["created"]), "amount": fmt_money(refunded)})
        props = {"order_id": o["order_id"], "net_amount": num(gross - discount), "currency": o["currency"],
                 "items": [{"product_id": i["product_id"], "quantity": i["quantity"], "price": num(i["price"])} for i in o["items"]],
                 "is_first_order": first_order.get(o["user_id"], (None, None))[1] == o["order_id"]}
        if o["codes"]:
            props["discount_codes"] = o["codes"]
        event_rows.append({"event_id": f"shopify-order-{o['order_id']}", "user_id": o["user_id"], "event_name": "order_completed",
                           "event_at": fmt_ts(o["created"]), "properties": jdump(props)})
    for c in checkouts:
        value = c["total"] if c["total"] is not None else sum((i["price"] or 0) * i["quantity"] for i in c["items"])
        props = {"checkout_id": c["checkout_id"], "value": num(value), "currency": c["currency"],
                 "items": [{"product_id": i["product_id"], "quantity": i["quantity"], "price": num(i["price"])} for i in c["items"]]}
        event_rows.append({"event_id": f"shopify-checkout-{c['checkout_id']}", "user_id": c["user_id"],
                           "event_name": "checkout_started", "event_at": fmt_ts(c["created"]), "properties": jdump(props)})
    multi = sum(1 for o in orders if len(o["codes"]) > 1)
    if discount_rows:
        report.assumptions.append("discount_type is left blank: the orders export names the code but not whether it is a "
                                  "percentage, a fixed amount or free shipping." +
                                  (f" {multi:,} order(s) carry several codes; their per-code amount is unknown and left blank." if multi else ""))
    if refund_undated:
        report.assumptions.append(f"ASSUMED: {refund_undated:,} refund(s) have an amount but no date in the orders export; "
                                  "each is dated at the order's Canceled at, else at the order time, so revenue is never "
                                  "counted in a period after its refund.")
    if products:
        report.assumptions.append("product_id is the Lineitem SKU (the orders export carries no product id), else the line "
                                  "item name; category, URL and image are not in these exports.")

    user_rows = [{"user_id": u["user_id"], "email": u["email"], "created_at": fmt_ts(u["first"]), "country": u["country"],
                  "phone": u["phone"]} for u in users.values()]
    apply_consent(user_rows, consent)
    if ts.naive:
        report.assumptions.append(f"ASSUMED: {ts.naive:,} timestamp(s) carried no UTC offset and were read as UTC.")
    if ts.failed:
        report.warnings.append(f"{ts.failed:,} timestamp(s) could not be read (for example {', '.join(ts.failed_examples)}); "
                               "those rows have no date.")
    if not files:
        report.warnings.append(f"no Shopify export found under {folder}")

    product_rows = [{"product_id": p["product_id"], "product_name": p.get("product_name", ""), "price": p.get("price", "")}
                    for p in products.values()]
    report.tables = write_tables(out_dir, {"users": user_rows, "orders": order_rows, "line_items": line_rows,
                                           "products": product_rows, "discounts": discount_rows, "refunds": refund_rows,
                                           "consent_events": consent, "track_events": event_rows})
    report.unmapped_columns = {k: v for k, v in report.unmapped_columns.items() if v}
    return report
