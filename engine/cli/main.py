"""lcm: one command for the whole engine, as a subcommand, in plain English, or with a YAML job file.

    lcm init my-brand --platform klaviyo        # every subcommand: lcm --help; each one's options: lcm plan --help
    lcm doctor
    lcm plan --text "a 3-touch abandoned cart flow" --data data
    lcm "make it 4 touches"                     # revises the newest run
    lcm "plan a 5-touch win-back for users who completed onboarding but haven't created a report in 14 days" --product Acme
    lcm "check deliverability for example.com"
    lcm "audit the cohort for briefs/active/winback.yaml" --data exports/
    lcm "compare our monthly newsletter with the win-back trigger" --brief briefs/active/winback.yaml --data exports/
    lcm "build the engagement tiers and the sunset list" --data exports/ --platform klaviyo
    lcm "read the holdout results in results/winback_q3.json"
    lcm "how many people do we need to detect a 20% lift on a 10% baseline with a 15% holdout"
    lcm job.yaml                      # kind: plan | revise | deliverability | audit | blast | engagement | readout | size
    lcm --dry-run "..."               # say which job it would run, and with what, without running it

(Without installing: python3 -m engine.cli.main "...". pip install -e . puts lcm on your PATH.)

The router is rules, not a model: the same sentence always runs the same job, and anything it is
not sure about it asks for instead of guessing. Every job is the same tool you could call directly;
the router only picks it and fills in its arguments.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shlex
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DOMAIN = re.compile(r"\b((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})\b", re.I)
YAML_PATH = re.compile(r"[\w./-]+\.ya?ml\b")
JSON_PATH = re.compile(r"[\w./-]+\.json\b")
PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
INTENTS = (
    ("priorities", r"\bpriorit(y|ies)\s+(builds?|campaigns?|flows?|projects?)\b|\btop priorit|"
                   r"what (should|do|can) (i|we) (build|launch|work on|focus on|prioriti[sz]e)\b|what to build|"
                   r"which (flows?|campaigns?|builds?) should (i|we) (build|launch|start|do|prioriti[sz]e)\b|"
                   r"top \d+ (\w+ ){0,3}?(builds?|campaigns?|campaign ideas|ideas|flows?|priorities)\b(?! by)|campaign ideas|"
                   r"(flow|win-?back|lifecycle|retention|email) ideas|ideas for (new )?(flows?|campaigns?)|"
                   r"(next|best) (\w+ )?(flow|campaign|build)s? to (build|launch)|what('s| is) (the )?next (best )?(build|flow|campaign)\b|"
                   r"biggest (\w+ )?opportunit|(best|biggest|top) opportunities|where .*(leaving|losing) (money|revenue)|money on the table"),
    ("daily", r"what broke|overnight|anything break|morning (check|brief|summary|report)|daily (check|summary|health|report)"),
    ("check_data", r"\bis (our|my|the) data (ready|ok|okay|good|complete|clean|trustworthy)|check (our|my|the) (data|exports)|"
                   r"data (quality|check)|(missing|bad|broken) data\b|can (i|we) trust (our|my|the) data"),
    ("program", r"\b(flows?|campaigns?|journeys?) (collide|overlap|clash|step on each other)|(collide|overlap|clash) (with|across)|"
                r"(too many|how many) (emails|messages|texts) .*(a|per) (week|day)|all (our |my |the )?(live )?flows together|"
                r"(frequency|caps?|too many|over-?mail\w*)[^.?]*across (all )?(our |my |the )?(live )?flows"),
    ("tracking", r"events? (are |is )?firing|event (stream|tracking|taxonomy|schema)|tracking plan|tracking (health|break|broke)|"
                 r"schema (drift|fidelity)|data (has )?drift|has the data drifted|webhook|null values|break a trigger|broke a trigger|"
                 r"triggers? (stopped|broke|breaking)|break (our |the )?tracking|still (coming|arriving)|\bnull\b"),
    ("retention", r"retention curve|churn cliff|stop using|stop doing|when do (customers|users|people) (stop|churn|drop|leave)|"
                  r"retention (drop|cliff|by|differ|look|rate)|week \d+ retention|levels? off|flatten"),
    ("deliverability", r"deliverab|inbox|\bspam\b|warm-?up|new (sending )?domain|sending plan|\bspf\b|\bdkim\b|\bdmarc\b|mta-sts|authenticat|sending gate|bounce rate|blocklist|blacklist"),
    ("engagement", r"engagement tier|unengaged|sunset|suppress|re-?permission list|inactive subscribers|stop (emailing|mailing|sending to)|"
                   r"over-?mail|list (health|hygiene)|healthy is our list|dormant"),
    ("blast", r"blast|newsletter|calendar|batch(?:-and-| and )blast|one-off send|scheduled send"),
    ("audit", r"\baudit\b|health check|cohort health|anomal|unusual|drop(ping)? off in onboarding|onboarding (drop|leak|funnel)|funnel|\bleak"),
    ("size", r"sample size|how many (people|users|entrants)|power|minimum detectable|\bmde\b|how long .*holdout|holdout (size|share)|"
             r"how (big|large) .*holdout"),
    ("readout", r"read ?out|results?\b|incremental|attributed|did (it|the [\w -]*?(flow|campaign|test)) (actually |really )?work|holdout (results?|read|lift)|lift we got|"
                r"ready to call|call (the|this|my) test|significan|actually add|platform credits|credited|underperform|"
                r"\broi\b (of|on|from) (the|our|my|this)\b"),
    ("roi", r"\broi\b|business case|program worth|engine worth|worth next year"),
    ("plan", r"\bplan\b|flow|campaign|journey|sequence|series|touch|win-?back|onboarding|welcome|activation|nurture|re-?engage"),
)
# Asking for a flow is a plan, whatever else the sentence mentions ("a re-engagement flow for inactive subscribers").
PLAN_FIRST = re.compile(r"\b(plan|build|design|create|draft|write|set up|map out)\b[^.?!]*\b(flow|campaign|journey|sequence|series|cadence|drip|"
                        r"win-?back|onboarding|welcome|nurture|re-?engagement|reactivation|activation)\b", re.I)
# ...and so is naming one, without a verb: "a 5-touch win-back flow with a 15% holdout", "nurture sequence for dormant leads"
PLAN_NOUN = re.compile(r"^\s*(?:(?:i|we) (?:need|want|would like) )?(?:an? |the |our )?(?:\d+[- ](?:touch|step|email|message)s? )?"
                       r"(?:[\w-]+ ){0,2}?(?:flow|sequence|series|journey|drip|cadence|win-?back)\b(?! (?:ideas?|priorities)\b)", re.I)
PLAN_IMPERATIVE = re.compile(r"^\s*(?:please )?(?:re-?engage|win back|bring back|reactivate)\b", re.I)
QUESTION = re.compile(r"^\s*(what|which|when|where|why|who|how|is|are|do|does|did|should|can|could|would|will)\b|\?\s*$", re.I)
NOT_DOMAINS = {"e.g", "i.e"}
# Changing a plan just made is a revision of the newest run, not a new plan: "make it 4 touches", "add SMS", "use a 20% holdout"
REVISE = re.compile(r"^\s*(?:ok(?:ay)?,? |now |please |and |actually,? |then )*(?:make it|make the (?:flow|holdout|wait)|change|add|remove|drop|"
                    r"switch|increase|decrease|cut|bump|lower|raise|use|go with|set(?! up)|only)\b", re.I)
REVISABLE = re.compile(r"\b(touch(?:es)?|steps?|messages?|emails?|sms|texts?|push|in-?app|hold-?out|days?|weeks?|klaviyo|braze|hubspot|"
                       r"customer\.?io|sfmc|salesforce|iterable)\b", re.I)
WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8}
PLATFORM_WORDS = (("klaviyo", "klaviyo"), ("braze", "braze"), ("hubspot", "hubspot"), ("customer.io", "customerio"), ("customerio", "customerio"),
                  ("salesforce", "sfmc"), ("sfmc", "sfmc"), ("iterable", "iterable"))
# what each job needs, and how to ask for it
NEEDS: Dict[str, List[Tuple[str, str]]] = {
    "check_data": [("data", "--data (the folder of exports to check)")],
    "deliverability": [("domain", "the sending domain (for example: check deliverability for example.com)")],
    "audit": [("brief", "--brief (which flow's cohort)"), ("data", "--data (a folder or URL prefix with users, orders, track_events exports)")],
    "blast": [("brief", "--brief (which flow's cohort)"), ("data", "--data (a folder or URL prefix with users, orders, track_events exports)")],
    "engagement": [("data", "--data (a folder with users and message_events exports)")],
    "priorities": [("data", "--data (a folder or URL prefix with users, orders, track_events exports)")],
    "tracking": [("data", "--data (a folder or URL prefix with users and track_events exports)")],
    "retention": [("data", "--data (a folder or URL prefix with users and track_events exports)")],
    "readout": [("input", "the results file (for example: read the holdout results in results/winback.json)")],
    "size": [("baseline", "the baseline rate (for example: on a 10% baseline)"), ("mde_rel", "the lift worth detecting (for example: a 20% lift)")],
}
DEFAULTS: Dict[str, Dict[str, Any]] = {"size": {"holdout": 0.15}}


@dataclass
class Job:
    kind: str
    args: Dict[str, Any] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)
    why: str = ""


def classify(text: str) -> Optional[str]:
    """Rules, in order: asking for a flow is a plan; then "what should I build"; then naming a flow; then the first job
    whose words appear. A question that only mentions a flow ("which flows should we retire?") matches no job: lcm
    says so and points to Claude, instead of starting a campaign plan nobody asked for."""
    low = text.lower()
    if REVISE.search(low) and REVISABLE.search(low) and not PLAN_FIRST.search(low):
        return "revise"
    if PLAN_FIRST.search(low):
        return "plan"
    if re.search(INTENTS[0][1], low):      # "what should I build" asks for priorities, not a plan
        return "priorities"
    asking = bool(QUESTION.search(low))
    if not asking and (PLAN_NOUN.search(low) or PLAN_IMPERATIVE.search(low)):
        return "plan"
    for kind, pattern in INTENTS[1:]:
        if re.search(pattern, low):
            if kind == "plan" and asking and not re.search(r"\b(plan|draft|design|build|worth)\b", low):
                return None
            return kind
    return None


def route(text: str, opts: Dict[str, Any]) -> Job:
    kind = classify(text)
    if kind is None:
        return Job("help", why="no job matched")
    low = text.lower()
    a: Dict[str, Any] = {k: v for k, v in opts.items() if v not in (None, False, [])}
    brief = a.get("brief") or next(iter(YAML_PATH.findall(text)), None)
    job = Job(kind, why=f"matched '{kind}'")
    if kind == "revise":
        job.args = {"run": a.get("run") or newest_run(), "changes": revision(text, a.get("run") or newest_run())}
        if not job.args["run"]:
            job.missing.append("--run (no plan has been run here yet; plan one first, or name the run folder)")
        elif not job.args["changes"]:
            job.missing.append("what to change (for example: make it 4 touches, add SMS, use a 20% holdout, switch to Klaviyo)")
        return job
    if kind == "plan":
        job.args = {"text": text, "product": a.get("product"), "data": a.get("data"), "copy_mode": a.get("copy_mode", "auto"), "out": a.get("out", "out")}
        if brief:
            job.args = {"brief": brief, **{k: v for k, v in job.args.items() if k != "text"}}
        elif not a.get("product"):
            from engine import workspace
            if not workspace.brief_defaults().get("product_name"):
                job.missing.append("--product (the product name customers know; or set brand.name in lmm.yaml)")
    elif kind == "deliverability":
        domains = [d for d in DOMAIN.findall(text) if d.lower() not in NOT_DOMAINS and not d.lower().endswith((".yaml", ".yml", ".json"))]
        sel = re.search(r"selectors?\s+([\w,\s-]+?)(?:\.|$| and )", text, re.I)
        job.args = {"domain": a.get("domain") or (domains[0] if domains else None), "selectors": a.get("selectors") or (sel.group(1).replace(" ", "") if sel else None),
                    "signals": a.get("signals"), "envelope": a.get("envelope")}
    elif kind in ("audit", "blast"):
        job.args = {"brief": brief, "data": a.get("data"), "out": a.get("out", "out")}
        if kind == "blast":
            job.args["dates"] = a.get("dates") or ",".join(re.findall(r"\b20\d\d-\d\d-\d\d\b", text)) or None
        if not brief:
            from engine import workspace
            active = sorted(glob.glob(str(workspace.find().active_briefs / "*.yaml")))
            if len(active) == 1:
                job.args["brief"] = active[0]
            else:
                job.missing.append("--brief (which flow's cohort)" + (f"; active briefs: {', '.join(Path(p).name for p in active)}" if active else ""))
    elif kind == "daily":
        job.args = {"data": a.get("data"), "domains": a.get("domain") or os.environ.get("LCM_DOMAINS") or None, "out": a.get("out", "out")}
    elif kind == "tracking":
        job.args = {"data": a.get("data"), "events_dir": a.get("events_dir") or os.environ.get("LCM_EVENTS_DIR"), "out": a.get("out", "out")}
    elif kind == "retention":
        by = a.get("by") or next((c for c, pat in (("country", r"countr|region|\bgeo"), ("lifecycle_stage", r"lifecycle stage"),
                                                    ("plan", r"\bplans?\b|tier|enterprise|segment")) if re.search(pat, low)), None)
        job.args = {"data": a.get("data"), "brief": brief, "by": by, "out": a.get("out", "out")}
    elif kind == "roi":
        job.args = {}
    elif kind == "check_data":
        from engine import workspace
        ws = workspace.find()
        job.args = {"data": a.get("data") or (str(ws.data_dir) if not ws.fallback else None)}
    elif kind == "program":
        job.args = {"add": [brief] if brief else []}
    elif kind == "priorities":
        n = re.search(r"\btop\s+(\d+)", low)
        job.args = {"data": a.get("data"), "brief": a.get("brief"), "top": int(n.group(1)) if n else 3, "value": a.get("value"),
                    "domain": a.get("domain"), "out": a.get("out", "out")}
    elif kind == "engagement":
        plat = next((p for p in ("klaviyo", "hubspot", "braze", "customerio", "customer.io") if p in low), None)
        job.args = {"data": a.get("data"), "platform": a.get("platform") or (plat.replace(".", "") if plat else None), "out": a.get("out", "out")}
    elif kind == "readout":
        path = a.get("input") or next(iter(JSON_PATH.findall(text)), None)
        job.args = {"input": path}
    elif kind == "size":
        pcts = [float(x) / 100 for x in PCT.findall(text)]
        lift = next((p for p, m in zip(pcts, PCT.finditer(text)) if re.search(r"lift|increase|improve", text[m.end():m.end() + 25], re.I)), None)
        hold = next((p for p, m in zip(pcts, PCT.finditer(text)) if re.search(r"holdout", text[m.end():m.end() + 15], re.I)), None)
        base = next((p for p in pcts if p not in (lift, hold)), None)
        entrants = re.search(r"(\d[\d,]*)\s*(entrants|people|users|signups)\s*(a|per)\s*month", text, re.I)
        job.args = {"baseline": base, "mde_rel": lift, "holdout": hold or 0.15,
                    "entries_per_month": int(entrants.group(1).replace(",", "")) if entrants else None}
    return check(job)


def newest_run() -> Optional[str]:
    """The most recent run under the default output folder (the workspace's runs/, or ./out)."""
    from engine.pipeline.orchestrator import default_out
    states = sorted(Path(default_out()).glob("*/runs/*/state.json"), key=lambda p: p.stat().st_mtime)
    return str(states[-1].parent) if states else None


def revision(text: str, run: Optional[str]) -> Dict[str, Any]:
    """Brief fields a revision sentence changes. Channels are edited against the run's own channels."""
    low = text.lower()
    out: Dict[str, Any] = {}
    m = re.search(r"\b(\d+|one|two|three|four|five|six|seven|eight)[- ]?(?:touch(?:es)?|steps?|messages?|emails?)\b", low)
    if m:
        out["touches"] = int(m.group(1)) if m.group(1).isdigit() else WORDS[m.group(1)]
    m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*hold-?out|hold-?out\s+(?:to|of|at)\s+(\d+(?:\.\d+)?)\s*%?|no hold-?out", low)
    if m:
        out["holdout_pct"] = 0.0 if m.group(0).startswith("no") else float(m.group(1) or m.group(2))
    plat = next((v for k, v in PLATFORM_WORDS if k in low), None)
    if plat:
        out["platform"] = plat
    m = re.search(r"\b(\d+)[- ]?(day|week)s?\b(?! (?:hold|read))", low)
    if m and not re.search(r"touch|step|message", low[m.end():m.end() + 12]):
        days = int(m.group(1)) * (7 if m.group(2) == "week" else 1)
        out["_days"] = days
    chans = {"sms": r"\bsms\b|\btexts?\b", "push": r"\bpush\b", "in_app": r"in-?app"}
    named = [c for c, pat in chans.items() if re.search(pat, low)]
    if named or re.search(r"\bemail only\b|\bonly email\b", low):
        current: List[str] = ["email"]
        brief: Dict[str, Any] = {}
        if run and (Path(run) / "state.json").exists():
            brief = json.loads((Path(run) / "state.json").read_text(encoding="utf-8")).get("brief", {})
            current = list(brief.get("channels") or ["email"])
        if re.search(r"\bemail only\b|\bonly email\b", low):
            out["channels"] = ["email"]
        elif re.search(r"\b(remove|drop|no|without|cut)\b", low):
            out["channels"] = [c for c in current if c not in named] or ["email"]
        else:
            out["channels"] = current + [c for c in named if c not in current]
    if "_days" in out:
        days = out.pop("_days")
        ft = None
        if run and (Path(run) / "state.json").exists():
            ft = json.loads((Path(run) / "state.json").read_text(encoding="utf-8")).get("brief", {}).get("flow_type")
        field_for = {"winback": "churn_threshold_days", "sunset": "churn_threshold_days", "replenishment": "replenish_after_days",
                     "trial_convert": "trial_days"}
        if ft in field_for:
            out[field_for[ft]] = days
    return out


def check(job: Job) -> Job:
    """Fill defaults and list what the job still needs, so nothing runs half-specified."""
    for k, v in DEFAULTS.get(job.kind, {}).items():
        if job.args.get(k) in (None, ""):
            job.args[k] = v
    job.args.setdefault("out", "out")
    for key, ask in NEEDS.get(job.kind, []):
        if job.args.get(key) in (None, "", []) and not any(m.startswith(ask.split(" (")[0]) for m in job.missing):
            job.missing.append(ask)
    return job


def from_yaml(path: Path, opts: Dict[str, Any]) -> Job:
    import yaml
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return Job("help", why=f"{path}: a job file is YAML keys and values (kind: ..., then the job's settings)")
    kind = raw.pop("kind", None)
    given = {k: v for k, v in opts.items() if v not in (None, False, [])}
    if kind is None and "product_name" in raw:   # a campaign brief is a plan job
        return check(Job("plan", {"brief": str(path), "data": given.get("data"), "copy_mode": given.get("copy_mode", "auto"),
                                  "out": given.get("out", "out")}, why="a campaign brief"))
    if kind == "plan" and "brief" not in raw:
        return Job("help", why=f"{path}: a plan job names its brief (brief: briefs/my_flow.yaml)")
    if kind == "revise":
        if "run" not in raw or "set" not in raw:
            return Job("help", why=f"{path}: a revise job names its run and the fields to set (run: ..., set: {{touches: 4}})")
        return Job("revise", {"run": str(raw["run"]), "changes": dict(raw["set"])}, why="kind: revise")
    if kind not in {k for k, _ in INTENTS}:
        return Job("help", why=f"{path}: kind must be one of revise, {', '.join(k for k, _ in INTENTS)} (got {kind!r})")
    return check(Job(kind, {**given, **raw}, why=f"kind: {kind}"))


def run(job: Job) -> int:
    a = {k: v for k, v in job.args.items() if v not in (None, "", [])}
    if job.kind == "revise":
        from engine.pipeline import orchestrator
        argv = ["revise", "--run", a["run"]]
        for k, v in a["changes"].items():
            argv += ["--set", f"{k}={json.dumps(v) if isinstance(v, list) else v}"]
        print(f"revising {a['run']}: " + ", ".join(f"{k} = {v}" for k, v in a["changes"].items()))
        return orchestrator._cli(argv)
    if job.kind == "plan":
        from engine.pipeline import orchestrator
        argv = ["plan"] + (["--brief", a["brief"]] if "brief" in a else ["--text", a["text"]])
        for k in ("product", "data", "copy_mode", "out"):
            if k in a and not ("brief" in a and k == "product"):
                argv += [f"--{k.replace('_', '-')}", str(a[k])]
        return orchestrator._cli(argv)
    if job.kind == "daily":
        from engine.workflows import daily
        out = Path(a.get("out", "out")) / "daily"
        if a.get("data"):
            daily.run("url", data=a["data"], domains=str(a.get("domains") or "").split(",") if a.get("domains") else [], out=out, sheet=False)
        elif not (out / "summary.md").exists():
            print("No morning check has run here yet. Give --data (a folder or URL prefix with your exports) to run one now.", file=sys.stderr)
            return 2
        else:
            print(f"(from the check written {datetime.fromtimestamp((out / 'summary.md').stat().st_mtime).isoformat(timespec='minutes')})\n")
        print((out / "summary.md").read_text(encoding="utf-8"))
        return 0
    if job.kind == "tracking":
        from engine.agents import tracking
        return tracking._cli(["--data", a["data"]] + (["--events-dir", a["events_dir"]] if a.get("events_dir") else []))
    if job.kind == "retention":
        from engine.agents import retention
        return retention._cli(["--data", a["data"]] + (["--by", a["by"]] if a.get("by") else []) + (["--brief", a["brief"]] if a.get("brief") else []))
    if job.kind == "roi":
        from engine.measurement import roi as roi_model
        return roi_model._cli([])
    if job.kind == "check_data":
        from engine.data import check
        return check._cli([a["data"]])
    if job.kind == "program":
        from engine.policy import program
        return program._cli([x for b in a.get("add", []) for x in ("--add", b)])
    if job.kind == "priorities":
        from engine.agents import priorities
        argv = ["--data", a["data"], "--top", str(a.get("top", 3)), "--out", str(a.get("out", "out"))]
        if a.get("brief"):
            argv += ["--brief", str(a["brief"])]
        if a.get("value"):
            argv += ["--value-per-conversion", str(a["value"])]
        for d in str(a.get("domain") or "").split(","):
            if d.strip():
                argv += ["--domain", d.strip()]
        return priorities._cli(argv)
    if job.kind == "deliverability":
        from engine.deliverability import dns_collect
        argv = [a["domain"], "--analyze", "--markdown"]
        for k in ("selectors", "signals", "envelope"):
            if k in a:
                argv += [f"--{k}", str(a[k])]
        return dns_collect._cli(argv)
    if job.kind == "audit":
        from engine.agents import cohort_analyst
        return cohort_analyst._cli(["audit", "--brief", a["brief"], "--data", a["data"], "--out", str(Path(a.get("out", "out")) / "audit")])
    if job.kind == "blast":
        from engine.agents import cohort_analyst
        argv = ["blast-vs-trigger", "--brief", a["brief"], "--data", a["data"], "--out", str(Path(a.get("out", "out")) / "blast")]
        if "dates" in a:
            argv += ["--dates", a["dates"]]
        return cohort_analyst._cli(argv)
    if job.kind == "engagement":
        from engine.agents import engagement
        argv = ["--data", a["data"], "--out", str(Path(a.get("out", "out")) / "engagement")]
        if "platform" in a:
            argv += ["--platform", a["platform"]]
        return engagement._cli(argv)
    if job.kind == "readout":
        from engine.measurement import stats
        return stats._cli(["incrementality", a["input"], "--markdown"])
    if job.kind == "size":
        from engine.measurement import stats
        if a.get("entries_per_month"):
            return stats._cli(["feasible", "--entries-per-month", str(a["entries_per_month"]), "--holdout", str(a["holdout"]),
                               "--baseline", str(a["baseline"]), "--mde-rel", str(a["mde_rel"])])
        return stats._cli(["size", "--baseline", str(a["baseline"]), "--mde-rel", str(a["mde_rel"]), "--holdout", str(a["holdout"])])
    if job.kind == "help" and job.why == "no job matched":
        print("lcm runs the engine's jobs; this does not ask for one. Ask it in a Claude conversation with the plugin installed, "
              "or see docs/QUESTION_BANK.md for what to type here.\n", file=sys.stderr)
    from engine.cli.commands import overview
    print(overview())
    return 0 if job.kind == "help" else 2


def main(argv: Optional[List[str]] = None) -> int:
    from engine.cli.commands import COMMANDS, overview
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(overview())
        return 0
    if argv[0] in ("-V", "--version"):
        from engine import __version__
        print(f"lcm (LMM Engine) {__version__}")
        return 0
    if argv[0] in COMMANDS:
        return COMMANDS[argv[0]].run(argv[1:])
    return natural(argv)


def natural(argv: List[str]) -> int:
    """lcm "a sentence": the rule-based router picks the job and fills its arguments."""
    ap = argparse.ArgumentParser(prog="lcm", description="One command for the LMM Engine: a subcommand (lcm --help lists them) or a sentence")
    ap.add_argument("request", nargs="*", help="what you want, in plain English, or a YAML job file")
    ap.add_argument("--product", help="the product or brand name customers know (default: brand.name in lmm.yaml)")
    ap.add_argument("--brief", help="a brief YAML: which flow to plan, audit or compare")
    ap.add_argument("--run", help="the run to revise (default: the newest run)")
    ap.add_argument("--data", help="a folder or URL prefix with your exports (users, orders, track_events, message_events)")
    ap.add_argument("--platform", help="the messaging platform, for engagement tiers")
    ap.add_argument("--dates", help="comma-separated send dates, for a blast comparison")
    ap.add_argument("--domain", help="the sending domain, for deliverability and priorities")
    ap.add_argument("--selectors", help="comma-separated DKIM selectors to look up")
    ap.add_argument("--signals", help="a JSON of sending signals to merge into the deliverability check")
    ap.add_argument("--envelope", help="the Return-Path domain, when it differs from the From domain")
    ap.add_argument("--input", help="a results JSON, for a readout")
    ap.add_argument("--copy-mode", dest="copy_mode", choices=["auto", "claude", "handoff", "offline"], help="who writes the copy (see lcm plan --help)")
    ap.add_argument("--value", "--value-per-conversion", dest="value", type=float,
                    help="what one more converted person is worth, in your currency, for priorities")
    ap.add_argument("--by", choices=["plan", "country", "lifecycle_stage"], help="segment the retention curve")
    ap.add_argument("--events-dir", dest="events_dir", help="the webhook receiver's folder, for tracking health")
    ap.add_argument("--out", help="where outputs are written (default: the workspace's runs/, or ./out)")
    ap.add_argument("--dry-run", action="store_true", help="show the job and its arguments without running it")
    a = ap.parse_args(argv)
    opts = {k: v for k, v in vars(a).items() if k not in ("request", "dry_run")}
    text = " ".join(a.request).strip()
    if not text:
        from engine.cli.commands import overview
        print(overview())
        return 0
    job = from_yaml(Path(text), opts) if YAML_PATH.fullmatch(text) and Path(text).exists() else route(text, opts)
    if job.kind == "help" and job.why not in ("", "no job matched"):
        print(f"error: {job.why}", file=sys.stderr)
        return 2
    if a.dry_run or job.missing:
        print(json.dumps({"job": job.kind, "why": job.why, "args": {k: v for k, v in job.args.items() if v is not None},
                          **({"needs": job.missing} if job.missing else {})}, indent=2))
        if job.missing:
            print("\nAdd what is missing and run it again, for example:\n  lcm " + shlex.quote(text) + " " +
                  " ".join(m.split(" ")[0] + " ..." for m in job.missing if m.startswith("--")), file=sys.stderr)
            return 2
        return 0
    return run(job)


if __name__ == "__main__":
    sys.exit(main())
