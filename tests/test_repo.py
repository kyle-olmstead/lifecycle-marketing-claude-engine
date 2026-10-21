import filecmp
import json
import re
import shutil
import subprocess
import sys

from tests.conftest import ROOT
from tools import repo_check

TREE = [
    ".github/ISSUE_TEMPLATE/flow_bug.md", ".github/ISSUE_TEMPLATE/deliverability_incident.md",
    "docs/ARCHITECTURE.md", "docs/LIQUID_CHEATSHEET.md", "docs/SQL_SCHEMA_CONVENTIONS.md", "docs/OPERATIONAL_GUIDE.md",
    "core/system_prompt.md", "core/context_loader.xml",
    "modules/01_deliverability_and_infrastructure/prompts/header_analyzer.md",
    "modules/01_deliverability_and_infrastructure/prompts/warmup_schedule_generator.md",
    "modules/01_deliverability_and_infrastructure/checklists/dmarc_alignment_audit.md",
    "modules/02_behavioral_triggers_and_flows/prompts/activation_flow_architect.md",
    "modules/02_behavioral_triggers_and_flows/prompts/winback_branching_logic.md",
    "modules/02_behavioral_triggers_and_flows/schemas/custom_event_definitions.json",
    "modules/03_copywriting_and_multichannel/prompts/dynamic_liquid_email_writer.md",
    "modules/03_copywriting_and_multichannel/prompts/sms_push_modal_orchestrator.md",
    "modules/03_copywriting_and_multichannel/templates/modular_email_components.html",
    "modules/04_data_schemas_and_sql/prompts/rfm_segmentation_builder.md",
    "modules/04_data_schemas_and_sql/prompts/ltv_cohort_query_generator.md",
    "modules/04_data_schemas_and_sql/queries/rolling_d30_retention.sql",
    "modules/05_experimentation_and_analytics/prompts/ab_power_calculator.md",
    "modules/05_experimentation_and_analytics/prompts/holdout_incrementality_analyzer.md",
    "modules/05_experimentation_and_analytics/frameworks/test_hypothesis_matrix.md",
    "LICENSE",
]


TEAMMATE_TREE = [
    ".github/workflows/daily_cohort_audit.yml",
    "agents/orchestrator.py", "agents/cohort_analyst.py", "agents/campaign_architect.py", "agents/experimentation_agent.py",
    "agents/copywriter.py", "agents/models.py", "agents/brief.py", "agents/guardrails.py", "agents/sql_dialects.py",
    "config/mcp_server_config.json", "config/esp_payload_schemas.json",
    "scripts/sheets_sync.py", "scripts/webhook_parser.py", "scripts/campaign_simulator.py", "scripts/mcp_server.py",
    "docs/quickstart_for_marketers.md", "docs/architectural_overview.md",
    "briefs/brief_template.yaml", "skills/lifecycle-teammate/SKILL.md", "tools/event_payloads.py",
    # v2.2
    "agents/pipeline.py", "agents/stages.py", "agents/strategist.py", "agents/compliance.py", "agents/engagement.py", "agents/cli.py",
    "tools/dns_collect.py", "tools/contracts.py", "scripts/daily.py", "config/compliance_rules.json", "pyproject.toml",
    "schemas/deliverability_input.schema.json", "schemas/readout_input.schema.json", "schemas/warmup_tiers.schema.json",
    "tests/fixtures/message_events.csv", "docs/AUDIT.md", "briefs/active/README.md", "docs/ROI_MEMO.md", "scripts/roi_model.py",
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
    shutil.copy(ROOT / "tests/fixtures/make_fixtures.py", tmp_path / "make_fixtures.py")
    subprocess.run([sys.executable, str(tmp_path / "make_fixtures.py")], check=True, capture_output=True)
    for name in ("users.csv", "orders.csv", "track_events.csv", "message_events.csv"):
        assert filecmp.cmp(tmp_path / name, ROOT / "tests/fixtures" / name, shallow=False), name


def test_cli_entry_points_run():
    for args in (["tools/deliverability.py", "analyze", "examples/deliverability/incident.json"],
                 ["tools/warmup.py", "--target", "1000", "--tiers", '{"engaged_30d": 5000}', "--gate", "GREEN"],
                 ["tools/stats.py", "size", "--baseline", "0.1", "--mde-abs", "0.02"],
                 ["tools/sql_check.py", "modules/04_data_schemas_and_sql/queries/rolling_d30_retention.sql", "--run"]):
        r = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True)
        assert r.returncode == 0, (args, r.stderr[-500:])


def test_versions_agree():
    from agents import __version__ as a
    from tools import __version__ as t
    plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())["version"]
    market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
    pyproject = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    versions = {a, t, plugin, pyproject, market["metadata"]["version"] if "metadata" in market else a, *[p["version"] for p in market["plugins"]]}
    assert versions == {"2.2.0"}, versions
    assert "## 2.2.0" in (ROOT / "CHANGELOG.md").read_text()


def test_new_cli_entry_points_run(tmp_path):
    for args in (["scripts/daily.py", "--no-sheet", "--out", str(tmp_path / "daily")],
                 ["-m", "agents.cli", "--dry-run", "check deliverability for example.com"],
                 ["-m", "agents.engagement", "--sql-only", "--dialect", "snowflake", "--out", str(tmp_path / "eng")],
                 ["tools/dns_collect.py", "--help"],
                 ["tools/contracts.py"]):
        r = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True)
        assert r.returncode == 0, (args, r.stderr[-500:])
