"""lcm: one command for the whole engine, in plain English or with a YAML job file.

    lcm "plan a 5-touch win-back for users who completed onboarding but haven't created a report in 14 days" --product Acme
    lcm "check deliverability for example.com"
    lcm "audit the cohort for briefs/active/winback.yaml" --data exports/
    lcm "compare our monthly newsletter with the win-back trigger" --brief briefs/active/winback.yaml --data exports/
    lcm "build the engagement tiers and the sunset list" --data exports/ --platform klaviyo
    lcm "read the holdout results in results/winback_q3.json"
    lcm "how many people do we need to detect a 20% lift on a 10% baseline with a 15% holdout"
    lcm job.yaml                      # kind: plan | deliverability | audit | blast | engagement | readout | size
    lcm --dry-run "..."               # say which job it would run, and with what, without running it

(Without installing: python3 -m agents.cli "...". pip install -e . puts lcm on your PATH.)

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

ROOT = Path(__file__).resolve().parents[1]
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
# what each job needs, and how to ask for it
NEEDS: Dict[str, List[Tuple[str, str]]] = {
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
    if kind == "plan":
        job.args = {"text": text, "product": a.get("product"), "data": a.get("data"), "copy_mode": a.get("copy_mode", "auto"), "out": a.get("out", "out")}
        if brief:
            job.args = {"brief": brief, **{k: v for k, v in job.args.items() if k != "text"}}
        elif not a.get("product"):
            job.missing.append("--product (the product name customers know)")
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
            active = sorted(glob.glob(str(ROOT / "briefs" / "active" / "*.yaml")))
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
    if kind not in {k for k, _ in INTENTS}:
        return Job("help", why=f"{path}: kind must be one of {', '.join(k for k, _ in INTENTS)} (got {kind!r})")
    return check(Job(kind, {**given, **raw}, why=f"kind: {kind}"))


def run(job: Job) -> int:
    a = {k: v for k, v in job.args.items() if v not in (None, "", [])}
    if job.kind == "plan":
        from agents import orchestrator
        argv = ["plan"] + (["--brief", a["brief"]] if "brief" in a else ["--text", a["text"]])
        for k in ("product", "data", "copy_mode", "out"):
            if k in a and not ("brief" in a and k == "product"):
                argv += [f"--{k.replace('_', '-')}", str(a[k])]
        return orchestrator._cli(argv)
    if job.kind == "daily":
        from scripts import daily
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
        from agents import tracking
        return tracking._cli(["--data", a["data"]] + (["--events-dir", a["events_dir"]] if a.get("events_dir") else []))
    if job.kind == "retention":
        from agents import retention
        return retention._cli(["--data", a["data"]] + (["--by", a["by"]] if a.get("by") else []) + (["--brief", a["brief"]] if a.get("brief") else []))
    if job.kind == "roi":
        from scripts import roi_model
        return roi_model._cli([])
    if job.kind == "priorities":
        from agents import priorities
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
        from tools import dns_collect
        argv = [a["domain"], "--analyze", "--markdown"]
        for k in ("selectors", "signals", "envelope"):
            if k in a:
                argv += [f"--{k}", str(a[k])]
        return dns_collect._cli(argv)
    if job.kind == "audit":
        from agents import cohort_analyst
        return cohort_analyst._cli(["audit", "--brief", a["brief"], "--data", a["data"], "--out", str(Path(a.get("out", "out")) / "audit")])
    if job.kind == "blast":
        from agents import cohort_analyst
        argv = ["blast-vs-trigger", "--brief", a["brief"], "--data", a["data"], "--out", str(Path(a.get("out", "out")) / "blast")]
        if "dates" in a:
            argv += ["--dates", a["dates"]]
        return cohort_analyst._cli(argv)
    if job.kind == "engagement":
        from agents import engagement
        argv = ["--data", a["data"], "--out", str(Path(a.get("out", "out")) / "engagement")]
        if "platform" in a:
            argv += ["--platform", a["platform"]]
        return engagement._cli(argv)
    if job.kind == "readout":
        from tools import stats
        return stats._cli(["incrementality", a["input"], "--markdown"])
    if job.kind == "size":
        from tools import stats
        if a.get("entries_per_month"):
            return stats._cli(["feasible", "--entries-per-month", str(a["entries_per_month"]), "--holdout", str(a["holdout"]),
                               "--baseline", str(a["baseline"]), "--mde-rel", str(a["mde_rel"])])
        return stats._cli(["size", "--baseline", str(a["baseline"]), "--mde-rel", str(a["mde_rel"]), "--holdout", str(a["holdout"])])
    if job.kind == "help" and job.why == "no job matched":
        print("lcm runs the engine's jobs; this does not ask for one. Ask it in a Claude conversation with the plugin installed, "
              "or see docs/QUESTION_BANK.md for what to type here.\n", file=sys.stderr)
    print(__doc__)
    return 0 if job.kind == "help" else 2


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="lcm", description="One command for the LMM Engine", add_help=True)
    ap.add_argument("request", nargs="*", help="what you want, in plain English, or a YAML job file")
    ap.add_argument("--product")
    ap.add_argument("--brief")
    ap.add_argument("--data")
    ap.add_argument("--platform")
    ap.add_argument("--dates")
    ap.add_argument("--domain")
    ap.add_argument("--selectors")
    ap.add_argument("--signals")
    ap.add_argument("--envelope")
    ap.add_argument("--input")
    ap.add_argument("--copy-mode", dest="copy_mode")
    ap.add_argument("--value", type=float, help="what one more converted person is worth, for priorities")
    ap.add_argument("--by", choices=["plan", "country", "lifecycle_stage"], help="segment the retention curve")
    ap.add_argument("--events-dir", dest="events_dir", help="the webhook receiver's folder, for tracking health")
    ap.add_argument("--out")
    ap.add_argument("--dry-run", action="store_true", help="show the job and its arguments without running it")
    a = ap.parse_args(argv)
    opts = {k: v for k, v in vars(a).items() if k not in ("request", "dry_run")}
    text = " ".join(a.request).strip()
    if not text:
        print(__doc__)
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
