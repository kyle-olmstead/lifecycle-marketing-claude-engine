"""Workflows: named job graphs of lcm commands, written as YAML, run in order with their outputs kept.

    lcm workflow list
    lcm workflow run morning_check
    lcm workflow run new_flow --set sentence="a 3-touch abandoned cart flow"
    lcm workflow run quarterly_review --dry-run

A workflow file (workflows/*.yaml in the repository, or workflows/*.yaml in your workspace, which wins on a name clash):

    name: morning_check
    description: what it is for, in one line
    inputs:                                   # values steps use as ${name}; --set name=value overrides
      data: {default: "${workspace}/data", help: "the exports folder"}
      domain: {help: "your sending domain"}   # no default: steps that use it are skipped until it is given
    steps:
      - id: data
        run: check-data ${data}               # any lcm command, exactly as you would type it after `lcm`
        on_fail: stop                         # stop: a failure ends the run; continue (default): later steps still run
      - id: tracking
        run: tracking --data ${data}
        needs: [data]                         # runs after these; skipped if one of them failed or was skipped

Built-in values: ${workspace} (the workspace folder), ${out} (this run's folder), ${today} (UTC date), ${repo} (the
engine). Each step's output is saved to <out>/<step id>.txt and the run's table to <out>/summary.md. Exit codes: 0 means
the step ran clean, 1 means it ran and found something to look at, 2 or more means it could not run. A step skipped
because an input was not given (a domain, say) leaves the run OK; one skipped because a step it needs failed does not.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import shlex
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from engine.paths import ROOT, WORKFLOWS

VAR = re.compile(r"\$\{(\w+)\}")


@dataclass
class Step:
    id: str
    run: str
    needs: List[str] = field(default_factory=list)
    on_fail: str = "continue"
    help: str = ""


@dataclass
class Workflow:
    name: str
    description: str
    inputs: Dict[str, Dict[str, Any]]
    steps: List[Step]
    path: Path


def _dirs() -> List[Path]:
    from engine import workspace
    ws = workspace.find()
    return [ws.root / "workflows", WORKFLOWS] if not ws.fallback else [WORKFLOWS]


def available() -> Dict[str, Path]:
    """Workflow name to file; the workspace's own workflows/ wins over the repository's."""
    found: Dict[str, Path] = {}
    for d in reversed(_dirs()):
        for p in sorted(d.glob("*.y*ml")) if d.exists() else []:
            found[p.stem] = p
    return found


def load(path: Path) -> Workflow:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict) or not raw.get("steps"):
        raise ValueError(f"{path}: a workflow has a name, inputs and a list of steps")
    steps = []
    for s in raw["steps"]:
        unknown = set(s) - {"id", "run", "needs", "on_fail", "help"}
        if unknown or "id" not in s or "run" not in s:
            raise ValueError(f"{path}: each step has an id and a run line (unknown keys: {sorted(unknown)})")
        if s.get("on_fail", "continue") not in ("stop", "continue"):
            raise ValueError(f"{path}: step {s['id']}: on_fail is stop or continue")
        steps.append(Step(id=str(s["id"]), run=str(s["run"]), needs=list(s.get("needs") or []),
                          on_fail=s.get("on_fail", "continue"), help=str(s.get("help", ""))))
    ids = [s.id for s in steps]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path}: step ids must be unique")
    for s in steps:
        later = [n for n in s.needs if n not in ids[:ids.index(s.id)]]
        if later:
            raise ValueError(f"{path}: step {s.id} needs {later}, which must be earlier steps (the file is the run order)")
    return Workflow(name=str(raw.get("name") or Path(path).stem), description=str(raw.get("description", "")),
                    inputs=dict(raw.get("inputs") or {}), steps=steps, path=Path(path))


def _values(wf: Workflow, given: Dict[str, str], out: Path) -> Dict[str, str]:
    from engine import workspace
    ws = workspace.find()
    vals = {"workspace": str(ws.root), "out": str(out), "today": datetime.now(timezone.utc).date().isoformat(), "repo": str(ROOT)}
    for name, spec in wf.inputs.items():
        spec = spec if isinstance(spec, dict) else {"default": spec}
        if name in given:
            vals[name] = given[name]
        elif spec.get("default") not in (None, ""):
            vals[name] = VAR.sub(lambda m: vals.get(m.group(1), m.group(0)), str(spec["default"]))
    vals.update({k: v for k, v in given.items() if k not in vals})
    return vals


def _fill(line: str, vals: Dict[str, str]) -> tuple:
    missing = sorted({m.group(1) for m in VAR.finditer(line) if m.group(1) not in vals})
    return VAR.sub(lambda m: shlex.quote(vals[m.group(1)]) if m.group(1) in vals else m.group(0), line), missing


def _invoke(argv: List[str]) -> tuple:
    from engine.cli.commands import COMMANDS
    if not argv or argv[0] not in COMMANDS:
        return 2, "", f"unknown command: {argv[0] if argv else '(empty)'}"
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = COMMANDS[argv[0]].run(argv[1:])
        except SystemExit as e:                     # argparse errors and modules that exit
            rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 2)
        except Exception as e:                      # noqa: BLE001 - one failing step must not lose the others' output
            rc = 2
            print(f"{type(e).__name__}: {e}", file=sys.stderr)
    return int(rc or 0), out.getvalue(), err.getvalue()


def run(wf: Workflow, given: Optional[Dict[str, str]] = None, out_root: Optional[Path] = None, dry_run: bool = False) -> Dict[str, Any]:
    from engine.pipeline.orchestrator import default_out
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(out_root or default_out()) / "workflows" / wf.name / stamp
    vals = _values(wf, dict(given or {}), out)
    if not dry_run:
        out.mkdir(parents=True, exist_ok=True)
    results: Dict[str, Dict[str, Any]] = {}
    stopped: Optional[str] = None
    for s in wf.steps:
        line, missing = _fill(s.run, vals)
        rec: Dict[str, Any] = {"id": s.id, "run": line, "needs": s.needs}
        blocked = [n for n in s.needs if results.get(n, {}).get("status") in ("error", "skipped", "stopped")]
        if stopped:
            rec.update(status="stopped", detail=f"not run: {stopped} failed and is marked on_fail: stop")
        elif missing:
            # an input nobody gave: skipped by design (the workflow says so), not something that went wrong
            rec.update(status="skipped", reason="input", detail=f"needs {', '.join(missing)} (give it with --set {missing[0]}=...)")
        elif blocked:
            rec.update(status="skipped", reason="blocked", detail=f"skipped because {', '.join(blocked)} did not run cleanly")
        elif dry_run:
            rec.update(status="planned", detail="")
        else:
            t0 = time.time()
            rc, so, se = _invoke(shlex.split(line))
            rec["seconds"] = round(time.time() - t0, 2)
            rec["exit"] = rc
            rec["status"] = "ok" if rc == 0 else ("attention" if rc == 1 else "error")
            text = so + (("\n--- stderr ---\n" + se) if se.strip() else "")
            (out / f"{s.id}.txt").write_text(f"$ lcm {line}\n\n{text}", encoding="utf-8")
            rec["log"] = str(out / f"{s.id}.txt")
            first = next((ln.strip(" #*") for ln in (so or se).splitlines() if ln.strip()), "")
            rec["detail"] = first[:160]
            if rec["status"] == "error" and s.on_fail == "stop":
                stopped = s.id
        results[s.id] = rec
    statuses = [r["status"] for r in results.values()]
    blocked_skips = [r for r in results.values() if r["status"] == "skipped" and r.get("reason") != "input"]
    overall = ("planned" if dry_run else "error" if "error" in statuses or "stopped" in statuses else
               "attention" if "attention" in statuses or blocked_skips else "ok")
    report = {"workflow": wf.name, "status": overall, "out": str(out), "inputs": vals, "steps": list(results.values())}
    if not dry_run:
        (out / "summary.md").write_text(render(report), encoding="utf-8")
        (out / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def render(r: Dict[str, Any]) -> str:
    mark = {"ok": "OK", "attention": "LOOK", "error": "FAIL", "skipped": "SKIP", "stopped": "STOP", "planned": "PLAN"}
    L = [f"# Workflow {r['workflow']}: {r['status'].upper()}", "", f"Outputs: {r['out']}", "",
         "| Step | Result | Command | Detail |", "|---|---|---|---|"]
    L += [f"| {s['id']} | {mark[s['status']]} | `lcm {s['run']}` | {str(s.get('detail', '')).replace('|', '/')} |" for s in r["steps"]]
    return "\n".join(L) + "\n"


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="lcm workflow", description="Run a named graph of lcm commands (workflows/*.yaml) and keep every output")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="the workflows available here, with their inputs")
    r = sub.add_parser("run", help="run a workflow by name or path")
    r.add_argument("workflow", help="a name from `lcm workflow list`, or a path to a workflow YAML")
    r.add_argument("--set", action="append", default=[], metavar="NAME=VALUE", help="give an input, e.g. domain=example.com (repeatable)")
    r.add_argument("--out", help="where runs are written (default: the workspace's runs/, or ./out)")
    r.add_argument("--dry-run", action="store_true", help="show the commands it would run, with inputs filled in, and run none")
    r.add_argument("--json", action="store_true", help="print the run report as JSON")
    a = ap.parse_args(argv)
    if a.cmd == "list":
        for name, path in available().items():
            wf = load(path)
            ins = ", ".join(f"{k}{'' if (v or {}).get('default') not in (None, '') else ' (needed)'}" for k, v in wf.inputs.items())
            print(f"{name:18} {wf.description}" + (f"\n{'':18} inputs: {ins}" if ins else ""))
        return 0
    path = Path(a.workflow) if a.workflow.endswith((".yaml", ".yml")) else available().get(a.workflow)
    if not path or not Path(path).exists():
        print(f"error: no workflow {a.workflow!r}; `lcm workflow list` shows them", file=sys.stderr)
        return 2
    given = {}
    for item in a.set:
        k, eq, v = item.partition("=")
        if not eq:
            print(f"error: --set takes NAME=VALUE (got {item!r})", file=sys.stderr)
            return 2
        given[k.strip()] = v
    try:
        report = run(load(path), given, Path(a.out) if a.out else None, a.dry_run)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2) if a.json else render(report))
    return {"ok": 0, "planned": 0, "attention": 1}.get(report["status"], 2)


if __name__ == "__main__":
    sys.exit(_cli())
