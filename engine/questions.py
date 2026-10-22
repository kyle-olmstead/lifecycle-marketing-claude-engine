"""Write docs/QUESTION_BANK.md from config/question_bank.yaml, and check that every question reaches its job.

    python3 engine/questions.py            # check: every routed question classifies to its job
    python3 engine/questions.py --write    # regenerate the Markdown
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BANK = ROOT / "config" / "question_bank.yaml"
DOC = ROOT / "docs" / "QUESTION_BANK.md"
CADENCES = ("daily", "weekly", "monthly", "quarterly", "annual")
SKILLS = {"lifecycle-sql", "lifecycle-measurement", "lifecycle-flows", "deliverability-gate", "lifecycle-teammate", "lifecycle-copy",
          "lifecycle-setup"}
FIELDS = ("id", "ask", "expert", "route", "command", "you_get", "needs", "limits")


def load() -> Dict[str, List[Dict[str, Any]]]:
    import yaml
    bank = yaml.safe_load(BANK.read_text(encoding="utf-8"))
    if list(bank) != list(CADENCES):
        raise ValueError(f"cadences must be {CADENCES} in that order")
    return bank


def check(bank: Dict[str, List[Dict[str, Any]]]) -> List[str]:
    from engine.cli.main import INTENTS, classify
    jobs = {k for k, _ in INTENTS}
    problems, ids = [], set()
    for cadence, items in bank.items():
        for q in items:
            missing = [f for f in FIELDS if not q.get(f)]
            if missing:
                problems.append(f"{q.get('id', '?')}: missing {missing}")
                continue
            if q["id"] in ids:
                problems.append(f"{q['id']}: duplicate id")
            ids.add(q["id"])
            route = q["route"]
            if "job" in route:
                if route["job"] not in jobs:
                    problems.append(f"{q['id']}: no lcm job {route['job']!r}")
                elif classify(q["ask"]) != route["job"]:
                    problems.append(f"{q['id']}: {q['ask']!r} routes to {classify(q['ask'])!r}, not {route['job']!r}")
                if not q["command"].startswith("lcm "):
                    problems.append(f"{q['id']}: a job's command starts with lcm")
            elif route.get("skill") not in SKILLS:
                problems.append(f"{q['id']}: unknown skill {route.get('skill')!r}")
    return problems


def render(bank: Dict[str, List[Dict[str, Any]]]) -> str:
    n = sum(len(v) for v in bank.values())
    L = ["# The question bank", "",
         "> **In plain English.** These are the questions a lifecycle marketing manager asks every day, week, month, quarter",
         "> and year, and what the engine does with each. Ask any of them in a Claude conversation with the plugin installed.",
         "> The ones with an `lcm` command also run in a terminal: type the command shown. Each one lists what you get, what",
         "> data it needs, and what it cannot answer, so a partial answer is never mistaken for a whole one. The single most",
         "> useful one is the first: *what are my top 3 priority builds today and why?* The morning check answers it every day",
         "> without being asked.", "",
         f"{n} questions. The plain version is how you would say it; the expert version is the same question at full depth.",
         "In Claude either works. After `lcm`, type the plain version: the terminal router is rules, not a model, and CI checks",
         "that every plain question with an `lcm` command reaches the job named here. Questions marked *the ... skill in Claude*",
         "have no terminal job; `lcm` says so instead of guessing. This page is written from `config/question_bank.yaml`",
         "(`python3 engine/questions.py --write`).", ""]
    L += ["| Cadence | Questions |", "|---|---|"]
    L += [f"| [{c.capitalize()}](#{c}) | " + "; ".join(q["ask"] for q in bank[c]) + " |" for c in CADENCES]
    for c in CADENCES:
        L += ["", f"## {c.capitalize()}", ""]
        for q in bank[c]:
            route = q["route"]
            how = f"`lcm` job `{route['job']}`" if "job" in route else f"the `{route['skill']}` skill in Claude"
            L += [f"### {q['ask']}", "",
                  f"*Expert version:* {q['expert']}", "",
                  f"- **How:** {how}: `{q['command']}`" if q["command"].startswith("lcm ") else f"- **How:** {how}: {q['command']}",
                  f"- **You get:** {q['you_get']}",
                  f"- **Needs:** {q['needs']}",
                  f"- **Cannot answer:** {q['limits']}", ""]
    return "\n".join(L).rstrip() + "\n"


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="lcm questions", description="Check the question bank and write docs/QUESTION_BANK.md")
    ap.add_argument("--write", action="store_true", help="also rewrite docs/QUESTION_BANK.md from the bank (default: check only)")
    a = ap.parse_args(argv)
    bank = load()
    problems = check(bank)
    for p in problems:
        print(f"error: {p}", file=sys.stderr)
    if problems:
        return 1
    if a.write:
        DOC.write_text(render(bank), encoding="utf-8")
        print(f"wrote {DOC.relative_to(ROOT)}")
    else:
        print(f"{sum(len(v) for v in bank.values())} questions; every routed one reaches its job")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
