"""The teammate as a local MCP server, for Claude Desktop or Claude Code without the plugin.

Add it to claude_desktop_config.json (the block is in config/mcp_server_config.json) and ask Claude
in plain English: "Plan a 5-touch win-back for Acme users who finished onboarding but have not
created a report in 14 days." Claude calls these tools; they run locally, read only the folders
you point them at, write plans under LCM_OUT_DIR (default ./out), and never send anything.

Tools
  draft_brief      a sentence to an editable YAML brief, with every assumption listed
  plan_campaign    the full plan from a sentence or a brief file (optionally verified on data)
  audit_cohort     the cohort health check on a folder of exports
  simulate_flow    dry-run any flow spec JSON

Requires the Python packages in requirements.txt (mcp 2.x).

    python3 scripts/mcp_server.py            # stdio, which is what Claude Desktop starts
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from agents import cohort_analyst, orchestrator  # noqa: E402
from agents.brief import load_brief, parse_text, to_yaml  # noqa: E402
from scripts import campaign_simulator  # noqa: E402

OUT = Path(os.environ.get("LCM_OUT_DIR", "out")).resolve()

server = MCPServer(
    name="lcm-teammate", version="2.1.0",
    instructions=("Lifecycle marketing teammate from the LMM Engine. Use draft_brief when the request is vague and the marketer should confirm "
                  "assumptions; plan_campaign to produce a build-ready plan; audit_cohort for a cohort health check; simulate_flow to "
                  "dry-run a flow spec. Always show the plan's status and its 'Confirm before you build' list to the user. Nothing is sent "
                  "to any platform by these tools."),
)


@server.tool()
def draft_brief(text: str, product_name: Optional[str] = None) -> str:
    """Turn a one-sentence campaign request into an editable YAML brief. Unstated values are listed as assumptions to confirm."""
    return to_yaml(parse_text(text, product_name))


@server.tool()
def plan_campaign(text: Optional[str] = None, brief_path: Optional[str] = None, product_name: Optional[str] = None,
                  data_dir: Optional[str] = None, copy_mode: str = "auto") -> str:
    """Build the full execution plan (cohort, logic tree, copy, holdout, QA) from a sentence or a brief YAML path.
    data_dir: folder with users, orders and track_events exports, to verify events and estimate volume.
    copy_mode: auto, claude, offline or handoff. Returns the plan in Markdown and where the files were written."""
    if not text and not brief_path:
        return "Give either text (the request in a sentence) or brief_path (a filled brief YAML)."
    try:
        brief = load_brief(Path(brief_path)) if brief_path else parse_text(text or "", product_name)
        plan = orchestrator.run(brief, data_dir, copy_mode=copy_mode)
    except (ValueError, FileNotFoundError) as e:
        return f"Could not plan: {e}"
    out = orchestrator.write_outputs(plan, OUT)
    return f"Files written to {out}\n\n" + orchestrator.render_plan(plan)


@server.tool()
def audit_cohort(brief_path: str, data_dir: str) -> str:
    """Run the daily cohort health check for a brief on a folder of exports; returns the audit in Markdown."""
    try:
        r = cohort_analyst.audit(load_brief(Path(brief_path)), data_dir)
    except (ValueError, FileNotFoundError) as e:
        return f"Could not audit: {e}"
    return cohort_analyst.render_audit(r)


@server.tool()
def simulate_flow(flow_spec_path: str) -> str:
    """Dry-run a flow spec JSON (structure, synthetic journeys if simulation.json sits next to it)."""
    p = Path(flow_spec_path)
    spec = json.loads(p.read_text(encoding="utf-8"))
    rules_path = p.with_name("simulation.json")
    rules = json.loads(rules_path.read_text(encoding="utf-8")) if rules_path.exists() else None
    return campaign_simulator.render(campaign_simulator.simulate(spec, rules))


if __name__ == "__main__":
    server.run("stdio")
