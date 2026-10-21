"""The subcommands lcm runs itself (init, doctor, simulate, size, readout, deliverability) and the table of every
subcommand, each one the same tool you could call directly as a module."""
from __future__ import annotations

import argparse
import importlib
import json
import os
import platform as _platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

Handler = Callable[[List[str]], int]


def _call(module: str, func: str = "_cli", prefix: Optional[List[str]] = None, needs: str = "") -> Handler:
    """A handler that imports `module` only when run, so a missing optional package fails one command, not lcm."""
    def run(argv: List[str]) -> int:
        try:
            mod = importlib.import_module(module)
        except ImportError as e:
            print(f"error: this command needs {needs or e.name} ({e}). Run `lcm doctor` to see what is missing.", file=sys.stderr)
            return 2
        rc = getattr(mod, func)((prefix or []) + list(argv))
        return int(rc or 0)
    return run


@dataclass(frozen=True)
class Command:
    group: str
    help: str
    run: Handler


# ----------------------------------------------------------------------------------------------
# lcm init
# ----------------------------------------------------------------------------------------------

def init(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog="lcm init", description="Create a workspace: lmm.yaml, brand.yaml, data/, briefs/active/ and runs/")
    ap.add_argument("folder", nargs="?", default=".", help="where to create it (default: the current folder)")
    ap.add_argument("--name", help="the brand name customers know (default: the folder's name)")
    ap.add_argument("--platform", choices=["klaviyo", "hubspot", "braze", "customerio", "iterable", "sfmc"],
                    help="the messaging platform you send from (default: klaviyo; edit lmm.yaml later to change it)")
    ap.add_argument("--business-model", choices=["ecommerce", "subscription", "saas", "marketplace", "services", "sales_led_b2b"],
                    help="how the business makes money; sets what the flows aim at (default: ecommerce)")
    a = ap.parse_args(argv)
    from engine import workspace
    folder = Path(a.folder).expanduser().resolve()
    name = a.name or folder.name.replace("-", " ").replace("_", " ").title()
    try:
        created = workspace.init(folder, name, a.platform, a.business_model)
    except FileExistsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"Created a workspace for {name} in {folder}:")
    for p in created:
        print(f"  {p.relative_to(folder)}")
    print("\nNext:\n"
          "  1. Fill in brand.postal_address and sender_name in lmm.yaml, and your logo and colors in brand.yaml\n"
          "  2. Put your exports in data/ (or `lcm import shopify <export folder> --out data`), then `lcm check-data data`\n"
          "  3. `lcm deliverability check yourdomain.com --write-gate gate.json`, and set sending_gate: gate.json\n"
          "  4. `lcm doctor` to confirm, then `lcm plan --text \"a 3-touch abandoned cart flow\" --data data`")
    return 0


# ----------------------------------------------------------------------------------------------
# lcm doctor
# ----------------------------------------------------------------------------------------------

REQUIRED_PACKAGES = (("pydantic", "pydantic"), ("duckdb", "duckdb"), ("sqlglot", "sqlglot"), ("jsonschema", "jsonschema"),
                     ("referencing", "referencing"), ("yaml", "PyYAML"), ("liquid", "python-liquid"), ("jinja2", "Jinja2"),
                     ("django", "Django"), ("dns", "dnspython"), ("httpx", "httpx"), ("publicsuffixlist", "publicsuffixlist"),
                     ("mjml", "mjml"))
OPTIONAL_PACKAGES = (("anthropic", "anthropic", "Claude writes the copy (copy-mode claude or auto with ANTHROPIC_API_KEY)"),
                     ("mcp", "mcp", "the MCP server (lcm mcp)"), ("fastapi", "fastapi", "the webhook receiver (lcm webhooks serve)"),
                     ("gspread", "gspread", "Google Sheets logging (--sheet)"))
KEY_NAMES = (("ANTHROPIC_API_KEY", "Claude-written copy"), ("LCM_SHEET_ID", "Google Sheets logging"),
             ("KLAVIYO_API_KEY", "lcm send to Klaviyo"), ("CUSTOMERIO_APP_API_KEY", "lcm send to Customer.io"),
             ("BRAZE_API_KEY", "lcm send to Braze"), ("HUBSPOT_ACCESS_TOKEN", "lcm send to HubSpot"),
             ("ITERABLE_API_KEY", "lcm send to Iterable"), ("SFMC_CLIENT_ID", "lcm send to Salesforce Marketing Cloud"))


def _version(mod: Any) -> str:
    return str(getattr(mod, "__version__", "") or getattr(mod, "VERSION", "") or "installed")


def diagnose() -> List[Dict[str, str]]:
    """Every check doctor runs, as rows: {name, status (ok, note, fail), detail}."""
    rows: List[Dict[str, str]] = []

    def row(name: str, status: str, detail: str) -> None:
        rows.append({"name": name, "status": status, "detail": detail})

    v = sys.version_info
    row("python", "ok" if v >= (3, 12) else "fail", f"{_platform.python_version()}" + ("" if v >= (3, 12) else "; the engine needs 3.12 or newer"))
    missing = []
    for mod, pkg in REQUIRED_PACKAGES:
        try:
            importlib.import_module(mod)
        except Exception:
            missing.append(pkg)
    row("required packages", "fail" if missing else "ok",
        f"missing {', '.join(missing)}: run `pip install -e .` (or `uv sync`) in the repository" if missing else f"{len(REQUIRED_PACKAGES)} found")
    for mod, pkg, why in OPTIONAL_PACKAGES:
        try:
            m = importlib.import_module(mod)
            row(f"optional: {pkg}", "ok", f"{_version(m)}, for {why}")
        except Exception:
            row(f"optional: {pkg}", "note", f"not installed; only needed for {why}")
    try:
        from engine import __version__
        row("engine", "ok", f"LMM Engine {__version__}")
    except Exception as e:      # noqa: BLE001
        row("engine", "fail", f"cannot import the engine: {e}")
        return rows
    if not missing:
        try:
            from engine.render.mjml_render import compile_mjml
            html = compile_mjml('<mjml><mj-body><mj-section><mj-column><mj-text>ok {{ first_name }}</mj-text>'
                                '</mj-column></mj-section></mj-body></mjml>')
            row("mjml compile", "ok" if "{{ first_name }}" in html else "fail",
                "compiles and keeps template tags" if "{{ first_name }}" in html else "compiled but lost a template tag")
        except Exception as e:  # noqa: BLE001
            row("mjml compile", "fail", f"{type(e).__name__}: {e}")

    from engine import workspace
    ws = workspace.find()
    if ws.fallback:
        row("workspace", "note", f"none here; using the bundled demo ({ws.root}). `lcm init` creates one, or set LCM_WORKSPACE")
    else:
        row("workspace", "ok", str(ws.root))
        if not ws.config_path.exists():
            row("lmm.yaml", "fail", f"{ws.config_path} not found")
        else:
            try:
                errs = workspace.validate(ws)
                row("lmm.yaml", "fail" if errs else "ok", "; ".join(errs[:4]) if errs else "valid against schemas/workspace.schema.json")
                d = workspace.brief_defaults(ws)
                gate = d.get("sending_gate", "UNKNOWN")
                if isinstance(gate, dict):
                    from engine.policy.contracts import effective_gate
                    eff, why = effective_gate(gate)
                    row("sending gate", "ok" if eff == gate.get("verdict") else "note",
                        why if eff == gate.get("verdict") else f"{gate.get('verdict')} recorded, runs as {eff}: {why}")
                else:
                    row("sending gate", "note", f"{gate}: runs as AMBER at best. `lcm deliverability check <domain> --write-gate gate.json` "
                                                f"records an observed one")
            except Exception as e:  # noqa: BLE001
                row("lmm.yaml", "fail", f"{type(e).__name__}: {e}")
    try:
        from engine.render.brand import find_brand
        kit = find_brand(ws)
        bad = [r for r in kit.contrast_report() if not r.ok and r.enforced]
        row("brand kit", "fail" if bad else "ok", (f"{len(bad)} color pair(s) below WCAG AA; run `lcm brand check`" if bad else
                                                  f"{kit.source or 'neutral default'}; contrast passes WCAG AA"))
    except Exception as e:      # noqa: BLE001
        row("brand kit", "fail", f"{e}")
    data = ws.data_dir
    if not data.exists() or not any(p.suffix in (".csv", ".parquet", ".json", ".jsonl") for p in data.rglob("*")):
        row("data", "note", f"{data} is empty; plans run without volume or baseline estimates until it holds exports")
    else:
        try:
            from engine.data.load import connect
            con, notes = connect(str(data))
            tables = [r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()]
            row("data", "ok", f"{len(tables)} table(s): {', '.join(sorted(tables))}" + (f"; {len(notes)} note(s), see `lcm check-data {data}`" if notes else ""))
        except Exception as e:  # noqa: BLE001
            row("data", "fail", f"{e}; run `lcm check-data {data}` for the full report")
    briefs = sorted(ws.active_briefs.glob("*.y*ml")) if ws.active_briefs.exists() else []
    if briefs:
        from engine.brief import load_brief
        bad = []
        for p in briefs:
            try:
                load_brief(p)
            except Exception as e:  # noqa: BLE001
                bad.append(f"{p.name}: {str(e)[:120]}")
        row("active briefs", "fail" if bad else "ok", "; ".join(bad) if bad else f"{len(briefs)} load cleanly")
    for name, why in KEY_NAMES:
        row(f"key: {name}", "ok" if os.environ.get(name) else "note", ("set" if os.environ.get(name) else "not set") + f" (for {why})")
    return rows


def doctor(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog="lcm doctor", description="Check the install, the workspace, the brand kit, the data and which keys are set "
                                                            "(names only; values are never printed)")
    ap.add_argument("--json", action="store_true", help="print the checks as JSON")
    a = ap.parse_args(argv)
    rows = diagnose()
    if a.json:
        print(json.dumps(rows, indent=2))
    else:
        mark = {"ok": "OK  ", "note": "NOTE", "fail": "FAIL"}
        for r in rows:
            print(f"  {mark[r['status']]}  {r['name']}: {r['detail']}")
        fails = sum(r["status"] == "fail" for r in rows)
        print(f"\n{'Ready.' if not fails else f'{fails} problem(s) to fix before planning.'}")
    return 1 if any(r["status"] == "fail" for r in rows) else 0


# ----------------------------------------------------------------------------------------------
# commands that pick between a module's subcommands
# ----------------------------------------------------------------------------------------------

def simulate(argv: List[str]) -> int:
    """lcm simulate <flow_spec.json>  |  lcm simulate --program [--add brief.yaml]"""
    if "--program" in argv:
        from engine.policy import program
        return program._cli([x for x in argv if x != "--program"])
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: lcm simulate FLOW_SPEC [--rules simulation.json] [--payloads DIR] [--json]\n"
              "       lcm simulate --program [--briefs DIR] [--add BRIEF ...] [--json]\n\n"
              "One flow: walk synthetic people through a flow spec and check caps, send windows, exits and payloads.\n"
              "--program: every live brief in briefs/active/ at once, against the workspace's one frequency policy.")
        return 0
    from engine.policy import simulator
    return int(simulator.main(argv) or 0)


STATS_SUBS = ("size", "mde", "feasible", "readout", "incrementality")


def size(argv: List[str]) -> int:
    """lcm size --baseline 0.1 --mde-rel 0.2 [--holdout 0.15] [--entries-per-month N]; or lcm size mde|feasible ..."""
    from engine.measurement import stats
    if argv and argv[0] in STATS_SUBS:
        return stats._cli(argv)
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: lcm size --baseline RATE (--mde-rel R | --mde-abs A) [--holdout SHARE] [--entries-per-month N] "
              "[--outcome-window-days D] [--json]\n       lcm size mde --baseline RATE --nc N --nt N\n\n"
              "With --entries-per-month, says how many months the holdout takes to read and what a larger holdout would buy.\n"
              "Rates are shares: --baseline 0.10 is 10%. `lcm size size --help` lists every flag.")
        return 0
    return stats._cli((["feasible"] if "--entries-per-month" in argv else ["size"]) + argv)


def readout(argv: List[str]) -> int:
    """lcm readout results.json (incremental vs attributed)  |  lcm readout --xt .. --nt .. --xc .. --nc .."""
    from engine.measurement import stats
    if argv and argv[0] in STATS_SUBS:
        return stats._cli(argv)
    if argv and argv[0].endswith(".json"):
        return stats._cli(["incrementality", argv[0]] + (["--markdown"] if "--json" not in argv else []))
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: lcm readout RESULTS.json [--json]\n       lcm readout --xt N --nt N --xc N --nc N [--expected-holdout SHARE] [--json]\n\n"
              "A results file reads incremental against attributed conversions; four counts read one holdout.")
        return 0
    return stats._cli(["readout"] + argv)


DELIVERABILITY = {
    "check": ("engine.deliverability.dns_collect", [], "look up a domain's SPF, DKIM, DMARC, MTA-STS and BIMI and issue a gate"),
    "analyze": ("engine.deliverability.analyze", ["analyze"], "analyze a collected snapshot or signals JSON"),
    "gate": ("engine.deliverability.analyze", ["gate"], "issue a Red/Amber/Green gate from a signals JSON"),
    "dkim": ("engine.deliverability.analyze", ["dkim"], "parse one DKIM record"),
    "headers": ("engine.deliverability.analyze", ["headers"], "read the authentication results in a raw message's headers"),
    "monitor": ("engine.deliverability.monitor", [], "the daily trend check across sending signals"),
    "bounces": ("engine.deliverability.bounces", [], "classify bounces: hard, soft, block, policy"),
    "complaints": ("engine.deliverability.complaints", [], "complaint rates by mailbox provider and by flow"),
    "providers": ("engine.deliverability.providers", [], "mailbox provider share of a list"),
}


def deliverability(argv: List[str]) -> int:
    if not argv or argv[0] in ("-h", "--help") or argv[0] not in DELIVERABILITY:
        print("usage: lcm deliverability {" + ",".join(DELIVERABILITY) + "} ...\n")
        for k, (_, _, h) in DELIVERABILITY.items():
            print(f"  {k:11} {h}")
        print("\n`lcm deliverability check example.com --write-gate gate.json` records the gate lmm.yaml points to.")
        return 0 if not argv or argv[0] in ("-h", "--help") else 2
    mod, prefix, _ = DELIVERABILITY[argv[0]]
    rest = argv[1:]
    if argv[0] == "check" and rest and not any(x in rest for x in ("--analyze", "--json")):
        rest = rest + ["--analyze", "--markdown"] if "--write-gate" not in rest else rest + ["--analyze"]
    return _call(mod, prefix=prefix)(rest)


def brand(argv: List[str]) -> int:
    if argv[:1] == ["check"]:
        argv = argv[1:]
    return _call("engine.render.brand")(argv)


def mcp(argv: List[str]) -> int:
    if argv[:1] in (["-h"], ["--help"]):
        print("usage: lcm mcp\n\nRun the engine's MCP server on stdio (needs `pip install -e .[server]`).")
        return 0
    try:
        from engine import mcp_server
    except ImportError as e:
        print(f"error: the MCP server needs the server extras: pip install -e '.[server]' ({e})", file=sys.stderr)
        return 2
    mcp_server.server.run("stdio")
    return 0


def _orchestrator(cmd: str) -> Handler:
    return _call("engine.pipeline.orchestrator", prefix=[cmd])


COMMANDS: Dict[str, Command] = {
    # setup
    "init": Command("setup", "create a workspace (lmm.yaml, brand.yaml, data/, briefs/active/, runs/)", init),
    "doctor": Command("setup", "check the install, workspace, brand kit, data and key names", doctor),
    "check-data": Command("setup", "check exports against the canonical tables: missing, late, empty, malformed",
                          _call("engine.data.check", needs="the source adapters (engine/data/check.py)")),
    "import": Command("setup", "map a vendor export (Shopify, Klaviyo, Stripe, HubSpot, GA4, Segment...) to canonical tables",
                      _call("adapters.sources.cli", needs="the source adapters (adapters/sources/cli.py)")),
    "questions": Command("setup", "the questions the engine asks before planning, and which ones your workspace answers",
                         _call("engine.questions")),
    # plan
    "plan": Command("plan", "one brief in, a build-ready plan out", _orchestrator("plan")),
    "brief": Command("plan", "turn a sentence into an editable YAML brief", _orchestrator("brief")),
    "revise": Command("plan", "a new run from an old one with fields changed; approved copy is kept", _orchestrator("revise")),
    "approve": Command("plan", "approve a copy variant so revisions keep it", _orchestrator("approve")),
    "status": Command("plan", "a run's stages", _orchestrator("status")),
    "resume": Command("plan", "continue a run that failed part way", _orchestrator("resume")),
    "simulate": Command("plan", "dry-run one flow spec, or every live flow at once with --program", simulate),
    # build and send
    "render": Command("ship", "compile the MJML email templates with the brand kit", _call("engine.render.mjml_render")),
    "brand": Command("ship", "validate a brand kit and print its contrast table (lcm brand check)", brand),
    "lint": Command("ship", "lint a template in its platform dialect", _call("engine.render.template_lint")),
    "sms": Command("ship", "SMS encoding, segments and opt-out language", _call("engine.render.sms")),
    "sync": Command("ship", "profile sync request bodies for the platform (tiers, sunset flags)", _call("adapters.destinations.payloads")),
    "events": Command("ship", "push the flow's trigger events from the warehouse ledger", _call("adapters.destinations.event_payloads")),
    "sheets": Command("ship", "log plans and the morning check to Google Sheets", _call("adapters.destinations.sheets", "main", needs="gspread")),
    # deliverability
    "deliverability": Command("deliverability", "check, analyze, gate, monitor, bounces, complaints, providers", deliverability),
    "warmup": Command("deliverability", "a warm-up schedule for a new domain or IP, with abort rules", _call("engine.deliverability.warmup")),
    "engagement": Command("deliverability", "engagement tiers and the sunset list, by clicks not opens", _call("engine.agents.engagement")),
    # measure
    "size": Command("measure", "people and months a holdout needs", size),
    "readout": Command("measure", "read a holdout: incremental, not attributed", readout),
    "experiments": Command("measure", "the experiment registry: add, list, check overlaps, record looks", _call("engine.measurement.registry")),
    "sequential": Command("measure", "group-sequential and always-valid stopping boundaries", _call("engine.measurement.sequential")),
    "cluster": Command("measure", "ICC, design effect and cluster-aware sizing for account-level tests", _call("engine.measurement.cluster")),
    "global-holdout": Command("measure", "the program-wide holdout every flow excludes", _call("engine.measurement.global_holdout")),
    "discounts": Command("measure", "discount dependency by customer and flow", _call("engine.measurement.discounts")),
    "roi": Command("measure", "what the program is worth next year", _call("engine.measurement.roi")),
    # analyze
    "audit": Command("analyze", "audit a flow's cohort against the data", _call("engine.agents.cohort_analyst", prefix=["audit"])),
    "blast": Command("analyze", "compare a newsletter or blast with a triggered flow", _call("engine.agents.cohort_analyst", prefix=["blast-vs-trigger"])),
    "priorities": Command("analyze", "the next flows worth building, ranked by money", _call("engine.agents.priorities")),
    "tracking": Command("analyze", "are the events firing: schema drift, nulls, late arrivals", _call("engine.agents.tracking")),
    "retention": Command("analyze", "retention curves and the churn cliff", _call("engine.agents.retention")),
    "daily": Command("analyze", "the morning check: what broke overnight", _call("engine.workflows.daily", "main")),
    "webhooks": Command("analyze", "parse or receive platform webhooks", _call("adapters.sources.webhooks")),
    # developer
    "mcp": Command("developer", "run the MCP server (stdio)", mcp),
    "sql-check": Command("developer", "parse and check generated SQL in every dialect", _call("engine.sql.check")),
    "repo-check": Command("developer", "the repository's own consistency checks", _call("engine.repo_check", "main")),
}
GROUPS = (("setup", "Set up"), ("plan", "Plan"), ("ship", "Build and send"), ("deliverability", "Deliverability"),
          ("measure", "Measure"), ("analyze", "Analyze"), ("developer", "Developer"))


def overview() -> str:
    lines = ["usage: lcm COMMAND [options]      (lcm COMMAND --help for each command's options)",
             "       lcm \"what you want, in plain English\" [--data DIR] [--product NAME] [--dry-run]", ""]
    for key, title in GROUPS:
        lines.append(f"{title}:")
        lines += [f"  {name:15} {c.help}" for name, c in COMMANDS.items() if c.group == key]
        lines.append("")
    lines.append("Inside a workspace (lmm.yaml here or above, or LCM_WORKSPACE) every command uses its platform, brand, gate, policies and data.")
    return "\n".join(lines)
