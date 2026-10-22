"""The teammate as a local MCP server, for Claude Desktop or Claude Code without the plugin.

Add it to claude_desktop_config.json (the block is in config/mcp_server_config.json) and ask Claude
in plain English: "Plan a 5-touch win-back for Acme users who finished onboarding but have not
created a report in 14 days." Claude calls these tools; they run locally, read only the folders
you point them at, write plans under LCM_OUT_DIR (default ./out), and never send anything.

Tools
  draft_brief      a sentence to an editable YAML brief, with every assumption listed
  plan_campaign    the full plan from a sentence or a brief file (optionally verified on data)
  revise_plan, approve_variant, priorities, engagement_tiers, check_domain
  audit_cohort     the cohort health check on a folder of exports
  simulate_flow    dry-run any flow spec JSON
  simulate_program every live flow in the workspace together, against its one frequency policy
  check_data       a data folder against the canonical tables, column by column
  import_exports   vendor exports (Shopify, Klaviyo, Stripe, HubSpot, ...) into the canonical tables
  size_holdout     people and months a holdout needs, in sentences
  workspace_doctor what is set up in the workspace and what is missing

Plans are written to LCM_OUT_DIR, else the workspace's runs/ folder, else ./out.

Requires the Python packages in requirements.txt (mcp 2.x).

    python3 engine/mcp_server.py            # stdio, which is what Claude Desktop starts
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from engine import __version__  # noqa: E402
from engine.agents import cohort_analyst, engagement  # noqa: E402
from engine.pipeline import orchestrator  # noqa: E402
from engine.pipeline.core import PipelineError, approve, revise  # noqa: E402
from engine.brief import load_brief, parse_text, to_yaml  # noqa: E402
from engine.policy import simulator as campaign_simulator  # noqa: E402

OUT = Path(os.environ.get("LCM_OUT_DIR") or orchestrator.default_out()).resolve()

server = MCPServer(
    name="lcm-teammate", version=__version__,
    instructions=("Lifecycle marketing teammate from the LMM Engine. Use draft_brief when the request is vague and the marketer should confirm "
                  "assumptions; plan_campaign to produce a build-ready plan; revise_plan to change one thing in a plan (unchanged copy and "
                  "approvals are kept); approve_variant to record the copy to ship; priorities for what to build next and why; audit_cohort for a cohort health check; "
                  "engagement_tiers for who has stopped engaging; check_domain for a sending domain's live authentication records; "
                  "simulate_flow to dry-run a flow spec; simulate_program for every live flow at once; check_data before trusting a data folder; "
                  "import_exports to turn vendor exports into the engine's tables; size_holdout for test sizing; workspace_doctor for setup. Always show the plan's status, its 'Confirm before you build' list and any "
                  "compliance line that is not PASS. Nothing is sent to any platform by these tools."),
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
        plan = orchestrator.run(brief, data_dir, copy_mode=copy_mode, root=OUT)
    except PipelineError as e:
        return f"The run stopped at the {e.stage} stage: {e.error}. Fix the cause and plan again."
    except (ValueError, FileNotFoundError) as e:
        return f"Could not plan: {e}"
    out = orchestrator.write_outputs(plan, OUT)
    return f"Files written to {out} (run {plan.run_id}; revise with revise_plan using flow '{plan.brief.slug}')\n\n" + orchestrator.render_plan(plan)


@server.tool()
def revise_plan(flow: str, changes: Dict[str, Any]) -> str:
    """Change brief fields of the newest plan for a flow (its slug, e.g. 'acme_winback_14d') and re-run only what the change affects.
    changes: field to new value, e.g. {"touches": 4} or {"channels": ["email", "sms"], "holdout_pct": 20}."""
    try:
        run, plan = revise(orchestrator._latest(OUT / flow), changes, OUT)
    except PipelineError as e:
        return f"The revision stopped at the {e.stage} stage: {e.error}"
    except (ValueError, FileNotFoundError) as e:
        return f"Could not revise: {e}"
    out = orchestrator.write_outputs(plan, OUT)
    return f"Files written to {out} (run {plan.run_id}, a revision of {plan.parent_run_id})\n\n" + orchestrator.render_plan(plan)


@server.tool()
def approve_variant(flow: str, step_id: str, variant_id: str) -> str:
    """Record which copy variant ships for one step of the newest plan for a flow; revisions keep it while the step is unchanged."""
    try:
        approve(orchestrator._latest(OUT / flow), step_id, variant_id)
    except (ValueError, FileNotFoundError) as e:
        return f"Could not approve: {e}"
    return f"Approved {step_id} variant {variant_id} for {flow}."


@server.tool()
def engagement_tiers(data_dir: str, as_of: Optional[str] = None) -> str:
    """Engagement tiers (clicks only) from users and message_events exports, with the unengaged share for the sending gate."""
    try:
        r = engagement.run(data_dir, as_of)
    except (ValueError, FileNotFoundError) as e:
        return f"Could not build tiers: {e}"
    return engagement.render(r["summary"])


@server.tool()
def priorities(data_dir: str, top: int = 3, value_per_conversion: Optional[float] = None, domains: Optional[List[str]] = None) -> str:
    """Today's top builds, ranked from the data with their why, size, confidence and effort; fixes that cap everything
    else come first (a RED sending domain among them when domains, or LCM_DOMAINS, names one). Use for "what should I
    build next?", "what are my top 3 priorities today?", or campaign ideas."""
    from engine.agents import priorities as prio
    domains = domains or [d.strip() for d in os.environ.get("LCM_DOMAINS", "").split(",") if d.strip()]
    try:
        r = prio.run(data_dir, top=top, value=value_per_conversion, out=OUT,
                     deliverability=prio.check_domains(domains) if domains else None)
    except (ValueError, FileNotFoundError) as e:
        return f"Could not rank priorities: {e}"
    return prio.render(r)


@server.tool()
def check_domain(domain: str, selectors: Optional[List[str]] = None, unengaged_share: Optional[float] = None) -> str:
    """Look up a sending domain's SPF, DKIM, DMARC, MTA-STS, TLS-RPT and BIMI records now, and return the deliverability analysis."""
    from engine.deliverability import analyze as deliverability, dns_collect
    from engine.contracts import validate_deliverability
    try:
        doc = dns_collect.collect(domain, selectors)
    except (LookupError, TimeoutError) as e:
        return f"Could not look up {domain}: {e}"
    doc.pop("_lookups", None)
    if unengaged_share is not None:
        doc["signals"] = {"unengaged_share": unengaged_share}
    return deliverability.render_markdown(deliverability.analyze(validate_deliverability(doc)))


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


@server.tool()
def simulate_program(add_brief_paths: Optional[List[str]] = None) -> str:
    """Every brief in the workspace's briefs/active/ (plus any candidates) simulated together against the workspace's
    frequency policy and collision rule: which caps break across flows, and the rule or platform limit that holds them."""
    from engine.policy import program
    briefs = program.active_briefs() + [load_brief(Path(x)) for x in (add_brief_paths or [])]
    if not briefs:
        return "No live flows: copy the briefs of live flows into briefs/active/, or pass add_brief_paths."
    return program.render(program.simulate(briefs, program.workspace_policy()))


@server.tool()
def check_data(data_dir: str) -> str:
    """Check a folder of exports against the canonical tables: missing or late tables, empty or malformed columns, future
    dates, duplicate keys and orphaned rows, each with examples and a fix. Returns Markdown."""
    from engine.data import check
    return check.render_markdown(check.check_folder(Path(data_dir)))


@server.tool()
def import_exports(source: str, folders: List[str], out_dir: str) -> str:
    """Map vendor export folders into the canonical tables in out_dir (source: shopify, klaviyo, stripe, hubspot, customerio,
    ga4, segment, rudderstack, or auto to detect), then check the result. Returns the import and check reports."""
    from adapters.sources import cli as import_cli
    from engine.data import check
    summary = import_cli.run_import(source, [Path(f) for f in folders], Path(out_dir))
    return import_cli.render_markdown(summary) + "\n" + check.render_markdown(check.check_folder(Path(out_dir)))


@server.tool()
def size_holdout(baseline: float, mde_rel: float, holdout: float = 0.15, entries_per_month: Optional[float] = None,
                 outcome_window_days: float = 0) -> str:
    """People a holdout test needs to detect a relative lift on a baseline rate (shares: 0.10 = 10%), and with
    entries_per_month, how many months it takes to read and what a larger holdout would buy."""
    from engine.measurement import stats
    if entries_per_month:
        return stats.render_feasible(stats.feasibility(entries_per_month, holdout, baseline, None, mde_rel, outcome_window_days=outcome_window_days))
    ratio = (1 - holdout) / holdout if holdout else 1.0
    return stats.render_size(stats.sample_size(baseline, baseline * (1 + mde_rel), 0.05, 0.80, ratio, True), holdout)


@server.tool()
def workspace_doctor() -> str:
    """What is set up and what is missing: Python and packages, the workspace's lmm.yaml, the sending gate's age, the
    brand kit's contrast, the data, the live briefs, and which credential names are set (values are never read out)."""
    from engine.cli.commands import diagnose
    mark = {"ok": "OK", "note": "NOTE", "fail": "FAIL"}
    return "\n".join(f"{mark[r['status']]:4}  {r['name']}: {r['detail']}" for r in diagnose())


if __name__ == "__main__":
    server.run("stdio")
