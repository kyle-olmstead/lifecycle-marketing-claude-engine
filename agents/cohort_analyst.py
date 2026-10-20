"""Cohort Analyst: who enters the flow, on what evidence, and what the event stream says today.

Two jobs.

define   From a Brief: the exact entry rule (qualifying event, inactivity threshold measured from
         each person's own last core action), the exclusions, the exit, the platform pattern for an
         absence trigger, and the SQL in the warehouse dialect plus DuckDB. With data (--data), it
         also verifies every event the flow depends on, measures property fill rates, counts today's
         entrants, and estimates monthly entry volume and the natural return rate from history.

audit    The daily health check (.github/workflows/daily_cohort_audit.yml, 05:00 UTC). Daily series
         for signups, active users, core-action users, orders and threshold crossings; a snapshot of
         the qualified base by risk band (active, at risk, lapsed); the onboarding leak map; robust
         anomaly detection on the latest complete day; and a data-freshness check.

Absence is invisible to most ESPs, so the recommended trigger is a warehouse job that runs the entry
query daily and pushes one event per entrant (tools/event_payloads.py builds the request bodies).
The holdout bucket is computed in the same query, so the warehouse records both arms.

    python3 -m agents.cohort_analyst define --brief briefs/examples/winback_14d.yaml --data tests/fixtures
    python3 -m agents.cohort_analyst audit --brief briefs/examples/winback_14d.yaml --data tests/fixtures --out out/audit
    python3 -m agents.cohort_analyst audit-sql --brief briefs/examples/winback_14d.yaml --dialect bigquery --out out/audit_sql
    python3 -m agents.cohort_analyst audit --brief briefs/examples/winback_14d.yaml --from-results out/audit_results --out out/audit
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.models import Brief, CohortDefinition, DataDependency, EventMatch, Estimate, PLATFORM_LABEL  # noqa: E402
from agents.sql_dialects import Dialect, dialect  # noqa: E402
from tools import sql_check  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
TABLES = ("users", "orders", "track_events")
AUDIT_LOOKBACK_DAYS = 56
ANOMALY_Z = 3.5          # Iglewicz and Hoaglin's cut-off for the modified z-score
MIN_MEDIAN_FOR_ANOMALY = 5
STALE_AFTER_DAYS = 2

PLATFORM_PATTERNS = {
    "klaviyo": "Metric-triggered flow on the pushed {trigger} event. Flow filter (every step): has not done {core} since starting this flow. "
               "Holdout: conditional split on the event property in_holdout.",
    "hubspot": "Contact-based workflow enrolled on the custom event {trigger}, with re-enrollment on so a person who lapses again re-enters. "
               "Goal criteria: {core} occurred after enrollment. Holdout: if/then branch on the event property in_holdout.",
    "braze": "Canvas with action-based entry on the custom event {trigger}, re-eligibility on. Exit criteria: performs {core}. "
             "Holdout: Audience Paths on the event property in_holdout (Canvas entry properties).",
    "customerio": "Event-triggered campaign on {trigger}, re-entry allowed. Exit: performs {core} (conversion goal with exit on conversion). "
                  "Holdout: True/False branch on the event attribute in_holdout.",
    "sfmc": "Journey Builder with an API Event entry fed by the daily job. Goal and exit criteria: {core} recorded in the data extension. "
            "Holdout: Decision Split on in_holdout from the entry event.",
}
ACTIVATION_PATTERNS = {
    "klaviyo": "Metric-triggered flow on {trigger}{filter}. Flow filter (every step): has not done {core} since starting this flow. "
               "Holdout: conditional split on the trigger event's in_holdout property.",
    "hubspot": "Contact-based workflow enrolled on the {trigger} event{filter}, re-enrollment off. Goal criteria: {core}. "
               "Holdout: if/then branch on in_holdout (copied from the event to a contact property at enrollment).",
    "braze": "Canvas with action-based entry on {trigger}{filter}, re-eligibility off. Exit criteria: performs {core}. "
             "Holdout: Audience Paths on the in_holdout entry property.",
    "customerio": "Event-triggered campaign on {trigger}{filter}, re-entry off. Exit: performs {core} (conversion goal with exit on conversion). "
                  "Holdout: True/False branch on the event attribute in_holdout.",
    "sfmc": "Journey Builder with an API Event entry on {trigger}{filter}. Goal and exit criteria: {core}. "
            "Holdout: Decision Split on in_holdout from the entry event.",
}
CATCH_UP_DAYS = 3
LEDGER = "lcm_flow_entries"


# ----------------------------------------------------------------------------------------------
# data access
# ----------------------------------------------------------------------------------------------

def connect(data: Optional[str]):
    """DuckDB over the canonical tables, from a folder or URL prefix holding users, orders and track_events (.parquet or .csv).

    For each table, .parquet is tried first, then .csv. orders and the entry ledger are optional: a product without orders
    has an empty orders table, and a first run has an empty ledger."""
    import duckdb
    if data is None:
        return None
    con = duckdb.connect()
    remote = data.startswith(("http://", "https://", "s3://", "gs://"))
    if remote:
        con.execute("INSTALL httpfs; LOAD httpfs;")
    base = data.rstrip("/")
    for table, cols in sql_check.TABLE_DDL.items():
        con.execute(f"CREATE TABLE {table} ({cols})")
        errors = []
        loaded = False
        for source in (f"{base}/{table}.parquet", f"{base}/{table}.csv"):
            if not remote and not Path(source).exists():
                continue
            reader = (f"read_parquet('{source}')" if source.endswith(".parquet") else
                      f"read_csv('{source}', header=true, all_varchar=true, nullstr='')")
            try:
                con.execute(f"INSERT INTO {table} SELECT * FROM {reader}")
                loaded = True
                break
            except Exception as e:  # noqa: BLE001
                errors.append(f"{source}: {str(e).splitlines()[0]}")
        if not loaded and table not in ("orders",) + sql_check.OPTIONAL_TABLES:
            raise FileNotFoundError(f"{data}: could not load {table} from .parquet or .csv ({'; '.join(errors) or 'no file'})")
    return con


def data_label(data: Optional[str]) -> str:
    if data is None:
        return "UNKNOWN"
    try:
        if Path(data).resolve() == FIXTURES.resolve():
            return "DEMO DATA"
    except OSError:
        pass
    return "LIVE PULL"


def latest_complete_day(con) -> Tuple[date, date]:
    """(as_of, latest event date). as_of is the latest event date, or yesterday UTC if the feed reaches today."""
    latest = con.execute("SELECT CAST(MAX(event_at) AS DATE) FROM track_events").fetchone()[0]
    if latest is None:
        raise ValueError("track_events is empty")
    today = datetime.now(timezone.utc).date()
    return (min(latest, today - timedelta(days=1)), latest)


def _rows(con, sql: str) -> List[Dict[str, Any]]:
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ----------------------------------------------------------------------------------------------
# SQL builders
# ----------------------------------------------------------------------------------------------

def _source(D: Dialect, m: EventMatch, alias: str) -> str:
    """SELECT user_id, event_at for one event. order_completed reads the transaction object, not the event stream."""
    if m.event == "order_completed":
        return (f"SELECT {alias}.user_id, {alias}.order_at AS event_at FROM orders {alias} "
                f"WHERE {alias}.status = 'completed' AND {alias}.user_id IS NOT NULL")
    return (f"SELECT {alias}.user_id, {alias}.event_at FROM track_events {alias} "
            f"WHERE {D.event_filter(alias + '.', m.event, m.where)} AND {alias}.user_id IS NOT NULL")


def _as_of_expr(D: Dialect, as_of: Optional[str]) -> str:
    return D.date_literal(as_of) if as_of else D.current_date


def _ctes(D: Dialect, brief: Brief, as_of: str) -> str:
    q, c = brief.qualifying_event, brief.core_conversion_event
    return f"""qualified AS (
  SELECT s.user_id, MIN(s.event_at) AS qualified_at
  FROM ({_source(D, q, 'qe')}) s
  WHERE {D.to_date('s.event_at')} <= {as_of}
  GROUP BY s.user_id
),
last_core AS (
  SELECT s.user_id, MAX(s.event_at) AS last_core_at
  FROM ({_source(D, c, 'ce')}) s
  JOIN qualified q ON q.user_id = s.user_id
  WHERE {D.to_date('s.event_at')} <= {as_of}
  GROUP BY s.user_id
),
anchored AS (
  SELECT q.user_id, q.qualified_at, lc.last_core_at,
         {D.to_date('GREATEST(COALESCE(lc.last_core_at, q.qualified_at), q.qualified_at)')} AS anchor_date
  FROM qualified q
  LEFT JOIN last_core lc ON lc.user_id = q.user_id
)"""


def _window(brief: Brief) -> int:
    from agents.campaign_architect import outcome_window
    return outcome_window(brief)


def entry_sql(brief: Brief, dialect_name: str, as_of: Optional[str] = None, catch_up: int = CATCH_UP_DAYS, order: bool = True) -> str:
    """Who is due on the run date. Win-back: each lapse is keyed by (flow, user_id, anchor_date); lapses already in the
    ledger lcm_flow_entries are excluded, so a late or missed run catches up for `catch_up` days without queuing anyone
    twice (queue_sql writes the due rows to the ledger; pending_sql and mark_sql drive the push). Activation: an audit
    and backfill of the live trigger, with no ledger."""
    D = dialect(dialect_name)
    a = _as_of_expr(D, as_of)
    salt = brief.slug
    pct = brief.holdout_pct
    n = brief.churn_threshold_days
    bucket = D.holdout_bucket("u.user_id", salt)
    header = (f"-- dialect: {dialect_name}\n"
              f"-- {brief.product_name}, {brief.flow_type} flow: who is due on the run date ({'the literal as-of date' if as_of else 'today'}).\n"
              f"-- Qualifying: {brief.qualifying_event.describe()} ({brief.qualifying_event.detail()}). "
              f"Core action: {brief.core_conversion_event.describe()} ({brief.core_conversion_event.detail()}).\n")
    tail = "ORDER BY u.user_id\n" if order else ""
    cols = (f"SELECT u.user_id, u.email, a.qualified_at, a.last_core_at, a.anchor_date,\n"
            f"       {D.days_between(a, 'a.anchor_date')} AS days_inactive,\n"
            f"       u.email_consent_at IS NOT NULL AS email_consent,\n"
            f"       u.sms_consent_at IS NOT NULL AS sms_consent,\n"
            f"       {bucket} AS holdout_bucket,\n"
            f"       {bucket} < {pct:g} AS in_holdout\n")
    if brief.flow_type == "activation":
        return (header +
                f"-- Entry: qualified on the run date or up to {catch_up} days before, and has not done the core action. The live trigger\n"
                f"-- is the ESP's own {brief.qualifying_event.event} event; this query audits and backfills it.\n"
                f"WITH {_ctes(D, brief, a)}\n" + cols +
                f"FROM anchored a\nJOIN users u ON u.user_id = a.user_id\n"
                f"WHERE {D.to_date('a.qualified_at')} BETWEEN {D.add_days(a, -catch_up)} AND {a} AND a.last_core_at IS NULL\n" + tail)
    return (header +
            f"-- Entry: {n} calendar days or more since the last core action (or since qualifying, if none since): the anchor date is\n"
            f"-- between {n} and {n + catch_up} days before the run date, and this lapse (user, anchor date) is not yet in {LEDGER}.\n"
            f"-- A missed run catches up for {catch_up} days. The ESP applies consent and suppression; this query decides who crossed.\n"
            f"-- Holdout: holdout_bucket < {pct:g} (salt '{salt}'); identical in every dialect and in agents/sql_dialects.holdout_bucket.\n"
            f"WITH {_ctes(D, brief, a)}\n" + cols +
            f"FROM anchored a\nJOIN users u ON u.user_id = a.user_id\n"
            f"LEFT JOIN {LEDGER} l ON l.flow = '{salt}' AND l.user_id = a.user_id AND l.anchor_date = a.anchor_date\n"
            f"WHERE a.anchor_date BETWEEN {D.add_days(a, -(n + catch_up))} AND {D.add_days(a, -n)}\n"
            f"  AND l.user_id IS NULL\n" + tail)


def _now(dialect_name: str) -> str:
    return {"snowflake": "CURRENT_TIMESTAMP()", "bigquery": "CURRENT_TIMESTAMP()"}.get(dialect_name, "CURRENT_TIMESTAMP")


def ledger_ddl(dialect_name: str) -> str:
    text, boolean, integer = ("STRING", "BOOL", "INT64") if dialect_name == "bigquery" else ("VARCHAR", "BOOLEAN", "INTEGER")
    key = "" if dialect_name == "bigquery" else ",\n  PRIMARY KEY (flow, user_id, anchor_date)"
    return (f"-- dialect: {dialect_name}\n"
            f"-- The ledger of win-back entries: an outbox. queue writes due lapses as 'pending', the job pushes every pending row,\n"
            f"-- and mark sets them to 'pushed'. A lapse is in here at most once, so nobody enters twice for one lapse. Create once.\n"
            f"CREATE TABLE IF NOT EXISTS {LEDGER} (\n  flow {text} NOT NULL,\n  user_id {text} NOT NULL,\n  anchor_date DATE NOT NULL,\n"
            f"  holdout_bucket {integer} NOT NULL,\n  in_holdout {boolean} NOT NULL,\n  status {text} NOT NULL,\n"
            f"  queued_at TIMESTAMP NOT NULL,\n  pushed_at TIMESTAMP{key}\n)\n")


def queue_sql(brief: Brief, dialect_name: str, as_of: Optional[str] = None) -> str:
    """Step 1 of the daily job: evaluate who is due and write them to the ledger as pending, in one statement."""
    body = "\n".join(line for line in entry_sql(brief, dialect_name, as_of, order=False).splitlines() if not line.startswith("--"))
    return (f"-- dialect: {dialect_name}\n-- Daily step 1 of 3 for {brief.slug}: queue every lapse that is due as 'pending'.\n"
            f"INSERT INTO {LEDGER} (flow, user_id, anchor_date, holdout_bucket, in_holdout, status, queued_at)\n"
            f"SELECT '{brief.slug}', e.user_id, e.anchor_date, e.holdout_bucket, e.in_holdout, 'pending', {_now(dialect_name)}\n"
            f"FROM (\n{body}\n) e\n")


def pending_sql(brief: Brief, dialect_name: str, as_of: Optional[str] = None) -> str:
    """Step 2: the rows to push, including any left pending by an earlier failed push. Feed them to tools/event_payloads.py."""
    D = dialect(dialect_name)
    a = _as_of_expr(D, as_of)
    return (f"-- dialect: {dialect_name}\n-- Daily step 2 of 3 for {brief.slug}: push these rows, one trigger event each "
            f"(tools/event_payloads.py).\n"
            f"SELECT l.user_id, u.email, l.anchor_date, {D.days_between(a, 'l.anchor_date')} AS days_inactive,\n"
            f"       l.holdout_bucket, l.in_holdout\n"
            f"FROM {LEDGER} l\nJOIN users u ON u.user_id = l.user_id\n"
            f"WHERE l.flow = '{brief.slug}' AND l.status = 'pending'\nORDER BY l.anchor_date, l.user_id\n")


MARK_PARAMS = {"postgres": (":user_id", "CAST(:anchor_date AS DATE)"), "snowflake": (":user_id", "CAST(:anchor_date AS DATE)"),
               "bigquery": ("@user_id", "@anchor_date"), "duckdb": ("$user_id", "CAST($anchor_date AS DATE)")}


def mark_sql(brief: Brief, dialect_name: str) -> str:
    """Step 3: once per pushed row, right after the request carrying it succeeded (for a Braze batch, each row in it).
    Only those keys are marked, so a row read by an overlapping run, or one whose request failed, stays pending and goes out
    on the next run. A row is pushed twice only if its request succeeded and its mark then failed: Klaviyo, HubSpot and
    Customer.io drop that repeat (the event id is the lapse key); Braze and Salesforce Marketing Cloud do not."""
    uid, anchor = MARK_PARAMS[dialect_name]
    bind = ("BigQuery query parameters @user_id (STRING) and @anchor_date (DATE)" if dialect_name == "bigquery" else
            "named parameters :user_id and :anchor_date (SQLAlchemy text() binds :name in every dialect)" if dialect_name != "duckdb" else
            "DuckDB parameters $user_id and $anchor_date")
    return (f"-- dialect: {dialect_name}\n-- no-run: needs bound parameters\n"
            f"-- Daily step 3 of 3 for {brief.slug}: once per row whose push succeeded. Bind {bind}.\n"
            f"UPDATE {LEDGER} SET status = 'pushed', pushed_at = {_now(dialect_name)}\n"
            f"WHERE flow = '{brief.slug}' AND status = 'pending' AND user_id = {uid} AND anchor_date = {anchor}\n")


def daily_job_sql(brief: Brief, dialect_name: str) -> Dict[str, str]:
    return {"ledger": ledger_ddl(dialect_name), "queue": queue_sql(brief, dialect_name), "pending": pending_sql(brief, dialect_name),
            "mark": mark_sql(brief, dialect_name)}


def _lapses_ctes(D: Dialect, brief: Brief, a: str) -> str:
    n = brief.churn_threshold_days
    return f"""{_ctes(D, brief, a)},
core_and_anchor AS (
  SELECT s.user_id, s.event_at
  FROM ({_source(D, brief.core_conversion_event, 'ce2')}) s
  JOIN qualified q ON q.user_id = s.user_id AND s.event_at >= q.qualified_at
  UNION ALL
  SELECT user_id, qualified_at AS event_at FROM qualified
),
seq AS (
  SELECT user_id, {D.to_date('event_at')} AS d,
         LEAD({D.to_date('event_at')}) OVER (PARTITION BY user_id ORDER BY event_at) AS next_d
  FROM core_and_anchor
),
lapses AS (
  SELECT user_id, {D.add_days('d', n)} AS crossed_on, next_d
  FROM seq
  WHERE (next_d IS NULL OR next_d > {D.add_days('d', n)}) AND {D.add_days('d', n)} <= {a}
)"""


def _month(dialect_name: str, expr: str) -> str:
    return {"postgres": f"DATE_TRUNC('month', {expr})", "duckdb": f"date_trunc('month', {expr})",
            "snowflake": f"DATE_TRUNC('month', {expr})", "bigquery": f"DATE_TRUNC({expr}, MONTH)"}[dialect_name]


def _new_qualifiers_ctes(D: Dialect, brief: Brief, a: str) -> str:
    """Activation: people who qualified, minus those who had already done the core action, with their first core action after."""
    c = brief.core_conversion_event
    return f"""{_ctes(D, brief, a)},
core_after AS (
  SELECT q.user_id,
         MIN(CASE WHEN s.event_at >= q.qualified_at THEN s.event_at END) AS first_core_at,
         MAX(CASE WHEN s.event_at < q.qualified_at THEN 1 ELSE 0 END) AS core_before
  FROM qualified q
  JOIN ({_source(D, c, 'ce2')}) s ON s.user_id = q.user_id
  GROUP BY q.user_id
),
newcomers AS (
  SELECT q.user_id, {D.to_date('q.qualified_at')} AS entered_on, {D.to_date('ca.first_core_at')} AS first_core_on
  FROM qualified q
  LEFT JOIN core_after ca ON ca.user_id = q.user_id
  WHERE COALESCE(ca.core_before, 0) = 0
)"""


def history_sql(brief: Brief, dialect_name: str, as_of: Optional[str] = None) -> str:
    """Entrants by month, and the share who did the core action within the outcome window with no flow running.
    Win-back: threshold crossings and their natural return. Activation: new qualifiers and their unaided activation."""
    D = dialect(dialect_name)
    a = _as_of_expr(D, as_of)
    w = _window(brief)
    if brief.flow_type == "activation":
        return (f"-- dialect: {dialect_name}\n"
                f"-- {brief.product_name}: new qualifiers by month (would-be entrants), and the unaided activation rate: the share of\n"
                f"-- matured qualifiers (at least {w} days ago) who did the core action within {w} days, with no flow running.\n"
                f"WITH {_new_qualifiers_ctes(D, brief, a)}\n"
                f"SELECT {D.to_date(_month(dialect_name, 'n.entered_on'))} AS month,\n"
                f"       COUNT(*) AS entrants,\n"
                f"       SUM(CASE WHEN n.entered_on <= {D.add_days(a, -w)} THEN 1 ELSE 0 END) AS matured,\n"
                f"       SUM(CASE WHEN n.entered_on <= {D.add_days(a, -w)} AND n.first_core_on IS NOT NULL\n"
                f"                 AND n.first_core_on <= {D.add_days('n.entered_on', w)} THEN 1 ELSE 0 END) AS returned\n"
                f"FROM newcomers n\nGROUP BY 1\nORDER BY 1\n")
    return (f"-- dialect: {dialect_name}\n"
            f"-- {brief.product_name}: threshold crossings by month (would-be entrants) and the natural return rate: the share of\n"
            f"-- matured crossings (at least {w} days old) whose next core action came within {w} days, with no flow running.\n"
            f"WITH {_lapses_ctes(D, brief, a)}\n"
            f"SELECT {D.to_date(_month(dialect_name, 'l.crossed_on'))} AS month,\n"
            f"       COUNT(*) AS entrants,\n"
            f"       SUM(CASE WHEN l.crossed_on <= {D.add_days(a, -w)} THEN 1 ELSE 0 END) AS matured,\n"
            f"       SUM(CASE WHEN l.crossed_on <= {D.add_days(a, -w)} AND l.next_d IS NOT NULL\n"
            f"                 AND l.next_d <= {D.add_days('l.crossed_on', w)} THEN 1 ELSE 0 END) AS returned\n"
            f"FROM lapses l\nGROUP BY 1\nORDER BY 1\n")


def audit_sql(brief: Brief, dialect_name: str, as_of: Optional[str] = None, lookback: int = AUDIT_LOOKBACK_DAYS) -> Dict[str, str]:
    """Three queries, each small enough to return as JSON from any warehouse CLI."""
    D = dialect(dialect_name)
    a = _as_of_expr(D, as_of)
    start = D.add_days(a, -(lookback - 1))
    n = brief.churn_threshold_days
    half = max(1, math.ceil(n / 2))
    in_window = lambda col: f"{D.to_date(col)} BETWEEN {start} AND {a}"  # noqa: E731
    c = brief.core_conversion_event
    if brief.flow_type == "activation":
        ctes = _new_qualifiers_ctes(D, brief, a)
        entries = (f"SELECT n.entered_on AS day, 'new_qualifiers' AS metric, COUNT(*) AS value\n"
                   f"FROM newcomers n WHERE n.entered_on BETWEEN {start} AND {a} GROUP BY 1\n")
    else:
        ctes = _lapses_ctes(D, brief, a)
        entries = (f"SELECT l.crossed_on AS day, 'threshold_crossings' AS metric, COUNT(*) AS value\n"
                   f"FROM lapses l WHERE l.crossed_on BETWEEN {start} AND {a} GROUP BY 1\n")
    daily = (f"-- dialect: {dialect_name}\n-- daily series for the audit, long format: day, metric, value\n"
             f"WITH {ctes}\n"
             f"SELECT {D.to_date('t.event_at')} AS day, 'signups' AS metric, COUNT(DISTINCT t.user_id) AS value\n"
             f"FROM track_events t WHERE t.event_name = 'signed_up' AND {in_window('t.event_at')} GROUP BY 1\n"
             f"UNION ALL\n"
             f"SELECT {D.to_date('t.event_at')} AS day, 'active_users' AS metric, COUNT(DISTINCT t.user_id) AS value\n"
             f"FROM track_events t WHERE t.user_id IS NOT NULL AND {in_window('t.event_at')} GROUP BY 1\n"
             f"UNION ALL\n"
             f"SELECT {D.to_date('s.event_at')} AS day, 'core_action_users' AS metric, COUNT(DISTINCT s.user_id) AS value\n"
             f"FROM ({_source(D, c, 'ce3')}) s WHERE {in_window('s.event_at')} GROUP BY 1\n"
             f"UNION ALL\n"
             f"SELECT {D.to_date('o.order_at')} AS day, 'orders' AS metric, COUNT(*) AS value\n"
             f"FROM orders o WHERE o.status = 'completed' AND {in_window('o.order_at')} GROUP BY 1\n"
             f"UNION ALL\n" + entries +
             f"ORDER BY 1, 2\n")
    snapshot = (f"-- dialect: {dialect_name}\n-- the qualified base on the as-of date, by days since the last core action\n"
                f"WITH {_ctes(D, brief, a)}\n"
                f"SELECT CASE WHEN {D.days_between(a, 'anchor_date')} >= {n} THEN 'lapsed'\n"
                f"            WHEN {D.days_between(a, 'anchor_date')} >= {half} THEN 'at_risk'\n"
                f"            ELSE 'active' END AS band,\n"
                f"       COUNT(*) AS users\n"
                f"FROM anchored\nGROUP BY 1\nORDER BY 1\n")
    funnel = (f"-- dialect: {dialect_name}\n-- onboarding leak map for people who signed up 8 to 35 days before the as-of date (a week to finish)\n"
              f"WITH cohort AS (\n"
              f"  SELECT DISTINCT t.user_id FROM track_events t\n"
              f"  WHERE t.event_name = 'signed_up' AND {D.to_date('t.event_at')} BETWEEN {D.add_days(a, -35)} AND {D.add_days(a, -8)}\n"
              f")\n"
              f"SELECT '0 signed_up' AS step, COUNT(*) AS users FROM cohort\n"
              f"UNION ALL\n"
              f"SELECT CONCAT('1 step ', {D.json_text('t.properties', 'step_index')}) AS step, COUNT(DISTINCT t.user_id) AS users\n"
              f"FROM track_events t JOIN cohort c ON c.user_id = t.user_id\n"
              f"WHERE t.event_name = 'onboarding_step_completed' GROUP BY 1\n"
              f"UNION ALL\n"
              f"SELECT '2 activation_reached' AS step, COUNT(DISTINCT t.user_id) AS users\n"
              f"FROM track_events t JOIN cohort c ON c.user_id = t.user_id WHERE t.event_name = 'activation_reached'\n"
              f"ORDER BY 1\n")
    return {"daily": daily, "snapshot": snapshot, "funnel": funnel}


def check_sql(sql_by_dialect: Dict[str, str]) -> Dict[str, str]:
    out = {}
    for name, sql in sql_by_dialect.items():
        if name == "duckdb":
            continue
        ok, msg = sql_check.parse(sql, name)
        out[name] = "parses" if ok else f"does not parse: {msg}"
    return out


# ----------------------------------------------------------------------------------------------
# define
# ----------------------------------------------------------------------------------------------

def _dependencies(brief: Brief, con) -> List[DataDependency]:
    deps: List[DataDependency] = []

    def event_dep(m: EventMatch, role: str) -> DataDependency:
        name = m.event + (" (" + ", ".join(f"{k}={v}" for k, v in m.where.items()) + ")" if m.where else "")
        if con is None:
            return DataDependency(name=name, kind="event", status="unknown", note=f"{role}; not checked (run with --data)")
        if m.event == "order_completed":
            n = con.execute("SELECT COUNT(*), COUNT(DISTINCT user_id) FROM orders WHERE status = 'completed'").fetchone()
            return DataDependency(name=name, kind="object" if n[0] else "event", status="verified" if n[0] else "blocked",
                                  note=f"{role}; orders table (the transaction object): {n[0]} completed orders, {n[1]} people")
        rows, users = con.execute("SELECT COUNT(*), COUNT(DISTINCT user_id) FROM track_events WHERE event_name = ?", [m.event]).fetchone()
        if rows == 0:
            return DataDependency(name=name, kind="event", status="blocked", note=f"{role}; no {m.event} rows in track_events")
        fill = None
        note = f"{role}; {rows} events from {users} people"
        for k, v in m.where.items():
            filled, matching = con.execute(
                f"SELECT COUNT(json_extract_string(properties, '$.{k}')), "
                f"SUM(CASE WHEN json_extract_string(properties, '$.{k}') = ? THEN 1 ELSE 0 END) "
                f"FROM track_events WHERE event_name = ?", [str(v).lower() if isinstance(v, bool) else str(v), m.event]).fetchone()
            fill = filled / rows
            note += f"; property {k} filled on {fill:.0%} of events, {matching or 0} match {v}"
            if not matching:
                return DataDependency(name=name, kind="event", status="blocked", fill_rate=round(fill, 4), note=note + ": nothing matches the filter")
        return DataDependency(name=name, kind="event", status="verified", fill_rate=None if fill is None else round(fill, 4), note=note)

    deps.append(event_dep(brief.qualifying_event, "qualifies a person for the flow"))
    deps.append(event_dep(brief.core_conversion_event, "the core action: entry clock, exit and primary metric"))

    def attr(name: str, column: str, note: str, required: bool) -> DataDependency:
        if con is None:
            return DataDependency(name=name, kind="attribute", status="unknown", note=note + "; not checked")
        total, filled = con.execute(f"SELECT COUNT(*), COUNT({column}) FROM users").fetchone()
        rate = (filled / total) if total else 0.0
        status = "verified" if filled else ("blocked" if required else "unknown")
        return DataDependency(name=name, kind="attribute", status=status, fill_rate=round(rate, 4), note=f"{note}; users.{column} filled {rate:.0%}")

    deps.append(attr("email", "email", "address for every email touch", True))
    deps.append(attr("email_consent", "email_consent_at", "email marketing consent", True))
    if "sms" in brief.channels:
        deps.append(attr("sms_consent", "sms_consent_at", "SMS consent with an auditable record (10DLC)", False))
    if "push" in brief.channels:
        if con is None:
            deps.append(DataDependency(name="push_permission_changed", kind="event", status="unknown", note="push eligibility; not checked"))
        else:
            n = con.execute("SELECT COUNT(*) FROM track_events WHERE event_name = 'push_permission_changed'").fetchone()[0]
            deps.append(DataDependency(name="push_permission_changed", kind="event", status="verified" if n else "blocked",
                                       note="push eligibility" + ("" if n else "; not tracked, so push touches ship off until it is")))
    if "in_app" in brief.channels:
        deps.append(DataDependency(name="session_started", kind="event", status="unknown" if con is None else
                                   ("verified" if con.execute("SELECT COUNT(*) FROM track_events WHERE event_name='session_started'").fetchone()[0] else "blocked"),
                                   note="in-app messages show at the next session"))
    if brief.flow_type == "winback":
        deps.append(DataDependency(name="in_holdout", kind="attribute", status="unknown",
                                   note="set by the daily entry job on the pushed event; schedule the job before launch"))
        deps.append(DataDependency(name=LEDGER, kind="object", status="unknown",
                                   note="the ledger (outbox) of entries; create it once with cohort/job/ledger_<dialect>.sql"))
    else:
        deps.append(DataDependency(name="in_holdout", kind="attribute", status="unknown",
                                   note=f"a property on the {brief.qualifying_event.event} event, added where it is sent (formula in the build steps)"))
    deps.append(DataDependency(name="lcm_sunset", kind="attribute", status="unknown",
                               note="suppression flag the sunset step writes; create the property in the platform before launch"))
    return deps


def _estimates(brief: Brief, con, as_of: date, label: str) -> Tuple[Estimate, Estimate]:
    if con is None:
        return (Estimate(value=brief.monthly_entrants, label="SUPPLIED" if brief.monthly_entrants is not None else "UNKNOWN",
                         basis="from the brief" if brief.monthly_entrants is not None else "not supplied and no data to estimate from"),
                Estimate(value=brief.baseline_conversion_rate, label="SUPPLIED" if brief.baseline_conversion_rate is not None else "UNKNOWN",
                         basis="from the brief" if brief.baseline_conversion_rate is not None else "not supplied and no data to estimate from"))
    rows = _rows(con, history_sql(brief, "duckdb", as_of.isoformat()))
    this_month = as_of.replace(day=1)
    complete = [r for r in rows if r["month"] < this_month][-3:]
    unit, did = (("threshold crossings", "came back") if brief.flow_type == "winback" else
                 ("new qualifiers", f"went on to {brief.core_conversion_event.do}"))
    entrants = Estimate(value=None, label="UNKNOWN", basis=f"no complete month of {unit} in the data")
    if complete:
        avg = sum(r["entrants"] for r in complete) / len(complete)
        entrants = Estimate(value=round(avg, 1), label=label if label != "LIVE PULL" else "ESTIMATE",
                            basis=f"mean {unit} over {len(complete)} complete month(s) to {complete[-1]['month']:%Y-%m}")
    matured = sum(int(r["matured"] or 0) for r in rows[-6:])
    returned = sum(int(r["returned"] or 0) for r in rows[-6:])
    baseline = Estimate(value=None, label="UNKNOWN", basis=f"only {matured} matured {unit}; at least 30 are needed")
    if matured >= 30:
        baseline = Estimate(value=round(returned / matured, 4), label=label if label != "LIVE PULL" else "ESTIMATE",
                            basis=f"{returned} of {matured} matured {unit} {did} within {_window(brief)} days with no flow")
    if brief.monthly_entrants is not None:
        entrants = Estimate(value=brief.monthly_entrants, label="SUPPLIED", basis=f"from the brief (data says {entrants.value})")
    if brief.baseline_conversion_rate is not None:
        baseline = Estimate(value=brief.baseline_conversion_rate, label="SUPPLIED", basis=f"from the brief (data says {baseline.value})")
    return entrants, baseline


def define(brief: Brief, data: Optional[str] = None, as_of: Optional[str] = None) -> CohortDefinition:
    from tools import event_payloads
    con = connect(data)
    label = data_label(data)
    as_of_date: Optional[date] = date.fromisoformat(as_of) if as_of else None
    if con is not None and as_of_date is None:
        as_of_date, _latest = latest_complete_day(con)
    q, c, n = brief.qualifying_event, brief.core_conversion_event, brief.churn_threshold_days
    trigger = f"core_action_lapsed_{n}d" if brief.flow_type == "winback" else q.event
    sql = {d: entry_sql(brief, d) for d in ("postgres", "snowflake", "bigquery")}
    sql["duckdb"] = entry_sql(brief, "duckdb", as_of_date.isoformat() if as_of_date else None)
    hist = {d: history_sql(brief, d) for d in ("postgres", "snowflake", "bigquery")}
    checks = {f"entry {k}": v for k, v in check_sql(sql).items()}
    checks.update({f"history {k}": v for k, v in check_sql(hist).items()})
    if brief.flow_type == "winback":
        for step in ("ledger", "queue", "pending", "mark"):
            checks.update({f"{step} {k}": v for k, v in check_sql({d: daily_job_sql(brief, d)[step]
                                                                   for d in ("postgres", "snowflake", "bigquery")}).items()})
    entrants_today = None
    if con is not None:
        try:
            rows = _rows(con, sql["duckdb"])
            entrants_today = len(rows)
            checks["entry duckdb"] = f"ran on {label.lower()}: {entrants_today} entrant(s) on {as_of_date}"
        except Exception as e:  # noqa: BLE001
            checks["entry duckdb"] = f"failed: {str(e).splitlines()[0]}"
    monthly, baseline = _estimates(brief, con, as_of_date or date.today(), label)

    if brief.flow_type == "winback":
        plain = (f"People who {q.describe()} and then went {n} days without the core action ({c.describe()}). "
                 f"They enter on day {n}, once per lapse, and leave the moment they do the core action again.")
        entry = [f"{q.describe()} ({q.detail()}) at least once (evaluated at entry)",
                 f"{n} or more calendar days since the core action ({c.detail()}), counted from their own last one, or from qualifying "
                 f"if they never did it",
                 f"queued on the first daily run after crossing (a missed run catches up for {CATCH_UP_DAYS} days) and pushed from "
                 f"the ledger {LEDGER}, which holds each lapse once"]
        exit_rule = f"leaves the moment they {c.do} again (checked before every step)"
    else:
        plain = f"People who {q.describe()} and have not yet done the core action ({c.describe()}). They leave the moment they do it."
        entry = [f"{q.describe()} ({q.detail()}): the flow trigger", f"has not yet done {c.detail()}"]
        exit_rule = f"leaves the moment they {c.do}"
    exclusions = ["no consent for any channel in the flow", "on the platform's suppression list", "internal or test account"]
    if brief.flow_type == "winback":
        pattern = PLATFORM_PATTERNS[brief.platform].format(trigger=trigger, core=c.detail())
    else:
        filt = (" (trigger filter: " + ", ".join(f"{k} = {v}" for k, v in q.where.items()) + ")") if q.where else ""
        pattern = ACTIVATION_PATTERNS[brief.platform].format(trigger=trigger, filter=filt, core=c.detail())
    example: Dict[str, Any] = {}
    if brief.flow_type == "winback" and brief.platform != "sfmc":   # activation is triggered natively; nothing is pushed
        day = (as_of_date or date(2026, 10, 18))
        anchor = (day - timedelta(days=n)).isoformat()
        example = event_payloads.build(brief.platform, {
            "user_id": "u_0002", "email": "u_0002@example.test", "event": trigger,
            "properties": {"days_inactive": n, "in_holdout": False, "holdout_bucket": 42, "flow": brief.slug, "anchor_date": anchor},
            "occurred_at": f"{day.isoformat()}T05:00:00Z", "event_id": f"{brief.slug}:u_0002:{anchor}"})
    return CohortDefinition(
        name=f"{PLATFORM_LABEL[brief.platform]} {brief.flow_type} cohort: {brief.product_name}",
        plain_english=plain, trigger_event=trigger, entry_rules=entry, exclusions=exclusions, exit_rule=exit_rule,
        platform_pattern=pattern, sql=sql, history_sql=hist, sql_checks=checks,
        job_sql=({d: daily_job_sql(brief, d) for d in ("postgres", "snowflake", "bigquery")} if brief.flow_type == "winback" else {}),
        data_dependencies=_dependencies(brief, con), monthly_entrants=monthly, baseline_conversion_rate=baseline,
        entrants_on_as_of=entrants_today, as_of=as_of_date.isoformat() if as_of_date else None,
        trigger_payload_example=example)


# ----------------------------------------------------------------------------------------------
# audit
# ----------------------------------------------------------------------------------------------

def robust_z(value: float, history: List[float]) -> Optional[float]:
    """Modified z-score (Iglewicz and Hoaglin): 0.6745 (x - median) / MAD. None when MAD is zero."""
    med = statistics.median(history)
    mad = statistics.median([abs(h - med) for h in history])
    if mad == 0:
        return None
    return 0.6745 * (value - med) / mad


def detect(series: Dict[str, Dict[date, float]], as_of: date, window: int = 28) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for metric, by_day in series.items():
        history = [float(by_day.get(as_of - timedelta(days=i), 0)) for i in range(1, window + 1)]
        value = float(by_day.get(as_of, 0))
        med = statistics.median(history)
        row: Dict[str, Any] = {"value": value, "median_28d": med, "z": None, "status": "ok"}
        if med < MIN_MEDIAN_FOR_ANOMALY and value < MIN_MEDIAN_FOR_ANOMALY * 2:
            row["status"] = "too small to judge"
        else:
            z = robust_z(value, history)
            if z is None:
                if med and abs(value - med) / med > 0.5:
                    row["status"] = "anomaly"
                    row["reason"] = f"{value:g} against a flat median of {med:g}"
            else:
                row["z"] = round(z, 2)
                if abs(z) >= ANOMALY_Z:
                    row["status"] = "anomaly"
                    row["reason"] = f"{'above' if z > 0 else 'below'} the trailing 28-day median ({med:g}); modified z {z:+.1f}"
        out[metric] = row
    return out


def _to_date(v: Any) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def audit(brief: Brief, data: Optional[str] = None, results: Optional[Path] = None, as_of: Optional[str] = None) -> Dict[str, Any]:
    """Run the three audit queries (DuckDB over files, or read warehouse results) and judge the latest day."""
    label = data_label(data) if data else ("LIVE PULL" if results else "UNKNOWN")
    latest: Optional[date] = None
    if results is not None:
        load = lambda name: json.loads((Path(results) / f"{name}.json").read_text(encoding="utf-8"))  # noqa: E731
        daily, snap, funnel = load("daily"), load("snapshot"), load("funnel")
        latest_rows = [r for r in daily if r.get("metric") == "active_users"]
        latest = max((_to_date(r["day"]) for r in latest_rows), default=None)
        as_of_date = date.fromisoformat(as_of) if as_of else (min(latest, datetime.now(timezone.utc).date() - timedelta(days=1)) if latest else None)
        if as_of_date is None:
            raise ValueError(f"{results}: no active_users rows in daily.json")
    else:
        con = connect(data)
        if con is None:
            raise ValueError("audit needs --data or --from-results")
        as_of_date, latest = latest_complete_day(con)
        if as_of:
            as_of_date = date.fromisoformat(as_of)
        q = audit_sql(brief, "duckdb", as_of_date.isoformat())
        daily, snap, funnel = _rows(con, q["daily"]), _rows(con, q["snapshot"]), _rows(con, q["funnel"])

    entries_metric = "threshold_crossings" if brief.flow_type == "winback" else "new_qualifiers"
    series: Dict[str, Dict[date, float]] = {m: {} for m in ("signups", "active_users", "core_action_users", "orders", entries_metric)}
    for r in daily:
        series.setdefault(r["metric"], {})[_to_date(r["day"])] = float(r["value"])
    metrics = detect(series, as_of_date)
    today = datetime.now(timezone.utc).date()
    lag = (today - latest).days if latest else None
    anomalies = [{"metric": k, **v} for k, v in metrics.items() if v["status"] == "anomaly"]
    if lag is not None and lag > STALE_AFTER_DAYS and label != "DEMO DATA":
        anomalies.insert(0, {"metric": "data_freshness", "value": lag, "status": "anomaly",
                             "reason": f"newest event is {lag} days old: the pipeline into the warehouse may be stalled"})
    bands = {r["band"]: int(r["users"]) for r in snap}
    qualified = sum(bands.values())
    funnel_rows = [{"step": str(r["step"]).split(" ", 1)[1] if " " in str(r["step"]) else str(r["step"]), "users": int(r["users"])} for r in funnel]
    top = funnel_rows[0]["users"] if funnel_rows else 0
    for f in funnel_rows:
        f["share_of_signups"] = round(f["users"] / top, 4) if top else None
    leak = None
    for prev, cur in zip(funnel_rows, funnel_rows[1:]):
        drop = (prev["users"] - cur["users"]) / prev["users"] if prev["users"] else 0
        if leak is None or drop > leak["drop"]:
            leak = {"from": prev["step"], "to": cur["step"], "drop": round(drop, 4)}
    return {
        "as_of": as_of_date.isoformat(), "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "data_label": label, "latest_event_date": latest.isoformat() if latest else None, "data_lag_days": lag,
        "flow": brief.slug, "core_action": brief.core_conversion_event.describe(), "threshold_days": brief.churn_threshold_days,
        "metrics": metrics,
        "risk_bands": {"qualified": qualified, **bands,
                       "at_risk_share": round(bands.get("at_risk", 0) / qualified, 4) if qualified else None,
                       "lapsed_share": round(bands.get("lapsed", 0) / qualified, 4) if qualified else None,
                       "at_risk_definition": f"{max(1, math.ceil(brief.churn_threshold_days / 2))} to {brief.churn_threshold_days - 1} days since the last core action"},
        "funnel": funnel_rows, "largest_leak": leak,
        "anomalies": anomalies, "status": "anomaly" if anomalies else "ok",
        "series_28d": {m: {(as_of_date - timedelta(days=i)).isoformat(): series[m].get(as_of_date - timedelta(days=i), 0) for i in range(27, -1, -1)}
                       for m in series},
    }


def render_audit(r: Dict[str, Any]) -> str:
    lines = [f"## Cohort health: {r['flow']} | {r['as_of']} | {r['data_label']}", ""]
    lines.append(f"**Status: {r['status'].upper()}**" + (f" ({len(r['anomalies'])} finding(s))" if r["anomalies"] else ""))
    for a in r["anomalies"]:
        lines.append(f"- **{a['metric']}**: {a.get('reason', '')}")
    lines += ["", "| Metric | Latest day | 28-day median | Modified z | Status |", "|---|---:|---:|---:|---|"]
    for k, v in r["metrics"].items():
        lines.append(f"| {k} | {v['value']:g} | {v['median_28d']:g} | {'' if v['z'] is None else v['z']} | {v['status']} |")
    b = r["risk_bands"]
    lines += ["", f"**Qualified base:** {b['qualified']:,} people. Active {b.get('active', 0):,}, at risk {b.get('at_risk', 0):,} "
                  f"({b['at_risk_definition']}), lapsed {b.get('lapsed', 0):,}."]
    if r["funnel"]:
        lines += ["", "| Onboarding step | People | Share of signups |", "|---|---:|---:|"]
        lines += [f"| {f['step']} | {f['users']:,} | {'' if f['share_of_signups'] is None else format(f['share_of_signups'], '.0%')} |" for f in r["funnel"]]
        if r["largest_leak"]:
            lk = r["largest_leak"]
            lines.append(f"\nLargest leak: {lk['from']} to {lk['to']}, {lk['drop']:.0%} drop.")
    lines.append(f"\nNewest event: {r['latest_event_date']} ({r['data_lag_days']} day(s) ago).")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------------------------

def _cli(argv: Optional[List[str]] = None) -> int:
    from agents.brief import load_brief
    ap = argparse.ArgumentParser(description="Cohort Analyst: define the entry cohort, or audit cohort health")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("define")
    d.add_argument("--brief", required=True)
    d.add_argument("--data", help="folder or URL prefix with users, orders, track_events (.csv or .parquet)")
    d.add_argument("--as-of")
    a = sub.add_parser("audit")
    a.add_argument("--brief", required=True)
    g = a.add_mutually_exclusive_group(required=True)
    g.add_argument("--data")
    g.add_argument("--from-results", help="folder with daily.json, snapshot.json, funnel.json from a warehouse run of audit-sql")
    a.add_argument("--as-of")
    a.add_argument("--out", required=True)
    a.add_argument("--fail-on-anomaly", action="store_true")
    s = sub.add_parser("audit-sql")
    s.add_argument("--brief", required=True)
    s.add_argument("--dialect", required=True, choices=["postgres", "snowflake", "bigquery"])
    s.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    brief = load_brief(Path(args.brief))
    if args.cmd == "define":
        print(define(brief, args.data, args.as_of).model_dump_json(indent=2))
        return 0
    if args.cmd == "audit-sql":
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for name, sql in audit_sql(brief, args.dialect).items():
            (out / f"{name}.sql").write_text(sql, encoding="utf-8")
        print(f"wrote {', '.join(f'{n}.sql' for n in ('daily', 'snapshot', 'funnel'))} to {out}")
        return 0
    r = audit(brief, args.data, Path(args.from_results) if args.from_results else None, args.as_of)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "audit.json").write_text(json.dumps(r, indent=2, default=str), encoding="utf-8")
    (out / "audit.md").write_text(render_audit(r), encoding="utf-8")
    print(render_audit(r))
    return 1 if (args.fail_on_anomaly and r["status"] == "anomaly") else 0


if __name__ == "__main__":
    sys.exit(_cli())
