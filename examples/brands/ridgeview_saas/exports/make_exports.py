"""
Deterministic sample vendor exports for Ridgeview, the fictional B2B SaaS demo, one folder per source.

Nothing here is real: users u_0001 to u_0010 with addresses at the reserved .test domain, names "User 0001",
fictional workspaces, documentation-range IP addresses (203.0.113.0/24) and the reserved example domain
ridgeview.example. The same ten people appear in every export, with the sign-up time, country and plan they have
in ../data/users.csv and the product events they have in ../data/track_events.csv, so each export is a view of one
consistent world:

  stripe/       customers.csv, subscriptions.csv, invoices.csv   Dashboard-style CSV exports
  hubspot/      all-contacts.csv                                 contacts export with subscription columns
  customerio/   people.csv, deliveries.csv                       people export and Deliveries & Drafts export
  ga4/          events-000000000000.ndjson                       a newline-delimited JSON dump of events_YYYYMMDD rows
  rudderstack/  tracks.csv, identifies.csv, <event>.csv          warehouse tables dumped to CSV
  segment/      tracks.csv, identifies.csv, users.csv, <event>.csv

The GA4, RudderStack and Segment dumps are three alternative instrumentations of the same product events (a company
runs one of them), so import one of them alongside Stripe, HubSpot and Customer.io rather than all three: Segment and
RudderStack share event ids and would merge, but GA4 has its own ids and would count every event twice.

The billing story (Stripe) as of 2026-10-19: u_0002 converted from a trial and had one failed payment that a retry
recovered; u_0003 is past due; u_0004 asked to cancel at the end of an annual term; u_0005 pays with a coupon; u_0006
cancelled during the trial; u_0007 pays monthly in CAD; u_0009 churned after four failed attempts; u_0010 is trialing;
u_0001 is a free customer and u_0008 never reached Stripe.

    python3 examples/brands/ridgeview_saas/exports/make_exports.py     # rewrites every file under exports/
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
USERS = [f"u_{i:04d}" for i in range(1, 11)]
UTC = timezone.utc
COUNTRY_NAME = {"US": "United States", "DE": "Germany", "CA": "Canada", "GB": "United Kingdom", "AU": "Australia"}
TIMEZONE = {"US": "America/New_York", "DE": "Europe/Berlin", "CA": "America/Toronto", "GB": "Europe/London", "AU": "Australia/Sydney"}
TITLE = {"page_viewed": "Page Viewed", "signed_up": "Signed Up", "session_started": "Session Started", "feature_used": "Feature Used",
         "onboarding_step_completed": "Onboarding Step Completed", "activation_reached": "Activation Reached",
         "order_completed": "Order Completed"}


def ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC) if len(s) > 16 else \
        datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)


def dash(dt: Optional[datetime]) -> str:
    """Stripe and HubSpot style: 2026-04-21 02:00."""
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


def zulu(dt: Optional[datetime]) -> str:
    """Warehouse style: 2026-04-21T02:00:00.000Z."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z") if dt else ""


def unix(dt: Optional[datetime]) -> str:
    return str(int(dt.timestamp())) if dt else ""


def rfc(dt: Optional[datetime]) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


def write(path: Path, header: List[str], rows: List[Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=header, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in header})


def load():
    users = {r["user_id"]: r for r in csv.DictReader((DATA / "users.csv").open(encoding="utf-8")) if r["user_id"] in USERS}
    anons = {f"anon_{u[2:]}" for u in USERS}
    events = [r for r in csv.DictReader((DATA / "track_events.csv").open(encoding="utf-8"))
              if r["user_id"] in USERS or (not r["user_id"] and r["anonymous_id"] in anons)]
    return users, events


# ----------------------------------------------------------------------------------------------
# Stripe
# ----------------------------------------------------------------------------------------------

def add_months(dt: datetime, n: int) -> datetime:
    m = dt.month - 1 + n
    return dt.replace(year=dt.year + m // 12, month=m % 12 + 1)


SUBS = [
    # user, product, price id, unit amount, qty, interval, currency, start, trial days, status, extras
    dict(u="u_0002", product="Ridgeview Growth", plan="price_rv_growth_monthly", amount="99.00", qty=3, interval="month", cur="usd",
         trial=14, status="active", cycles=10, failed_once={6}),
    dict(u="u_0003", product="Ridgeview Starter", plan="price_rv_starter_monthly", amount="29.00", qty=1, interval="month", cur="usd",
         trial=0, status="past_due", cycles=6, open_last=True),
    dict(u="u_0004", product="Ridgeview Starter", plan="price_rv_starter_annual_eur", amount="290.00", qty=1, interval="year", cur="eur",
         trial=0, status="active", cycles=1, cancel_requested="2026-09-15 10:00", feedback="too_expensive"),
    dict(u="u_0005", product="Ridgeview Starter", plan="price_rv_starter_monthly", amount="29.00", qty=2, interval="month", cur="usd",
         trial=14, status="active", cycles=9, coupon="LAUNCH20"),
    dict(u="u_0006", product="Ridgeview Starter", plan="price_rv_starter_monthly", amount="29.00", qty=1, interval="month", cur="usd",
         trial=14, status="canceled", cycles=0, cancel_now="2026-03-15 09:00", feedback="unused"),
    dict(u="u_0007", product="Ridgeview Scale", plan="price_rv_scale_monthly_cad", amount="499.00", qty=1, interval="month", cur="cad",
         trial=0, status="active", cycles=10),
    dict(u="u_0009", product="Ridgeview Starter", plan="price_rv_starter_monthly_cad", amount="39.00", qty=1, interval="month", cur="cad",
         trial=0, status="canceled", cycles=2, dunning_churn="2026-10-18 05:00"),
    dict(u="u_0010", product="Ridgeview Starter", plan="price_rv_starter_monthly", amount="29.00", qty=1, interval="month", cur="usd",
         trial=14, status="trialing", cycles=0),
]


def stripe(users: Dict[str, Dict[str, str]]) -> None:
    out = HERE / "stripe"
    customers = []
    for u in ["u_0001"] + [s["u"] for s in SUBS]:
        n = u[2:]
        paying = u != "u_0001"
        customers.append({"id": f"cus_{n}", "Description": "Ridgeview workspace owner" if paying else "Ridgeview free user",
                          "Email": users[u]["email"], "Name": f"User {n}", "Created (UTC)": dash(ts(users[u]["created_at"])),
                          "Delinquent": "true" if u in ("u_0003", "u_0009") else "false", "Currency": "",
                          "Address Country": users[u]["country"], "user_id (metadata)": u,
                          "account_id (metadata)": f"acc_{n}" if paying else "",
                          "account_name (metadata)": f"Workspace {n}" if paying else ""})
    write(out / "customers.csv", ["id", "Description", "Email", "Name", "Created (UTC)", "Delinquent", "Currency", "Address Country",
                                  "user_id (metadata)", "account_id (metadata)", "account_name (metadata)"], customers)

    subs, invoices = [], []
    for s in SUBS:
        u, n = s["u"], s["u"][2:]
        start = ts(users[u]["created_at"])
        trial_end = start + timedelta(days=s["trial"]) if s["trial"] else None
        first_bill = trial_end or start
        step = 12 if s["interval"] == "year" else 1
        unit = float(s["amount"])
        due = round(unit * s["qty"] * (0.8 if s.get("coupon") else 1.0), 2)
        canceled_at = ended_at = cancel_at = None
        cancel_at_period_end = "false"
        reason = ""
        if trial_end:   # Stripe issues a 0.00 invoice when a trial starts
            invoices.append({"id": f"in_{n}_00", "Number": f"RV-{n}-0000", "Customer": f"cus_{n}", "Customer Email": users[u]["email"],
                             "Subscription": f"sub_{n}", "Status": "paid", "Billing Reason": "subscription_create", "Currency": s["cur"],
                             "Amount Due": "0.00", "Amount Paid": "0.00", "Amount Remaining": "0.00", "Attempt Count": "0",
                             "Attempted": "true", "Date (UTC)": dash(start), "Finalized At (UTC)": dash(start), "Paid At (UTC)": dash(start),
                             "Next Payment Attempt (UTC)": ""})
        for k in range(s["cycles"]):
            created = add_months(first_bill, k * step)
            row = {"id": f"in_{n}_{k + 1:02d}", "Number": f"RV-{n}-{k + 1:04d}", "Customer": f"cus_{n}", "Customer Email": users[u]["email"],
                   "Subscription": f"sub_{n}", "Status": "paid", "Billing Reason": "subscription_create" if k == 0 and not trial_end else "subscription_cycle",
                   "Currency": s["cur"], "Amount Due": f"{due:.2f}", "Amount Paid": f"{due:.2f}", "Amount Remaining": "0.00",
                   "Attempt Count": "1", "Attempted": "true", "Date (UTC)": dash(created), "Finalized At (UTC)": dash(created),
                   "Paid At (UTC)": dash(created + timedelta(hours=1)), "Next Payment Attempt (UTC)": ""}
            if k + 1 in s.get("failed_once", set()):
                row.update({"Attempt Count": "2", "Paid At (UTC)": dash(created + timedelta(days=3, hours=2))})
            last = k == s["cycles"] - 1
            if last and s.get("open_last"):
                row.update({"Status": "open", "Amount Paid": "0.00", "Amount Remaining": f"{due:.2f}", "Attempt Count": "3",
                            "Paid At (UTC)": "", "Next Payment Attempt (UTC)": dash(created + timedelta(days=31))})
            if last and s.get("dunning_churn"):
                row.update({"Status": "uncollectible", "Amount Paid": "0.00", "Amount Remaining": f"{due:.2f}", "Attempt Count": "4",
                            "Paid At (UTC)": ""})
            invoices.append(row)
        period_start = add_months(first_bill, (max(s["cycles"], 1) - 1) * step) if s["cycles"] else start
        period_end = add_months(period_start, step) if s["cycles"] else (trial_end or add_months(start, step))
        if s.get("cancel_requested"):
            canceled_at, cancel_at, cancel_at_period_end, reason = ts(s["cancel_requested"]), period_end, "true", "cancellation_requested"
        if s.get("cancel_now"):
            canceled_at = ended_at = ts(s["cancel_now"])
            reason = "cancellation_requested"
        if s.get("dunning_churn"):
            canceled_at = ended_at = ts(s["dunning_churn"])
            reason = "payment_failed"
        subs.append({"id": f"sub_{n}", "Customer ID": f"cus_{n}", "Customer Email": users[u]["email"], "Customer Name": f"User {n}",
                     "Plan": s["plan"], "Product": s["product"], "Quantity": s["qty"], "Interval": s["interval"], "Amount": s["amount"],
                     "Currency": s["cur"], "Status": s["status"], "Created (UTC)": dash(start), "Start Date (UTC)": dash(start),
                     "Current Period Start (UTC)": dash(period_start if s["cycles"] else start),
                     "Current Period End (UTC)": dash(period_end), "Trial Start (UTC)": dash(start) if trial_end else "",
                     "Trial End (UTC)": dash(trial_end), "Canceled At (UTC)": dash(canceled_at), "Cancel At Period End": cancel_at_period_end,
                     "Cancel At (UTC)": dash(cancel_at), "Ended At (UTC)": dash(ended_at), "Coupon": s.get("coupon", ""),
                     "Cancellation Reason": reason, "Cancellation Feedback": s.get("feedback", "")})
    write(out / "subscriptions.csv", ["id", "Customer ID", "Customer Email", "Customer Name", "Plan", "Product", "Quantity", "Interval",
                                      "Amount", "Currency", "Status", "Created (UTC)", "Start Date (UTC)", "Current Period Start (UTC)",
                                      "Current Period End (UTC)", "Trial Start (UTC)", "Trial End (UTC)", "Canceled At (UTC)",
                                      "Cancel At Period End", "Cancel At (UTC)", "Ended At (UTC)", "Coupon", "Cancellation Reason",
                                      "Cancellation Feedback"], subs)
    write(out / "invoices.csv", ["id", "Number", "Customer", "Customer Email", "Subscription", "Status", "Billing Reason", "Currency",
                                 "Amount Due", "Amount Paid", "Amount Remaining", "Attempt Count", "Attempted", "Date (UTC)",
                                 "Finalized At (UTC)", "Paid At (UTC)", "Next Payment Attempt (UTC)"], invoices)


# ----------------------------------------------------------------------------------------------
# HubSpot
# ----------------------------------------------------------------------------------------------

STAGE = {"u_0001": "Lead", "u_0002": "Customer", "u_0003": "Customer", "u_0004": "Customer", "u_0005": "Customer", "u_0006": "Lead",
         "u_0007": "Customer", "u_0008": "Lead", "u_0009": "Customer", "u_0010": "Opportunity"}


def hubspot(users: Dict[str, Dict[str, str]]) -> None:
    rows = []
    for u in USERS:
        n = u[2:]
        modified = {"u_0006": "2026-06-01 10:30", "u_0004": "2026-07-10 08:00"}.get(u, "2026-10-18 06:00")
        rows.append({"Record ID": str(5100000 + int(n)), "First Name": "User", "Last Name": n, "Email": users[u]["email"],
                     "Create Date": dash(ts(users[u]["created_at"])), "Last Modified Date": modified, "Lifecycle Stage": STAGE[u],
                     "Country/Region": COUNTRY_NAME[users[u]["country"]],
                     "Marketing contact status": "Non-marketing contact" if u == "u_0009" else "Marketing contact",
                     "Unsubscribed from all email": "true" if u == "u_0006" else "false",
                     "Opted out of email: Newsletter": "true" if u == "u_0004" else "false",
                     "Opted out of email: Product Updates": "false", "User ID": u, "Associated Company": f"Workspace {n}"})
    write(HERE / "hubspot" / "all-contacts.csv",
          ["Record ID", "First Name", "Last Name", "Email", "Create Date", "Last Modified Date", "Lifecycle Stage", "Country/Region",
           "Marketing contact status", "Unsubscribed from all email", "Opted out of email: Newsletter",
           "Opted out of email: Product Updates", "User ID", "Associated Company"], rows)


# ----------------------------------------------------------------------------------------------
# Customer.io
# ----------------------------------------------------------------------------------------------

DELIVERY_COLUMNS = ["id", "campaign_id", "campaign_name", "newsletter_id", "newsletter_name", "template_id", "template_name", "channel",
                    "transactional_message_id", "transactional_message_name", "subject", "action_id", "parent_action_id", "customer_id",
                    "recipient", "created", "created_RFC3339", "failure_message", "sent", "sent_RFC3339", "delivered", "delivered_RFC3339",
                    "opened", "opened_RFC3339", "clicked", "clicked_RFC3339", "converted", "converted_RFC3339", "bounced", "bounced_RFC3339",
                    "spammed", "spammed_RFC3339", "unsubscribed", "unsubscribed_RFC3339", "suppressed", "suppressed_RFC3339", "failed",
                    "failed_RFC3339", "drafted", "drafted_RFC3339", "topic_unsubscribed", "topic_unsubscribed_RFC3339", "email"]


def customerio(users: Dict[str, Dict[str, str]]) -> None:
    out = HERE / "customerio"
    people = [{"id": u, "cio_id": f"cio_{u[2:]}", "email": users[u]["email"], "created_at": unix(ts(users[u]["created_at"])),
               "unsubscribed": "true" if u == "u_0006" else "false", "plan": users[u]["plan"] or "free",
               "country": users[u]["country"]} for u in USERS]
    write(out / "people.csv", ["id", "cio_id", "email", "created_at", "unsubscribed", "plan", "country"], people)

    send = ts("2026-06-01 10:00")
    story = {"u_0001": ["delivered", "opened"], "u_0002": ["delivered", "opened", "clicked"], "u_0003": ["bounced"],
             "u_0004": ["delivered"], "u_0005": ["delivered", "opened", "clicked"], "u_0006": ["delivered", "opened", "unsubscribed"],
             "u_0007": ["delivered", "spammed"], "u_0008": ["delivered", "opened"]}
    offset = {"delivered": timedelta(minutes=1), "opened": timedelta(minutes=14), "clicked": timedelta(minutes=16),
              "bounced": timedelta(minutes=1), "unsubscribed": timedelta(minutes=30), "spammed": timedelta(hours=22)}
    rows = []

    def delivery(n: int, u: str, at: datetime, states: List[str], **fields: str) -> None:
        r = {"id": f"dlv_{n:04d}", "channel": "email", "customer_id": u, "recipient": users[u]["email"], "email": users[u]["email"],
             "created": unix(at - timedelta(minutes=1)), "created_RFC3339": rfc(at - timedelta(minutes=1)),
             "sent": unix(at), "sent_RFC3339": rfc(at)}
        r.update(fields)
        for st in states:
            when = at + offset.get(st, timedelta(minutes=5))
            r[st], r[f"{st}_RFC3339"] = unix(when), rfc(when)
        rows.append(r)

    for k, u in enumerate(sorted(story), 1):
        extra = {"failure_message": "550 5.1.1 recipient address rejected: user unknown"} if "bounced" in story[u] else {}
        delivery(k, u, send, story[u], newsletter_id="12", newsletter_name="Ridgeview Monthly, June", template_id="31",
                 template_name="June newsletter", subject="What is new in Ridgeview this month", **extra)
    delivery(20, "u_0010", ts("2026-10-16 10:00"), ["delivered", "opened"], campaign_id="7", campaign_name="Trial ending",
             template_id="44", template_name="Trial ends in 7 days", action_id="44", subject="Your Ridgeview trial ends soon")
    push = {"id": "dlv_0021", "channel": "push", "customer_id": "u_0002", "recipient": "", "email": users["u_0002"]["email"],
            "campaign_id": "9", "campaign_name": "Weekly digest push", "template_id": "52", "template_name": "Digest push",
            "created": unix(ts("2026-10-01 15:00")), "created_RFC3339": rfc(ts("2026-10-01 15:00")),
            "sent": unix(ts("2026-10-01 15:00")), "sent_RFC3339": rfc(ts("2026-10-01 15:00")),
            "delivered": unix(ts("2026-10-01 15:01")), "delivered_RFC3339": rfc(ts("2026-10-01 15:01")),
            "opened": unix(ts("2026-10-01 18:20")), "opened_RFC3339": rfc(ts("2026-10-01 18:20"))}
    rows.append(push)
    write(out / "deliveries.csv", DELIVERY_COLUMNS, rows)


# ----------------------------------------------------------------------------------------------
# GA4 BigQuery export rows
# ----------------------------------------------------------------------------------------------

def _param(key: str, value: object) -> Dict[str, object]:
    v: Dict[str, object] = {"string_value": None, "int_value": None, "float_value": None, "double_value": None}
    if isinstance(value, bool) or isinstance(value, str):
        v["string_value"] = str(value)
    elif isinstance(value, int):
        v["int_value"] = str(value)          # BigQuery's JSON export writes INT64 as a string
    else:
        v["double_value"] = float(value)
    return {"key": key, "value": v}


def ga4(users: Dict[str, Dict[str, str]], events: List[Dict[str, str]]) -> None:
    rows = []
    for seq, e in enumerate(sorted(events, key=lambda e: (e["event_at"], e["event_id"])), 1):
        uid = e["user_id"] or None
        n = e["anonymous_id"][5:]
        u = users[f"u_{n}"]
        at = ts(e["event_at"])
        props = json.loads(e["properties"])
        session = int(at.timestamp()) // 3600 * 3600
        params = [_param("ga_session_id", session), _param("engagement_time_msec", 1200)]
        items: List[Dict[str, object]] = []
        ecommerce = None
        name = e["event_name"]
        platform = props.get("platform", "web")
        if name == "page_viewed":
            name = "page_view"
            params += [_param("page_location", f"https://ridgeview.example{props['path']}"), _param("page_title", "Pricing")]
        elif name == "signed_up":
            name = "sign_up"
            params += [_param("method", props["method"]), _param("plan", props["plan"])]
        elif name == "session_started":
            name = "login"
            params += [_param("method", "password"), _param("platform", platform)]
        elif name == "order_completed":
            name = "purchase"
            qty = int(props["items"])
            params += [_param("transaction_id", props["order_id"]), _param("value", float(props["net_amount"])), _param("currency", props["currency"])]
            items = [{"item_id": "rv_seat", "item_name": "Ridgeview seat", "item_brand": "Ridgeview", "item_variant": None,
                      "item_category": "subscription", "price": round(float(props["net_amount"]) / qty, 2), "quantity": str(qty),
                      "item_revenue": float(props["net_amount"]), "coupon": None}]
            ecommerce = {"transaction_id": props["order_id"], "purchase_revenue": float(props["net_amount"]), "total_item_quantity": str(qty)}
        else:
            params += [_param(k, v) for k, v in sorted(props.items())]
        rows.append({"event_date": at.strftime("%Y%m%d"), "event_timestamp": str(int(at.timestamp()) * 1_000_000), "event_name": name,
                     "event_params": params, "event_bundle_sequence_id": str(seq), "batch_event_index": "0",
                     "user_id": uid, "user_pseudo_id": f"{1000000000 + int(n)}.{int(ts(u['created_at']).timestamp()) - 86400}",
                     "device": {"category": "mobile" if platform in ("ios", "android") else "desktop",
                                "operating_system": {"ios": "iOS", "android": "Android"}.get(platform, "Macintosh")},
                     "geo": {"country": COUNTRY_NAME[u["country"]], "continent": "Europe" if u["country"] == "DE" else "Americas"},
                     "platform": "WEB", "stream_id": "1000000001", "items": items, "ecommerce": ecommerce})
    # one custom event in camelCase, which the mapper turns into snake_case
    at = ts("2026-10-02 11:20:00")
    rows.append({"event_date": "20261002", "event_timestamp": str(int(at.timestamp()) * 1_000_000 + 250), "event_name": "reportShared",
                 "event_params": [_param("ga_session_id", int(at.timestamp()) // 3600 * 3600), _param("report_id", "rpt_0005_01"),
                                  _param("recipients", 3)],
                 "event_bundle_sequence_id": str(len(rows) + 1), "batch_event_index": "1", "user_id": "u_0005",
                 "user_pseudo_id": f"{1000000005}.{int(ts(users['u_0005']['created_at']).timestamp()) - 86400}",
                 "device": {"category": "desktop", "operating_system": "Macintosh"}, "geo": {"country": "United States", "continent": "Americas"},
                 "platform": "WEB", "stream_id": "1000000001", "items": [], "ecommerce": None})
    path = HERE / "ga4" / "events-000000000000.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows), encoding="utf-8")


# ----------------------------------------------------------------------------------------------
# Segment and RudderStack warehouse tables
# ----------------------------------------------------------------------------------------------

def warehouse(users: Dict[str, Dict[str, str]], events: List[Dict[str, str]], vendor: str) -> None:
    out = HERE / vendor
    seg = vendor == "segment"
    lib = {"library_name": "analytics.js", "library_version": "5.2.0"} if seg else \
        {"library_name": "RudderLabs JavaScript SDK", "library_version": "3.11.0"}
    base_cols = ["id", "anonymous_id", "user_id", "received_at", "sent_at", "original_timestamp", "timestamp",
                 "context_library_name", "context_library_version", "context_ip"]
    vendor_cols = ["uuid", "uuid_ts"] if seg else ["context_source_id", "context_source_type", "context_destination_id",
                                                   "context_destination_type", "context_request_ip", "uuid_ts"]

    def common(at: datetime, rid: str, uid: str, anon: str) -> Dict[str, str]:
        r = {"id": rid, "anonymous_id": anon, "user_id": uid, "received_at": zulu(at + timedelta(seconds=2)),
             "sent_at": zulu(at + timedelta(seconds=1)), "original_timestamp": zulu(at), "timestamp": zulu(at),
             "context_library_name": lib["library_name"], "context_library_version": lib["library_version"],
             "context_ip": "203.0.113.10", "uuid_ts": zulu(at + timedelta(minutes=5))}
        if seg:
            r["uuid"] = f"uuid-{rid}"
        else:
            r.update({"context_source_id": "src_ridgeview_web", "context_source_type": "Javascript",
                      "context_destination_id": "dst_ridgeview_wh", "context_destination_type": "BQ",
                      "context_request_ip": "203.0.113.10"})
        return r

    tracks, by_event = [], {}
    for e in sorted(events, key=lambda e: (e["event_at"], e["event_id"])):
        at = ts(e["event_at"])
        r = common(at, e["event_id"], e["user_id"], e["anonymous_id"])
        r.update({"event": e["event_name"], "event_text": TITLE[e["event_name"]]})
        tracks.append(r)
        props = json.loads(e["properties"])
        by_event.setdefault(e["event_name"], []).append({**r, **{k: v for k, v in props.items()}})
    write(out / "tracks.csv", base_cols + ["event", "event_text"] + vendor_cols, tracks)
    for name, rows in sorted(by_event.items()):
        keys = sorted({k for r in rows for k in r} - set(base_cols) - set(vendor_cols) - {"event", "event_text"})
        write(out / f"{name}.csv", base_cols + ["event", "event_text"] + keys + vendor_cols, rows)

    identifies, latest = [], {}
    for k, u in enumerate(USERS, 1):
        rec = users[u]
        at = ts(rec["created_at"])
        traits = {"email": rec["email"], "plan": rec["plan"] or "free", "country": rec["country"], "created_at": zulu(at),
                  "context_timezone": TIMEZONE[rec["country"]]}
        r = {**common(at, f"idf_{k:04d}", u, f"anon_{u[2:]}"), **traits}
        identifies.append(r)
        latest[u] = r
    moved = ts("2026-02-01 09:00")   # u_0002 identifies again later, from a new time zone
    r = {**common(moved, "idf_0011", "u_0002", "anon_0002"), "email": users["u_0002"]["email"], "plan": "growth", "country": "US",
         "created_at": zulu(ts(users["u_0002"]["created_at"])), "context_timezone": "America/Chicago"}
    identifies.append(r)
    latest["u_0002"] = r
    trait_cols = ["email", "plan", "country", "created_at", "context_timezone"]
    write(out / "identifies.csv", base_cols + trait_cols + vendor_cols, identifies)
    if seg:
        urows = [{"id": u, "received_at": latest[u]["received_at"], "uuid_ts": latest[u]["uuid_ts"], "email": latest[u]["email"],
                  "plan": latest[u]["plan"], "country": latest[u]["country"], "created_at": latest[u]["created_at"]} for u in USERS]
        write(out / "users.csv", ["id", "received_at", "email", "plan", "country", "created_at", "uuid_ts"], urows)


def main() -> None:
    users, events = load()
    stripe(users)
    hubspot(users)
    customerio(users)
    ga4(users, events)
    warehouse(users, events, "segment")
    warehouse(users, events, "rudderstack")
    print(f"exports written under {HERE} for {len(users)} users and {len(events)} product events")


if __name__ == "__main__":
    main()
