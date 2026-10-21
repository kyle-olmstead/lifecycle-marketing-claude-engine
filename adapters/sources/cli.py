"""lcm import: turn a vendor export folder into the canonical tables, check them, and say what was assumed.

What it does, in order:
  1. picks the mapper: the one you name (shopify, klaviyo, ...), or with `auto` every mapper whose detect() recognizes
     files in the folder (adapters/sources/registry.py finds them);
  2. runs each mapper, which writes canonical CSVs and a mapping report (tables, rows, columns it could not use, and
     every assumption, ASSUMED where it had to choose a value the export does not carry);
  3. when more than one source ran, merges them (below);
  4. writes <table>.csv into --out, plus import_report.json with every mapping report, so the assumptions travel with
     the data;
  5. runs check-data (engine/data/check.py) on the result and prints both reports.

Merging several sources (lcm import auto on a folder holding a Shopify and a Klaviyo export, for example). Sources run
in a fixed order, commerce and billing first (shopify, stripe, hubspot, klaviyo, customerio, ga4, rudderstack, segment,
then any other), and on a conflict the earlier source wins:
  * people are matched by lower-cased email: a later source's user id is replaced by the earlier one everywhere;
  * a user keeps the first non-blank value of each column, and the earliest created_at that a source actually read
    (a mapper lists the created_at values it had to assume in `report.assumed_values`, an optional extension of the
    mapping report; an assumed date only wins when no source has a real one);
  * an order_completed or checkout_started that two sources both report (same order_id or checkout_id) is kept once;
  * rows with the same primary key are kept once (orders, line items, refunds, events by event_id);
  * consent: every record is kept, except that an undated grant (source import_undated) is dropped for a person and
    channel when another source has dated records for them; undated revokes are always kept. users.email_consent_at
    and sms_consent_at are then recomputed from consent_events, newest wins.

Exit code: 0 when the imported folder passes check-data (warnings allowed), 1 when it has errors, 2 when nothing could
be imported (unknown source, nothing detected).

    python3 adapters/sources/cli.py --list
    python3 adapters/sources/cli.py shopify exports/shopify --out data/
    python3 adapters/sources/cli.py auto examples/brands/northfield_dtc/exports --out examples/brands/northfield_dtc/data
    lcm import auto exports/ --out data/          # the same, once wired into the lcm command
"""
from __future__ import annotations

if __package__ in (None, ""):  # run as a file (python3 <path>): make the repository importable
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse
import json
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from adapters.sources import registry
from adapters.sources.export_io import UNDATED_SOURCE, consent_rollup, read_canonical, write_tables
from adapters.sources.report import MappingReport
from engine.data.schema import TABLES

PRIORITY = ("shopify", "stripe", "hubspot", "klaviyo", "customerio", "ga4", "rudderstack", "segment")
REPORT_FILE = "import_report.json"
USER_COLUMNS = {t: "user_id" for t, table in TABLES.items() if "user_id" in table.column_names}
KEYS: Dict[str, Tuple[str, ...]] = {
    "orders": ("order_id",), "line_items": ("line_id", "order_id", "product_id"), "discounts": ("order_id", "code"),
    "refunds": ("refund_id",), "accounts": ("account_id",), "subscriptions": ("subscription_id",),
    "invoices": ("invoice_id",), "message_events": ("event_id",), "lcm_flow_entries": ("flow", "user_id", "anchor_date"),
}


def _rank(name: str) -> Tuple[int, str]:
    return (PRIORITY.index(name) if name in PRIORITY else len(PRIORITY), name)


def plan_runs(source: str, folders: Sequence[Path]) -> List[Tuple[Any, Path]]:
    """(mapper module, folder) pairs to run, in merge order. Raises KeyError for an unknown source."""
    runs: List[Tuple[Any, Path]] = []
    for folder in folders:
        mods = registry.detect(folder) if source == "auto" else [registry.get(source)]
        runs.extend((m, Path(folder)) for m in mods)
    return sorted(runs, key=lambda r: _rank(str(r[0].NAME)))


def _ref(row: Dict[str, str]) -> Optional[Tuple[str, str]]:
    if row.get("event_name") not in ("order_completed", "checkout_started"):
        return None
    try:
        props = json.loads(row.get("properties") or "{}")
    except ValueError:
        return None
    ref = props.get("order_id") if row["event_name"] == "order_completed" else props.get("checkout_id")
    return (row["event_name"], str(ref)) if ref else None


def merge(runs: List[Tuple[str, Dict[str, List[Dict[str, str]]]]],
          assumed: Optional[List[Dict[str, List[str]]]] = None) -> Tuple[Dict[str, List[Dict[str, str]]], List[str]]:
    """Merge canonical tables from several sources (earlier wins). `assumed` holds, per run, the mapper's
    report.assumed_values ({"users.created_at": [user ids]}). Returns (tables, notes in plain words)."""
    out: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    notes: List[str] = []
    users: Dict[str, Dict[str, str]] = {}
    email_index: Dict[str, str] = {}
    seen_keys: Dict[str, set] = defaultdict(set)
    event_ids: set = set()
    refs: set = set()
    consent: List[Tuple[int, Dict[str, str]]] = []
    placeholder: set = set()    # user ids whose merged created_at is still an assumed value
    for n, (name, tables) in enumerate(runs):
        run_assumed = set((assumed[n] if assumed and n < len(assumed) else {}).get("users.created_at", []))
        remap: Dict[str, str] = {}
        for u in tables.get("users", []):
            email = (u.get("email") or "").strip().lower()
            if email and email in email_index and email_index[email] != u["user_id"]:
                remap[u["user_id"]] = email_index[email]
        if remap and n:
            notes.append(f"{name}: {len(remap):,} user(s) matched to an earlier source by email and given its user_id.")
        for tname, rows in tables.items():
            col = USER_COLUMNS.get(tname)
            if col and remap:
                for r in rows:
                    if r.get(col) in remap:
                        r[col] = remap[r[col]]
        run_assumed = {remap.get(uid, uid) for uid in run_assumed}
        new_users = matched = 0
        for u in tables.get("users", []):
            have = users.get(u["user_id"])
            guessed = u["user_id"] in run_assumed
            if have is None:
                users[u["user_id"]] = dict(u)
                if guessed:
                    placeholder.add(u["user_id"])
                new_users += 1
                email = (u.get("email") or "").strip().lower()
                if email:
                    email_index.setdefault(email, u["user_id"])
                continue
            matched += 1
            for k, v in u.items():
                if k == "created_at":
                    if not v or guessed and u["user_id"] not in placeholder:
                        continue      # an assumed date never replaces one a source read
                    if u["user_id"] in placeholder and not guessed or not have.get(k) or v < have[k]:
                        have[k] = v
                        if not guessed:
                            placeholder.discard(u["user_id"])
                elif v and not have.get(k):
                    have[k] = v
        if n and (new_users or matched):
            notes.append(f"{name}: {matched:,} user(s) already known, {new_users:,} new.")
        dropped_dupes = dropped_refs = 0
        for r in tables.get("track_events", []):
            ref = _ref(r)
            if r["event_id"] in event_ids or (ref and ref in refs):
                dropped_dupes += r["event_id"] in event_ids
                dropped_refs += bool(ref and ref in refs and r["event_id"] not in event_ids)
                continue
            event_ids.add(r["event_id"])
            if ref:
                refs.add(ref)
            out["track_events"].append(r)
        if dropped_refs:
            notes.append(f"{name}: {dropped_refs:,} order_completed or checkout_started event(s) were already reported by an "
                         "earlier source (same order_id or checkout_id) and kept once, from the earlier source.")
        if dropped_dupes:
            notes.append(f"{name}: {dropped_dupes:,} event(s) with an event_id already imported were dropped.")
        for r in tables.get("consent_events", []):
            consent.append((n, r))
        for tname, rows in tables.items():
            if tname in ("users", "track_events", "consent_events"):
                continue
            if tname == "products":
                index = {p["product_id"]: p for p in out["products"]}
                for r in rows:
                    have = index.get(r["product_id"])
                    if have is None:
                        out["products"].append(r)
                        index[r["product_id"]] = r
                    else:
                        for k, v in r.items():
                            if v and not have.get(k):
                                have[k] = v
                continue
            key_cols = KEYS.get(tname, tuple(TABLES[tname].column_names[:1]))
            dup = 0
            for r in rows:
                key = tuple(r.get(k) or "" for k in key_cols)
                if key in seen_keys[tname]:
                    dup += 1
                    continue
                seen_keys[tname].add(key)
                out[tname].append(r)
            if dup and n:
                notes.append(f"{name}: {dup:,} {tname} row(s) already imported from an earlier source were kept once.")
    # consent: drop undated grants where another source has dated records for the same person and channel
    dated_by: Dict[Tuple[str, str], set] = defaultdict(set)
    for n, r in consent:
        if r.get("source") != UNDATED_SOURCE:
            dated_by[(r["user_id"], r["channel"])].add(n)
    kept, dropped, seen = [], 0, set()
    for n, r in consent:
        key = (r["user_id"], r["channel"])
        if r.get("source") == UNDATED_SOURCE and r["status"] == "granted" and dated_by[key] - {n}:
            dropped += 1
            continue
        ident = tuple(sorted(r.items()))
        if ident in seen:
            continue
        seen.add(ident)
        kept.append(r)
    if dropped:
        notes.append(f"{dropped:,} undated consent grant(s) (import_undated) were dropped because another source has dated "
                     "consent records for the same person and channel.")
    user_rows = list(users.values())
    if kept:
        # recompute consent columns from the records, newest wins; a person or channel with no record keeps its value
        state = consent_rollup(kept)
        for u in user_rows:
            for channel, col in (("email", "email_consent_at"), ("sms", "sms_consent_at")):
                if (u["user_id"], channel) in state:
                    u[col] = state[(u["user_id"], channel)]
        out["consent_events"] = kept
    out["users"] = user_rows
    return dict(out), notes


def run_import(source: str, folders: Sequence[Path], out_dir: Path) -> Dict[str, Any]:
    """Map, merge and write. Returns {sources: [mapping reports], merge: [notes], tables: {table: rows}, out: path}."""
    runs = plan_runs(source, folders)
    if not runs:
        raise LookupError("no export recognized in " + ", ".join(str(f) for f in folders) +
                          f"; known sources: {', '.join(registry.names())}. Name one explicitly to see what it expects.")
    results: List[Tuple[str, Dict[str, List[Dict[str, str]]]]] = []
    reports: List[MappingReport] = []
    assumed: List[Dict[str, List[str]]] = []
    with tempfile.TemporaryDirectory(prefix="lcm_import_") as tmp:
        for i, (module, folder) in enumerate(runs):
            target = Path(tmp) / f"{i}_{module.NAME}"
            report = module.map(folder, target)
            reports.append(report)
            assumed.append(getattr(report, "assumed_values", {}) or {})
            results.append((str(module.NAME), read_canonical(target)))
    if len(results) == 1:
        tables, notes = results[0][1], []
    else:
        tables, notes = merge(results, assumed)
    out_dir = Path(out_dir)
    written = write_tables(out_dir, tables)
    untouched = sorted(n for n in TABLES if n not in written and (out_dir / f"{n}.csv").exists())
    summary = {"sources": [r.to_dict() for r in reports], "merge": notes, "tables": written}
    (out_dir / REPORT_FILE).write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    summary["out"] = str(out_dir)
    summary["untouched"] = untouched
    return summary


def render_markdown(summary: Dict[str, Any]) -> str:
    names = " + ".join(s["source"] for s in summary["sources"])
    L = [f"# Import: {names} into {summary['out']}", ""]
    for s in summary["sources"]:
        L += [f"## {s['source']}", ""]
        if s["tables"]:
            L += ["| Table | Rows |", "|---|---:|"] + [f"| {t} | {n:,} |" for t, n in s["tables"].items()] + [""]
        else:
            L += ["No tables written.", ""]
        if s["assumptions"]:
            L += ["Assumptions:", ""] + [f"- {a}" for a in s["assumptions"]] + [""]
        if s["warnings"]:
            L += ["Warnings:", ""] + [f"- {w}" for w in s["warnings"]] + [""]
        if s["unmapped_columns"]:
            L += ["Columns not used:", ""] + [f"- {f}: {', '.join(c)}" for f, c in s["unmapped_columns"].items()] + [""]
    if summary["merge"]:
        L += ["## Merge", ""] + [f"- {m}" for m in summary["merge"]] + [""]
    L += ["## Written", "", "| Table | Rows |", "|---|---:|"] + [f"| {t} | {n:,} |" for t, n in summary["tables"].items()]
    L += ["", f"Mapping reports saved to {Path(summary['out']) / REPORT_FILE}.", ""]
    if summary.get("untouched"):
        L += ["Left as they were (not produced by this import): " + ", ".join(f"{t}.csv" for t in summary["untouched"]) +
              ". Delete them if they came from an older import.", ""]
    return "\n".join(L)


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="lcm import", description="Map a vendor export folder into the canonical tables")
    ap.add_argument("source", nargs="?", help="a source name (see --list) or auto to detect")
    ap.add_argument("folders", nargs="*", type=Path, help="export folder(s)")
    ap.add_argument("--out", type=Path, help="data folder to write the canonical tables into")
    ap.add_argument("--json", action="store_true", help="print the import and check reports as JSON")
    ap.add_argument("--no-check", action="store_true", help="skip check-data on the result")
    ap.add_argument("--as-of", help="check-data: judge future dates and freshness against this UTC time")
    ap.add_argument("--list", action="store_true", help="list the source mappers and exit")
    a = ap.parse_args(argv)
    if a.list:
        for m in registry.all():
            print(f"{m.NAME:12} {m.DESCRIPTION}")
        for name, err in registry.errors().items():
            print(f"{name:12} (not loaded: {err})")
        return 0
    if not a.source or not a.folders or not a.out:
        ap.error("usage: lcm import <source|auto> <export folder> --out <data folder>")
    missing = [str(f) for f in a.folders if not f.is_dir()]
    if missing:
        print(f"not a folder: {', '.join(missing)}", file=sys.stderr)
        return 2
    try:
        summary = run_import(a.source, a.folders, a.out)
    except (KeyError, LookupError) as e:
        print(str(e).strip("'\""), file=sys.stderr)
        return 2
    check = None
    if not a.no_check:
        from engine.data.check import _parse_as_of, check_folder
        from engine.data.check import render_markdown as check_markdown
        check = check_folder(a.out, _parse_as_of(a.as_of))
    if a.json:
        print(json.dumps({"import": summary, "check": check}, indent=2, ensure_ascii=False))
    else:
        print(render_markdown(summary))
        if check is not None:
            print(check_markdown(check))
    return 1 if check is not None and not check["ok"] else 0


if __name__ == "__main__":
    sys.exit(_cli())
