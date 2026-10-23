"""
Deterministic synthetic data for Ridgeview, the fictional B2B reporting tool: the canonical tables at a realistic scale.

Nothing here is real. Ridgeview sells seats to companies (accounts). 5,000 accounts signed up between August 2025 and
October 2026, each with one to forty people, about 28,000 people in all. The shapes are chosen so a lifecycle team
would recognize them:

  accounts      each has a plan (free, starter, growth, scale), a seat count and a health every person in it shares;
                month by month an account is steady or at risk (its people go quiet more and come back less), and some
                cancel, after which nobody in them returns
  onboarding    a three-step checklist (connect a data source, invite a teammate, create a first report), with the
                biggest drop at the invite step
  reports       people who finish onboarding create reports every few days, go quiet now and then, and some come back
                on their own: about 1,200 people a month cross 14 days without a report
  billing       one invoice per 30-day renewal for each paying account, seats times the plan's seat price, paid by the
                account owner, after a 14-day trial
  email         a newsletter every two weeks for a year to people with consent, with opens (some machine opens, as
                Apple Mail Privacy Protection produces), clicks that follow each person's product activity, and the
                occasional bounce, complaint or unsubscribe

users.csv and orders.csv are CSV; track_events and message_events are Parquet, because at this scale they would be
tens of megabytes as CSV. The engine reads both (engine/data/load.py). A smaller 400-person version of this world,
which the test suite pins exact numbers to, is in tests/fixtures/ridgeview_small/.

    python3 examples/brands/ridgeview_saas/data/make_data.py              # rewrites the tables next to this file
    python3 examples/brands/ridgeview_saas/data/make_data.py OUT [SEED]   # into another folder, or another draw
"""
from __future__ import annotations

import csv
import json
import math
import random
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEED = 20261022
AS_OF = datetime(2026, 10, 19, tzinfo=timezone.utc)
START = datetime(2025, 8, 1, tzinfo=timezone.utc)
N_ACCOUNTS = 5000
COUNTRIES = ["US"] * 6 + ["CA", "GB", "DE", "AU"]
PLANS = (("free", 0.0, 22), ("starter", 25.0, 38), ("growth", 45.0, 28), ("scale", 75.0, 12))   # name, seat price, weight
ONBOARDING_STEPS = ["connected_data_source", "invited_teammate", "created_first_report"]
OTHER_FEATURES = ["exports", "alerts", "api", "dashboards"]


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def seats(rng: random.Random) -> int:
    r = rng.random()
    if r < 0.30:
        return 1
    if r < 0.65:
        return rng.randint(2, 4)
    if r < 0.90:
        return rng.randint(5, 12)
    return rng.randint(13, 40)


def main(out: Path = HERE, seed: int = SEED) -> None:
    rng = random.Random(seed)
    users, orders, events = [], [], []
    n_event = 0
    span = (AS_OF - START).total_seconds()

    def event(user_id, anon, name, at, props=None):
        nonlocal n_event
        if at >= AS_OF:
            return
        n_event += 1
        events.append((f"ev_{n_event:07d}", user_id or "", anon or "", name, iso(at),
                       json.dumps(props or {}, separators=(",", ":"))))

    n_user = 0
    for a in range(1, N_ACCOUNTS + 1):
        acct = f"acct_{a:04d}"
        # signups grow through the period: later months bring more accounts
        created = START + timedelta(seconds=span * math.sqrt(rng.random()))
        plan, price, _ = rng.choices(PLANS, weights=[p[2] for p in PLANS])[0]
        country = rng.choice(COUNTRIES)
        health = rng.gauss(0, 0.7)                   # shared by everyone in the account
        target = seats(rng)
        # the account's life: a paying account renews every 30 days after a 14-day trial until it cancels, and a free one is
        # abandoned at a faster rate. When it ends, everyone in it stops together and nobody comes back, which is why
        # entrants in one account are alike and an account-level holdout is the right design
        # Accounts also go through rough patches (a budget review, a champion on leave, a migration): month by month an account
        # is steady or at risk, and while it is at risk its people go quiet more often and come back less.
        risk, at_risk = False, []
        for _ in range(int((AS_OF - created).days / 30) + 2):
            risk = rng.random() < (0.45 if risk else sigmoid(-2.4 - 0.8 * health))
            at_risk.append(risk)
        renewals, month, end = [], created + timedelta(days=14), AS_OF
        while month < AS_OF:
            renewals.append(month)
            shaky = at_risk[min(len(at_risk) - 1, (month - created).days // 30)]
            if rng.random() < sigmoid((-2.2 if shaky else -4.2) + (0 if price else 0.7) - 0.9 * health):
                end = min(AS_OF, month + timedelta(days=rng.uniform(5, 28)))
                break
            month += timedelta(days=30)
        members = []
        for k in range(target):
            joined = created if k == 0 else created + timedelta(days=rng.expovariate(1 / 20), hours=rng.randint(0, 23))
            if joined >= min(end, AS_OF - timedelta(hours=1)):
                continue
            n_user += 1
            uid = f"u_{n_user:05d}"
            anon = f"anon_{n_user:05d}"
            propensity = health + rng.gauss(0, 0.6)
            email_consent = joined if rng.random() < 0.85 else None
            sms_consent = (min(joined + timedelta(days=rng.randint(0, 30)), AS_OF - timedelta(hours=1))
                           if rng.random() < 0.2 else None)
            if k == 0 or rng.random() < 0.3:
                event(None, anon, "page_viewed", joined - timedelta(hours=rng.randint(1, 72)), {"path": "/pricing" if k == 0 else "/invite"})
            event(uid, anon, "signed_up", joined, {"method": rng.choice(["email", "google", "sso"]), "plan": plan,
                                                   "role": "owner" if k == 0 else "invited"})
            # onboarding: the invite step loses the most people
            p1 = sigmoid(1.3 + propensity)
            p2 = sigmoid(0.1 + propensity)
            p3 = sigmoid(1.1 + propensity)
            t, done = joined, 0
            for step, p in zip(ONBOARDING_STEPS, (p1, p2, p3)):
                if rng.random() > p:
                    break
                t += timedelta(hours=rng.randint(1, 96))
                if t >= end:
                    break
                done += 1
                event(uid, anon, "onboarding_step_completed", t, {"step": step, "step_index": done})
            activated = None
            if done == 3 and rng.random() < 0.85:
                activated = t + timedelta(hours=rng.randint(1, 48))
                event(uid, anon, "activation_reached", activated, {"definition": "first_report_shared",
                                                                   "days_since_signup": (activated - joined).days})
            # activity: people who finished onboarding report every few days, sometimes go quiet, and some come back
            if done == 3:
                day = t + timedelta(hours=rng.randint(1, 30))
                mean_gap = max(1.5, 4.5 - 1.2 * propensity)
                while day < end:
                    shaky = at_risk[min(len(at_risk) - 1, (day - created).days // 30)]
                    event(uid, anon, "session_started", day, {"platform": rng.choice(["web", "web", "web", "ios"])})
                    if rng.random() < 0.9:
                        event(uid, anon, "feature_used", day + timedelta(minutes=4), {"feature": "reports"})
                    if rng.random() < 0.25:
                        event(uid, anon, "feature_used", day + timedelta(minutes=9), {"feature": rng.choice(OTHER_FEATURES)})
                    if rng.random() < sigmoid(-2.8 - 0.8 * propensity + (1.9 if shaky else 0.0)):
                        r = rng.random()
                        p_back = sigmoid(-1.45 + 0.7 * propensity - (3.6 if shaky else 0.0))
                        if r < p_back:
                            gap = rng.uniform(15, 44)                   # comes back on their own within a month of lapsing
                        elif r < p_back + 0.28:
                            gap = rng.uniform(45, 160)                  # comes back much later
                        else:
                            break                                       # gone
                    else:
                        gap = min(10.0, rng.expovariate(1 / mean_gap) + 0.3)
                    day += timedelta(days=gap, hours=rng.randint(-3, 3))
            else:
                # stalled in onboarding: a few visits that fade
                p_visit, day = 0.35, joined
                while rng.random() < p_visit:
                    day += timedelta(days=rng.randint(2, 20), hours=rng.randint(0, 23))
                    if day >= end:
                        break
                    event(uid, anon, "session_started", day, {"platform": "web"})
                    p_visit *= 0.75
            users.append({"user_id": uid, "email": f"{uid}@example.test", "created_at": iso(joined), "country": country,
                          "plan": plan, "lifecycle_stage": "", "email_consent_at": iso(email_consent) if email_consent else "",
                          "sms_consent_at": iso(sms_consent) if sms_consent else "", "account_id": acct})
            members.append((uid, joined))
        # billing: one invoice per renewal for a paying account, for the seats taken by then, paid by the owner
        if price and members:
            owner = members[0][0]
            for n, month in enumerate(renewals, start=1):
                taken = sum(1 for _, j in members if j <= month) or 1
                status = rng.choices(["completed", "refunded", "cancelled"], weights=[96, 2, 2])[0]
                gross = round(taken * price, 2)
                net = gross if status == "completed" else 0.0
                orders.append({"order_id": f"inv_{acct[5:]}_{n:02d}", "user_id": owner, "order_at": iso(month + timedelta(hours=2)),
                               "status": status, "gross_amount": f"{gross:.2f}", "discount_amount": "0.00", "net_amount": f"{net:.2f}",
                               "currency": "USD"})

    users.sort(key=lambda u: u["user_id"])
    events.sort(key=lambda e: (e[4], e[0]))
    write_csv(out / "users.csv", users)
    write_csv(out / "orders.csv", orders)
    write_parquet(out / "track_events.parquet", ["event_id", "user_id", "anonymous_id", "event_name", "event_at", "properties"], events)
    messages = message_events(users, events, seed)
    write_parquet(out / "message_events.parquet",
                  ["event_id", "user_id", "channel", "event_name", "event_at", "message_id", "campaign", "machine_open"], messages)
    for stale in ("track_events.csv", "message_events.csv"):
        (out / stale).unlink(missing_ok=True)
    print(f"accounts={N_ACCOUNTS} users={len(users)} orders={len(orders)} events={len(events)} message_events={len(messages)}")


def message_events(users, events, seed: int = SEED):
    rng = random.Random(seed + 1)
    active = {}
    for e in events:
        if e[3] == "session_started" and e[1]:
            active.setdefault(e[1], []).append(datetime.strptime(e[4], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc))
    sends = [AS_OF - timedelta(days=14 * k, hours=-10) for k in range(26, 0, -1)]   # a year of a biweekly newsletter, 10:00 UTC
    out = []
    n = 0

    def add(uid, name, at, mid, machine=""):
        nonlocal n
        n += 1
        out.append((f"me_{n:07d}", uid, "email", name, iso(at), mid, "newsletter", machine))

    for u in users:
        if not u["email_consent_at"]:
            continue
        uid = u["user_id"]
        consent = datetime.strptime(u["email_consent_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        mpp = rng.random() < 0.35                              # an Apple Mail reader: every delivered email registers a machine open
        seen = active.get(uid, [])
        for send in sends:
            if send < consent + timedelta(days=1):
                continue
            mid = f"m_{send:%Y%m%d}"
            recent = sum(1 for t in seen if send - timedelta(days=45) <= t < send)
            eng = min(1.0, recent / 6)
            if rng.random() < 0.004:
                add(uid, "bounced", send + timedelta(minutes=1), mid)
                break
            add(uid, "delivered", send + timedelta(minutes=1), mid)
            if mpp:
                add(uid, "opened", send + timedelta(minutes=rng.randint(2, 30)), mid, "true")
            if rng.random() < 0.18 + 0.5 * eng:
                opened_at = send + timedelta(hours=rng.randint(1, 40))
                add(uid, "opened", opened_at, mid, "false")
                if rng.random() < 0.10 + 0.40 * eng:
                    add(uid, "clicked", opened_at + timedelta(minutes=rng.randint(1, 20)), mid)
            if rng.random() < 0.0006:
                add(uid, "complained", send + timedelta(hours=rng.randint(1, 48)), mid)
                break
            if rng.random() < 0.004:
                add(uid, "unsubscribed", send + timedelta(hours=rng.randint(1, 48)), mid)
                break
    return out


def write_csv(path: Path, rows) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def write_parquet(path: Path, columns, rows) -> None:
    """Every column as text, like the CSVs: the loader casts on read, so both formats load the same way."""
    import duckdb
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "rows.csv"
        with staged.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(columns)
            w.writerows(rows)
        con = duckdb.connect()
        con.execute(f"COPY (SELECT * FROM read_csv('{staged}', header=true, all_varchar=true, nullstr='')) "
                    f"TO '{path}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 200000)")


if __name__ == "__main__":
    import sys
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else HERE, int(sys.argv[2]) if len(sys.argv) > 2 else SEED)
