"""Log teammate output to a Google Sheet: plans, cohort health, benchmarks, anomalies, engagement, deliverability, priorities.

The daily job (engine/workflows/daily.py, run at 05:00 UTC by .github/workflows/daily_cohort_audit.yml) calls
sync() once with everything it produced; the orchestrator calls it with --sheet after a plan. Seven
tabs, created if missing, each with a fixed header and a key, so re-running a day updates rows in
place instead of duplicating them:

  Campaign Plans   one row per plan run                  key: run_id
  Cohort Health    one row per flow per day              key: as_of + flow
  Benchmarks       one row per metric per flow per day   key: as_of + flow + metric
  Anomalies        one row per finding                   key: as_of + flow + metric
  Engagement       one row per audience per day          key: as_of + audience
  Deliverability   one row per sending domain per day    key: as_of + domain
  Priorities       one row per ranked priority per day   key: as_of + rank

Authentication is a Google Cloud service account, never a personal login:
  1. Create a service account, enable the Google Sheets API on its project, and make a JSON key.
  2. Share the sheet with the service account's email address as an Editor.
  3. Provide the key as GOOGLE_SERVICE_ACCOUNT_JSON (the JSON text, the way a CI secret holds it)
     or GOOGLE_APPLICATION_CREDENTIALS (a path), and the sheet id as LCM_SHEET_ID (the long id in
     the sheet's URL).
With no credentials or sheet id, it runs dry: it prints what it would write and saves CSVs.

    python3 adapters/destinations/sheets.py --audit out/audit/audit.json
    python3 adapters/destinations/sheets.py --plan out/ridgeview_winback_14d/plan.json --audit out/audit/audit.json
    python3 adapters/destinations/sheets.py --audit out/audit/audit.json --dry-run --out out/sheets
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TABS: Dict[str, Dict[str, Any]] = {
    "Campaign Plans": {"key": ["run_id"], "header": [
        "run_id", "generated_at", "flow", "platform", "status", "flow_type", "touches", "channels", "threshold_days", "holdout_pct",
        "monthly_entrants", "entrants_label", "baseline_rate", "baseline_label", "read", "read_after", "assumptions", "open_checks", "engine_version"]},
    "Cohort Health": {"key": ["as_of", "flow"], "header": [
        "as_of", "flow", "status", "data_label", "qualified", "active", "at_risk", "lapsed", "at_risk_share", "lapsed_share",
        "largest_leak", "leak_drop", "anomalies", "latest_event_date", "data_lag_days", "generated_at"]},
    "Benchmarks": {"key": ["as_of", "flow", "metric"], "header": [
        "as_of", "flow", "metric", "value", "median_28d", "modified_z", "status", "data_label"]},
    "Anomalies": {"key": ["as_of", "flow", "metric"], "header": ["as_of", "flow", "metric", "value", "reason", "logged_at"]},
    "Engagement": {"key": ["as_of", "audience"], "header": [
        "as_of", "audience", "people", "mailable", "suppressed", "engaged_30d", "engaged_90d", "engaged_180d", "unengaged", "new",
        "unengaged_share", "gate_band", "logged_at"]},
    "Priorities": {"key": ["as_of", "rank"], "header": [
        "as_of", "rank", "kind", "title", "monthly_low", "monthly_high", "conversions_low", "conversions_high", "confidence", "effort",
        "why", "next_step", "logged_at"]},
    "Deliverability": {"key": ["as_of", "domain"], "header": [
        "as_of", "domain", "gate", "auth_state", "dmarc_policy", "dmarc_effective_policy", "spf_lookups", "dkim_keys_found",
        "mta_sts_mode", "critical", "high", "primary_cause", "top_finding", "logged_at"]},
}
RETRY_STATUS = {429, 500, 502, 503, 504}


# ----------------------------------------------------------------------------------------------
# rows
# ----------------------------------------------------------------------------------------------

def plan_rows(plan: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    b, c, e = plan["brief"], plan["cohort"], plan["experiment"]
    return {"Campaign Plans": [{
        "run_id": plan["run_id"], "generated_at": plan["generated_at"], "flow": plan["flow"]["flow_spec"]["flow"], "platform": b["platform"],
        "status": plan["qa"]["status"], "flow_type": b["flow_type"], "touches": b["touches"], "channels": ", ".join(b["channels"]),
        "threshold_days": b["churn_threshold_days"], "holdout_pct": b["holdout_pct"],
        "monthly_entrants": c["monthly_entrants"]["value"], "entrants_label": c["monthly_entrants"]["label"],
        "baseline_rate": c["baseline_conversion_rate"]["value"], "baseline_label": c["baseline_conversion_rate"]["label"],
        "read": e["read"], "read_after": e["read_after"], "assumptions": len(b["assumptions"]),
        "open_checks": sum(1 for k in plan["qa"]["checks"] if k["status"] != "pass"), "engine_version": plan["engine_version"]}]}


def audit_rows(a: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    rb, leak = a["risk_bands"], a.get("largest_leak") or {}
    health = {"as_of": a["as_of"], "flow": a["flow"], "status": a["status"], "data_label": a["data_label"], "qualified": rb["qualified"],
              "active": rb.get("active", 0), "at_risk": rb.get("at_risk", 0), "lapsed": rb.get("lapsed", 0),
              "at_risk_share": rb.get("at_risk_share"), "lapsed_share": rb.get("lapsed_share"),
              "largest_leak": f"{leak['from']} to {leak['to']}" if leak else "", "leak_drop": leak.get("drop"),
              "anomalies": len(a["anomalies"]), "latest_event_date": a["latest_event_date"], "data_lag_days": a["data_lag_days"],
              "generated_at": a["generated_at"]}
    bench = [{"as_of": a["as_of"], "flow": a["flow"], "metric": m, "value": v["value"], "median_28d": v["median_28d"], "modified_z": v["z"],
              "status": v["status"], "data_label": a["data_label"]} for m, v in a["metrics"].items()]
    anomalies = [{"as_of": a["as_of"], "flow": a["flow"], "metric": x["metric"], "value": x.get("value"), "reason": x.get("reason", ""),
                  "logged_at": a["generated_at"]} for x in a["anomalies"]]
    return {"Cohort Health": [health], "Benchmarks": bench, "Anomalies": anomalies}


def engagement_rows(summary: Dict[str, Any], audience: str = "email subscribers", logged_at: str = "") -> Dict[str, List[Dict[str, Any]]]:
    t = summary["tiers"]
    return {"Engagement": [{"as_of": summary["as_of"], "audience": audience, "people": summary["people"], "mailable": summary["mailable"],
                            **{k: t.get(k, 0) for k in ("suppressed", "engaged_30d", "engaged_90d", "engaged_180d", "unengaged", "new")},
                            "unengaged_share": summary["unengaged_share"], "gate_band": summary["gate_band"], "logged_at": logged_at}]}


def priority_rows(p: Dict[str, Any], logged_at: str = "") -> Dict[str, List[Dict[str, Any]]]:
    """Rows from engine/agents/priorities.py (its to_json form): the ranked list for the day."""
    return {"Priorities": [{"as_of": p["as_of"], "rank": o["rank"], "kind": o["kind"], "title": o["title"],
                            "monthly_low": o.get("revenue_low"), "monthly_high": o.get("revenue_high"),
                            "conversions_low": o.get("conversions_low"), "conversions_high": o.get("conversions_high"),
                            "confidence": o["confidence"], "effort": o["effort"], "why": " | ".join(o["why"]),
                            "next_step": o.get("next_step", ""), "logged_at": logged_at} for o in p["top"]]}


def deliverability_rows(r: Dict[str, Any], as_of: str, logged_at: str = "") -> Dict[str, List[Dict[str, Any]]]:
    """One row from a engine/deliverability/analyze.py analysis."""
    c = r["checked"]
    sev = [f["severity"] for f in r["findings"]]
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    top = min(r["findings"], key=lambda f: order.get(f["severity"], 9), default=None)
    return {"Deliverability": [{
        "as_of": as_of, "domain": c.get("from_domain"), "gate": r["gate"]["verdict"], "auth_state": next((x["value"] for x in r["gate"].get("signals") or [] if x.get("signal") == "authentication_state"), None),
        "dmarc_policy": (c.get("dmarc") or {}).get("policy"), "dmarc_effective_policy": (c.get("dmarc") or {}).get("effective_policy"),
        "spf_lookups": (c.get("spf") or {}).get("lookups_total"), "dkim_keys_found": len(c.get("dkim_keys") or []),
        "mta_sts_mode": (c.get("mta_sts") or {}).get("mode"), "critical": sev.count("critical"), "high": sev.count("high"),
        "primary_cause": r.get("primary_cause_class"), "top_finding": f"{top['severity']}: {top['title']}" if top else "", "logged_at": logged_at}]}


def _cell(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, float):
        return round(v, 6)
    return v


def _merge(into: Dict[str, List[Dict[str, Any]]], more: Dict[str, List[Dict[str, Any]]]) -> None:
    for k, rows in more.items():
        into.setdefault(k, []).extend(rows)


# ----------------------------------------------------------------------------------------------
# sheet I/O
# ----------------------------------------------------------------------------------------------

def with_retry(fn: Callable[[], Any], attempts: int = 6, sleep: Callable[[float], None] = time.sleep) -> Any:
    """Exponential backoff with jitter on quota (429) and server (5xx) errors from the Sheets API."""
    from gspread.exceptions import APIError
    for i in range(attempts):
        try:
            return fn()
        except APIError as e:
            code = getattr(getattr(e, "response", None), "status_code", None) or getattr(e, "code", None)
            if code not in RETRY_STATUS or i == attempts - 1:
                raise
            sleep(min(64.0, 2 ** i) + random.random())
    return None


def _a1_col(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def upsert(ws: Any, header: Sequence[str], key: Sequence[str], rows: List[Dict[str, Any]], sleep: Callable[[float], None] = time.sleep) -> Dict[str, int]:
    """Write rows keyed on `key`: existing keys are updated in place, new keys appended. Header is enforced."""
    header = list(header)
    cols = getattr(ws, "col_count", len(header))
    if cols < len(header):
        with_retry(lambda: ws.add_cols(len(header) - cols), sleep=sleep)
    values = with_retry(ws.get_all_values, sleep=sleep) or []
    current = [c for c in (values[0] if values else []) if c != ""]
    if current != header:
        if current and header[: len(current)] != current:
            raise ValueError(f"tab '{getattr(ws, 'title', '?')}' has a different header ({current[:4]}...): rename or clear the tab, "
                             f"and the sync will recreate it")
        with_retry(lambda: ws.update(values=[header], range_name="A1", value_input_option="RAW"), sleep=sleep)
        values = [header] + values[1:]
    idx = {name: i for i, name in enumerate(header)}
    existing: Dict[tuple, int] = {}
    for r, row in enumerate(values[1:], start=2):
        k = tuple((row[idx[c]] if idx[c] < len(row) else "") for c in key)
        existing[k] = r
    upd: Dict[tuple, List[Any]] = {}
    app: Dict[tuple, List[Any]] = {}
    for row in rows:                                   # a key repeated within one call: the last row wins
        out = [_cell(row.get(h)) for h in header]
        k = tuple(str(_cell(row.get(c))) for c in key)
        (upd if k in existing else app)[k] = out
    updates = [{"range": f"A{existing[k]}:{_a1_col(len(header))}{existing[k]}", "values": [v]} for k, v in upd.items()]
    appends = list(app.values())
    if updates:
        with_retry(lambda: ws.batch_update(updates, value_input_option="RAW"), sleep=sleep)
    if appends:
        with_retry(lambda: ws.append_rows(appends, value_input_option="RAW"), sleep=sleep)
    return {"updated": len(updates), "appended": len(appends)}


def open_sheet(sheet_id: str) -> Any:
    import gspread
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw:
        try:
            info = json.loads(raw)
        except ValueError:
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: paste the whole key file as the secret's value") from None
        gc = gspread.service_account_from_dict(info, scopes=SCOPES)
    else:
        path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if not path or not Path(path).exists():
            raise ValueError("no service-account credentials: set GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_APPLICATION_CREDENTIALS")
        gc = gspread.service_account(filename=path, scopes=SCOPES)
    try:
        return with_retry(lambda: gc.open_by_key(sheet_id))
    except Exception as e:  # noqa: BLE001
        email = getattr(getattr(gc, "auth", None), "service_account_email", "the service account")
        raise RuntimeError(f"could not open sheet {sheet_id}: share it with {email} as an Editor, and enable the Sheets API "
                           f"({type(e).__name__}: {str(e)[:160]})") from e


def worksheet(sh: Any, title: str, cols: int) -> Any:
    from gspread.exceptions import WorksheetNotFound
    try:
        return with_retry(lambda: sh.worksheet(title))
    except WorksheetNotFound:
        return with_retry(lambda: sh.add_worksheet(title=title, rows=1000, cols=max(cols, 10)))


def sync(plans: Optional[List[Dict[str, Any]]] = None, audits: Optional[List[Dict[str, Any]]] = None, sheet_id: Optional[str] = None,
         dry_run: bool = False, out: Optional[Path] = None, spreadsheet: Any = None,
         extra: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> Dict[str, Any]:
    """extra: rows already shaped for a tab (engagement_rows, deliverability_rows)."""
    rows: Dict[str, List[Dict[str, Any]]] = {}
    for p in plans or []:
        _merge(rows, plan_rows(p))
    for a in audits or []:
        _merge(rows, audit_rows(a))
    _merge(rows, extra or {})
    unknown = set(rows) - set(TABS)
    if unknown:
        raise ValueError(f"no tab named {sorted(unknown)}; tabs are {list(TABS)}")
    sheet_id = sheet_id or os.environ.get("LCM_SHEET_ID")
    has_creds = bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))
    if spreadsheet is None and (dry_run or not sheet_id or not has_creds):
        folder = Path(out or "out/sheets")
        folder.mkdir(parents=True, exist_ok=True)
        for tab, rs in rows.items():
            header = TABS[tab]["header"]
            path = folder / f"{tab.lower().replace(' ', '_')}.csv"
            with path.open("w", encoding="utf-8", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(header)
                w.writerows([[_cell(r.get(h)) for h in header] for r in rs])
        why = "dry run" if dry_run else ("LCM_SHEET_ID is not set" if not sheet_id else "no service-account credentials")
        return {"mode": "dry run", "reason": why, "folder": str(folder), "rows": {k: len(v) for k, v in rows.items()}}
    sh = spreadsheet or open_sheet(sheet_id)
    result = {}
    for tab, rs in rows.items():
        spec = TABS[tab]
        ws = worksheet(sh, tab, len(spec["header"]))
        result[tab] = upsert(ws, spec["header"], spec["key"], rs)
    return {"mode": "sheet", "sheet_id": sheet_id, "tabs": result}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Log plans and cohort audits to Google Sheets")
    ap.add_argument("--plan", action="append", default=[], help="plan.json from the orchestrator (repeatable)")
    ap.add_argument("--audit", action="append", default=[], help="audit.json from the cohort analyst (repeatable)")
    ap.add_argument("--sheet-id", help="defaults to LCM_SHEET_ID")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default="out/sheets", help="where dry runs write CSVs")
    a = ap.parse_args(argv)
    if not a.plan and not a.audit:
        ap.error("give --plan and/or --audit")
    load = lambda p: json.loads(Path(p).read_text(encoding="utf-8"))  # noqa: E731
    try:
        r = sync([load(p) for p in a.plan], [load(p) for p in a.audit], a.sheet_id, a.dry_run, Path(a.out))
    except (ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(r, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
