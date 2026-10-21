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
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DOMAIN = re.compile(r"\b((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})\b", re.I)
YAML_PATH = re.compile(r"[\w./-]+\.ya?ml\b")
JSON_PATH = re.compile(r"[\w./-]+\.json\b")
PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
INTENTS = (
    ("deliverability", r"deliverab|inbox|spam folder|\bspf\b|\bdkim\b|\bdmarc\b|mta-sts|authenticat|sending gate|bounce rate|blocklist|blacklist"),
    ("engagement", r"engagement tier|unengaged|sunset|suppress|re-?permission list|inactive subscribers"),
    ("blast", r"blast|newsletter|calendar|batch(?:-and-| and )blast|one-off send|scheduled send"),
    ("audit", r"\baudit\b|health check|cohort health|anomal"),
    ("readout", r"read ?out|results?\b|incremental|attributed|did (it|the flow) work|holdout result|lift we got"),
    ("size", r"sample size|how many (people|users|entrants)|power|minimum detectable|\bmde\b|how long .*holdout"),
    ("plan", r"\bplan\b|flow|campaign|journey|sequence|series|touch|win-?back|onboarding|welcome|activation|nurture|re-?engage"),
)
NOT_DOMAINS = {"e.g", "i.e"}


@dataclass
class Job:
    kind: str
    args: Dict[str, Any] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)
    why: str = ""


def classify(text: str) -> Optional[str]:
    low = text.lower()
    for kind, pattern in INTENTS:
        if re.search(pattern, low):
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
        if not job.args["domain"]:
            job.missing.append("the sending domain (for example: check deliverability for example.com)")
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
        if not job.args["data"]:
            job.missing.append("--data (a folder or URL prefix with users, orders, track_events exports)")
    elif kind == "engagement":
        plat = next((p for p in ("klaviyo", "hubspot", "braze", "customerio", "customer.io") if p in low), None)
        job.args = {"data": a.get("data"), "platform": a.get("platform") or (plat.replace(".", "") if plat else None), "out": a.get("out", "out")}
        if not job.args["data"]:
            job.missing.append("--data (a folder with users and message_events exports)")
    elif kind == "readout":
        path = a.get("input") or next(iter(JSON_PATH.findall(text)), None)
        job.args = {"input": path}
        if not path:
            job.missing.append("the results file (for example: read the holdout results in results/winback.json)")
    elif kind == "size":
        pcts = [float(x) / 100 for x in PCT.findall(text)]
        lift = next((p for p, m in zip(pcts, PCT.finditer(text)) if re.search(r"lift|increase|improve", text[m.end():m.end() + 25], re.I)), None)
        hold = next((p for p, m in zip(pcts, PCT.finditer(text)) if re.search(r"holdout", text[m.end():m.end() + 15], re.I)), None)
        base = next((p for p in pcts if p not in (lift, hold)), None)
        entrants = re.search(r"(\d[\d,]*)\s*(entrants|people|users|signups)\s*(a|per)\s*month", text, re.I)
        job.args = {"baseline": base, "mde_rel": lift, "holdout": hold or 0.15,
                    "entries_per_month": int(entrants.group(1).replace(",", "")) if entrants else None}
        if base is None:
            job.missing.append("the baseline rate (for example: on a 10% baseline)")
        if lift is None:
            job.missing.append("the lift worth detecting (for example: a 20% lift)")
    return job


def from_yaml(path: Path, opts: Dict[str, Any]) -> Job:
    import yaml
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    kind = raw.pop("kind", None)
    if kind is None and "product_name" in raw:   # a campaign brief is a plan job
        return Job("plan", {"brief": str(path), "data": opts.get("data"), "copy_mode": opts.get("copy_mode", "auto"), "out": opts.get("out", "out")},
                   why="a campaign brief")
    if kind == "plan" and "brief" not in raw:
        return Job("help", why=f"{path}: a plan job names its brief (brief: briefs/my_flow.yaml)")
    job = Job(kind or "help", {**opts, **raw}, why=f"kind: {kind}")
    if kind == "plan":
        job.args.setdefault("out", "out")
    return job


def run(job: Job) -> int:
    a = {k: v for k, v in job.args.items() if v not in (None, "", [])}
    if job.kind == "plan":
        from agents import orchestrator
        argv = ["plan"] + (["--brief", a["brief"]] if "brief" in a else ["--text", a["text"]])
        for k in ("product", "data", "copy_mode", "out"):
            if k in a and not ("brief" in a and k == "product"):
                argv += [f"--{k.replace('_', '-')}", str(a[k])]
        return orchestrator._cli(argv)
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
    ap.add_argument("--out")
    ap.add_argument("--dry-run", action="store_true", help="show the job and its arguments without running it")
    a = ap.parse_args(argv)
    opts = {k: v for k, v in vars(a).items() if k not in ("request", "dry_run")}
    text = " ".join(a.request).strip()
    if not text:
        print(__doc__)
        return 0
    job = from_yaml(Path(text), opts) if YAML_PATH.fullmatch(text) and Path(text).exists() else route(text, opts)
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
