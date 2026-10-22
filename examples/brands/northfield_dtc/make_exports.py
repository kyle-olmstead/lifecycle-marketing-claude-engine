"""
Northfield Coffee Roasters: deterministic synthetic vendor exports for the second demo brand.

Northfield is FICTIONAL: a direct-to-consumer coffee roaster on Shopify with Klaviyo for email. Nothing here is real:
ids look like cust_0001, emails like cust_0001@example.test, names like "Customer 0001", phones sit in the fictional
555-01xx block, and the store's domain is northfield.example (reserved by RFC 2606).

It writes the files a Northfield marketer would download, in the vendors' own formats, so `lcm import` can be shown
working on real-shaped exports rather than on tables someone already cleaned:

  exports/shopify/customers_export.csv            Shopify admin > Customers > Export (the documented columns)
  exports/shopify/orders_export_1.csv             Shopify admin > Orders > Export: one row per line item, order-level
                                                  fields only on the first row, times in the store's offset (-0600/-0500)
  exports/shopify/abandoned_checkouts_export.csv  Shopify admin > Orders > Abandoned checkouts > Export
  exports/klaviyo/profiles_all.csv                Klaviyo segment export (all profiles) with consent fields
  exports/klaviyo/metrics.json                    Klaviyo Get Metrics API response: metric id to name
  exports/klaviyo/sftp/exports/events/dt=2026-10-19/00-12-07-000000.csv.gz
                                                  Klaviyo SFTP event export: id, metric_id, profile_id, event_properties,
                                                  datetime, uuid (gzip-compressed, as Klaviyo delivers it)

About 2,000 people over about 9 months ending 2026-10-18, behaving the way a coffee store's customers do:
  * product views, add-to-carts and checkouts; about 70 percent of started checkouts are abandoned, and about 1 in 9
    abandoners comes back and buys within 72 hours on their own (the abandoned-checkout emails here change nothing,
    so a holdout would find no lift: the honest baseline for an abandonment flow);
  * coffee beans run out in 3 to 4 weeks a bag and paper filters in about 2 months, so loyal customers reorder on that
    rhythm with noise, occasional customers every 6 to 15 weeks, and many buy once; nobody gets a replenishment
    reminder yet, which is the gap the engine should find;
  * discount codes: WELCOME10 on first orders from newsletter sign-ups, three seasonal sales, and a group of
    deal hunters who buy with BREW15 or a sale code almost every time;
  * a few refunds, partial refunds and cancellations;
  * email: a welcome series, an abandoned-checkout series, a post-purchase thank-you and 14 campaigns; opens include
    Apple Mail Privacy Protection opens (flagged "Apple Privacy Open": true), clicks, a few hard and soft bounces,
    spam complaints and unsubscribes;
  * email consent from the sign-up form or the checkout box; SMS consent for about 1 in 5 subscribers;
  * mostly US, some Canada and Great Britain, with IANA time zones.

    python3 examples/brands/northfield_dtc/make_exports.py            # rewrites exports/ next to this file
    python3 examples/brands/northfield_dtc/make_exports.py --out /tmp/x
    lcm import auto examples/brands/northfield_dtc/exports --out examples/brands/northfield_dtc/data

Standard library only, one seeded random stream: the same run writes the same bytes (the .csv.gz is compared by its
decompressed content, since compressors differ between platforms).
"""
from __future__ import annotations

if __package__ in (None, ""):  # run as a file (python3 <path>): make the repository importable
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import argparse
import csv
import gzip
import io
import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
SEED = 20261018
UTC = timezone.utc
START = datetime(2026, 1, 19, 0, 0, 0, tzinfo=UTC)
END = datetime(2026, 10, 18, 23, 59, 59, tzinfo=UTC)
N_PEOPLE = 2000
DOMAIN = "https://northfield.example"
SFTP_EVENTS = Path("sftp/exports/events/dt=2026-10-19/00-12-07-000000.csv.gz")

# sku, Shopify product id, title, variant, price, category, kind, days one unit lasts
PRODUCTS = [
    ("NF-HOUSE-12", "prod_01", "Northfield House Blend", "12 oz / Whole Bean", 17.00, "Whole Bean Coffee", "bean", 24),
    ("NF-DARK-12", "prod_02", "Ridgeline Dark Roast", "12 oz / Whole Bean", 18.00, "Whole Bean Coffee", "bean", 24),
    ("NF-LIGHT-12", "prod_03", "Morning Fog Light Roast", "12 oz / Whole Bean", 18.00, "Whole Bean Coffee", "bean", 24),
    ("NF-ETH-12", "prod_04", "Single Origin Ethiopia", "12 oz / Whole Bean", 21.00, "Whole Bean Coffee", "bean", 24),
    ("NF-DECAF-12", "prod_05", "Swiss Water Decaf", "12 oz / Whole Bean", 19.00, "Whole Bean Coffee", "bean", 28),
    ("NF-FILTER-100", "prod_06", "Cone Paper Filters", "100 Pack", 8.00, "Brewing Supplies", "filter", 60),
    ("NF-GRINDER", "prod_07", "Hand Burr Grinder", "", 89.00, "Equipment", "durable", 0),
    ("NF-MUG", "prod_08", "Stoneware Mug", "12 oz", 24.00, "Drinkware", "durable", 0),
    ("NF-DRIPPER", "prod_09", "Ceramic Pour-Over Dripper", "", 32.00, "Equipment", "durable", 0),
]
BEANS = [p for p in PRODUCTS if p[6] == "bean"]
FILTER = PRODUCTS[5]
DURABLES = [p for p in PRODUCTS if p[6] == "durable"]

GEO = {  # country -> [(timezone, [province codes]), weight]
    "US": [("America/New_York", ["NY", "MA", "PA", "GA", "FL", "NC", "VA", "OH"], 34), ("America/Chicago", ["IL", "TX", "MN", "WI", "MO", "TN"], 26),
           ("America/Denver", ["CO", "UT", "NM"], 9), ("America/Phoenix", ["AZ"], 3), ("America/Los_Angeles", ["CA", "WA", "OR", "NV"], 28)],
    "CA": [("America/Toronto", ["ON"], 50), ("America/Vancouver", ["BC"], 35), ("America/Edmonton", ["AB"], 15)],
    "GB": [("Europe/London", [""], 100)],
}
PROVINCE_NAMES = {"NY": "New York", "MA": "Massachusetts", "PA": "Pennsylvania", "GA": "Georgia", "FL": "Florida", "NC": "North Carolina",
                  "VA": "Virginia", "OH": "Ohio", "IL": "Illinois", "TX": "Texas", "MN": "Minnesota", "WI": "Wisconsin", "MO": "Missouri",
                  "TN": "Tennessee", "CO": "Colorado", "UT": "Utah", "NM": "New Mexico", "AZ": "Arizona", "CA": "California",
                  "WA": "Washington", "OR": "Oregon", "NV": "Nevada", "ON": "Ontario", "BC": "British Columbia", "AB": "Alberta", "": ""}
COUNTRY_NAMES = {"US": "United States", "CA": "Canada", "GB": "United Kingdom"}

SALES = [("SPRING15", 0.15, datetime(2026, 3, 20, 16, tzinfo=UTC), 7), ("SUMMER20", 0.20, datetime(2026, 7, 1, 16, tzinfo=UTC), 7),
         ("HARVEST15", 0.15, datetime(2026, 9, 22, 16, tzinfo=UTC), 7)]
CAMPAIGNS = [  # (send time UTC, name, subject)
    (datetime(2026, 1, 27, 16, tzinfo=UTC), "January Roast Notes", "What we are roasting this month"),
    (datetime(2026, 3, 20, 16, tzinfo=UTC), "Spring Sale", "15% off every bag this week"),
    (datetime(2026, 4, 28, 16, tzinfo=UTC), "April Roast Notes", "How fresh is fresh?"),
    (datetime(2026, 7, 1, 16, tzinfo=UTC), "Cold Brew Season Sale", "20% off for cold brew season"),
    (datetime(2026, 7, 28, 16, tzinfo=UTC), "July Roast Notes", "Meet the farm behind our house blend"),
    (datetime(2026, 9, 22, 16, tzinfo=UTC), "Harvest Sale", "15% off: the harvest is in"),
    (datetime(2026, 9, 29, 16, tzinfo=UTC), "September Roast Notes", "Filters, freshness and the perfect cup"),
    (datetime(2026, 10, 13, 16, tzinfo=UTC), "New Single Origin: Ethiopia", "Blueberry, jasmine, and a story"),
]
METRICS = [  # (id, name, integration)
    ("NFm01", "Placed Order", "Shopify"), ("NFm02", "Checkout Started", "Shopify"), ("NFm03", "Fulfilled Order", "Shopify"),
    ("NFm04", "Viewed Product", "Klaviyo"), ("NFm05", "Added to Cart", "Klaviyo"), ("NFm06", "Active on Site", "Klaviyo"),
    ("NFm07", "Received Email", "Klaviyo"), ("NFm08", "Opened Email", "Klaviyo"), ("NFm09", "Clicked Email", "Klaviyo"),
    ("NFm10", "Bounced Email", "Klaviyo"), ("NFm11", "Marked Email as Spam", "Klaviyo"), ("NFm12", "Unsubscribed", "Klaviyo"),
    ("NFm13", "Subscribed to List", "Klaviyo"),
]
METRIC_ID = {name: mid for mid, name, _i in METRICS}
BROWSERS = [("Chrome", "Mac OS X"), ("Safari", "Mac OS X"), ("Mobile Safari", "iOS"), ("Chrome", "Windows"), ("Chrome", "Android"),
            ("Edge", "Windows"), ("Firefox", "Windows")]


# ---------------------------------------------------------------------------------------------------------------------
# time formats


def _dst_start(year: int) -> datetime:
    """US daylight time starts the second Sunday of March at 02:00 local; for the store (Central) that is 08:00 UTC."""
    d = datetime(year, 3, 1, 8, tzinfo=UTC)
    sundays = 0
    while True:
        if d.weekday() == 6:
            sundays += 1
            if sundays == 2:
                return d
        d += timedelta(days=1)


def _dst_end(year: int) -> datetime:
    d = datetime(year, 11, 1, 7, tzinfo=UTC)
    while d.weekday() != 6:
        d += timedelta(days=1)
    return d


def shopify_time(dt: Optional[datetime]) -> str:
    """The store's local time with its UTC offset, the way Shopify writes export dates: '2026-03-15 14:22:10 -0500'."""
    if dt is None:
        return ""
    hours = -5 if _dst_start(dt.year) <= dt < _dst_end(dt.year) else -6
    local = dt + timedelta(hours=hours)
    return local.strftime("%Y-%m-%d %H:%M:%S") + f" -{abs(hours):02d}00"


def klaviyo_profile_time(dt: Optional[datetime]) -> str:
    return "" if dt is None else dt.strftime("%Y-%m-%d %H:%M:%S")


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def money(x: float) -> str:
    return f"{x:.2f}"


# ---------------------------------------------------------------------------------------------------------------------
# the simulation


@dataclass
class Person:
    n: int
    first_seen: datetime
    country: str
    province: str
    tz: Optional[str]
    channel: str            # signup_form, checkout, browse
    persona: str            # never, one, occasional, loyal
    deal_hunter: bool
    apple_mail: bool
    engage: float           # chance of a real open per email
    favorite: tuple
    email_consent_at: Optional[datetime] = None
    consent_undated: bool = False
    sms_consent_at: Optional[datetime] = None
    phone: str = ""
    unsub_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None   # unsubscribed, complained or hard bounced: no more email
    last_filter: Optional[datetime] = None  # when they last bought paper filters
    klaviyo: List[Tuple[datetime, str, dict]] = field(default_factory=list)

    @property
    def cid(self) -> str:
        return f"cust_{self.n:04d}"

    @property
    def email(self) -> str:
        return f"{self.cid}@example.test"

    @property
    def kid(self) -> str:
        return f"kl_{self.n:04d}"

    def consented(self, at: datetime) -> bool:
        return self.email_consent_at is not None and self.email_consent_at <= at and (self.unsub_at is None or at < self.unsub_at)


@dataclass
class Checkout:
    person: Person
    created: datetime
    items: List[Tuple[tuple, int]]
    code: str
    pct: float
    abandoned: bool
    order: Optional["Order"] = None
    id: str = ""


@dataclass
class Order:
    person: Person
    created: datetime
    items: List[Tuple[tuple, int]]
    code: str
    pct: float
    checkout: Checkout
    id: str = ""
    name: str = ""
    financial: str = "paid"
    refunded: float = 0.0
    canceled_at: Optional[datetime] = None
    fulfilled_at: Optional[datetime] = None

    @property
    def items_total(self) -> float:
        return round(sum(p[4] * q for p, q in self.items), 2)

    @property
    def discount(self) -> float:
        return round(self.items_total * self.pct, 2)

    @property
    def subtotal(self) -> float:
        return round(self.items_total - self.discount, 2)

    @property
    def shipping(self) -> float:
        if self.person.country == "US":
            return 0.0 if self.subtotal >= 35 else 5.95
        return 9.95 if self.person.country == "CA" else 12.95

    @property
    def tax_rate(self) -> Tuple[str, float]:
        return {"US": ("State Tax 6.5%", 0.065), "CA": ("GST 5%", 0.05)}.get(self.person.country, ("", 0.0))

    @property
    def taxes(self) -> float:
        return round(self.subtotal * self.tax_rate[1], 2)

    @property
    def total(self) -> float:
        return round(self.subtotal + self.shipping + self.taxes, 2)


class Sim:
    def __init__(self, seed: int = SEED):
        self.rng = random.Random(seed)
        self.people: List[Person] = []
        self.checkouts: List[Checkout] = []
        self.orders: List[Order] = []
        self.phones = 0

    # -- people ---------------------------------------------------------------------------------------------------
    def make_people(self) -> None:
        r = self.rng
        span = (END - START).total_seconds()
        for n in range(1, N_PEOPLE + 1):
            first = START + timedelta(seconds=int(span * (r.random() ** 0.9)))
            country = r.choices(["US", "CA", "GB"], [82, 9, 9])[0]
            zones = GEO[country]
            tz, provinces, _w = r.choices(zones, [z[2] for z in zones])[0]
            channel = r.choices(["signup_form", "checkout", "browse"], [45, 40, 15])[0]
            persona = r.choices(["never", "one", "occasional", "loyal"],
                                {"signup_form": [58, 20, 14, 8], "checkout": [16, 34, 30, 20], "browse": [62, 24, 10, 4]}[channel])[0]
            engage = {"loyal": 0.42, "occasional": 0.24, "one": 0.14, "never": 0.08}[persona] * r.uniform(0.5, 1.4)
            p = Person(n, first, country, r.choice(provinces), tz if r.random() < 0.88 else None, channel, persona,
                       persona != "never" and r.random() < 0.15, r.random() < 0.40, min(engage, 0.8), r.choice(BEANS))
            if channel == "signup_form":
                p.email_consent_at = first
                p.consent_undated = r.random() < 0.03
            elif channel == "browse" and r.random() < 0.3:
                p.email_consent_at = first + timedelta(days=r.randint(0, 20), minutes=r.randint(1, 600))
            self.people.append(p)

    # -- shopping -------------------------------------------------------------------------------------------------
    def basket(self, p: Person, first: bool, at: datetime) -> List[Tuple[tuple, int]]:
        r = self.rng
        bags = 2 if p.persona == "loyal" and r.random() < 0.3 else 1
        items = [(p.favorite, bags)]
        if r.random() < 0.18:
            other = r.choice([b for b in BEANS if b is not p.favorite])
            items.append((other, 1))
        if first and r.random() < 0.35:
            items.append((FILTER, 1))
        elif p.last_filter is not None and (at - p.last_filter).days >= 50 and r.random() < 0.75:
            items.append((FILTER, 1))           # a 100-pack lasts about two months
        if first and r.random() < 0.22:
            items.append((r.choice(DURABLES), 1))
        return items

    def code_for(self, p: Person, at: datetime, first: bool) -> Tuple[str, float]:
        r = self.rng
        for code, pct, start, days in SALES:
            if start <= at < start + timedelta(days=days) and (p.deal_hunter or r.random() < 0.55):
                return code, pct
        if first and p.email_consent_at is not None and p.email_consent_at <= at and p.channel != "checkout" and r.random() < 0.6:
            return "WELCOME10", 0.10
        if p.deal_hunter and r.random() < 0.85:
            return "BREW15", 0.15
        return "", 0.0

    def session(self, p: Person, at: datetime, viewed: List[tuple]) -> bool:
        """A site visit with product views. Klaviyo's onsite script only records visitors it has identified (a cookie
        from an email click or a form), so about 45 percent of visits leave no onsite events. Returns whether it did."""
        r = self.rng
        browser, os_name = r.choice(BROWSERS[1:3] if p.apple_mail and r.random() < 0.6 else BROWSERS)
        start = at - timedelta(minutes=r.randint(3, 25))
        if r.random() >= 0.55:
            return False
        p.klaviyo.append((start, "Active on Site", {"Browser": browser, "Os": os_name, "Page": DOMAIN + "/"}))
        for k, prod in enumerate(viewed):
            p.klaviyo.append((start + timedelta(minutes=1 + 2 * k, seconds=r.randint(0, 59)), "Viewed Product",
                              {"Product": prod[2], "ProductID": prod[1], "Categories": [prod[5]], "Price": prod[4],
                               "URL": f"{DOMAIN}/products/{prod[0].lower()}"}))
        return True

    def attempt(self, p: Person, at: datetime, items: List[Tuple[tuple, int]], first: bool) -> Optional[Order]:
        """One shopping visit that reaches checkout. Returns the order if one was placed (now or within 72 hours)."""
        r = self.rng
        if at > END:
            return None
        viewed = [prod for k, (prod, _q) in enumerate(items) if k == 0 or r.random() < 0.4]
        tracked = self.session(p, at, viewed)
        for prod, qty in items[:1] if tracked else []:   # quick-adds from the cart drawer are not tracked
            p.klaviyo.append((at - timedelta(minutes=1, seconds=r.randint(0, 50)), "Added to Cart",
                              {"$value": round(prod[4] * qty, 2), "Product Name": prod[2], "ProductID": prod[1], "Variant SKU": prod[0],
                               "Price": prod[4], "Quantity": qty, "$currency": "USD", "Categories": [prod[5]]}))
        code, pct = self.code_for(p, at, first)
        if p.channel == "checkout" and first and p.email_consent_at is None and r.random() < 0.6:
            p.email_consent_at = at        # ticked the marketing box at their first checkout
        abandon_rate = {"loyal": 0.50, "occasional": 0.72, "one": 0.80, "never": 1.0}[p.persona]
        abandoned = r.random() < abandon_rate
        co = Checkout(p, at, items, code, pct, abandoned)
        self.checkouts.append(co)
        if not abandoned:
            placed = at + timedelta(minutes=r.randint(2, 14), seconds=r.randint(0, 59))
        elif p.persona != "never" and r.random() < 0.115:
            placed = at + timedelta(minutes=r.randint(60, 72 * 60 - 1))   # came back on their own within 72 hours
            browser, os_name = r.choice(BROWSERS)
            if r.random() < 0.55:
                p.klaviyo.append((placed - timedelta(minutes=r.randint(2, 9)), "Active on Site",
                                  {"Browser": browser, "Os": os_name, "Page": f"{DOMAIN}/cart"}))
        else:
            return None
        if placed > END:
            return None
        order = Order(p, placed, items, code, pct, co)
        co.order = order
        self.orders.append(order)
        return order

    def shop(self, p: Person) -> None:
        r = self.rng
        if p.persona == "never":
            for _ in range(r.choice([0, 0, 1, 1, 2])):
                at = p.first_seen + timedelta(days=r.uniform(0, 120), minutes=r.randint(0, 900))
                if at <= END:
                    picks = r.sample(PRODUCTS, 1)
                    self.session(p, at, picks)
                    if p.channel == "checkout" or r.random() < 0.15:
                        self.attempt(p, at + timedelta(minutes=8), [(picks[0], 1)], first=True)
            return
        at = p.first_seen + (timedelta(minutes=r.randint(5, 40)) if p.channel == "checkout" else
                             timedelta(days=r.choice([0, 1, 2, 4, 7, 12, 20, 35]), minutes=r.randint(10, 600)))
        orders = 0
        max_orders = {"one": 1, "occasional": 4, "loyal": 40}[p.persona]
        retries = 0
        while at <= END and orders < max_orders:
            items = self.basket(p, first=orders == 0, at=at)
            order = self.attempt(p, at, items, first=orders == 0)
            if order is None:
                # abandoned and not back within 72 hours: try again later, or give up
                retries += 1
                if retries > 3 or r.random() < {"one": 0.55, "occasional": 0.3, "loyal": 0.15}[p.persona]:
                    if p.persona == "one" or (orders == 0 and retries > 3):
                        break
                    at += timedelta(days=r.uniform(15, 40))
                else:
                    at += timedelta(days=r.uniform(1, 5) if p.persona == "loyal" else r.uniform(3, 12), minutes=r.randint(0, 600))
                continue
            retries = 0
            orders += 1
            if any(prod is FILTER for prod, _q in order.items):
                p.last_filter = order.created
            if p.persona == "loyal":
                bags = sum(q for prod, q in order.items if prod[6] == "bean")
                days = bags * p.favorite[7] * r.gauss(1.0, 0.15)
                if r.random() < 0.05:
                    break             # churned
                at = order.created + timedelta(days=max(10.0, days), minutes=r.randint(-300, 300))
            elif p.persona == "occasional":
                if r.random() < 0.25:
                    break
                at = order.created + timedelta(days=r.uniform(42, 105), minutes=r.randint(0, 600))
            # browse-only visits between orders
            if r.random() < 0.12:
                visit = order.created + timedelta(days=r.uniform(2, 20))
                if visit <= END:
                    self.session(p, visit, r.sample(PRODUCTS, 1))

    # -- after the fact -------------------------------------------------------------------------------------------
    def settle_orders(self) -> None:
        r = self.rng
        self.orders.sort(key=lambda o: (o.created, o.person.n))
        for k, o in enumerate(self.orders, 1):
            o.id, o.name = f"ord_{k:05d}", f"#NF{1000 + k}"
            o.fulfilled_at = o.created + timedelta(days=r.uniform(0.6, 3.0))
            if o.fulfilled_at > END:
                o.fulfilled_at = None
            x = r.random()
            if x < 0.007:
                o.canceled_at = o.created + timedelta(hours=r.uniform(1, 20))
                o.canceled_at = min(o.canceled_at, END)
                o.financial, o.refunded, o.fulfilled_at = "refunded", o.total, None
            elif x < 0.032:
                o.financial, o.refunded = "refunded", o.total
            elif x < 0.047:
                bean = max((p for p, _q in o.items), key=lambda p: p[4])
                o.financial, o.refunded = "partially_refunded", round(bean[4] * (1 - o.pct), 2)
        self.checkouts.sort(key=lambda c: (c.created, c.person.n))
        for k, c in enumerate(self.checkouts, 1):
            c.id = f"chk_{k:06d}"

    def phone_for(self, p: Person) -> str:
        k = self.phones
        self.phones += 1
        return f"+1{201 + k // 100}55501{k % 100:02d}"

    def consent_and_sms(self) -> None:
        r = self.rng
        for p in self.people:
            if p.email_consent_at is not None and p.email_consent_at <= END and r.random() < 0.21:
                at = p.email_consent_at + timedelta(days=r.choice([0, 0, 1, 3, 9, 20]), minutes=r.randint(1, 300))
                if at <= END:
                    p.sms_consent_at = at
            if p.sms_consent_at is not None or (p.persona in ("loyal", "occasional") and r.random() < 0.15):
                p.phone = self.phone_for(p)

    # -- email ----------------------------------------------------------------------------------------------------
    def email_program(self) -> None:
        r = self.rng
        by_person: Dict[int, List[Order]] = {}
        for o in self.orders:
            by_person.setdefault(o.person.n, []).append(o)
        abandoned_by_person: Dict[int, List[Checkout]] = {}
        for c in self.checkouts:
            if c.abandoned:
                abandoned_by_person.setdefault(c.person.n, []).append(c)
        hard_bouncers = set(r.sample(range(1, N_PEOPLE + 1), 14))
        for p in self.people:
            if p.email_consent_at is None or p.email_consent_at > END:
                continue
            sends: List[Tuple[datetime, str, str, str, Optional[str]]] = []   # (at, message id, campaign name, subject, flow)
            if p.channel in ("signup_form", "browse"):
                if not p.consent_undated:      # undated ones came over from a previous email tool, with no event
                    p.klaviyo.append((p.email_consent_at, "Subscribed to List", {"List": "Newsletter"}))
                sends.append((p.email_consent_at + timedelta(minutes=2), "msg_welcome_1", "Welcome Series: Email 1", "Welcome to Northfield", "flow_welcome"))
                sends.append((p.email_consent_at + timedelta(days=3), "msg_welcome_2", "Welcome Series: Email 2", "How to brew your first bag", "flow_welcome"))
            for k, (at, name, subject) in enumerate(CAMPAIGNS, 1):
                if p.email_consent_at <= at:
                    sends.append((at + timedelta(seconds=r.randint(0, 900)), f"msg_c{k:02d}", name, subject, None))
            orders = by_person.get(p.n, [])
            for c in abandoned_by_person.get(p.n, []):
                if not p.consented(c.created):
                    continue
                for hours, mid, label in ((4, "msg_abandon_1", "Abandoned Checkout: Reminder"),):
                    at = c.created + timedelta(hours=hours)
                    if not any(c.created <= o.created <= at for o in orders):
                        sends.append((at, mid, label, "You left something in your cart", "flow_abandoned_checkout"))
            if orders:
                first = orders[0]
                sends.append((first.created + timedelta(days=2), "msg_thanks_1", "Post-Purchase: Thank You", "Thanks for your first order", "flow_post_purchase"))
            sends.sort()
            mailed = 0
            last_touch = p.email_consent_at
            for at, mid, name, subject, flow in sends:
                if at > END or (p.stopped_at is not None and at >= p.stopped_at) or not p.consented(at):
                    continue
                if flow is None and mid > "msg_c02":
                    # from the third campaign on, campaigns go to the engaged segment: opened, ordered or subscribed
                    # in the last 45 days (privacy opens count, as they do in a real engaged segment)
                    recent = any(at - timedelta(days=45) <= o.created <= at for o in orders)
                    if not recent and last_touch < at - timedelta(days=45):
                        continue
                base = {"Campaign Name": name, "Subject": subject, "$message": mid}
                if flow:
                    base["$flow"] = flow
                mailed += 1
                if p.n in hard_bouncers and mailed == 1:
                    p.klaviyo.append((at + timedelta(seconds=40), "Bounced Email", dict(base, **{"Bounce Type": "hard"})))
                    p.stopped_at = at
                    continue
                if r.random() < 0.002:
                    p.klaviyo.append((at + timedelta(minutes=5), "Bounced Email", dict(base, **{"Bounce Type": "soft"})))
                    continue
                p.klaviyo.append((at + timedelta(seconds=30), "Received Email", base))
                opened_at = None
                if p.apple_mail and r.random() < 0.85:
                    opened_at = at + timedelta(minutes=r.randint(1, 40))
                    p.klaviyo.append((opened_at, "Opened Email", dict(base, **{"Apple Privacy Open": True})))
                    last_touch = opened_at
                human = r.random() < p.engage * (1.4 if flow else 1.0)
                if human and not p.apple_mail:
                    opened_at = at + timedelta(minutes=int(r.lognormvariate(4.5, 1.2)) + 1)
                    p.klaviyo.append((opened_at, "Opened Email", dict(base, **{"Apple Privacy Open": False})))
                    last_touch = opened_at
                if human and r.random() < 0.22:
                    click_at = (opened_at or at) + timedelta(minutes=r.randint(1, 12))
                    if click_at <= END:
                        p.klaviyo.append((click_at, "Clicked Email", dict(base, URL=f"{DOMAIN}/collections/coffee")))
                low = p.engage < 0.12
                x = r.random()
                if x < (0.0045 if low else 0.002):
                    when = at + timedelta(minutes=r.randint(5, 600))
                    if when <= END:
                        p.klaviyo.append((when, "Unsubscribed", {"Campaign Name": name, "$message": mid}))
                        p.unsub_at = p.stopped_at = when
                elif x < (0.0045 if low else 0.002) + 0.0005:
                    when = at + timedelta(minutes=r.randint(5, 600))
                    if when <= END:
                        p.klaviyo.append((when, "Marked Email as Spam", base))
                        p.unsub_at = p.stopped_at = when

    def order_events(self) -> None:
        for c in self.checkouts:
            lines = [{"product_id": prod[1], "sku": prod[0], "title": prod[2], "variant_title": prod[3], "quantity": q,
                      "price": money(prod[4])} for prod, q in c.items]
            items_total = round(sum(prod[4] * q for prod, q in c.items), 2)
            c.person.klaviyo.append((c.created, "Checkout Started", {
                "$event_id": c.id, "$value": round(items_total * (1 - c.pct), 2), "Items": [prod[2] for prod, _q in c.items],
                "Item Count": sum(q for _p, q in c.items), "Discount Codes": [c.code] if c.code else [],
                "$extra": {"id": c.id, "currency": "USD", "line_items": lines}}))
        for o in self.orders:
            lines = [{"product_id": prod[1], "sku": prod[0], "title": prod[2], "variant_title": prod[3], "quantity": q,
                      "price": money(prod[4])} for prod, q in o.items]
            o.person.klaviyo.append((o.created, "Placed Order", {
                "$event_id": o.id, "$value": o.total, "Items": [prod[2] for prod, _q in o.items],
                "Item Count": sum(q for _p, q in o.items), "Discount Codes": [o.code] if o.code else [],
                "Total Discounts": o.discount, "Source Name": "web",
                "$extra": {"id": o.id, "name": o.name, "currency": "USD", "line_items": lines}}))
            if o.fulfilled_at is not None:
                o.person.klaviyo.append((o.fulfilled_at, "Fulfilled Order", {"$event_id": o.id, "$value": o.total}))

    def run(self) -> "Sim":
        self.make_people()
        for p in self.people:
            self.shop(p)
        self.settle_orders()
        self.consent_and_sms()
        self.email_program()
        self.order_events()
        return self


# ---------------------------------------------------------------------------------------------------------------------
# writers


def _csv_bytes(header: List[str], rows: List[List[str]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


# https://help.shopify.com/en/manual/customers/import-export-customers
CUSTOMER_HEADER = ["First Name", "Last Name", "Email", "Accepts Email Marketing", "Default Address Company", "Default Address Address1",
                   "Default Address Address2", "Default Address City", "Default Address Province Code", "Default Address Country Code",
                   "Default Address Zip", "Default Address Phone", "Phone", "Accepts SMS Marketing", "Accepts WhatsApp Marketing",
                   "Total Spent", "Total Orders", "Note", "Tax Exempt", "Tags"]
# https://help.shopify.com/en/manual/orders/export-orders (Phone is listed twice there, and so it is here)
ORDER_HEADER = ["Name", "Phone", "Email", "Financial Status", "Paid at", "Fulfillment Status", "Fulfilled at", "Accepts Marketing",
                "Currency", "Subtotal", "Shipping", "Taxes", "Total", "Discount Code", "Discount Amount", "Shipping Method",
                "Created at", "Lineitem quantity", "Lineitem name", "Lineitem price", "Lineitem compare-at price", "Lineitem SKU",
                "Lineitem requires shipping", "Lineitem taxable", "Lineitem fulfillment status", "Billing Name", "Billing Street",
                "Billing Address1", "Billing Address2", "Billing Company", "Billing City", "Billing Zip", "Billing Province",
                "Billing Province Name", "Billing Country", "Billing Phone", "Shipping Name", "Shipping Street", "Shipping Address1",
                "Shipping Address2", "Shipping Company", "Shipping City", "Shipping Zip", "Shipping Province", "Shipping Province Name",
                "Shipping Country", "Shipping Phone", "Notes", "Note Attributes", "Canceled at", "Payment Method",
                "Payment Reference (deprecated)", "Payment References", "Refunded Amount", "Vendor", "Outstanding Balance", "Employee",
                "Location", "Device ID", "Id", "Tags", "Risk Level", "Source", "Lineitem discount",
                "Tax 1 Name", "Tax 1 Value", "Tax 2 Name", "Tax 2 Value", "Tax 3 Name", "Tax 3 Value", "Tax 4 Name", "Tax 4 Value",
                "Tax 5 Name", "Tax 5 Value", "Phone", "Payment ID", "Payment terms", "Next payment due at"]
# Not documented by Shopify: the order-export columns an abandoned checkout has, plus its recovery link.
CHECKOUT_HEADER = ["Id", "Email", "Phone", "Accepts Marketing", "Currency", "Subtotal", "Shipping", "Taxes", "Total", "Discount Code",
                   "Discount Amount", "Created at", "Lineitem quantity", "Lineitem name", "Lineitem price", "Lineitem SKU",
                   "Billing Name", "Billing Province", "Billing Country", "Shipping Name", "Shipping Province", "Shipping Country",
                   "Abandoned Checkout URL"]
PROFILE_HEADER = ["Email", "Klaviyo ID", "First Name", "Last Name", "Phone Number", "State / Region", "Country", "Timezone",
                  "Profile Created On", "Email Marketing Consent", "Email Marketing Consent Timestamp", "SMS Marketing Consent",
                  "SMS Marketing Consent Timestamp"]
EVENT_HEADER = ["id", "metric_id", "profile_id", "event_properties", "datetime", "uuid"]   # Klaviyo SFTP event export


def shopify_files(sim: Sim) -> Dict[str, bytes]:
    customers = []
    spent: Dict[int, float] = {}
    count: Dict[int, int] = {}
    for o in sim.orders:
        if o.canceled_at is None:
            spent[o.person.n] = spent.get(o.person.n, 0.0) + o.total - o.refunded
            count[o.person.n] = count.get(o.person.n, 0) + 1
    for p in sim.people:
        accepts = p.consented(END) and p.stopped_at is None
        customers.append(["Customer", f"{p.n:04d}", p.email, "yes" if accepts else "no", "", "", "", "", p.province, p.country, "", "",
                          p.phone, "yes" if p.sms_consent_at else "no", "no", money(spent.get(p.n, 0.0)), str(count.get(p.n, 0)), "",
                          "no", "newsletter" if p.channel == "signup_form" else ""])
    orders = []
    for o in sorted(sim.orders, key=lambda x: (x.created, x.id), reverse=True):
        p = o.person
        tax_name, _rate = o.tax_rate
        for k, (prod, qty) in enumerate(o.items):
            name = prod[2] + (f" - {prod[3]}" if prod[3] else "")
            line_status = "fulfilled" if o.fulfilled_at else "pending"
            row = {h: "" for h in ORDER_HEADER}
            row.update({"Name": o.name, "Email": p.email, "Created at": shopify_time(o.created), "Lineitem quantity": str(qty),
                        "Lineitem name": name, "Lineitem price": money(prod[4]), "Lineitem SKU": prod[0],
                        "Lineitem requires shipping": "true", "Lineitem taxable": "true" if tax_name else "false",
                        "Lineitem fulfillment status": line_status, "Vendor": "Northfield Coffee Roasters", "Lineitem discount": "0.00"})
            values = [row[h] for h in ORDER_HEADER]
            if k == 0:
                first = {
                    "Phone": p.phone, "Financial Status": o.financial, "Paid at": shopify_time(o.created + timedelta(seconds=4)),
                    "Fulfillment Status": "fulfilled" if o.fulfilled_at else "unfulfilled", "Fulfilled at": shopify_time(o.fulfilled_at),
                    "Accepts Marketing": "yes" if p.consented(o.created) else "no", "Currency": "USD",
                    "Subtotal": money(o.subtotal), "Shipping": money(o.shipping), "Taxes": money(o.taxes), "Total": money(o.total),
                    "Discount Code": o.code, "Discount Amount": money(o.discount),
                    "Shipping Method": "Standard" if o.shipping else "Free Shipping",
                    "Billing Name": f"Customer {p.n:04d}", "Billing Province": p.province, "Billing Province Name": PROVINCE_NAMES[p.province],
                    "Billing Country": p.country, "Shipping Name": f"Customer {p.n:04d}", "Shipping Province": p.province,
                    "Shipping Province Name": PROVINCE_NAMES[p.province], "Shipping Country": p.country,
                    "Canceled at": shopify_time(o.canceled_at), "Payment Method": "Shopify Payments",
                    "Refunded Amount": money(o.refunded), "Outstanding Balance": "0.00", "Id": o.id, "Risk Level": "Low", "Source": "web",
                    "Tax 1 Name": tax_name, "Tax 1 Value": money(o.taxes) if tax_name else "", "Payment ID": f"pay_{o.id[4:]}",
                }
                values = [first.get(h, v) if h != "Phone" else (p.phone if i == 1 else "") for i, (h, v) in enumerate(zip(ORDER_HEADER, values, strict=True))]
            orders.append(values)
    checkouts = []
    for c in sorted((c for c in sim.checkouts if c.abandoned), key=lambda x: (x.created, x.id), reverse=True):
        p = c.person
        items_total = round(sum(prod[4] * q for prod, q in c.items), 2)
        discount = round(items_total * c.pct, 2)
        subtotal = round(items_total - discount, 2)
        for k, (prod, qty) in enumerate(c.items):
            name = prod[2] + (f" - {prod[3]}" if prod[3] else "")
            if k == 0:
                checkouts.append([c.id, p.email, p.phone, "yes" if p.consented(c.created) else "no", "USD", money(subtotal), "", "",
                                  money(subtotal), c.code, money(discount), shopify_time(c.created), str(qty), name, money(prod[4]),
                                  prod[0], f"Customer {p.n:04d}", p.province, p.country, f"Customer {p.n:04d}", p.province, p.country,
                                  f"{DOMAIN}/checkouts/{c.id}/recover"])
            else:
                checkouts.append([c.id, p.email, "", "", "", "", "", "", "", "", "", "", str(qty), name, money(prod[4]), prod[0],
                                  "", "", "", "", "", "", ""])
    return {"customers_export.csv": _csv_bytes(CUSTOMER_HEADER, customers),
            "orders_export_1.csv": _csv_bytes(ORDER_HEADER, orders),
            "abandoned_checkouts_export.csv": _csv_bytes(CHECKOUT_HEADER, checkouts)}


def klaviyo_files(sim: Sim) -> Dict[str, bytes]:
    profiles = []
    for p in sim.people:
        if p.unsub_at is not None:
            consent, consent_at = "UNSUBSCRIBED", klaviyo_profile_time(p.unsub_at)
        elif p.email_consent_at is not None and p.email_consent_at <= END:
            consent, consent_at = "SUBSCRIBED", "" if p.consent_undated else klaviyo_profile_time(p.email_consent_at)
        else:
            consent, consent_at = "NEVER_SUBSCRIBED", ""
        sms = ("SUBSCRIBED", klaviyo_profile_time(p.sms_consent_at)) if p.sms_consent_at else ("NEVER_SUBSCRIBED", "")
        profiles.append([p.email, p.kid, "Customer", f"{p.n:04d}", p.phone, p.province, COUNTRY_NAMES[p.country], p.tz or "",
                         klaviyo_profile_time(p.first_seen), consent, consent_at, sms[0], sms[1]])
    events = []
    for p in sim.people:
        for at, metric, props in p.klaviyo:
            if at <= END:
                events.append((at, p.n, metric, props))
    events.sort(key=lambda e: (e[0], e[1], e[2]))
    rows = []
    for k, (at, n, metric, props) in enumerate(events, 1):
        rows.append([f"ev_{k:06d}", METRIC_ID[metric], f"kl_{n:04d}", json.dumps(props, separators=(",", ":"), sort_keys=True),
                     iso(at), f"00000000-0000-4000-8000-{k:012d}"])
    metrics = {"data": [{"type": "metric", "id": mid, "attributes": {
        "name": name, "created": "2025-11-03T17:20:00+00:00", "updated": "2025-11-03T17:20:00+00:00",
        "integration": {"object": "integration", "id": f"int_{integ.lower()}", "name": integ,
                        "category": "eCommerce" if integ == "Shopify" else "Internal"}},
        "links": {"self": f"https://a.klaviyo.com/api/metrics/{mid}/"}} for mid, name, integ in METRICS]}
    return {"profiles_all.csv": _csv_bytes(PROFILE_HEADER, profiles),
            "metrics.json": (json.dumps(metrics, indent=2) + "\n").encode("utf-8"),
            SFTP_EVENTS.as_posix(): _csv_bytes(EVENT_HEADER, rows)}


def build() -> Dict[str, bytes]:
    """Every export file as {relative path under exports/: uncompressed bytes}."""
    sim = Sim().run()
    out = {f"shopify/{k}": v for k, v in shopify_files(sim).items()}
    out.update({f"klaviyo/{k}": v for k, v in klaviyo_files(sim).items()})
    return out


def write(out: Path) -> List[Path]:
    written = []
    for rel, data in build().items():
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if rel.endswith(".gz"):
            data = gzip.compress(data, compresslevel=9, mtime=0)
        path.write_bytes(data)
        written.append(path)
    return written


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Write Northfield's synthetic Shopify and Klaviyo exports")
    ap.add_argument("--out", type=Path, default=HERE / "exports", help="folder to write (default: exports/ next to this file)")
    a = ap.parse_args(argv)
    for p in write(a.out):
        print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
