"""The daily job: cohort health for every active flow, engagement tiers, and sending-domain health, logged to Sheets.

.github/workflows/daily_cohort_audit.yml runs this at 05:00 UTC. It is one command so the workflow
stays short and the same run works on a laptop:

    python3 scripts/daily.py                                   # demo fixtures, dry-run Sheets (CSV)
    python3 scripts/daily.py --data exports/ --domains example.com
    python3 scripts/daily.py --mode bigquery --bq-project acme-prod --bq-dataset lifecycle

What it does, in order. Each step is independent: one that fails is recorded and the rest still run.

  1. Cohort audit for every brief in briefs/active/*.yaml (the example brief when that folder is
     empty): daily metrics against their 28-day median, risk bands, the funnel's largest leak.
  2. Engagement tiers from message_events (agents/engagement.py): the unengaged share is the sending
     gate's input. Skipped, not failed, when the data has no message_events table.
  2b. Tracking health (agents/tracking.py): is every event in the tracking plan still arriving, is the feed stale, and
     has the exact trigger or core action of any live flow stopped? File data only (demo or url mode).
  3. For each domain in --domains (or LCM_DOMAINS), a live DNS pull (tools/dns_collect.py) and the
     deliverability analysis, with today's unengaged share as a signal.
  4. Today's priorities (agents/priorities.py): the builds most worth doing next, ranked from the data, with fixes that
     cap everything else first. They open summary.md and fill the Priorities tab. File data only (demo or url mode).
  5. One Sheets sync with everything (scripts/sheets_sync.py; CSV files when no sheet is configured).

Data modes: demo (tests/fixtures), url (a folder or https prefix with .parquet or .csv exports), and
bigquery (the audit and tier SQL run in your warehouse through the bq command line; only aggregates
come back). Nothing is sent to an ESP from here.

Output in --out (default out/daily): one folder per flow, engagement/, deliverability/, summary.md,
status.json (what ran, what failed) and issues.json (the GitHub issues the workflow should open or
update: one per flow with an anomaly, per RED domain, and for a RED unengaged share).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODES = ("demo", "url", "bigquery")
EXAMPLE_BRIEF = ROOT / "briefs" / "examples" / "winback_14d.yaml"
BqRunner = Callable[[str], List[Dict[str, Any]]]


def active_briefs(folder: Path) -> Tuple[List[Path], Optional[str]]:
    found = sorted(p for p in folder.glob("*.y*ml") if p.suffix in (".yaml", ".yml"))
    if found:
        return found, None
    return [EXAMPLE_BRIEF], f"no briefs in {folder}: audited the example brief. Copy a brief into {folder} to audit your own flows"


def strip_comments(sql: str) -> str:
    """bq reads a query that starts with '--' as a flag; whole-line comments carry nothing the warehouse needs."""
    return "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--")).strip()


def bq_runner(project: str, dataset: str, max_rows: int = 100_000) -> BqRunner:
    def run(sql: str) -> List[Dict[str, Any]]:
        cmd = ["bq", "query", f"--project_id={project}", f"--dataset_id={project}:{dataset}", "--use_legacy_sql=false",
               "--format=json", f"--max_rows={max_rows}", "--quiet", strip_comments(sql)]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if p.returncode != 0:
            raise RuntimeError(f"bq query failed ({p.returncode}): {(p.stderr or p.stdout).strip()[:400]}")
        text = p.stdout.strip()
        return json.loads(text) if text else []
    return run


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _error(e: BaseException) -> str:
    return f"{type(e).__name__}: {str(e).strip()[:300]}"


# ----------------------------------------------------------------------------------------------
# steps
# ----------------------------------------------------------------------------------------------

def audit_flow(brief_path: Path, mode: str, data: Optional[str], bq: Optional[BqRunner], out: Path) -> Dict[str, Any]:
    from agents import cohort_analyst
    from agents.brief import load_brief
    brief = load_brief(brief_path)
    folder = out / brief.slug
    folder.mkdir(parents=True, exist_ok=True)
    if mode == "bigquery":
        results = folder / "warehouse_results"
        results.mkdir(exist_ok=True)
        for name, sql in cohort_analyst.audit_sql(brief, "bigquery").items():
            (folder / f"{name}.sql").write_text(sql, encoding="utf-8")
            (results / f"{name}.json").write_text(json.dumps(bq(sql)), encoding="utf-8")
        r = cohort_analyst.audit(brief, results=results)
    else:
        r = cohort_analyst.audit(brief, data)
    (folder / "audit.json").write_text(json.dumps(r, indent=2, default=str), encoding="utf-8")
    (folder / "audit.md").write_text(cohort_analyst.render_audit(r), encoding="utf-8")
    return r


def engagement_step(mode: str, data: Optional[str], bq: Optional[BqRunner], out: Path) -> Optional[Dict[str, Any]]:
    """The tier summary, or None when the data has no message_events (a skip, not a failure)."""
    from agents import cohort_analyst, engagement
    folder = out / "engagement"
    folder.mkdir(parents=True, exist_ok=True)
    if mode == "bigquery":
        sql = engagement.counts_sql("bigquery")
        (folder / "counts_bigquery.sql").write_text(sql, encoding="utf-8")
        try:
            rows = bq(sql)
        except RuntimeError as e:
            if "message_events" in str(e) and ("not found" in str(e).lower() or "notfound" in str(e).lower()):
                return None
            raise
        summary = engagement.summarize_counts({r["tier"]: int(r["people"]) for r in rows}, datetime.now(timezone.utc).date().isoformat())
    else:
        con = cohort_analyst.connect(data)
        if con.execute("SELECT COUNT(*) FROM message_events").fetchone()[0] == 0:
            return None
        summary = engagement.run(data)["summary"]
    (folder / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    (folder / "summary.md").write_text(engagement.render(summary), encoding="utf-8")
    return summary


def deliverability_step(domain: str, signals: Dict[str, Any], out: Path, collect: Optional[Callable[..., Dict[str, Any]]] = None) -> Dict[str, Any]:
    from tools import deliverability, dns_collect
    from tools.contracts import validate_deliverability
    doc = (collect or dns_collect.collect)(domain)
    doc.pop("_lookups", None)
    if signals:
        doc["signals"] = {**doc.get("signals", {}), **signals}
    doc = validate_deliverability(doc)
    r = deliverability.analyze(doc)
    folder = out / "deliverability"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{domain}.input.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
    (folder / f"{domain}.json").write_text(json.dumps(r, indent=2, default=str), encoding="utf-8")
    (folder / f"{domain}.md").write_text(deliverability.render_markdown(r), encoding="utf-8")
    return {"analysis": r, "as_of": doc.get("observed_at")}


# ----------------------------------------------------------------------------------------------
# the run
# ----------------------------------------------------------------------------------------------

def run(mode: str = "demo", data: Optional[str] = None, briefs_dir: Path = ROOT / "briefs" / "active", domains: Optional[List[str]] = None,
        out: Path = Path("out/daily"), bq: Optional[BqRunner] = None, sheet: bool = True, dry_run: bool = False,
        collect: Optional[Callable[..., Dict[str, Any]]] = None) -> Dict[str, Any]:
    from agents import cohort_analyst, engagement
    from scripts import sheets_sync
    from tools import deliverability
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if mode == "demo":
        data = str(ROOT / "tests" / "fixtures")
    if mode == "url" and not data:
        raise ValueError("url mode needs --data (or LCM_DATA_URL): a folder or https prefix with the exports")
    if mode == "bigquery" and bq is None:
        raise ValueError("bigquery mode needs --bq-project and --bq-dataset (or LCM_BQ_PROJECT and LCM_BQ_DATASET)")
    out.mkdir(parents=True, exist_ok=True)
    logged_at = _now()
    status: Dict[str, Any] = {"generated_at": logged_at, "mode": mode, "notes": [], "flows": [], "engagement": None,
                              "deliverability": [], "tracking": None, "priorities": None, "errors": [], "sheets": None}
    eng_summary: Optional[Dict[str, Any]] = None
    analyses: Dict[str, Dict[str, Any]] = {}
    md: List[str] = [f"# Daily lifecycle health, {logged_at[:10]}", "", f"Data: {mode}" + (f" ({cohort_analyst.data_label(data)})" if data else ""), ""]
    issues: List[Dict[str, Any]] = []
    audits: List[Dict[str, Any]] = []
    extra: Dict[str, List[Dict[str, Any]]] = {}

    briefs, note = active_briefs(briefs_dir)
    if note:
        status["notes"].append(note)
        md += [f"> {note}", ""]
    for path in briefs:
        try:
            a = audit_flow(path, mode, data, bq, out)
        except Exception as e:  # noqa: BLE001 - one flow's failure must not stop the others
            status["flows"].append({"brief": str(path), "status": "error", "error": _error(e)})
            status["errors"].append(f"audit {path.name}: {_error(e)}")
            md += [f"## {path.stem}: audit failed", "", f"`{_error(e)}`", ""]
            continue
        audits.append(a)
        status["flows"].append({"brief": str(path), "flow": a["flow"], "status": a["status"], "as_of": a["as_of"], "anomalies": len(a["anomalies"])})
        md += [cohort_analyst.render_audit(a), ""]
        if a["status"] == "anomaly":
            body = out / a["flow"] / "issue.md"
            body.write_text(cohort_analyst.render_audit(a), encoding="utf-8")
            issues.append({"title": f"Cohort anomaly: {a['flow']}", "label": "cohort-anomaly", "body_path": str(body)})

    signals: Dict[str, Any] = {}
    try:
        summary = engagement_step(mode, data, bq, out)
        if summary is None:
            status["engagement"] = {"status": "skipped", "reason": "no message_events in the data"}
            md += ["## Engagement tiers", "", "Skipped: the data has no message_events table (docs/SQL_SCHEMA_CONVENTIONS.md).", ""]
        else:
            eng_summary = summary
            status["engagement"] = {"status": "ok", "as_of": summary["as_of"], "unengaged_share": summary["unengaged_share"], "gate_band": summary["gate_band"]}
            signals["unengaged_share"] = summary["unengaged_share"]
            sheets_sync._merge(extra, sheets_sync.engagement_rows(summary, logged_at=logged_at))
            md += [engagement.render(summary), ""]
            if summary["gate_band"] == "RED":
                body = out / "engagement" / "issue.md"
                body.write_text(engagement.render(summary), encoding="utf-8")
                issues.append({"title": "Unengaged share is RED on the sending gate", "label": "engagement", "body_path": str(body)})
    except Exception as e:  # noqa: BLE001
        status["engagement"] = {"status": "error", "error": _error(e)}
        status["errors"].append(f"engagement: {_error(e)}")
        md += ["## Engagement tiers: failed", "", f"`{_error(e)}`", ""]

    from agents.brief import load_brief
    tracking_r: Optional[Dict[str, Any]] = None
    if mode == "bigquery":
        status["tracking"] = {"status": "skipped", "reason": "tracking health reads file data; run it with --data on an export"}
    else:
        try:
            from agents import tracking
            events_dir = os.environ.get("LCM_EVENTS_DIR")
            tracking_r = tracking.health(data, briefs=[] if note else [load_brief(p) for p in briefs],
                                         events_dir=Path(events_dir) if events_dir else None)
            status["tracking"] = {"status": tracking_r["status"], "stale": bool(tracking_r["stale"]),
                                  "flows_affected": [f["flow"] for f in tracking_r["flows_affected"]]}
            if tracking_r["status"] == "ok":
                md += ["## Tracking health", "", "Every tracked event is arriving at its usual volume with its required properties.", ""]
            else:
                body = out / "tracking.md"
                body.write_text(tracking.render(tracking_r), encoding="utf-8")
                md += [tracking.render(tracking_r), ""]
                if tracking_r["stale"] or tracking_r["flows_affected"]:
                    issues.append({"title": "Tracking: a live flow's events stopped" if not tracking_r["stale"] else "Tracking: no new events",
                                   "label": "tracking", "body_path": str(body)})
        except Exception as e:  # noqa: BLE001
            status["tracking"] = {"status": "error", "error": _error(e)}
            status["errors"].append(f"tracking: {_error(e)}")

    for domain in [d.strip().lower() for d in (domains or []) if d.strip()]:
        try:
            res = deliverability_step(domain, {k: v for k, v in signals.items() if v is not None}, out, collect)
        except Exception as e:  # noqa: BLE001
            status["deliverability"].append({"domain": domain, "status": "error", "error": _error(e)})
            status["errors"].append(f"deliverability {domain}: {_error(e)}")
            md += [f"## Deliverability: {domain}: lookup failed", "", f"`{_error(e)}`", ""]
            continue
        r = res["analysis"]
        analyses[domain] = r
        status["deliverability"].append({"domain": domain, "status": "ok", "gate": r["gate"]["verdict"],
                                         "critical": sum(1 for f in r["findings"] if f["severity"] == "critical")})
        sheets_sync._merge(extra, sheets_sync.deliverability_rows(r, res["as_of"] or logged_at[:10], logged_at))
        md += [f"## Deliverability: {domain}", "", deliverability.render_markdown(r), ""]
        if r["gate"]["verdict"] == "RED":
            issues.append({"title": f"Deliverability RED: {domain}", "label": "deliverability", "body_path": str(out / "deliverability" / f"{domain}.md")})

    if mode == "bigquery":
        status["priorities"] = {"status": "skipped", "reason": "priorities read file data; run them with --data on an export"}
    else:
        from agents import priorities
        try:
            live = [] if note else [load_brief(p) for p in briefs]
            pr = priorities.run(data, briefs=[load_brief(p) for p in briefs], live=live, top=3, audits=audits, engagement=eng_summary,
                                deliverability=analyses, out=out, tracking=tracking_r)
            section = priorities.render(pr)
            (out / "priorities.md").write_text(section, encoding="utf-8")
            (out / "priorities.json").write_text(json.dumps(priorities.to_json(pr), indent=2, default=str), encoding="utf-8")
            status["priorities"] = {"status": "ok", "top": [o.title for o in pr["top"]]}
            sheets_sync._merge(extra, sheets_sync.priority_rows(priorities.to_json(pr), logged_at))
            md[4:4] = [section, ""]           # right under the title: what to do today comes first
        except Exception as e:  # noqa: BLE001
            status["priorities"] = {"status": "error", "error": _error(e)}
            status["errors"].append(f"priorities: {_error(e)}")
    if sheet:
        try:
            status["sheets"] = sheets_sync.sync(audits=audits, extra=extra, dry_run=dry_run, out=out / "sheets")
        except Exception as e:  # noqa: BLE001
            status["sheets"] = {"mode": "error", "error": _error(e)}
            status["errors"].append(f"sheets: {_error(e)}")
    if status["errors"]:
        md += ["## Errors", ""] + [f"- {x}" for x in status["errors"]] + [""]
    status["issues"] = len(issues)
    (out / "summary.md").write_text("\n".join(md).rstrip() + "\n", encoding="utf-8")
    (out / "issues.json").write_text(json.dumps(issues, indent=2), encoding="utf-8")
    (out / "status.json").write_text(json.dumps(status, indent=2, default=str), encoding="utf-8")
    return status


def main(argv: Optional[List[str]] = None) -> int:
    env = os.environ.get
    ap = argparse.ArgumentParser(description="Daily cohort, engagement and deliverability health check")
    ap.add_argument("--mode", choices=MODES, default=None, help="default: bigquery when a project is set, url when --data is set, else demo")
    ap.add_argument("--data", default=env("LCM_DATA_URL"), help="folder or https prefix with users, orders, track_events, message_events")
    ap.add_argument("--briefs", default=str(ROOT / "briefs" / "active"))
    ap.add_argument("--domains", default=env("LCM_DOMAINS", ""), help="comma-separated sending domains to check")
    ap.add_argument("--bq-project", default=env("LCM_BQ_PROJECT"))
    ap.add_argument("--bq-dataset", default=env("LCM_BQ_DATASET"))
    ap.add_argument("--out", default="out/daily")
    ap.add_argument("--no-sheet", action="store_true", help="skip the Sheets step entirely")
    ap.add_argument("--dry-run", action="store_true", help="write the Sheets rows as CSV instead of to the sheet")
    ap.add_argument("--strict", action="store_true", help="exit 1 when any step failed (the workflow checks status.json instead)")
    a = ap.parse_args(argv)
    mode = a.mode or ("bigquery" if a.bq_project else "url" if a.data else "demo")
    bq = bq_runner(a.bq_project, a.bq_dataset) if mode == "bigquery" and a.bq_project and a.bq_dataset else None
    try:
        status = run(mode, a.data, Path(a.briefs), a.domains.split(",") if a.domains else [], Path(a.out), bq,
                     sheet=not a.no_sheet, dry_run=a.dry_run)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 2
    print((Path(a.out) / "summary.md").read_text(encoding="utf-8"))
    print(json.dumps({k: status[k] for k in ("mode", "flows", "engagement", "tracking", "deliverability", "priorities", "errors", "issues")}, indent=2, default=str))
    return 1 if (a.strict and status["errors"]) else 0


if __name__ == "__main__":
    sys.exit(main())
