import filecmp
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
    for name in ("users.csv", "orders.csv", "track_events.csv"):
        assert filecmp.cmp(tmp_path / name, ROOT / "tests/fixtures" / name, shallow=False), name


def test_cli_entry_points_run():
    for args in (["tools/deliverability.py", "analyze", "examples/deliverability/incident.json"],
                 ["tools/warmup.py", "--target", "1000", "--tiers", '{"engaged_30d": 5000}', "--gate", "GREEN"],
                 ["tools/stats.py", "size", "--baseline", "0.1", "--mde-abs", "0.02"],
                 ["tools/sql_check.py", "modules/04_data_schemas_and_sql/queries/rolling_d30_retention.sql", "--run"]):
        r = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True)
        assert r.returncode == 0, (args, r.stderr[-500:])
