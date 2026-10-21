"""Where things live in the repository, in one place, so no module computes a path of its own."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
SCHEMAS = ROOT / "schemas"
PROMPTS = ROOT / "prompts"
TEMPLATES = ROOT / "templates"
EXAMPLES = ROOT / "examples"
WORKFLOWS = ROOT / "workflows"

TRACKING_PLAN = CONFIG / "tracking_plan.json"
COMPLIANCE_RULES = CONFIG / "compliance_rules.json"
ESP_PAYLOAD_SCHEMAS = CONFIG / "esp_payload_schemas.json"
QUESTION_BANK = CONFIG / "question_bank.yaml"
BRIEF_TEMPLATE = TEMPLATES / "brief.yaml"

# The bundled demo workspace: a fictional B2B SaaS company with its data, briefs and finished runs.
DEMO = EXAMPLES / "brands" / "ridgeview_saas"
DEMO_DATA = DEMO / "data"
DEMO_BRIEF = DEMO / "briefs" / "winback_14d.yaml"
DEMO_ACTIVE_BRIEFS = DEMO / "briefs" / "active"
