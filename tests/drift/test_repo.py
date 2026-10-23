import filecmp
import json
import re
import shutil
import subprocess
import sys

from tests.conftest import ROOT
from engine import repo_check

TREE = [
    ".github/ISSUE_TEMPLATE/flow_bug.md", ".github/ISSUE_TEMPLATE/deliverability_incident.md",
    "docs/ARCHITECTURE.md", "docs/LIQUID_CHEATSHEET.md", "docs/SQL_SCHEMA_CONVENTIONS.md", "docs/OPERATIONAL_GUIDE.md",
    "prompts/core/system_prompt.md", "templates/context/context_loader.xml",
    "prompts/jobs/deliverability/header_analyzer.md",
    "prompts/jobs/deliverability/warmup_schedule_generator.md",
    "prompts/jobs/deliverability/dmarc_alignment_audit.md",
    "prompts/jobs/flows/activation.md",
    "prompts/jobs/flows/winback.md",
    "config/tracking_plan.json",
    "prompts/jobs/copy/email_writer.md",
    "prompts/jobs/copy/sms_push_in_app.md",
    "templates/email/components.html",
    "prompts/jobs/sql/rfm_segmentation.md",
    "prompts/jobs/sql/ltv_cohort.md",
    "prompts/jobs/sql/rolling_d30_retention.sql",
    "prompts/jobs/measurement/ab_power_calculator.md",
    "prompts/jobs/measurement/holdout_incrementality.md",
    "prompts/jobs/measurement/test_hypothesis_matrix.md",
    "LICENSE",
]


TEAMMATE_TREE = [
    ".github/workflows/daily_cohort_audit.yml",
    "engine/pipeline/orchestrator.py", "engine/agents/cohort_analyst.py", "engine/agents/campaign_architect.py", "engine/agents/experimentation_agent.py",
    "engine/agents/copywriter.py", "engine/models.py", "engine/brief.py", "engine/policy/guardrails.py", "engine/sql/dialects.py",
    "config/mcp_server_config.json", "config/esp_payload_schemas.json",
    "adapters/destinations/sheets.py", "adapters/sources/webhooks.py", "engine/policy/simulator.py", "engine/mcp_server.py",
    "docs/quickstart_for_marketers.md", "docs/architectural_overview.md",
    "templates/brief.yaml", "skills/lifecycle-teammate/SKILL.md", "adapters/destinations/event_payloads.py",
    # v2.2
    "engine/pipeline/core.py", "engine/pipeline/stages.py", "engine/agents/strategist.py", "engine/policy/compliance.py", "engine/agents/engagement.py", "engine/cli/main.py",
    "engine/deliverability/dns_collect.py", "engine/contracts.py", "engine/workflows/daily.py", "config/compliance_rules.json", "pyproject.toml",
    "schemas/deliverability_input.schema.json", "schemas/readout_input.schema.json", "schemas/warmup_tiers.schema.json",
    "examples/brands/ridgeview_saas/data/message_events.parquet", "docs/AUDIT.md", "examples/brands/ridgeview_saas/briefs/active/README.md", "docs/ROI_MEMO.md", "engine/measurement/roi.py",
    "engine/agents/priorities.py", "engine/agents/tracking.py", "engine/agents/retention.py", "config/question_bank.yaml", "docs/QUESTION_BANK.md",
    "engine/questions.py",
]


def test_the_teammate_tree_exists():
    missing = [p for p in TEAMMATE_TREE if not (ROOT / p).is_file()]
    assert not missing, missing


def test_the_specified_tree_exists():
    missing = [p for p in TREE if not (ROOT / p).is_file()]
    assert not missing, missing


def test_module_folders_contain_exactly_the_specified_files():
    specified = {p for p in TREE if p.startswith("modules/")}
    actual = {p.relative_to(ROOT).as_posix() for p in (ROOT / "modules").rglob("*") if p.is_file()}
    assert actual == specified, actual ^ specified


def test_repository_examples_pass_their_own_rules():
    errors, counts = repo_check.check()
    assert not errors, errors
    assert counts["sql_executed"] >= 8 and counts["templates"] >= 20


def test_fixtures_are_deterministic(tmp_path):
    """Both Ridgeview worlds regenerate exactly: the small one byte for byte, the demo's Parquet tables row for row (the
    file bytes carry the writer's version)."""
    small = ROOT / "tests/fixtures/ridgeview_small/data"
    shutil.copy(small / "make_data.py", tmp_path / "make_fixtures.py")
    subprocess.run([sys.executable, str(tmp_path / "make_fixtures.py")], check=True, capture_output=True)
    for name in ("users.csv", "orders.csv", "track_events.csv", "message_events.csv"):
        assert filecmp.cmp(tmp_path / name, small / name, shallow=False), name
    demo, out = ROOT / "examples/brands/ridgeview_saas/data", tmp_path / "demo"
    out.mkdir()
    subprocess.run([sys.executable, str(demo / "make_data.py"), str(out)], check=True, capture_output=True)
    for name in ("users.csv", "orders.csv"):
        assert filecmp.cmp(out / name, demo / name, shallow=False), name
    import duckdb
    con = duckdb.connect()
    for name in ("track_events.parquet", "message_events.parquet"):
        diff = con.execute(f"SELECT COUNT(*) FROM ((SELECT * FROM '{out / name}' EXCEPT ALL SELECT * FROM '{demo / name}') "
                           f"UNION ALL (SELECT * FROM '{demo / name}' EXCEPT ALL SELECT * FROM '{out / name}'))").fetchone()[0]
        assert diff == 0, name


def test_cli_entry_points_run():
    for args in (["engine/deliverability/analyze.py", "analyze", "examples/deliverability/incident.json"],
                 ["engine/deliverability/warmup.py", "--target", "1000", "--tiers", '{"engaged_30d": 5000}', "--gate", "GREEN"],
                 ["engine/measurement/stats.py", "size", "--baseline", "0.1", "--mde-abs", "0.02"],
                 ["engine/sql/check.py", "prompts/jobs/sql/rolling_d30_retention.sql", "--run"]):
        r = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True)
        assert r.returncode == 0, (args, r.stderr[-500:])


def test_versions_agree():
    from engine import __version__ as a
    from engine import __version__ as t
    plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())["version"]
    market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
    pyproject = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    versions = {a, t, plugin, pyproject, market["metadata"]["version"] if "metadata" in market else a, *[p["version"] for p in market["plugins"]]}
    assert versions == {"3.0.0"}, versions
    assert "## 3.0.0" in (ROOT / "CHANGELOG.md").read_text()


def test_new_cli_entry_points_run(tmp_path):
    for args in (["engine/workflows/daily.py", "--no-sheet", "--out", str(tmp_path / "daily")],
                 ["-m", "engine.cli.main", "--dry-run", "check deliverability for example.com"],
                 ["-m", "engine.agents.engagement", "--sql-only", "--dialect", "snowflake", "--out", str(tmp_path / "eng")],
                 ["engine/deliverability/dns_collect.py", "--help"],
                 ["engine/contracts.py"]):
        r = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True)
        assert r.returncode == 0, (args, r.stderr[-500:])
