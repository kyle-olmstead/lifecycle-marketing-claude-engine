"""Engagement tiers and the unengaged-cohort suppression list, from the ESP's message events.

Who should keep getting marketing email is decided by what people do with it, so this reads the
canonical message_events table (delivered, opened, clicked, bounced, complained, unsubscribed, exported
from the ESP) and places every person with email consent in one tier as of a date:

  suppressed     hard bounced, complained or unsubscribed: never mailed again (the reason is kept)
  engaged_30d    clicked in the last 30 days
  engaged_90d    clicked in the last 90 days
  engaged_180d   clicked in the last 180 days
  unengaged      at least 5 deliveries in the last 180 days and no click in 180 days: the sunset cohort
  new            fewer than 5 deliveries and no click yet: too new to judge

Opens do not count. Mail privacy proxies open everything they fetch, so an open is not a person
reading (machine_open marks the ones the ESP could identify; the rest are not reliable either). The
tier names match engine/deliverability/warmup.py, and unengaged_share is the sending gate's input of that name.

Suppression here means a reversible attribute (lcm_engagement_tier, lcm_sunset) synced to the ESP and
used as a segment exclusion, not an unsubscribe: nobody's consent is changed, and a person who clicks
again leaves the sunset cohort on the next run. adapters/destinations/payloads.py builds the sync requests.

    python3 -m engine.agents.engagement --data examples/brands/ridgeview_saas/data --out out/engagement
    python3 -m engine.agents.engagement --data examples/brands/ridgeview_saas/data --dialect bigquery --sql-only --out out/engagement
    python3 -m engine.agents.engagement --data examples/brands/ridgeview_saas/data --platform klaviyo --out out/engagement
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.sql.dialects import dialect  # noqa: E402

TIERS = ("suppressed", "engaged_30d", "engaged_90d", "engaged_180d", "unengaged", "new")
MIN_DELIVERIES = 5
PLATFORMS = ("klaviyo", "hubspot", "braze", "customerio")


def tiers_sql(dialect_name: str, as_of: Optional[str] = None, min_deliveries: int = MIN_DELIVERIES, order: bool = True) -> str:
    D = dialect(dialect_name)
    a = D.date_literal(as_of) if as_of else D.current_date
    since = lambda days: f"{D.to_date('m.event_at')} > {D.add_days(a, -days)}"  # noqa: E731
    upto = f"{D.to_date('m.event_at')} <= {a}"
    return (f"-- dialect: {dialect_name}\n"
            f"-- Engagement tier per person with email consent, as of {'the literal date' if as_of else 'today'}. Clicks count; opens do not.\n"
            f"WITH stats AS (\n"
            f"  SELECT m.user_id,\n"
            f"         MAX(CASE WHEN m.event_name = 'clicked' AND {upto} THEN m.event_at END) AS last_click_at,\n"
            f"         SUM(CASE WHEN m.event_name = 'delivered' AND {since(180)} AND {upto} THEN 1 ELSE 0 END) AS deliveries_180d,\n"
            f"         MAX(CASE WHEN m.event_name = 'bounced' AND {upto} THEN 1 ELSE 0 END) AS hard_bounced,\n"
            f"         MAX(CASE WHEN m.event_name = 'complained' AND {upto} THEN 1 ELSE 0 END) AS complained,\n"
            f"         MAX(CASE WHEN m.event_name = 'unsubscribed' AND {upto} THEN 1 ELSE 0 END) AS unsubscribed\n"
            f"  FROM message_events m\n"
            f"  WHERE m.channel = 'email'\n"
            f"  GROUP BY m.user_id\n"
            f")\n"
            f"SELECT u.user_id, u.email,\n"
            f"       CASE WHEN COALESCE(s.hard_bounced, 0) = 1 OR COALESCE(s.complained, 0) = 1 OR COALESCE(s.unsubscribed, 0) = 1 THEN 'suppressed'\n"
            f"            WHEN {D.to_date('s.last_click_at')} > {D.add_days(a, -30)} THEN 'engaged_30d'\n"
            f"            WHEN {D.to_date('s.last_click_at')} > {D.add_days(a, -90)} THEN 'engaged_90d'\n"
            f"            WHEN {D.to_date('s.last_click_at')} > {D.add_days(a, -180)} THEN 'engaged_180d'\n"
            f"            WHEN COALESCE(s.deliveries_180d, 0) >= {int(min_deliveries)} THEN 'unengaged'\n"
            f"            ELSE 'new' END AS tier,\n"
            f"       CASE WHEN COALESCE(s.complained, 0) = 1 THEN 'complained'\n"
            f"            WHEN COALESCE(s.hard_bounced, 0) = 1 THEN 'hard bounce'\n"
            f"            WHEN COALESCE(s.unsubscribed, 0) = 1 THEN 'unsubscribed' END AS suppression_reason,\n"
            f"       s.last_click_at, COALESCE(s.deliveries_180d, 0) AS deliveries_180d\n"
            f"FROM users u\n"
            f"LEFT JOIN stats s ON s.user_id = u.user_id\n"
            f"WHERE u.email_consent_at IS NOT NULL AND {D.to_date('u.email_consent_at')} <= {a}\n"
            + ("ORDER BY u.user_id\n" if order else ""))


def counts_sql(dialect_name: str, as_of: Optional[str] = None, min_deliveries: int = MIN_DELIVERIES) -> str:
    """People per tier, for a warehouse run that should return six rows rather than one per person."""
    body = "\n".join(line for line in tiers_sql(dialect_name, as_of, min_deliveries, order=False).splitlines() if not line.startswith("--"))
    return (f"-- dialect: {dialect_name}\n-- People per engagement tier (engine/agents/engagement.py)\n"
            f"SELECT tier, COUNT(*) AS people FROM (\n{body}\n) AS tiers GROUP BY tier ORDER BY tier\n")


def summarize(rows: List[Dict[str, Any]], as_of: str) -> Dict[str, Any]:
    counts = {t: 0 for t in TIERS}
    for r in rows:
        counts[r["tier"]] += 1
    return summarize_counts(counts, as_of)


def summarize_counts(counts: Dict[str, int], as_of: str) -> Dict[str, Any]:
    counts = {t: int(counts.get(t, 0)) for t in TIERS}
    mailable = sum(v for k, v in counts.items() if k != "suppressed")
    unengaged_share = round(counts["unengaged"] / mailable, 4) if mailable else None
    band = None if unengaged_share is None else ("GREEN" if unengaged_share < 0.20 else "RED" if unengaged_share > 0.50 else "AMBER")
    return {"as_of": as_of, "people": sum(counts.values()), "mailable": mailable, "tiers": counts, "unengaged_share": unengaged_share,
            "gate_band": band, "gate_rule": "unengaged_share: GREEN under 20%, AMBER 20 to 50%, RED over 50% (engine/deliverability/analyze.py sending_gate)",
            "warmup_tiers": {k: counts[k] for k in ("engaged_30d", "engaged_90d", "engaged_180d", "unengaged")},
            "sunset_cohort": counts["unengaged"]}


def run(data: str, as_of: Optional[str] = None) -> Dict[str, Any]:
    from engine.agents import cohort_analyst
    con = cohort_analyst.connect(data)
    if as_of is None:
        latest = con.execute("SELECT CAST(MAX(event_at) AS DATE) FROM message_events").fetchone()[0]
        if latest is None:
            raise ValueError(f"{data}: message_events is empty or missing; export delivered, opened, clicked, bounced, complained and "
                             f"unsubscribed events from the ESP (docs/SQL_SCHEMA_CONVENTIONS.md)")
        as_of = latest.isoformat()
    rows = cohort_analyst._rows(con, tiers_sql("duckdb", as_of))
    return {"rows": rows, "summary": summarize(rows, as_of)}


def sync_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rows for adapters/destinations/payloads.py: the tier and the sunset flag, for everyone not already suppressed by the ESP."""
    return [{"user_id": r["user_id"], "email": r["email"], "lcm_engagement_tier": r["tier"], "lcm_sunset": r["tier"] == "unengaged"}
            for r in rows if r["tier"] != "suppressed"]


def render(summary: Dict[str, Any]) -> str:
    s = summary
    lines = [f"## Engagement tiers as of {s['as_of']}", "", "| Tier | People |", "|---|---:|"]
    lines += [f"| {t} | {s['tiers'][t]:,} |" for t in TIERS]
    share = "unknown" if s["unengaged_share"] is None else f"{s['unengaged_share']:.1%}"
    lines += ["", f"Mailable: {s['mailable']:,}. Unengaged share: {share} ({s['gate_band'] or 'n/a'} on the sending gate). "
                  f"Sunset cohort: {s['sunset_cohort']:,} people get lcm_sunset = true and leave marketing sends until they click again."]
    return "\n".join(lines) + "\n"


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Engagement tiers and the unengaged suppression cohort")
    ap.add_argument("--data", help="folder or URL prefix with users and message_events exports")
    ap.add_argument("--as-of")
    ap.add_argument("--dialect", default="postgres", choices=["postgres", "snowflake", "bigquery"])
    ap.add_argument("--sql-only", action="store_true", help="write the tier SQL for your warehouse and stop")
    ap.add_argument("--platform", choices=PLATFORMS, help="also build sync requests for this ESP (nothing is sent)")
    ap.add_argument("--braze-instance", default="iad-01", help="the REST instance in your Braze dashboard URL (iad-01, fra-02, ...)")
    ap.add_argument("--eu", action="store_true", help="Customer.io EU workspace")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"tiers_{a.dialect}.sql").write_text(tiers_sql(a.dialect, a.as_of), encoding="utf-8")
    if a.sql_only:
        print(f"wrote {out / f'tiers_{a.dialect}.sql'}")
        return 0
    if not a.data:
        ap.error("--data is required unless --sql-only")
    try:
        r = run(a.data, a.as_of)
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    with (out / "tiers.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(r["rows"][0].keys()) if r["rows"] else ["user_id"])
        w.writeheader()
        w.writerows(r["rows"])
    (out / "summary.json").write_text(json.dumps(r["summary"], indent=2, default=str), encoding="utf-8")
    (out / "summary.md").write_text(render(r["summary"]), encoding="utf-8")
    if a.platform:
        from adapters.destinations import payloads
        reqs, report = payloads.attribute_sync(a.platform, sync_rows(r["rows"]), braze_instance=a.braze_instance, eu=a.eu)
        for i, b in enumerate(reqs, 1):
            (out / f"{a.platform}_sync_{i:04d}.json").write_text(payloads.dumps(b), encoding="utf-8")
        print(json.dumps(report, indent=2))
    print(render(r["summary"]))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
