"""Tables in the docs that are generated from the code, so the prose cannot drift from what the engine does.

A generated block in any Markdown file looks like this; everything between the two markers is rewritten:

    <!-- generated: flows -->
    ...
    <!-- end generated -->

    python3 -m engine.docs_gen --check     # exit 1 and name the stale blocks (CI runs this through tests/test_drift.py)
    python3 -m engine.docs_gen --write     # rewrite every block from the code

Blocks: flows (engine/flows/library.py), platforms (engine/agents/campaign_architect.py VOCAB, the linter's unsubscribe
tokens, engine/render/product_blocks.py), windows (the permitted send windows and caps a workspace starts with,
templates/workspace/lmm.yaml and config/compliance_rules.json), commands (engine/cli/commands.py).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Callable, Dict, List

from engine.paths import COMPLIANCE_RULES, ROOT, TEMPLATES

BLOCK = re.compile(r"(<!-- generated: (\w+) -->\n)(.*?)(<!-- end generated -->)", re.S)
DOC_GLOBS = ("README.md", "CONNECTORS.md", "docs/*.md", "skills/*/SKILL.md")


def _hours(h: float) -> str:
    if h == 0:
        return "0"
    if h % 24 == 0:
        return f"day {int(h // 24)}"
    return f"{h:g}h"


def flows() -> str:
    from engine.flows.library import FLOWS
    L = ["| Flow | What it does | Trigger | Goal | Touches (default / max) | Schedule | Holdout default | Re-entry | Models |",
         "|---|---|---|---|---|---|---|---|---|"]
    for f in FLOWS.values():
        sched = ", ".join(_hours(h) for h in f.schedule_hours[:f.default_touches])
        trig, goal = f.trigger_event.get("*") or next(iter(f.trigger_event.values())), f.goal_event.get("*") or next(iter(f.goal_event.values()))
        hold = f"{f.holdout_default:g}%" + ("" if f.holdout_default else f" ({f.holdout_reason_if_none})" if f.holdout_reason_if_none else "")
        re_entry = f.re_entry[0] + (f" {f.re_entry[1]}" if f.re_entry[1] else "")
        L.append(f"| `{f.key}` {f.label} | {f.summary} | {trig} | {goal} | {f.default_touches} / {f.max_touches} | {sched} | "
                 f"{hold.replace('|', '/')} | {re_entry} | {', '.join(f.models) if f.models and f.models != ('*',) else 'all'} |")
    L += ["", "Anything else (a referral program, a birthday series, a price-drop alert, a loyalty tier change) is refused by name "
              "rather than planned as the nearest flow. Hours-level waits are honored only by the abandoned cart and browse flows."]
    return "\n".join(L)


def platforms() -> str:
    import re as _re
    from engine.agents.campaign_architect import VOCAB
    from engine.agents.copywriter import unsubscribe_html
    from engine.models import PLATFORM_LABEL
    dialect = {"klaviyo": "Klaviyo template language (Django-style)", "hubspot": "HubL", "braze": "Braze Liquid",
               "customerio": "Customer.io Liquid", "sfmc": "AMPscript", "iterable": "Handlebars"}
    lint_name = {"klaviyo": "klaviyo", "hubspot": "hubl", "braze": "braze", "customerio": "customerio", "sfmc": "ampscript", "iterable": "handlebars"}
    L = ["| Platform | Template language | Built as | Frequency limit at send time | Unsubscribe link the emails carry |",
         "|---|---|---|---|---|"]
    for key, v in VOCAB.items():
        try:
            html = unsubscribe_html(lint_name.get(key, key))
            m = _re.search(r'href="([^"]+)"', html)
            tok = m.group(1) if m else html
        except KeyError:
            tok = "n/a"
        L.append(f"| {PLATFORM_LABEL.get(key, key)} | {dialect.get(key, key)} | {v['container']} | {v['freq']} | `{tok}` |")
    return "\n".join(L)


def windows() -> str:
    import yaml
    cfg = yaml.safe_load((TEMPLATES / "workspace" / "lmm.yaml").read_text(encoding="utf-8"))
    freq = cfg["frequency"]
    rules = json.loads(COMPLIANCE_RULES.read_text(encoding="utf-8"))
    L = ["| Channel | Permitted send window (recipient local time) |", "|---|---|"]
    L += [f"| {w['channel']} | {w['allowed_from']} to {w['allowed_until']} |" for w in freq["send_windows"]]
    L += ["", "Caps across every flow: " + "; ".join(f"at most {c['max']} {'/'.join(c['channels'])} per {c['window']}" for c in freq["caps"])
          + f". Collision rule: {freq['collision_rule'].replace('_', ' ')}. Priority: {', '.join(freq['flow_priority'])}."]
    sms = _sms_window(rules)
    if sms:
        L += ["", f"The SMS window is the narrowest state window config/compliance_rules.json tracks ({sms}); a send hour outside "
                  "a channel's window is refused when the brief is read, not after the plan is written."]
    return "\n".join(L)


def _sms_window(rules: Dict) -> str:
    from engine.models import sms_window_from_rules
    w = sms_window_from_rules()
    return f"{w.allowed_from} to {w.allowed_until}, rule {w.source}"


def commands() -> str:
    from engine.cli.commands import COMMANDS, GROUPS
    L = []
    for key, title in GROUPS:
        names = [n for n, c in COMMANDS.items() if c.group == key]
        if not names:
            continue
        L += [f"**{title}**", "", "| Command | What it does |", "|---|---|"]
        L += [f"| `lcm {n}` | {COMMANDS[n].help} |" for n in names] + [""]
    return "\n".join(L).rstrip()


GENERATORS: Dict[str, Callable[[], str]] = {"flows": flows, "platforms": platforms, "windows": windows, "commands": commands}


def files() -> List[Path]:
    out: List[Path] = []
    for g in DOC_GLOBS:
        out += sorted(ROOT.glob(g))
    return out


def render(text: str, cache: Dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        name = m.group(2)
        if name not in GENERATORS:
            raise KeyError(f"unknown generated block {name!r}; known: {', '.join(GENERATORS)}")
        if name not in cache:
            cache[name] = GENERATORS[name]().rstrip() + "\n"
        return m.group(1) + cache[name] + m.group(4)
    return BLOCK.sub(sub, text)


def check() -> List[str]:
    """Files whose generated blocks differ from what the code produces now."""
    stale, cache = [], {}
    for p in files():
        t = p.read_text(encoding="utf-8")
        if "<!-- generated:" in t and render(t, cache) != t:
            stale.append(str(p.relative_to(ROOT)))
    return stale


def write() -> List[str]:
    changed, cache = [], {}
    for p in files():
        t = p.read_text(encoding="utf-8")
        if "<!-- generated:" not in t:
            continue
        new = render(t, cache)
        if new != t:
            p.write_text(new, encoding="utf-8")
            changed.append(str(p.relative_to(ROOT)))
    return changed


def _cli(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m engine.docs_gen", description="Regenerate or check the doc tables built from code")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="exit 1 and list the files whose generated tables are stale")
    g.add_argument("--write", action="store_true", help="rewrite every generated table from the code")
    a = ap.parse_args(argv)
    if a.check:
        stale = check()
        print("stale: " + ", ".join(stale) if stale else "every generated table matches the code")
        return 1 if stale else 0
    changed = write()
    print("rewrote: " + ", ".join(changed) if changed else "nothing to rewrite")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
