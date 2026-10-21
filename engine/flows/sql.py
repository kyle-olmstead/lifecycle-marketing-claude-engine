"""Warehouse SQL for the flow library: who is due now, and how many would enter each month and convert with no flow.

Every flow gets two queries in each dialect (postgres, snowflake, bigquery, and duckdb for runs on files):

  entry    who is due on the run date or time. For flows the ESP triggers natively (cart, browse, post-purchase,
           welcome, trial, dunning) it audits and backfills the live trigger; for flows the warehouse triggers
           (win-back, replenishment, sunset) it feeds the daily job through the ledger lcm_flow_entries.
  history  entrants by month and the share who reached the goal within the outcome window with no flow running,
           in the columns month, entrants, matured, returned: the volume and baseline the holdout is sized from.

Win-back and activation keep their builders in engine/agents/cohort_analyst.py; welcome and trial conversion use the
activation builders (a new qualifier, then the goal), so this module holds the rest. The holdout bucket hashes the
randomization unit: users.account_id when the brief randomizes by account (v2 hashed user_id regardless).
"""
from __future__ import annotations

from typing import Dict, Optional

from engine.models import Brief, EventMatch
from engine.sql.dialects import Dialect, dialect

LEDGER = "lcm_flow_entries"
DIALECTS = ("postgres", "snowflake", "bigquery")


def _q(v: object) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def month(D: Dialect, expr: str) -> str:
    return {"postgres": f"DATE_TRUNC('month', {expr})", "duckdb": f"date_trunc('month', {expr})",
            "snowflake": f"DATE_TRUNC('month', {expr})", "bigquery": f"DATE_TRUNC({expr}, MONTH)"}[D.name]


def unit_column(brief: Brief, alias: str = "u") -> str:
    """The column the holdout hashes: the account when the brief randomizes by account, else the person."""
    return f"COALESCE({alias}.account_id, {alias}.user_id)" if brief.randomization_unit == "account" else f"{alias}.user_id"


def holdout_cols(brief: Brief, D: Dialect, alias: str = "u") -> str:
    b = D.holdout_bucket(unit_column(brief, alias), brief.slug)
    return f"{b} AS holdout_bucket,\n       {b} < {brief.holdout_pct:g} AS in_holdout"


def global_filter(brief: Brief, D: Dialect, alias: str = "u") -> str:
    if brief.global_holdout_pct <= 0:
        return ""
    return f"\n  AND {D.holdout_bucket(alias + '.user_id', 'lmm_global')} >= {brief.global_holdout_pct:g}"


def source(D: Dialect, m: EventMatch, alias: str) -> str:
    """SELECT user_id, event_at, ref for one event. order_completed reads the orders table (the transaction object)."""
    if m.event == "order_completed":
        return (f"SELECT {alias}.user_id, {alias}.order_at AS event_at, {alias}.order_id AS ref FROM orders {alias} "
                f"WHERE {alias}.status = 'completed' AND {alias}.user_id IS NOT NULL")
    return (f"SELECT {alias}.user_id, {alias}.event_at, {alias}.event_id AS ref FROM track_events {alias} "
            f"WHERE {D.event_filter(alias + '.', m.event, m.where)} AND {alias}.user_id IS NOT NULL")


def _run_ts(D: Dialect, as_of: Optional[str]) -> str:
    """The run time: the end of the as-of day when given (audits read a whole day), else now."""
    return D.ts_literal(f"{as_of} 23:59:59") if as_of else D.now


def _run_date(D: Dialect, as_of: Optional[str]) -> str:
    return D.date_literal(as_of) if as_of else D.current_date


def _header(brief: Brief, name: str, what: str) -> str:
    return (f"-- dialect: {name}\n-- {brief.product_name}, {brief.flow.label.lower()} flow: {what}\n"
            f"-- Trigger: {brief.qualifying_event.describe()} ({brief.qualifying_event.detail()}). "
            f"Goal: {brief.core_conversion_event.describe()} ({brief.core_conversion_event.detail()}).\n")


# ----------------------------------------------------------------------------------------------
# cart and browse abandonment: an event, then hours without the goal
# ----------------------------------------------------------------------------------------------

def _abandon_ctes(brief: Brief, D: Dialect, run: str) -> str:
    q, c = brief.qualifying_event, brief.core_conversion_event
    abandon = brief.abandon_after_hours or brief.flow.abandon_after_hours or 1
    stops = [c] + [EventMatch(event=e) for e in brief.flow.exclude_events]
    stop_union = "\n  UNION ALL\n  ".join(source(D, m, f"s{i}") for i, m in enumerate(stops))
    ref = (f"{D.json_text('t.properties', 'product_id')}" if brief.flow_type == "browse_abandon" else
           f"{D.json_text('t.properties', 'checkout_id')}")
    return f"""triggers AS (
  SELECT t.user_id, MIN(t.event_at) AS started_at, MIN({ref}) AS ref
  FROM track_events t
  WHERE {D.event_filter('t.', q.event, q.where)} AND t.user_id IS NOT NULL AND t.event_at <= {run}
  GROUP BY t.user_id, {D.to_date('t.event_at')}
),
stops AS (
  {stop_union}
),
abandoned AS (
  SELECT tr.user_id, tr.started_at, tr.ref, MIN(st.event_at) AS next_stop_at
  FROM triggers tr
  LEFT JOIN stops st ON st.user_id = tr.user_id AND st.event_at >= tr.started_at
  GROUP BY tr.user_id, tr.started_at, tr.ref
  HAVING MIN(st.event_at) IS NULL OR MIN(st.event_at) > {D.add_hours('tr.started_at', abandon)}
)"""


def abandon_entry_sql(brief: Brief, name: str, as_of: Optional[str] = None, lookback_hours: float = 24, order: bool = True) -> str:
    D = dialect(name)
    run = _run_ts(D, as_of)
    abandon = brief.abandon_after_hours or brief.flow.abandon_after_hours or 1
    return (_header(brief, name, f"who abandoned in the {lookback_hours:g} hours before the run (audit and backfill of the native trigger)") +
            f"-- Entry: {brief.qualifying_event.event} at least {abandon:g} hour(s) ago with no {brief.core_conversion_event.event}"
            + (f" or {', '.join(brief.flow.exclude_events)}" if brief.flow.exclude_events else "") + " since; one per person per day.\n"
            f"WITH {_abandon_ctes(brief, D, run)}\n"
            f"SELECT u.user_id, u.email, a.started_at, a.ref, {D.hours_between(run, 'a.started_at')} AS hours_since,\n"
            f"       u.email_consent_at IS NOT NULL AS email_consent,\n       u.sms_consent_at IS NOT NULL AS sms_consent,\n"
            f"       {holdout_cols(brief, D)}\n"
            f"FROM abandoned a\nJOIN users u ON u.user_id = a.user_id\n"
            f"WHERE a.started_at <= {D.add_hours(run, -abandon)} AND a.started_at > {D.add_hours(run, -(abandon + lookback_hours))}\n"
            f"  AND (a.next_stop_at IS NULL OR a.next_stop_at > {run}){global_filter(brief, D)}\n"
            + ("ORDER BY u.user_id\n" if order else ""))


def abandon_history_sql(brief: Brief, name: str, window_days: int, as_of: Optional[str] = None) -> str:
    D = dialect(name)
    run = _run_ts(D, as_of)
    c = brief.core_conversion_event
    return (_header(brief, name, f"abandoners by month and the share who reached the goal within {window_days} days with no flow") +
            f"WITH {_abandon_ctes(brief, D, run)},\n"
            f"goal AS (\n  {source(D, c, 'g')}\n),\n"
            f"recovered AS (\n  SELECT a.user_id, a.started_at, MIN(g.event_at) AS goal_at\n  FROM abandoned a\n"
            f"  LEFT JOIN goal g ON g.user_id = a.user_id AND g.event_at >= a.started_at\n  GROUP BY a.user_id, a.started_at\n)\n"
            f"SELECT {D.to_date(month(D, 'r.started_at'))} AS month,\n       COUNT(*) AS entrants,\n"
            f"       SUM(CASE WHEN r.started_at <= {D.add_hours(run, -24 * window_days)} THEN 1 ELSE 0 END) AS matured,\n"
            f"       SUM(CASE WHEN r.started_at <= {D.add_hours(run, -24 * window_days)} AND r.goal_at IS NOT NULL\n"
            f"                 AND r.goal_at <= {D.add_hours('r.started_at', 24 * window_days)} THEN 1 ELSE 0 END) AS returned\n"
            f"FROM recovered r\nGROUP BY 1\nORDER BY 1\n")


# ----------------------------------------------------------------------------------------------
# post-purchase: the first order, then a second
# ----------------------------------------------------------------------------------------------

def _first_order_ctes(D: Dialect, run_date: str) -> str:
    return f"""ranked AS (
  SELECT o.user_id, o.order_id, o.order_at, ROW_NUMBER() OVER (PARTITION BY o.user_id ORDER BY o.order_at, o.order_id) AS n
  FROM orders o
  WHERE o.status = 'completed' AND o.user_id IS NOT NULL AND {D.to_date('o.order_at')} <= {run_date}
),
firsts AS (
  SELECT f.user_id, f.order_id, f.order_at, s.order_at AS second_at
  FROM ranked f
  LEFT JOIN ranked s ON s.user_id = f.user_id AND s.n = 2
  WHERE f.n = 1
)"""


def post_purchase_entry_sql(brief: Brief, name: str, as_of: Optional[str] = None, catch_up: int = 3, order: bool = True) -> str:
    D = dialect(name)
    a = _run_date(D, as_of)
    return (_header(brief, name, "first-time buyers due on the run date (audit and backfill of the native trigger)") +
            f"-- Entry: the person's first completed order, placed on the run date or up to {catch_up} days before, with no second order yet.\n"
            f"WITH {_first_order_ctes(D, a)}\n"
            f"SELECT u.user_id, u.email, f.order_id, f.order_at,\n       u.email_consent_at IS NOT NULL AS email_consent,\n"
            f"       {holdout_cols(brief, D)}\n"
            f"FROM firsts f\nJOIN users u ON u.user_id = f.user_id\n"
            f"WHERE {D.to_date('f.order_at')} BETWEEN {D.add_days(a, -catch_up)} AND {a} AND f.second_at IS NULL{global_filter(brief, D)}\n"
            + ("ORDER BY u.user_id\n" if order else ""))


def post_purchase_history_sql(brief: Brief, name: str, window_days: int, as_of: Optional[str] = None) -> str:
    D = dialect(name)
    a = _run_date(D, as_of)
    return (_header(brief, name, f"first orders by month and the share who ordered again within {window_days} days with no flow") +
            f"WITH {_first_order_ctes(D, a)}\n"
            f"SELECT {D.to_date(month(D, 'f.order_at'))} AS month,\n       COUNT(*) AS entrants,\n"
            f"       SUM(CASE WHEN {D.to_date('f.order_at')} <= {D.add_days(a, -window_days)} THEN 1 ELSE 0 END) AS matured,\n"
            f"       SUM(CASE WHEN {D.to_date('f.order_at')} <= {D.add_days(a, -window_days)} AND f.second_at IS NOT NULL\n"
            f"                 AND {D.to_date('f.second_at')} <= {D.add_days(D.to_date('f.order_at'), window_days)} THEN 1 ELSE 0 END) AS returned\n"
            f"FROM firsts f\nGROUP BY 1\nORDER BY 1\n")


# ----------------------------------------------------------------------------------------------
# replenishment: a product's supply runs out
# ----------------------------------------------------------------------------------------------

def _replenish_ctes(brief: Brief, D: Dialect, run_date: str, days: int) -> str:
    where = brief.qualifying_event.where
    scope = "".join(f" AND li.{k} = {_q(v)}" for k, v in where.items() if k in ("product_id", "sku", "category", "variant_id"))
    lead = brief.replenish_lead_days
    return f"""purchases AS (
  SELECT o.user_id, li.product_id, {D.to_date('o.order_at')} AS bought_on
  FROM line_items li
  JOIN orders o ON o.order_id = li.order_id
  WHERE o.status = 'completed' AND o.user_id IS NOT NULL AND {D.to_date('o.order_at')} <= {run_date}{scope}
  GROUP BY o.user_id, li.product_id, {D.to_date('o.order_at')}
),
cycles AS (
  SELECT p.user_id, p.product_id, p.bought_on,
         LEAD(p.bought_on) OVER (PARTITION BY p.user_id, p.product_id ORDER BY p.bought_on) AS next_on,
         COALESCE(pr.replenish_after_days, {int(days)}) AS supply_days
  FROM purchases p
  LEFT JOIN products pr ON pr.product_id = p.product_id
),
due AS (
  SELECT c.user_id, c.product_id, c.bought_on AS anchor_date, c.next_on,
         c.supply_days,
         CAST(c.supply_days - {int(lead)} AS INTEGER) AS due_after
  FROM cycles c
)"""


def _due_on(D: Dialect, alias: str = "d") -> str:
    """The date a purchase cycle comes due: bought_on + supply_days - lead, per row."""
    if D.name in ("postgres", "duckdb"):
        return f"({alias}.anchor_date + {alias}.due_after)"
    if D.name == "snowflake":
        return f"DATEADD(day, {alias}.due_after, {alias}.anchor_date)"
    return f"DATE_ADD({alias}.anchor_date, INTERVAL {alias}.due_after DAY)"


def replenish_entry_sql(brief: Brief, name: str, days: int, as_of: Optional[str] = None, catch_up: int = 3, order: bool = True) -> str:
    D = dialect(name)
    a = _run_date(D, as_of)
    due = _due_on(D)
    detail = _json_obj(D, {"product_id": "d.product_id"})
    return (_header(brief, name, "purchase cycles coming due on the run date") +
            f"-- Entry: the supply (products.replenish_after_days, else {days} days) less {brief.replenish_lead_days} lead days has passed since the\n"
            f"-- purchase, it came due in the last {catch_up} days, there is no repeat purchase of that product, and this cycle is not in {LEDGER}.\n"
            f"WITH {_replenish_ctes(brief, D, a, days)}\n"
            f"SELECT u.user_id, u.email, d.product_id, d.anchor_date, {due} AS due_on, {detail} AS detail,\n"
            f"       u.email_consent_at IS NOT NULL AS email_consent,\n       u.sms_consent_at IS NOT NULL AS sms_consent,\n"
            f"       {holdout_cols(brief, D)}\n"
            f"FROM due d\nJOIN users u ON u.user_id = d.user_id\n"
            f"LEFT JOIN {LEDGER} l ON l.flow = '{brief.slug}' AND l.user_id = d.user_id AND l.anchor_date = d.anchor_date\n"
            f"WHERE {due} BETWEEN {D.add_days(a, -catch_up)} AND {a} AND d.next_on IS NULL AND l.user_id IS NULL{global_filter(brief, D)}\n"
            + ("ORDER BY u.user_id, d.product_id\n" if order else ""))


def replenish_history_sql(brief: Brief, name: str, days: int, window_days: int, as_of: Optional[str] = None) -> str:
    D = dialect(name)
    a = _run_date(D, as_of)
    due = _due_on(D)
    return (_header(brief, name, f"cycles coming due by month and the share reordered within {window_days} days of the due date with no flow") +
            f"WITH {_replenish_ctes(brief, D, a, days)},\n"
            f"dated AS (SELECT d.*, {due} AS due_on FROM due d)\n"
            f"SELECT {D.to_date(month(D, 'x.due_on'))} AS month,\n       COUNT(*) AS entrants,\n"
            f"       SUM(CASE WHEN x.due_on <= {D.add_days(a, -window_days)} THEN 1 ELSE 0 END) AS matured,\n"
            f"       SUM(CASE WHEN x.due_on <= {D.add_days(a, -window_days)} AND x.next_on IS NOT NULL\n"
            f"                 AND x.next_on >= x.due_on AND x.next_on <= {D.add_days('x.due_on', window_days)} THEN 1 ELSE 0 END) AS returned\n"
            f"FROM dated x\nWHERE x.due_on <= {a}\nGROUP BY 1\nORDER BY 1\n")


def _json_obj(D: Dialect, kv: Dict[str, str]) -> str:
    args = ", ".join(f"{_q(k)}, {v}" for k, v in kv.items())
    if D.name == "postgres":
        return f"CAST(json_build_object({args}) AS VARCHAR)"
    if D.name == "snowflake":
        return f"TO_JSON(OBJECT_CONSTRUCT({args}))"
    if D.name == "bigquery":
        return f"TO_JSON_STRING(JSON_OBJECT({args}))"
    return f"CAST(json_object({args}) AS VARCHAR)"


# ----------------------------------------------------------------------------------------------
# dunning: a failed payment, then paid or not
# ----------------------------------------------------------------------------------------------

def dunning_entry_sql(brief: Brief, name: str, as_of: Optional[str] = None, catch_up: int = 3, order: bool = True) -> str:
    D = dialect(name)
    a = _run_date(D, as_of)
    q, c = brief.qualifying_event, brief.core_conversion_event
    return (_header(brief, name, "failed payments due on the run date (audit and backfill of the billing trigger)") +
            f"WITH fails AS (\n  {source(D, q, 'f')}\n),\npaid AS (\n  {source(D, c, 'p')}\n),\n"
            f"open_fails AS (\n  SELECT f.user_id, MIN(f.event_at) AS failed_at, MIN(f.ref) AS ref\n  FROM fails f\n"
            f"  WHERE {D.to_date('f.event_at')} BETWEEN {D.add_days(a, -catch_up)} AND {a}\n  GROUP BY f.user_id\n)\n"
            f"SELECT u.user_id, u.email, o.failed_at, o.ref\nFROM open_fails o\nJOIN users u ON u.user_id = o.user_id\n"
            f"WHERE NOT EXISTS (SELECT 1 FROM paid p WHERE p.user_id = o.user_id AND p.event_at >= o.failed_at)\n"
            + ("ORDER BY u.user_id\n" if order else ""))


def dunning_history_sql(brief: Brief, name: str, window_days: int, as_of: Optional[str] = None) -> str:
    D = dialect(name)
    a = _run_date(D, as_of)
    q, c = brief.qualifying_event, brief.core_conversion_event
    return (_header(brief, name, f"failed payments by month and the share paid within {window_days} days with no flow (processor retries only)") +
            f"WITH fails AS (\n  SELECT s.user_id, MIN(s.event_at) AS failed_at FROM ({source(D, q, 'f')}) s\n"
            f"  WHERE {D.to_date('s.event_at')} <= {a}\n  GROUP BY s.user_id, {D.to_date('s.event_at')}\n),\n"
            f"paid AS (\n  {source(D, c, 'p')}\n),\n"
            f"recovered AS (\n  SELECT f.user_id, f.failed_at, MIN(p.event_at) AS paid_at\n  FROM fails f\n"
            f"  LEFT JOIN paid p ON p.user_id = f.user_id AND p.event_at >= f.failed_at\n  GROUP BY f.user_id, f.failed_at\n)\n"
            f"SELECT {D.to_date(month(D, 'r.failed_at'))} AS month,\n       COUNT(*) AS entrants,\n"
            f"       SUM(CASE WHEN {D.to_date('r.failed_at')} <= {D.add_days(a, -window_days)} THEN 1 ELSE 0 END) AS matured,\n"
            f"       SUM(CASE WHEN {D.to_date('r.failed_at')} <= {D.add_days(a, -window_days)} AND r.paid_at IS NOT NULL\n"
            f"                 AND {D.to_date('r.paid_at')} <= {D.add_days(D.to_date('r.failed_at'), window_days)} THEN 1 ELSE 0 END) AS returned\n"
            f"FROM recovered r\nGROUP BY 1\nORDER BY 1\n")


# ----------------------------------------------------------------------------------------------
# sunset: deliveries without a click
# ----------------------------------------------------------------------------------------------

def _sunset_ctes(brief: Brief, D: Dialect, run_date: str) -> str:
    n = brief.churn_threshold_days
    return f"""clicks AS (
  SELECT m.user_id, MAX({D.to_date('m.event_at')}) AS last_click_on
  FROM message_events m WHERE m.channel = 'email' AND m.event_name IN ('clicked', 'replied') AND {D.to_date('m.event_at')} <= {run_date}
  GROUP BY m.user_id
),
firsts AS (
  SELECT m.user_id, MIN({D.to_date('m.event_at')}) AS first_delivery_on
  FROM message_events m WHERE m.channel = 'email' AND m.event_name = 'delivered' AND {D.to_date('m.event_at')} <= {run_date}
  GROUP BY m.user_id
),
anchored AS (
  SELECT f.user_id, COALESCE(c.last_click_on, f.first_delivery_on) AS anchor_date
  FROM firsts f LEFT JOIN clicks c ON c.user_id = f.user_id
),
counted AS (
  SELECT a.user_id, a.anchor_date, {D.add_days('a.anchor_date', n)} AS crossed_on,
         SUM(CASE WHEN m.event_name = 'delivered' AND {D.to_date('m.event_at')} > a.anchor_date THEN 1 ELSE 0 END) AS deliveries_since
  FROM anchored a
  JOIN message_events m ON m.user_id = a.user_id AND m.channel = 'email'
  GROUP BY a.user_id, a.anchor_date
)"""


def sunset_entry_sql(brief: Brief, name: str, as_of: Optional[str] = None, catch_up: int = 3, order: bool = True) -> str:
    D = dialect(name)
    a = _run_date(D, as_of)
    return (_header(brief, name, f"people who crossed {brief.churn_threshold_days} days without a click on the run date") +
            f"-- Entry: at least 5 email deliveries and no click or reply in {brief.churn_threshold_days} days, crossed in the last {catch_up} days,\n"
            f"-- and not yet in {LEDGER}. One re-permission ask per person per anchor.\n"
            f"WITH {_sunset_ctes(brief, D, a)}\n"
            f"SELECT u.user_id, u.email, c.anchor_date, c.crossed_on, c.deliveries_since,\n"
            f"       u.email_consent_at IS NOT NULL AS email_consent,\n       {holdout_cols(brief, D)}\n"
            f"FROM counted c\nJOIN users u ON u.user_id = c.user_id\n"
            f"LEFT JOIN {LEDGER} l ON l.flow = '{brief.slug}' AND l.user_id = c.user_id AND l.anchor_date = c.anchor_date\n"
            f"WHERE c.crossed_on BETWEEN {D.add_days(a, -catch_up)} AND {a} AND c.deliveries_since >= 5 AND l.user_id IS NULL\n"
            + ("ORDER BY u.user_id\n" if order else ""))


def sunset_history_sql(brief: Brief, name: str, window_days: int, as_of: Optional[str] = None) -> str:
    D = dialect(name)
    a = _run_date(D, as_of)
    return (_header(brief, name, f"crossings by month and the share who clicked again within {window_days} days with no flow") +
            f"WITH {_sunset_ctes(brief, D, a)},\n"
            f"later AS (\n  SELECT c.user_id, c.crossed_on, MIN({D.to_date('m.event_at')}) AS clicked_on\n  FROM counted c\n"
            f"  LEFT JOIN message_events m ON m.user_id = c.user_id AND m.event_name IN ('clicked', 'replied') AND {D.to_date('m.event_at')} > c.crossed_on\n"
            f"  WHERE c.crossed_on <= {a} AND c.deliveries_since >= 5\n  GROUP BY c.user_id, c.crossed_on\n)\n"
            f"SELECT {D.to_date(month(D, 'l.crossed_on'))} AS month,\n       COUNT(*) AS entrants,\n"
            f"       SUM(CASE WHEN l.crossed_on <= {D.add_days(a, -window_days)} THEN 1 ELSE 0 END) AS matured,\n"
            f"       SUM(CASE WHEN l.crossed_on <= {D.add_days(a, -window_days)} AND l.clicked_on IS NOT NULL\n"
            f"                 AND l.clicked_on <= {D.add_days('l.crossed_on', window_days)} THEN 1 ELSE 0 END) AS returned\n"
            f"FROM later l\nGROUP BY 1\nORDER BY 1\n")


# ----------------------------------------------------------------------------------------------
# the daily job for flows the warehouse triggers (replenishment, sunset): the same outbox as win-back
# ----------------------------------------------------------------------------------------------

def queue_sql(brief: Brief, name: str, entry: str) -> str:
    body = "\n".join(line for line in entry.splitlines() if not line.startswith("--"))
    now = dialect(name).now
    has_detail = " AS detail," in entry
    return (f"-- dialect: {name}\n-- Daily step 1 of 3 for {brief.slug}: queue every entrant who is due as 'pending'.\n"
            f"INSERT INTO {LEDGER} (flow, user_id, anchor_date, holdout_bucket, in_holdout, status, queued_at, detail)\n"
            f"SELECT '{brief.slug}', e.user_id, e.anchor_date, e.holdout_bucket, e.in_holdout, 'pending', {now}, "
            f"{'e.detail' if has_detail else 'NULL'}\nFROM (\n{body}\n) e\n")


def pending_sql(brief: Brief, name: str) -> str:
    return (f"-- dialect: {name}\n-- Daily step 2 of 3 for {brief.slug}: push these rows, one trigger event each (lcm send --approve).\n"
            f"SELECT l.user_id, u.email, l.anchor_date, l.holdout_bucket, l.in_holdout, l.detail\n"
            f"FROM {LEDGER} l\nJOIN users u ON u.user_id = l.user_id\n"
            f"WHERE l.flow = '{brief.slug}' AND l.status = 'pending'\nORDER BY l.anchor_date, l.user_id\n")
