"""The canonical tables: the one shape every source is mapped into and every query is written against.

Each table lists its columns with a type, whether the engine cannot work without it, and what it means.
`lcm check-data` checks a data folder against this file and names any bad column; `lcm import` maps a
vendor export (Shopify, Stripe, Klaviyo, HubSpot, Customer.io, GA4, RudderStack, Segment) into it;
docs/SQL_SCHEMA_CONVENTIONS.md explains the conventions behind it.

Only users and track_events are required. A SaaS product without orders, a store without
subscriptions, or a program without an ESP export works with what it has, and each feature that needs a
missing table says so instead of failing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    required: bool
    meaning: str


@dataclass(frozen=True)
class Table:
    name: str
    required: bool
    meaning: str
    columns: Tuple[Column, ...]

    @property
    def ddl(self) -> str:
        return ", ".join(f"{c.name} {c.type}" for c in self.columns)

    @property
    def column_names(self) -> List[str]:
        return [c.name for c in self.columns]


def _t(name: str, required: bool, meaning: str, *cols: Tuple[str, str, bool, str]) -> Table:
    return Table(name, required, meaning, tuple(Column(*c) for c in cols))


TABLES: Dict[str, Table] = {t.name: t for t in (
    _t("users", True, "one row per person the program can message",
       ("user_id", "VARCHAR", True, "stable id used everywhere else; the ESP's external id"),
       ("email", "VARCHAR", True, "email address (lower case)"),
       ("created_at", "TIMESTAMP", True, "when the person signed up or was first seen, UTC"),
       ("country", "VARCHAR", False, "ISO 3166-1 alpha-2 country; decides which law applies"),
       ("plan", "VARCHAR", False, "plan or tier name, for SaaS"),
       ("lifecycle_stage", "VARCHAR", False, "lifecycle stage, when an automation sets it"),
       ("email_consent_at", "TIMESTAMP", False, "when email marketing consent was given; empty means none on record"),
       ("sms_consent_at", "TIMESTAMP", False, "when SMS marketing consent was given (10DLC record)"),
       ("account_id", "VARCHAR", False, "the company or workspace the person belongs to; account-level holdouts hash this"),
       ("timezone", "VARCHAR", False, "IANA time zone (America/Chicago); send windows are in recipient local time"),
       ("phone", "VARCHAR", False, "E.164 phone number, for SMS")),
    _t("track_events", True, "every product or site event, one row each",
       ("event_id", "VARCHAR", True, "unique id; duplicates are dropped"),
       ("user_id", "VARCHAR", False, "the person, when known; empty for anonymous events"),
       ("anonymous_id", "VARCHAR", False, "device or cookie id before identification"),
       ("event_name", "VARCHAR", True, "object_action in snake_case: order_completed, checkout_started, product_viewed"),
       ("event_at", "TIMESTAMP", True, "when it happened, UTC"),
       ("properties", "JSON", False, "event properties as a JSON object")),
    _t("orders", False, "the transaction object for stores: one row per order",
       ("order_id", "VARCHAR", True, "unique order id"),
       ("user_id", "VARCHAR", True, "who placed it"),
       ("order_at", "TIMESTAMP", True, "when it was placed, UTC"),
       ("status", "VARCHAR", True, "completed, refunded, canceled; only completed counts as revenue"),
       ("gross_amount", "DECIMAL(18,2)", False, "before discounts"),
       ("discount_amount", "DECIMAL(18,2)", False, "total discount on the order; discount dependency is read from it"),
       ("net_amount", "DECIMAL(18,2)", False, "what the customer paid"),
       ("currency", "VARCHAR", False, "ISO 4217 currency code")),
    _t("line_items", False, "what was in each order; replenishment and product blocks read it",
       ("order_id", "VARCHAR", True, "the order"),
       ("line_id", "VARCHAR", False, "unique line id"),
       ("product_id", "VARCHAR", True, "the product"),
       ("variant_id", "VARCHAR", False, "the variant (size, flavor)"),
       ("sku", "VARCHAR", False, "stock keeping unit"),
       ("product_name", "VARCHAR", False, "name shown to customers"),
       ("category", "VARCHAR", False, "product category"),
       ("quantity", "INTEGER", True, "units"),
       ("unit_price", "DECIMAL(18,2)", False, "price per unit"),
       ("line_amount", "DECIMAL(18,2)", False, "quantity x unit price, after line discounts")),
    _t("products", False, "the catalog: names, links and how long one unit lasts",
       ("product_id", "VARCHAR", True, "the product"),
       ("product_name", "VARCHAR", True, "name shown to customers"),
       ("category", "VARCHAR", False, "product category"),
       ("url", "VARCHAR", False, "product page"),
       ("image_url", "VARCHAR", False, "product image"),
       ("price", "DECIMAL(18,2)", False, "current price"),
       ("replenish_after_days", "INTEGER", False, "days one unit lasts; measured from repeat orders when empty"),
       ("in_stock", "BOOLEAN", False, "whether it can be bought now")),
    _t("discounts", False, "discount codes used on orders",
       ("order_id", "VARCHAR", True, "the order"),
       ("code", "VARCHAR", True, "the code, as typed"),
       ("discount_type", "VARCHAR", False, "percentage, fixed_amount, free_shipping"),
       ("amount", "DECIMAL(18,2)", False, "value of the discount on this order")),
    _t("refunds", False, "money returned",
       ("refund_id", "VARCHAR", True, "unique refund id"),
       ("order_id", "VARCHAR", True, "the order"),
       ("user_id", "VARCHAR", False, "who was refunded"),
       ("refunded_at", "TIMESTAMP", True, "when, UTC"),
       ("amount", "DECIMAL(18,2)", True, "amount refunded"),
       ("reason", "VARCHAR", False, "reason given")),
    _t("accounts", False, "companies or workspaces, for B2B: the buying unit",
       ("account_id", "VARCHAR", True, "unique account id"),
       ("name", "VARCHAR", False, "account name"),
       ("created_at", "TIMESTAMP", False, "when the account was created"),
       ("plan", "VARCHAR", False, "plan or tier"),
       ("status", "VARCHAR", False, "trialing, active, past_due, canceled")),
    _t("subscriptions", False, "the transaction object for subscriptions: one row per subscription",
       ("subscription_id", "VARCHAR", True, "unique subscription id"),
       ("user_id", "VARCHAR", False, "the person who pays"),
       ("account_id", "VARCHAR", False, "the account it belongs to"),
       ("plan", "VARCHAR", False, "plan or price name"),
       ("status", "VARCHAR", True, "trialing, active, past_due, unpaid, canceled, paused"),
       ("started_at", "TIMESTAMP", True, "when it started"),
       ("trial_end_at", "TIMESTAMP", False, "when the trial ends or ended"),
       ("current_period_end_at", "TIMESTAMP", False, "end of the current billing period"),
       ("canceled_at", "TIMESTAMP", False, "when it was canceled"),
       ("mrr", "DECIMAL(18,2)", False, "monthly recurring revenue"),
       ("currency", "VARCHAR", False, "ISO 4217 currency code")),
    _t("invoices", False, "bills: dunning reads failed and open ones",
       ("invoice_id", "VARCHAR", True, "unique invoice id"),
       ("subscription_id", "VARCHAR", False, "the subscription billed"),
       ("user_id", "VARCHAR", False, "the person billed"),
       ("account_id", "VARCHAR", False, "the account billed"),
       ("created_at", "TIMESTAMP", True, "when it was issued"),
       ("status", "VARCHAR", True, "draft, open, paid, uncollectible, void"),
       ("amount_due", "DECIMAL(18,2)", False, "amount owed"),
       ("amount_paid", "DECIMAL(18,2)", False, "amount collected"),
       ("currency", "VARCHAR", False, "ISO 4217 currency code"),
       ("attempt_count", "INTEGER", False, "payment attempts so far"),
       ("next_payment_attempt_at", "TIMESTAMP", False, "when the processor retries next"),
       ("paid_at", "TIMESTAMP", False, "when it was paid")),
    _t("consent_events", False, "every consent change, newest wins: a later opt-in lifts an earlier unsubscribe",
       ("user_id", "VARCHAR", True, "the person"),
       ("channel", "VARCHAR", True, "email, sms or push"),
       ("status", "VARCHAR", True, "granted or revoked"),
       ("purpose", "VARCHAR", False, "marketing or transactional (default marketing)"),
       ("source", "VARCHAR", False, "where it was captured: checkout, signup_form, preference_center, import, esp_unsubscribe"),
       ("occurred_at", "TIMESTAMP", True, "when, UTC")),
    _t("message_events", False, "what the ESP did: deliveries, opens, clicks, bounces, complaints, unsubscribes",
       ("event_id", "VARCHAR", True, "unique id"),
       ("user_id", "VARCHAR", True, "the recipient"),
       ("channel", "VARCHAR", True, "email, sms, push"),
       ("event_name", "VARCHAR", True, "delivered, opened, clicked, bounced, deferred, complained, unsubscribed"),
       ("event_at", "TIMESTAMP", True, "when, UTC"),
       ("message_id", "VARCHAR", False, "the send"),
       ("campaign", "VARCHAR", False, "campaign or flow name"),
       ("machine_open", "BOOLEAN", False, "true for privacy-proxy opens (Apple Mail Privacy Protection)"),
       ("recipient_domain", "VARCHAR", False, "domain of the recipient address; triage per mailbox provider reads it"),
       ("smtp_status", "VARCHAR", False, "enhanced status code of a bounce or deferral (5.1.1, 4.7.28)"),
       ("diagnostic", "VARCHAR", False, "the receiving server's text for a bounce or deferral")),
    _t("lcm_flow_entries", False, "the engine's own ledger of flow entries already pushed",
       ("flow", "VARCHAR", True, "flow slug"),
       ("user_id", "VARCHAR", True, "the person"),
       ("anchor_date", "DATE", True, "the lapse key"),
       ("holdout_bucket", "INTEGER", True, "0 to 99"),
       ("in_holdout", "BOOLEAN", True, "holdout member"),
       ("status", "VARCHAR", True, "pending or pushed"),
       ("queued_at", "TIMESTAMP", True, "when queued"),
       ("pushed_at", "TIMESTAMP", False, "when pushed")),
)}

REQUIRED_TABLES = tuple(n for n, t in TABLES.items() if t.required)
OPTIONAL_TABLES = tuple(n for n, t in TABLES.items() if not t.required)
