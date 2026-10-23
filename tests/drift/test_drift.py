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
PROSE = [p for p in [ROOT / "README.md", ROOT / "CONNECTORS.md", ROOT / "QUICKSTART.md", *sorted((ROOT / "docs").glob("*.md")),
                     *sorted((ROOT / "skills").glob("*/SKILL.md")), *sorted((ROOT / "prompts").rglob("*.md")),
                     *sorted((ROOT / "templates").rglob("*.yaml")), *sorted((ROOT / "templates").rglob("README.md"))] if p.name not in HISTORY]


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


CONTEXT_XML = sorted((ROOT / "templates").rglob("*.xml")) + sorted((ROOT / "examples").rglob("*.xml"))


@pytest.mark.parametrize("path", PROSE + CONTEXT_XML, ids=_rel)
def test_send_times_are_permitted_windows_not_quiet_hours(path):
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"quiet[ _]hours", text, re.I), "say 'permitted send window' (the engine models allowed_from and allowed_until)"


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


def test_doctor_checks_the_credential_names_the_code_reads():
    from engine.cli.commands import KEY_NAMES
    code = "\n".join(p.read_text(encoding="utf-8") for d in ("engine", "adapters") for p in (ROOT / d).rglob("*.py")
                     if p.name != "commands.py")
    missing = [name for name, _ in KEY_NAMES if name not in code]
    assert not missing, f"lcm doctor checks names no code reads: {missing}"


def test_the_question_count_in_prose_matches_the_bank():
    from engine import questions
    n = sum(len(v) for v in questions.load().values())
    for p in PROSE:
        for m in re.finditer(r"\b(\d+) questions\b", p.read_text(encoding="utf-8")):
            assert int(m.group(1)) == n, f"{_rel(p)}: says {m.group(0)}, the bank has {n}"


def test_no_file_carries_a_path_from_the_authors_machine():
    import subprocess
    try:
        files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    except Exception:  # noqa: BLE001 - outside a git checkout there is nothing committed to check
        pytest.skip("not a git checkout")
    leaks = []
    for name in files:
        p = ROOT / name
        if p.suffix not in (".py", ".md", ".json", ".yaml", ".yml", ".sql", ".txt", ".html", ".mjml", ".csv") or not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"/home/[a-z]|/Users/[A-Za-z]|C:\\\\Users\\\\", text):
            leaks.append(name)
    assert not leaks, leaks


@pytest.mark.parametrize("path", PROSE, ids=_rel)
def test_every_test_file_named_exists(path):
    named = set(re.findall(r"\btests/[\w/]+\.py", path.read_text(encoding="utf-8")))
    assert all((ROOT / n).exists() for n in named), sorted(n for n in named if not (ROOT / n).exists())


TEMPLATE_FENCES = {"liquid customerio": "customerio", "liquid customerio_legacy": "customerio_legacy", "liquid braze": "braze",
                   "django klaviyo": "klaviyo", "hubl": "hubl", "ampscript": "ampscript", "handlebars": "handlebars"}


@pytest.mark.parametrize("doc", ["docs/LIQUID_CHEATSHEET.md", "prompts/examples/email_writer_five_dialects.md", "prompts/core/syntax_rules.md"])
def test_every_template_block_in_the_docs_passes_the_linter(doc):
    """docs/LIQUID_CHEATSHEET.md says every code block on the page passes the linter: hold it to that."""
    import re
    from engine.render import template_lint
    text = (ROOT / doc).read_text(encoding="utf-8")
    blocks = re.findall(r"^```([^\n`]*)\n(.*?)^```\s*$", text, re.S | re.M)
    checked = 0
    for info, body in blocks:
        dialect = TEMPLATE_FENCES.get(info.strip())
        if dialect is None:
            continue
        errors = template_lint.lint(body, dialect)["errors"]
        assert not errors, f"{doc}: {info} block: {errors}"
        checked += 1
    assert checked or doc != "docs/LIQUID_CHEATSHEET.md"


def test_uv_lock_installs_the_versions_ci_verifies():
    """requirements.txt pins what CI tests; uv.lock is what `uv sync` installs. They name the same versions."""
    import tomllib
    lock = {p["name"].lower(): p["version"] for p in tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))["package"]}
    differ = []
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        m = re.match(r"([A-Za-z0-9_.-]+)==([\w.]+)", line.strip())
        if m and lock.get(m.group(1).lower().replace("_", "-")) != m.group(2):
            differ.append(f"{m.group(1)}: requirements {m.group(2)}, uv.lock {lock.get(m.group(1).lower().replace('_', '-'))}")
    assert not differ, "align them (uv lock --upgrade-package NAME==VERSION): " + "; ".join(differ)
