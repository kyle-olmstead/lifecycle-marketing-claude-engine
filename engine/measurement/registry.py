"""
The experiment registry: one file per workspace that records every experiment before it starts and every look
taken at it, so nobody can quietly re-run, re-read or re-define a test after seeing the data.

The file is registry/experiments.json under the workspace root (engine/workspace.find()). Each entry follows
schemas/experiment.schema.json: experiment_id, flow, unit (user | account | household), assignment {hash_field,
salt}, holdout_share, primary_metric {event, window_days}, plan {baseline, mde_abs, n_per_arm, design_effect,
read_on}, stopping_rule {design: fixed_horizon | group_sequential | always_valid, looks, information_fractions},
status and looks_taken.

The schema enforces the shape, including: an account-level experiment hashes account_id and carries a design
effect (a household-level one hashes household_id and carries one too), and every experiment has a stopping rule.
check() adds what a schema cannot see:

  peeking            a fixed-horizon test with a primary-metric look recorded before its read date
  salt_collision     two running experiments on the same id with the same salt whose audiences are not declared
                     disjoint: the same people would be in both holdouts, so neither read is independent
  global_salt        an experiment that reuses the global holdout's salt
  readout_floor      a plan whose arms the readout would refuse
  too_many_looks     a group-sequential test with more efficacy looks than it planned
  fractions          information fractions that do not increase or do not end at 1
  design_effect      an account-level plan at design effect 1.0 with no ICC behind it
  overdue            a running test past its read date with no read recorded

Looks with kind "guardrail_only" (sample ratio and guardrails, the primary metric not read) are never peeking.

    python3 engine/measurement/registry.py add entry.json
    python3 engine/measurement/registry.py add design.json --from-design --flow winback_14d --event order_completed --launch 2026-11-02
    python3 engine/measurement/registry.py list
    python3 engine/measurement/registry.py check
    python3 engine/measurement/registry.py record-look winback_14d --kind primary --at 2026-12-01 --xt 410 --nt 4000 --xc 80 --nc 1000

Add --workspace PATH to any command to name the workspace; otherwise LCM_WORKSPACE or the nearest lmm.yaml is used.
The bundled demo workspace is read-only here: commands that write refuse it, so nothing lands in a folder the
user did not choose.
"""
from __future__ import annotations

if __package__ in (None, ""):  # run as a file (python3 engine/measurement/registry.py): make the repository importable
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse
import copy
import json
import os
import re
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from engine.measurement.stats import MIN_ARM_FOR_LIFT, MIN_CLUSTERS_PER_ARM, MIN_EVENTS_FOR_LIFT
from engine.paths import SCHEMAS

SCHEMA_PATH = SCHEMAS / "experiment.schema.json"
REGISTRY_DIR = "registry"
REGISTRY_FILE = "experiments.json"
HASH_FIELD = {"user": "user_id", "account": "account_id", "household": "household_id"}
DAYS_PER_MONTH = 30.44


class RegistryError(ValueError):
    """A registry entry or file that cannot be accepted; the message says why."""


# ----------------------------------------------------------------------------------------------
# schema and file
# ----------------------------------------------------------------------------------------------

def schema() -> Dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_entry(entry: Dict[str, Any]) -> List[str]:
    """Schema problems with one entry, as readable strings (empty when valid)."""
    import jsonschema
    v = jsonschema.Draft202012Validator(schema())
    out = []
    for e in sorted(v.iter_errors(entry), key=lambda e: list(e.absolute_path)):
        where = ".".join(str(p) for p in e.absolute_path) or "(entry)"
        out.append(f"{where}: {e.message}")
    return out


def registry_path(workspace: Optional[Path] = None) -> Path:
    """registry/experiments.json under the given workspace root, or under engine.workspace.find()."""
    if workspace is None:
        from engine.workspace import find
        workspace = find().root
    return Path(workspace) / REGISTRY_DIR / REGISTRY_FILE


def load(path: Path) -> Dict[str, Any]:
    """The registry document ({"version": 1, "experiments": [...]}); an empty one when the file does not exist yet."""
    if not path.exists():
        return {"version": 1, "experiments": []}
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("experiments"), list):
        raise RegistryError(f"{path}: expected {{\"version\": 1, \"experiments\": [...]}}")
    return doc


def save(path: Path, doc: Dict[str, Any]) -> None:
    """Validate every entry, then write atomically (a temporary file renamed over the old one)."""
    problems = []
    ids = set()
    for e in doc["experiments"]:
        for p in validate_entry(e):
            problems.append(f"{e.get('experiment_id', '?')}: {p}")
        if e.get("experiment_id") in ids:
            problems.append(f"{e['experiment_id']}: duplicate experiment_id")
        ids.add(e.get("experiment_id"))
    if problems:
        raise RegistryError("; ".join(problems))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".experiments.", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _find(doc: Dict[str, Any], experiment_id: str) -> Dict[str, Any]:
    for e in doc["experiments"]:
        if e["experiment_id"] == experiment_id:
            return e
    raise RegistryError(f"no experiment {experiment_id!r} in the registry")


# ----------------------------------------------------------------------------------------------
# building entries
# ----------------------------------------------------------------------------------------------

def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9_.-]+", "_", text.lower()).strip("_.-")
    return (s or "experiment")[:80]


def read_on_from(launched_on: Optional[str], months_to_read: Optional[float]) -> Optional[str]:
    """The planned read date: launch + months to read (enrollment plus the outcome window), in calendar days."""
    if not launched_on or months_to_read is None:
        return None
    return (date.fromisoformat(launched_on) + timedelta(days=round(months_to_read * DAYS_PER_MONTH))).isoformat()


def entry_from_design(design: Dict[str, Any], flow_slug: str, *, experiment_id: Optional[str] = None,
                      metric_event: Optional[str] = None, launched_on: Optional[str] = None,
                      stopping_rule: Optional[Dict[str, Any]] = None, status: str = "draft",
                      audience: Optional[Dict[str, Any]] = None, hash_field: Optional[str] = None,
                      icc: Optional[float] = None, mean_cluster_size: Optional[float] = None,
                      alpha: float = 0.05, power: float = 0.80) -> Optional[Dict[str, Any]]:
    """A registry entry from the experiment agent's design (ExperimentDesign.model_dump()).

    Reads holdout_pct, unit, salt, assignment_rule, primary_metric, outcome_window_days, read and feasibility
    (p_control, mde_abs, required_holdout_n, required_treatment_n, design_effect, months_to_read). metric_event is
    the event the primary metric counts (the design carries only its description); read_on is computed from
    launched_on when both it and months_to_read are known. The stopping rule defaults to a single fixed-horizon
    read. Returns None when the design has no holdout (holdout_pct 0): nothing is randomized, so there is nothing
    to register. The entry is validated against the schema before it is returned.
    """
    pct = float(design.get("holdout_pct", 0) or 0)
    if pct <= 0:
        return None
    unit = design.get("unit", "user")
    feas = design.get("feasibility") or {}
    rule = stopping_rule or {"design": "fixed_horizon", "looks": 1, "information_fractions": [1.0], "alpha": alpha}
    plan: Dict[str, Any] = {
        "baseline": feas.get("p_control"),
        "mde_abs": feas.get("mde_abs"),
        "n_per_arm": ({"holdout": int(feas["required_holdout_n"]), "treatment": int(feas["required_treatment_n"])}
                      if feas.get("required_holdout_n") else None),
        "design_effect": float(feas.get("design_effect", 1.0) or 1.0),
        "alpha": alpha, "power": power,
        "read_on": read_on_from(launched_on, feas.get("months_to_read")),
        "expected_read": design.get("read", "not readable"),
    }
    if icc is not None:
        plan["icc"] = icc
    if mean_cluster_size is not None:
        plan["mean_cluster_size"] = mean_cluster_size
    entry: Dict[str, Any] = {
        "experiment_id": _slug(experiment_id or f"{flow_slug}_holdout"),
        "flow": flow_slug,
        "unit": unit,
        "assignment": {"hash_field": hash_field or HASH_FIELD.get(unit, "user_id"), "salt": design["salt"]},
        "holdout_share": round(pct / 100, 6),
        "primary_metric": {"event": metric_event or design.get("primary_event") or design.get("primary_metric") or "unknown",
                           "window_days": int(design.get("outcome_window_days", 0))},
        "plan": plan,
        "stopping_rule": rule,
        "status": status,
        "launched_on": launched_on,
        "looks_taken": [],
    }
    if design.get("assignment_rule"):
        entry["assignment"]["rule"] = design["assignment_rule"]
    if design.get("primary_metric") and metric_event:
        entry["primary_metric"]["description"] = design["primary_metric"]
    if audience:
        entry["audience"] = audience
    problems = validate_entry(entry)
    if problems:
        raise RegistryError("the design does not make a valid registry entry: " + "; ".join(problems))
    return entry


# ----------------------------------------------------------------------------------------------
# checks
# ----------------------------------------------------------------------------------------------

def _issue(eid: str, severity: str, code: str, message: str) -> Dict[str, str]:
    return {"experiment_id": eid, "severity": severity, "code": code, "message": message}


def _disjoint(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    da = set((a.get("audience") or {}).get("disjoint_from", []))
    db = set((b.get("audience") or {}).get("disjoint_from", []))
    return b["experiment_id"] in da or a["experiment_id"] in db


def check_entry(e: Dict[str, Any], today: Optional[date] = None) -> List[Dict[str, str]]:
    """Issues with one entry on its own (schema, floor, looks, peeking, read date)."""
    from engine.measurement.global_holdout import GLOBAL_SALT
    today = today or date.today()
    eid = e.get("experiment_id", "?")
    issues = [_issue(eid, "error", "schema", p) for p in validate_entry(e)]
    if issues:
        return issues
    plan, rule = e["plan"], e["stopping_rule"]
    if e["assignment"]["salt"] == GLOBAL_SALT:
        issues.append(_issue(eid, "error", "global_salt",
                             f"salt {GLOBAL_SALT!r} is the global holdout's: this experiment's holdout would be the global holdout"))
    n = plan.get("n_per_arm")
    base = plan.get("baseline")
    if n and base:
        small = min(n["holdout"], n["treatment"])
        if small < MIN_ARM_FOR_LIFT or small * base < MIN_EVENTS_FOR_LIFT:
            issues.append(_issue(eid, "error", "readout_floor",
                                 f"planned arms ({n['holdout']:,} holdout / {n['treatment']:,} treatment at a {base:.2%} baseline) are below "
                                 f"the readout floor of {MIN_ARM_FOR_LIFT} entrants and {MIN_EVENTS_FOR_LIFT} expected conversions per arm: "
                                 "the read would be refused"))
        m = plan.get("mean_cluster_size")
        if e["unit"] != "user" and m and n["holdout"] / m < MIN_CLUSTERS_PER_ARM:
            issues.append(_issue(eid, "error", "readout_floor",
                                 f"about {n['holdout'] / m:.0f} holdout {e['unit']}s planned; the clustered readout needs {MIN_CLUSTERS_PER_ARM}"))
    if e["unit"] != "user" and plan.get("design_effect") == 1.0 and plan.get("icc") is None:
        issues.append(_issue(eid, "warning", "design_effect",
                             f"{e['unit']}-level randomization planned at design effect 1.0 with no ICC recorded: that assumes people in one "
                             f"{e['unit']} behave independently. Estimate the ICC (engine/measurement/cluster.py icc) and re-plan"))
    looks = e["looks_taken"]
    primary = [lk for lk in looks if lk["kind"] == "primary" and lk.get("counted", True)]
    design = rule["design"]
    if design == "fixed_horizon":
        read_on = plan.get("read_on")
        if read_on:
            early = [lk for lk in primary if lk["at"][:10] < read_on]
            if early:
                issues.append(_issue(eid, "error", "peeking",
                                     f"{len(early)} primary-metric look(s) recorded before the read date {read_on} ({', '.join(lk['at'][:10] for lk in early)}) "
                                     "on a fixed-horizon test: the error rate no longer holds. Report the planned read only, or re-register "
                                     "as group_sequential or always_valid before the next look"))
        elif primary:
            issues.append(_issue(eid, "warning", "peeking",
                                 "a primary-metric look is recorded but the plan has no read date, so whether it was early cannot be checked"))
    elif design == "group_sequential":
        fr = rule.get("information_fractions", [])
        if len(fr) != rule.get("looks"):
            issues.append(_issue(eid, "error", "fractions", f"{rule.get('looks')} looks planned but {len(fr)} information fractions given"))
        if any(b <= a for a, b in zip(fr, fr[1:])) or (fr and abs(fr[-1] - 1.0) > 1e-9):
            issues.append(_issue(eid, "error", "fractions", "information fractions must increase and end at 1"))
        if len(primary) > rule.get("looks", 0):
            issues.append(_issue(eid, "error", "too_many_looks",
                                 f"{len(primary)} efficacy looks recorded against {rule['looks']} planned: the extra looks are not covered "
                                 "by the spending plan"))
        taken = [lk.get("information_fraction") for lk in primary if lk.get("information_fraction") is not None]
        if any(b <= a for a, b in zip(taken, taken[1:])):
            issues.append(_issue(eid, "error", "fractions", "recorded looks' information fractions do not increase"))
    if e["status"] == "running" and plan.get("read_on") and plan["read_on"] < today.isoformat() and design == "fixed_horizon":
        if not any(lk["at"][:10] >= plan["read_on"] for lk in primary):
            issues.append(_issue(eid, "warning", "overdue", f"the read date {plan['read_on']} has passed with no read recorded"))
    return issues


def check(doc: Dict[str, Any], today: Optional[date] = None) -> List[Dict[str, str]]:
    """Every issue in the registry: each entry on its own, duplicate ids, and salt collisions between running experiments."""
    exps = doc["experiments"]
    issues: List[Dict[str, str]] = []
    seen = set()
    for e in exps:
        if e.get("experiment_id") in seen:
            issues.append(_issue(e.get("experiment_id", "?"), "error", "duplicate", "experiment_id appears more than once"))
        seen.add(e.get("experiment_id"))
        issues.extend(check_entry(e, today))
    running = [e for e in exps if e.get("status") == "running" and not validate_entry(e)]
    for i, a in enumerate(running):
        for b in running[i + 1:]:
            if a["assignment"]["salt"] != b["assignment"]["salt"] or _disjoint(a, b):
                continue
            if a["assignment"]["hash_field"] == b["assignment"]["hash_field"]:
                msg = (f"running experiments {a['experiment_id']} and {b['experiment_id']} hash {a['assignment']['hash_field']} with the same "
                       f"salt {a['assignment']['salt']!r} on audiences not declared disjoint: the same people land in both holdouts, so the "
                       "reads are not independent. Give one a new salt before it starts, or list the other in audience.disjoint_from "
                       "if their audiences truly cannot overlap")
                for eid in (a["experiment_id"], b["experiment_id"]):
                    issues.append(_issue(eid, "error", "salt_collision", msg))
            else:
                issues.append(_issue(a["experiment_id"], "warning", "salt_collision",
                                     f"shares salt {a['assignment']['salt']!r} with {b['experiment_id']} on a different id; harmless for assignment, "
                                     "but use distinct salts to keep the registry unambiguous"))
    return issues


# ----------------------------------------------------------------------------------------------
# writing
# ----------------------------------------------------------------------------------------------

def add(entry: Dict[str, Any], path: Path, today: Optional[date] = None) -> List[Dict[str, str]]:
    """Add one entry (schema-valid, new id) and return the registry issues that concern it."""
    problems = validate_entry(entry)
    if problems:
        raise RegistryError("; ".join(problems))
    doc = load(path)
    if any(e["experiment_id"] == entry["experiment_id"] for e in doc["experiments"]):
        raise RegistryError(f"experiment {entry['experiment_id']!r} is already registered; a registered plan is not edited, register a new id")
    doc["experiments"].append(copy.deepcopy(entry))
    save(path, doc)
    return [i for i in check(doc, today) if i["experiment_id"] == entry["experiment_id"]]


def evaluate_look(entry: Dict[str, Any], *, kind: str = "primary", at: Optional[str] = None,
                  x_t: Optional[int] = None, n_t: Optional[int] = None, x_c: Optional[int] = None, n_c: Optional[int] = None,
                  z: Optional[float] = None, information_fraction: Optional[float] = None, final: bool = False) -> Dict[str, Any]:
    """The look record for an entry, with the decision computed by the entry's stopping rule when counts are given:
    group_sequential through sequential.look_from_counts (or look() from z), always_valid through sequential.msprt
    carrying the last primary look forward, fixed_horizon through stats.readout."""
    from engine.measurement import sequential, stats
    rec: Dict[str, Any] = {"at": at or date.today().isoformat(), "kind": kind}
    if kind != "primary":
        rec["note"] = "sample ratio and guardrails only; the primary metric was not read"
        for k, v in (("n_treatment", n_t), ("n_holdout", n_c)):
            if v is not None:
                rec[k] = int(v)
        return rec
    rule, plan = entry["stopping_rule"], entry["plan"]
    alpha = rule.get("alpha", plan.get("alpha", 0.05))
    have_counts = None not in (x_t, n_t, x_c, n_c)
    if have_counts:
        rec.update({"n_treatment": int(n_t), "n_holdout": int(n_c), "conversions_treatment": int(x_t), "conversions_holdout": int(x_c)})
    primary = [lk for lk in entry["looks_taken"] if lk["kind"] == "primary" and lk.get("counted", True)]
    design = rule["design"]
    if design == "group_sequential":
        prev = [lk["information_fraction"] for lk in primary if lk.get("information_fraction")]
        sides = rule.get("sides", 2)
        if have_counts:
            n = plan.get("n_per_arm")
            if not n:
                raise RegistryError("the plan has no n_per_arm, so the information fraction cannot be computed; pass z and information_fraction")
            r = sequential.look_from_counts(x_t, n_t, x_c, n_c, n["treatment"], n["holdout"], prev, alpha, rule["spending"], sides,
                                            final, expected_holdout=entry["holdout_share"])
        elif z is not None and information_fraction is not None:
            r = sequential.look(z, information_fraction, prev, alpha, rule["spending"], sides, final)
            r["counted_as_look"] = True
        else:
            raise RegistryError("give counts (xt, nt, xc, nc) or z with information_fraction")
        rec.update({"decision": r["decision"], "counted": bool(r.get("counted_as_look", False))})
        for k in ("z", "boundary", "information_fraction"):
            if r.get(k) is not None:
                rec[k] = r[k]
        if r.get("detail"):
            rec["note"] = r["detail"]
    elif design == "always_valid":
        if not have_counts:
            raise RegistryError("an always-valid look needs counts (xt, nt, xc, nc)")
        last = next((lk for lk in reversed(primary) if lk.get("p_value") is not None), None)
        r = sequential.msprt(x_t, n_t, x_c, n_c, rule["tau"], alpha, previous=last)
        rec.update({"decision": r["decision"], "p_value": r["p_value"], "confidence_sequence": r["confidence_sequence"],
                    "counted": r["read"] != "not readable", "note": r.get("note", "")})
    else:
        if have_counts:
            r = stats.readout(x_t, n_t, x_c, n_c, alpha, expected_holdout=entry["holdout_share"])
            if r.get("z") is not None:
                rec["z"], rec["p_value"] = r["z"], r["p_value"]
            rec["counted"] = r["read"] != "not readable"
        elif z is not None:
            rec["z"] = z
        early = plan.get("read_on") and rec["at"][:10] < plan["read_on"]
        rec["decision"] = "peek before the read date" if early else "final read"
        if early:
            rec["note"] = "a primary-metric look before the read date on a fixed-horizon test: recorded, and flagged as peeking"
    return rec


def record_look(experiment_id: str, path: Path, today: Optional[date] = None, **look: Any) -> Dict[str, Any]:
    """Append a look to an experiment (always recorded, because the record is the audit trail), update the status when
    the stopping rule ends the test, save, and return {"look": ..., "issues": [...]} for that experiment."""
    doc = load(path)
    e = _find(doc, experiment_id)
    rec = evaluate_look(e, **look)
    e["looks_taken"].append(rec)
    decision = rec.get("decision", "")
    if decision == "stop for efficacy":
        e["status"] = "stopped"
    elif decision == "final read":
        e["status"] = "completed"
    elif e["status"] == "draft":
        e["status"] = "running"
    save(path, doc)
    return {"look": rec, "status": e["status"], "issues": [i for i in check(doc, today) if i["experiment_id"] == experiment_id]}


# ----------------------------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------------------------

def _workspace_for_write(arg: Optional[str]) -> Path:
    if arg:
        return Path(arg).expanduser().resolve()
    from engine.workspace import find
    ws = find()
    if ws.demo:
        raise RegistryError("no workspace found (no lmm.yaml here or above, and LCM_WORKSPACE is not set); the bundled demo is read-only. "
                            "Run lcm init, set LCM_WORKSPACE, or pass --workspace")
    return ws.root


def _render_list(doc: Dict[str, Any]) -> str:
    rows = [("experiment", "flow", "unit", "status", "design", "holdout", "read_on", "looks")]
    for e in doc["experiments"]:
        rows.append((e["experiment_id"], e["flow"], e["unit"], e["status"], e["stopping_rule"]["design"], f"{e['holdout_share']:.0%}",
                     e["plan"].get("read_on") or "-", str(len(e["looks_taken"]))))
    if len(rows) == 1:
        return "no experiments registered\n"
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows) + "\n"


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="The experiment registry: add, list, check, record-look")
    ap.add_argument("--workspace", help="workspace root (default: LCM_WORKSPACE or the nearest lmm.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a_ = sub.add_parser("add", help="register an experiment from an entry file, or from an experiment design with --from-design")
    a_.add_argument("input")
    a_.add_argument("--from-design", action="store_true", help="input is an ExperimentDesign (the agent's output), not an entry")
    a_.add_argument("--flow", help="flow slug (with --from-design)")
    a_.add_argument("--event", help="the event the primary metric counts (with --from-design)")
    a_.add_argument("--launch", help="launch date YYYY-MM-DD (with --from-design), sets read_on")
    a_.add_argument("--id", help="experiment id (with --from-design)")
    sub.add_parser("list")
    c = sub.add_parser("check")
    c.add_argument("--today", help="YYYY-MM-DD (default: today)")
    r = sub.add_parser("record-look", help="record a look; the decision follows the experiment's stopping rule")
    r.add_argument("experiment_id")
    r.add_argument("--kind", choices=("primary", "guardrail_only"), default="primary")
    r.add_argument("--at", help="YYYY-MM-DD (default: today)")
    for k in ("xt", "nt", "xc", "nc"):
        r.add_argument(f"--{k}", type=int)
    r.add_argument("--z", type=float)
    r.add_argument("--t", type=float, help="information fraction (with --z)")
    r.add_argument("--final", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd in ("list", "check"):
            path = registry_path(Path(a.workspace).expanduser().resolve() if a.workspace else None)
            doc = load(path)
            if a.cmd == "list":
                print(_render_list(doc), end="")
                return 0
            issues = check(doc, date.fromisoformat(a.today) if a.today else None)
            for i in issues:
                print(f"{i['severity'].upper():8} {i['experiment_id']}: [{i['code']}] {i['message']}")
            print(f"{len(doc['experiments'])} experiment(s), {sum(i['severity'] == 'error' for i in issues)} error(s), "
                  f"{sum(i['severity'] == 'warning' for i in issues)} warning(s)")
            return 1 if any(i["severity"] == "error" for i in issues) else 0
        path = registry_path(_workspace_for_write(a.workspace))
        if a.cmd == "add":
            src = json.loads(Path(a.input).read_text(encoding="utf-8"))
            if a.from_design:
                if not a.flow:
                    raise RegistryError("--from-design needs --flow")
                entry = entry_from_design(src, a.flow, experiment_id=a.id, metric_event=a.event, launched_on=a.launch)
                if entry is None:
                    raise RegistryError("the design has no holdout: nothing is randomized, so there is nothing to register")
            else:
                entry = src
            issues = add(entry, path)
            print(f"registered {entry['experiment_id']} in {path}")
            for i in issues:
                print(f"{i['severity'].upper():8} [{i['code']}] {i['message']}")
            return 1 if any(i["severity"] == "error" for i in issues) else 0
        if a.cmd == "record-look":
            out = record_look(a.experiment_id, path, kind=a.kind, at=a.at, x_t=a.xt, n_t=a.nt, x_c=a.xc, n_c=a.nc,
                              z=a.z, information_fraction=a.t, final=a.final)
            print(json.dumps(out, indent=2))
            return 1 if any(i["severity"] == "error" for i in out["issues"]) else 0
    except (RegistryError, FileNotFoundError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
