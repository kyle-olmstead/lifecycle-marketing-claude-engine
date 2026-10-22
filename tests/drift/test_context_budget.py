"""The context diet: what a typical skill invocation loads, against the v2.3 baseline in tests/fixtures/context_baseline.json
(computed with `python3 -m engine.context_budget --rev 7f3fe69 --json`, kept as a fixture so no git history is needed)."""
import re
import subprocess

import pytest

from engine import context_budget as cb
from engine.flows.library import FLOWS
from tests.conftest import ROOT

CORE = ROOT / "prompts" / "core"
NOW = cb.budget()
BASE = cb.load_baseline()


def test_the_baseline_fixture_covers_every_skill():
    assert BASE and BASE["rev"] == "7f3fe69"
    assert set(NOW["skills"]) == set(BASE["skills"]), set(NOW["skills"]) ^ set(BASE["skills"])


@pytest.mark.parametrize("skill", sorted(NOW["skills"]))
def test_each_skill_loads_at_least_30_percent_less_than_the_baseline(skill):
    now, base = NOW["skills"][skill]["total_tokens"], BASE["skills"][skill]["total_tokens"]
    assert now <= 0.70 * base, f"{skill}: {now} tokens against {base} at the baseline ({now / base - 1:+.0%})"


def test_all_skills_together_load_at_least_30_percent_less():
    assert NOW["total_tokens"] <= 0.70 * BASE["total_tokens"], (NOW["total_tokens"], BASE["total_tokens"])


@pytest.mark.parametrize("skill", sorted(NOW["skills"]))
def test_every_path_a_skill_references_exists(skill):
    assert not NOW["skills"][skill]["missing"], NOW["skills"][skill]["missing"]


@pytest.mark.parametrize("skill", sorted(NOW["skills"]))
def test_every_skill_reads_the_principles_and_marks_what_it_reads(skill):
    r = NOW["skills"][skill]
    assert r["mode"] == "marker", "use the 'Always read' and 'Route' markers (engine/context_budget.py)"
    assert "prompts/core/principles.md" in [a["path"] for a in r["always_read"]]


def test_principles_are_under_6_kb():
    size = len((CORE / "principles.md").read_text(encoding="utf-8").encode("utf-8"))
    assert size < 6 * 1024, size


def test_the_system_prompt_is_a_short_index_of_the_three_parts():
    text = (CORE / "system_prompt.md").read_text(encoding="utf-8")
    assert len(text.splitlines()) <= 12
    for part in ("principles.md", "syntax_rules.md", "diagnostics/"):
        assert part in text, part


def test_no_rule_is_stated_twice_across_the_core_files():
    files = [CORE / "principles.md", CORE / "syntax_rules.md", *sorted((CORE / "diagnostics").glob("*.md"))]
    assert len(files) >= 3
    seen = {}
    dupes = []
    for f in files:
        for line in {ln.strip() for ln in f.read_text(encoding="utf-8").splitlines() if len(ln.strip()) > 60}:
            if line in seen and seen[line] != f.name:
                dupes.append(f"{seen[line]} and {f.name}: {line[:80]}")
            seen.setdefault(line, f.name)
    assert not dupes, dupes


def test_the_fallback_rule_is_stated_once():
    assert "## The fallback rule" in (CORE / "syntax_rules.md").read_text(encoding="utf-8")
    restated = re.compile(r"every (personalization )?token (has|carries|with) (a|an explicit) fallback|fallbacks on every token", re.I)
    hits = [p.relative_to(ROOT).as_posix() for p in [*(ROOT / "prompts").rglob("*.md"), *(ROOT / "skills").glob("*/SKILL.md")]
            if restated.search(p.read_text(encoding="utf-8"))]
    assert not hits, hits


def test_one_flow_prompt_per_library_flow_each_within_1500_tokens_and_routed():
    flows_dir = ROOT / "prompts" / "jobs" / "flows"
    prompts = {p.stem for p in flows_dir.glob("*.md")}
    assert prompts == set(FLOWS), prompts ^ set(FLOWS)
    for key in FLOWS:
        text = (flows_dir / f"{key}.md").read_text(encoding="utf-8")
        assert cb.tokens(len(text)) <= 1500, (key, cb.tokens(len(text)))
        assert "lcm plan" in text, key
    routed = {j["path"] for j in NOW["skills"]["lifecycle-flows"]["routed"]}
    assert routed == {f"prompts/jobs/flows/{k}.md" for k in FLOWS}, routed


def test_worked_examples_live_in_prompts_examples():
    for p in (ROOT / "prompts" / "jobs").rglob("*.md"):
        assert "<worked_example>" not in p.read_text(encoding="utf-8"), p.relative_to(ROOT).as_posix()
    assert len(list((ROOT / "prompts" / "examples").glob("*.md"))) >= 6


@pytest.mark.parametrize("job", ["rfm_segmentation", "ltv_cohort"])
def test_reference_sql_is_one_file_per_dialect_with_a_dialect_header(job):
    from engine.sql import check as sql_check
    folder = ROOT / "prompts" / "jobs" / "sql" / job
    for d in cb.DIALECTS:
        sql = (folder / f"{job}_{d}.sql").read_text(encoding="utf-8")
        assert sql.startswith("-- dialect:") and sql_check.declared_dialect(sql) == d, d
        assert sql_check.parse(sql, d)[0], d
    md = (ROOT / "prompts" / "jobs" / "sql" / f"{job}.md").read_text(encoding="utf-8")
    assert "```sql" not in md.split("<reference_implementation>")[1].split("</reference_implementation>")[0]


def test_the_classifier_reads_both_conventions():
    legacy = ("# S\n\n## Step 0: Load\n\nRead `${CLAUDE_PLUGIN_ROOT}/core/a.md` and the context.\n\n"
              "## Step 1: Pick the module\n\n- X: `${CLAUDE_PLUGIN_ROOT}/b.md`\n- Y: `${CLAUDE_PLUGIN_ROOT}/c.md`\n\n"
              "## Step 2\n\nRead `${CLAUDE_PLUGIN_ROOT}/d.json` later.\n")
    c = cb.classify(legacy)
    assert (c["mode"], c["always"], c["routed"], c["on_demand"]) == ("legacy", ["core/a.md"], ["b.md", "c.md"], ["d.json"])
    marker = ("Always read: `${CLAUDE_PLUGIN_ROOT}/p.md`.\n\nRoute (one):\n- `${CLAUDE_PLUGIN_ROOT}/j1.md`\n"
              "- `${CLAUDE_PLUGIN_ROOT}/j2.md`\n\nRead `${CLAUDE_PLUGIN_ROOT}/s.md` only when needed; run "
              "`python3 ${CLAUDE_PLUGIN_ROOT}/engine/x.py`.\n")
    c = cb.classify(marker)
    assert (c["mode"], c["always"], c["routed"], c["on_demand"]) == ("marker", ["p.md"], ["j1.md", "j2.md"], ["s.md"])


def test_the_baseline_fixture_matches_git_when_history_is_there():
    if subprocess.run(["git", "cat-file", "-e", "7f3fe69"], cwd=ROOT, capture_output=True).returncode != 0:
        pytest.skip("no git history here; the fixture stands in for it")
    again = cb.budget("7f3fe69")
    assert {k: v["total_tokens"] for k, v in again["skills"].items()} == {k: v["total_tokens"] for k, v in BASE["skills"].items()}
