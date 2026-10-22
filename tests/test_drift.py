"""Drift: what the docs, prompts and skills say against what the code does.

A plan is only as good as the instructions around it. When the prose says one thing (an SMS window, a default holdout,
a command) and the code does another, a marketer following the prose ships the wrong thing, so the facts that matter
are checked here against their single source."""
import re

import pytest

from engine import docs_gen
from engine.cli.commands import COMMANDS
from engine.flows.library import FLOWS
from engine.models import sms_window_from_rules
from tests.conftest import ROOT

HISTORY = {"CHANGELOG.md", "AUDIT.md"}          # records of what was true then
PROSE = [p for p in [ROOT / "README.md", ROOT / "CONNECTORS.md", *sorted((ROOT / "docs").glob("*.md")),
                     *sorted((ROOT / "skills").glob("*/SKILL.md")), *sorted((ROOT / "prompts").rglob("*.md")),
                     *sorted((ROOT / "templates").rglob("*.yaml"))] if p.name not in HISTORY]


def _rel(p):
    return str(p.relative_to(ROOT))


def test_generated_tables_match_the_code():
    assert docs_gen.check() == [], "run: python3 -m engine.docs_gen --write"


def test_the_flow_library_page_carries_every_flow():
    text = (ROOT / "docs/FLOW_LIBRARY.md").read_text()
    for key in FLOWS:
        assert f"`{key}`" in text, key


@pytest.mark.parametrize("path", PROSE, ids=_rel)
def test_every_lcm_command_named_exists(path):
    text = path.read_text(encoding="utf-8")
    named = {m.group(1) for m in re.finditer(r"`lcm ([a-z][\w-]*)", text)}
    assert named <= set(COMMANDS), sorted(named - set(COMMANDS))


@pytest.mark.parametrize("path", PROSE, ids=_rel)
def test_every_engine_path_named_exists(path):
    text = path.read_text(encoding="utf-8")
    missing = []
    for m in re.finditer(r"python3 (?:-m )?((?:engine|adapters)[\w/.]*\w)", text):
        s = m.group(1)
        candidates = [ROOT / s, ROOT / (s.replace(".", "/") + ".py"), ROOT / s.replace(".", "/") / "__init__.py"]
        if not any(c.exists() for c in candidates):
            missing.append(s)
    assert not missing, missing


@pytest.mark.parametrize("path", PROSE, ids=_rel)
def test_send_times_are_permitted_windows_not_quiet_hours(path):
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"quiet hours", text, re.I), "say 'permitted send window' (the engine models allowed_from and allowed_until)"


def test_the_sms_window_is_stated_the_way_the_rules_file_has_it():
    w = sms_window_from_rules()
    assert (w.allowed_from, w.allowed_until) == ("09:00", "20:00")
    stale = []
    for p in PROSE:
        for line in p.read_text(encoding="utf-8").splitlines():
            if re.search(r"\b(sms|texts?|texting)\b", line, re.I) and re.search(r"\b8(:00)? ?a\.?m\.?\b.{0,20}\b9(:00)? ?p\.?m\.?|08:00 to 21:00", line, re.I) \
                    and not re.search(r"email|oregon|connecticut|state|tcpa", line, re.I):
                stale.append(f"{_rel(p)}: {line.strip()[:120]}")
    assert not stale, stale


def test_no_single_default_holdout_is_claimed():
    """The default holdout is per flow (engine/flows/library.py); prose that names one number for all flows is wrong."""
    pattern = re.compile(r"default holdout (?:is|of) \d+\s?%|holdout defaults? to \d+\s?%", re.I)
    stale = [f"{_rel(p)}: {m.group(0)}" for p in PROSE for m in pattern.finditer(p.read_text(encoding="utf-8"))]
    assert not stale, stale


def test_the_flow_count_in_prose_matches_the_library():
    words = {10: "ten"}
    n = len(FLOWS)
    for p in PROSE:
        for m in re.finditer(r"\b(?:plans|plan|ships|builds) (\w+) flows\b", p.read_text(encoding="utf-8")):
            word = m.group(1).lower()
            if word in ("these", "the", "all", "every", "its", "your", "live", "new", "lifecycle", "those"):
                continue
            assert word in (str(n), words.get(n)), f"{_rel(p)}: '{m.group(0)}' but the library has {n}"
