"""
The small Ridgeview world: 400 people, no accounts, for the tests that pin exact numbers and for the ten people in the
sample vendor exports (examples/brands/ridgeview_saas/exports). The demo workspace's own data is the same company at
full size (examples/brands/ridgeview_saas/data/make_data.py); this one stays small so a test can name a person.

Nothing here is real. The shapes are chosen so that every query in prompts/jobs/sql has something to
find: a spread of signup cohorts, users with zero to many orders, refunds, anonymous events that
later identify, and an activation event that some users never reach.

    python3 tests/fixtures/ridgeview_small/data/make_data.py        # writes users.csv, orders.csv, track_events.csv, message_events.csv next to this file

message_events is generated after the other three with its own random stream, so adding it changed
none of them: a newsletter every two weeks for 26 weeks to people with email consent, with opens
(some flagged as machine opens, as Apple Mail Privacy Protection produces), clicks that follow each
person's product activity, and the occasional hard bounce, complaint or unsubscribe, after which
that person is never mailed again.
"""
from __future__ import annotations

import csv
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEED = 20261019
AS_OF = datetime(2026, 10, 19, tzinfo=timezone.utc)
N_USERS = 400
COUNTRIES = ["US", "US", "US", "CA", "GB", "DE", "AU"]
PLANS = [None, "starter", "starter", "growth", "scale"]
ONBOARDING_STEPS = ["connected_data_source", "invited_teammate", "created_first_report"]


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def main() -> None:
    rng = random.Random(SEED)
    users, orders, events = [], [], []
    event_n = 0

    def event(user_id, anon, name, at, props=None):
        nonlocal event_n
        event_n += 1
        events.append({"event_id": f"ev_{event_n:06d}", "user_id": user_id, "anonymous_id": anon,
                       "event_name": name, "event_at": iso(at), "properties": json.dumps(props or {}, separators=(",", ":"))})

    for i in range(1, N_USERS + 1):
        uid = f"u_{i:04d}"
        anon = f"anon_{i:04d}"
        created = AS_OF - timedelta(days=rng.randint(1, 420), hours=rng.randint(0, 23))
        country = rng.choice(COUNTRIES)
        plan = rng.choice(PLANS)
        email_consent = created if rng.random() < 0.85 else None
        sms_consent = min(created + timedelta(days=rng.randint(0, 30)), AS_OF - timedelta(hours=1)) if rng.random() < 0.25 else None

        # anonymous activity before signup, then signup
        event(None, anon, "page_viewed", created - timedelta(hours=rng.randint(1, 72)), {"path": "/pricing"})
        event(uid, anon, "signed_up", created, {"method": rng.choice(["email", "google", "sso"]), "plan": plan or "free"})

        # onboarding: some stall, some activate
        steps_done = rng.choices([0, 1, 2, 3], weights=[20, 25, 20, 35])[0]
        t = created
        for s in ONBOARDING_STEPS[:steps_done]:
            t += timedelta(hours=rng.randint(1, 96))
            event(uid, anon, "onboarding_step_completed", t, {"step": s, "step_index": ONBOARDING_STEPS.index(s) + 1})
        activated_at = None
        if steps_done == 3 and rng.random() < 0.8:
            activated_at = t + timedelta(hours=rng.randint(1, 48))
            event(uid, anon, "activation_reached", activated_at, {"definition": "first_report_shared", "days_since_signup": (activated_at - created).days})

        # ongoing sessions: activated users keep coming back, others fade
        horizon = (AS_OF - created).days
        p_active = 0.50 if activated_at else 0.12
        day = 0
        while day < horizon:
            day += rng.randint(1, 9)
            if rng.random() < p_active:
                at = created + timedelta(days=day, hours=rng.randint(0, 23))
                if at < AS_OF:
                    event(uid, anon, "session_started", at, {"platform": rng.choice(["web", "web", "ios", "android"])})
                    if rng.random() < 0.4:
                        event(uid, anon, "feature_used", at + timedelta(minutes=5), {"feature": rng.choice(["reports", "exports", "alerts", "api"])})
            p_active *= 0.985 if activated_at else 0.90

        # orders: frequency skewed; activated users buy more
        n_orders = rng.choices([0, 1, 2, 3, 5, 8, 13], weights=[35, 30, 14, 9, 6, 4, 2])[0]
        if activated_at is None and n_orders > 2:
            n_orders = 2
        ot = created + timedelta(days=rng.randint(0, 20))
        for k in range(n_orders):
            if ot >= AS_OF:
                break
            gross = round(rng.choice([29, 49, 49, 99, 149, 299, 499]) * rng.uniform(0.9, 1.6), 2)
            discount = round(gross * rng.choice([0, 0, 0, 0.1, 0.2]), 2)
            status = rng.choices(["completed", "refunded", "cancelled"], weights=[92, 5, 3])[0]
            net = round(gross - discount, 2) if status == "completed" else 0.0
            oid = f"o_{uid[2:]}_{k + 1:02d}"
            orders.append({"order_id": oid, "user_id": uid, "order_at": iso(ot), "status": status,
                           "gross_amount": f"{gross:.2f}", "discount_amount": f"{discount:.2f}", "net_amount": f"{net:.2f}", "currency": "USD"})
            if status == "completed":
                event(uid, anon, "order_completed", ot, {"order_id": oid, "net_amount": net, "currency": "USD", "items": rng.randint(1, 4)})
            ot += timedelta(days=rng.randint(7, 75))

        users.append({"user_id": uid, "email": f"{uid}@example.test", "created_at": iso(created), "country": country,
                      "plan": plan or "", "lifecycle_stage": "", "email_consent_at": iso(email_consent) if email_consent else "",
                      "sms_consent_at": iso(sms_consent) if sms_consent else ""})

    for name, rows in (("users", users), ("orders", orders), ("track_events", events)):
        with (HERE / f"{name}.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    messages = message_events(users, events)
    with (HERE / "message_events.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(messages[0].keys()))
        w.writeheader()
        w.writerows(messages)
    print(f"users={len(users)} orders={len(orders)} events={len(events)} message_events={len(messages)}")


def message_events(users, events):
    rng = random.Random(SEED + 1)
    sessions = {}
    for e in events:
        if e["event_name"] == "session_started" and e["user_id"]:
            sessions.setdefault(e["user_id"], []).append(datetime.strptime(e["event_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc))
    sends = [AS_OF - timedelta(days=14 * k, hours=-10) for k in range(13, 0, -1)]   # 26 weeks of a biweekly newsletter, 10:00 UTC
    out = []
    n = 0

    def add(uid, name, at, mid, machine=""):
        nonlocal n
        n += 1
        out.append({"event_id": f"me_{n:06d}", "user_id": uid, "channel": "email", "event_name": name, "event_at": iso(at),
                    "message_id": mid, "campaign": "newsletter", "machine_open": machine})

    for u in users:
        if not u["email_consent_at"]:
            continue
        uid = u["user_id"]
        consent = datetime.strptime(u["email_consent_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        mpp = rng.random() < 0.35                              # an Apple Mail reader: every delivered email registers a machine open
        for send in sends:
            if send < consent + timedelta(days=1):
                continue
            mid = f"m_{send:%Y%m%d}"
            recent = sum(1 for t in sessions.get(uid, []) if send - timedelta(days=45) <= t < send)
            eng = min(1.0, recent / 6)
            r = rng.random()
            if r < 0.004:
                add(uid, "bounced", send + timedelta(minutes=1), mid)
                break
            add(uid, "delivered", send + timedelta(minutes=1), mid)
            if mpp:
                add(uid, "opened", send + timedelta(minutes=rng.randint(2, 30)), mid, "true")
            if rng.random() < 0.10 + 0.45 * eng:
                opened_at = send + timedelta(hours=rng.randint(1, 40))
                add(uid, "opened", opened_at, mid, "false")
                if rng.random() < 0.08 + 0.35 * eng:
                    add(uid, "clicked", opened_at + timedelta(minutes=rng.randint(1, 20)), mid)
            if rng.random() < 0.0008:
                add(uid, "complained", send + timedelta(hours=rng.randint(1, 48)), mid)
                break
            if rng.random() < 0.004:
                add(uid, "unsubscribed", send + timedelta(hours=rng.randint(1, 48)), mid)
                break
    return out


if __name__ == "__main__":
    main()
