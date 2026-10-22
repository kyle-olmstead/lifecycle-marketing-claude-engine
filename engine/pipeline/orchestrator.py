"""Orchestrator: one brief in, a build-ready execution plan out.

    python3 -m engine.pipeline.orchestrator plan --text "Build a 5-touch automated win-back flow for users who completed onboarding but haven't triggered a core action in 14 days" --product Acme
    python3 -m engine.pipeline.orchestrator plan --brief examples/brands/ridgeview_saas/briefs/winback_14d.yaml --data examples/brands/ridgeview_saas/data
    python3 -m engine.pipeline.orchestrator brief --text "..." --product Acme --out briefs/acme_winback.yaml

Sequence (each agent's output is a typed contract in engine/models.py):

  1. Guardrails   the sending gate decides who may be mailed at all (engine/policy/guardrails.py)
  2. Analyst      entry rule, exit, data dependencies, SQL, volume and baseline (engine/agents/cohort_analyst.py)
  3. Architect    the multi-channel logic tree as a flow spec plus simulation rules (engine/agents/campaign_architect.py)
  4. Copywriter   variants per touch, rendered and linted in the platform dialect (engine/agents/copywriter.py)
  5. Experiment   the persistent holdout, its sizing and the read date (engine/agents/experimentation_agent.py)
  6. QA           schema, simulated journeys, payloads, SQL, copy and guardrails, rolled into one status:
                  READY, READY WITH ASSUMPTIONS (things to confirm), or BLOCKED (something must change)

Outputs land in <out>/<flow>/: plan.md for the marketer, flow_spec.json and simulation.json for the
builder and the simulator, the cohort SQL in every dialect, rendered copy, the trigger-event payload
example, and plan.json with everything. That folder always holds the newest run; each run also keeps its
own copy in <out>/<flow>/runs/<run_id>/outputs/, so a revision never erases the plan it revised.
<out> is the workspace's runs/ folder inside a workspace, otherwise ./out. Nothing is sent and nothing is
changed in any platform.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.agents import copywriter  # noqa: E402
from engine.policy import guardrails  # noqa: E402
from engine.brief import load_brief, parse_text, to_yaml  # noqa: E402
from engine.models import PLATFORM_LABEL, Brief, ExecutionPlan, QAReport  # noqa: E402
from engine.pipeline.core import PipelineError, Run, approve, revise  # noqa: E402

def qa(brief: Brief, pol: Dict[str, Any], cohort, flow, copy_plan, exp, data_given: bool) -> QAReport:
    """Kept for callers of 2.1; the QA stage lives in engine/pipeline/stages.py."""
    from engine.pipeline.stages import qa_report
    return qa_report(brief, pol, cohort, flow, copy_plan, exp, data_given)


def run(brief: Brief, data: Optional[str] = None, as_of: Optional[str] = None, copy_mode: str = "auto",
        drafts: Optional[Dict[str, Any]] = None, client: Any = None, root: Optional[Path] = None) -> ExecutionPlan:
    """Run every stage. With root, the run is saved under root/<flow>/runs/<run_id>/ and can be resumed or revised."""
    r = Run.new(brief, root, data=data, as_of=as_of, copy_mode=copy_mode, drafts=drafts, client=client)
    return r.execute()


# ----------------------------------------------------------------------------------------------
# output
# ----------------------------------------------------------------------------------------------

def _pct(v: Optional[float]) -> str:
    return "unknown" if v is None else f"{v:.1%}"


def render_plan(p: ExecutionPlan) -> str:
    b, c, f, e = p.brief, p.cohort, p.flow, p.experiment
    core = b.core_conversion_event
    spec = f.flow_spec
    sends = [t for t in f.touches if "fallback" not in (t.condition or "")]
    span = max((t.day for t in sends), default=0)
    L: List[str] = [f"# {spec['flow']}", "",
                    f"**Status: {p.qa.status}** | {PLATFORM_LABEL[b.platform]} | generated {p.generated_at} | run {p.run_id} | LMM Engine {p.engine_version}", ""]
    if p.parent_run_id:
        L += [f"Revision of run {p.parent_run_id}.", ""]
    L += ["## In one paragraph", "",
          f"{c.plain_english} The flow sends up to {len(sends)} message(s) over {span} days "
          f"({', '.join(sorted({t.channel.replace('_', '-') for t in sends}))}), starts with a {e.holdout_pct:g}% holdout that receives nothing, "
          f"and stops the moment they {core.do}" + (" again. " if b.flow_type == "winback" else ". ")
          + (f"About {c.monthly_entrants.value:,.0f} people a month ({c.monthly_entrants.label}) would enter; "
             f"{_pct(c.baseline_conversion_rate.value)} {'come back' if b.flow_type == 'winback' else 'get there'} within "
             f"{e.outcome_window_days} days on their own ({c.baseline_conversion_rate.label}). "
             if c.monthly_entrants.value else "Entry volume is not known yet, so the read date is not computed. ")
          + f"Read: {e.read}. {p.gate_policy['note']}.", ""]
    confirm = [f"- **{a.field}**: {a.why}" for a in b.assumptions]
    confirm += [f"- **{k.name}**: {k.detail}" for k in p.qa.checks if k.status != "pass"]
    if confirm:
        L += ["## Confirm before you build", ""] + confirm + [""]
    if p.decisions:
        L += ["## Why the plan looks like this", "", "| Agent | Decision | Why |", "|---|---|---|"]
        L += [f"| {d.stage} | {d.decision} | {d.why.replace('|', '/')} |" for d in p.decisions] + [""]
    L += ["## Who enters", "", "| | |", "|---|---|",
          f"| Entry | {'; '.join(c.entry_rules)} |", f"| Never enters | {'; '.join(c.exclusions)} |", f"| Exit | {c.exit_rule} |",
          f"| Trigger | {c.trigger_event}: {c.platform_pattern} |",
          f"| Monthly entrants | {'unknown' if c.monthly_entrants.value is None else format(c.monthly_entrants.value, ',.0f')} ({c.monthly_entrants.label}: {c.monthly_entrants.basis}) |",
          f"| {'Come back with no flow' if b.flow_type == 'winback' else 'Reach the core action with no flow'} | "
          f"{_pct(c.baseline_conversion_rate.value)} ({c.baseline_conversion_rate.label}: {c.baseline_conversion_rate.basis}) |"]
    if c.entrants_on_as_of is not None:
        L.append(f"| Entering on {c.as_of} | {c.entrants_on_as_of} |")
    L += ["", "| Data dependency | Kind | Status | Fill | Note |", "|---|---|---|---|---|"]
    L += [f"| {d.name} | {d.kind} | {d.status} | {'' if d.fill_rate is None else format(d.fill_rate, '.0%')} | {d.note or ''} |" for d in c.data_dependencies]
    if b.flow_type == "winback":
        d = b.warehouse_dialect
        L += ["", f"The daily job ({d}), in `cohort/job/`: create the ledger once (`ledger_{d}.sql`); then each day queue who is due "
                  f"(`queue_{d}.sql`), push every pending row (`pending_{d}.sql` into adapters/destinations/event_payloads.py), and after each request "
                  f"succeeds mark the rows it carried (`mark_{d}.sql`, one row at a time). Each lapse sits in the ledger once, so nobody "
                  f"enters twice; a failed push stays pending and goes out on the next run. `cohort/entry_{d}.sql` shows who is due "
                  f"without queuing; other dialects and the history query are in `cohort/`.", ""]
    else:
        L += ["", f"The trigger is the live {b.qualifying_event.event} event. `cohort/entry_{b.warehouse_dialect}.sql` backfills and audits it; "
                  f"the history query (`cohort/history_*.sql`) measured the volume and unaided rate above.", ""]
    L += ["## The flow", "", "```mermaid", f.mermaid, "```", "", "| Day | Step | Channel | Job | Only if |", "|---:|---|---|---|---|"]
    L += [f"| {t.day} | {t.step_id} | {t.channel.replace('_', '-')} | {t.job} | {t.condition or ''} |" for t in f.touches]
    windows = "; ".join(f"{w['channel']} {w['allowed_from']} to {w['allowed_until']}" for w in spec["frequency"].get("send_windows", []))
    L += ["", f"Every step: {'; '.join(x['condition'] for x in spec['ongoing_filters'])}. Frequency: {spec['frequency']['cap']}. "
              f"Permitted send windows, recipient local: {windows}.", ""]
    if p.strategy:
        L += ["| Touch | Why this job and channel |", "|---|---|"]
        L += [f"| {t.index}. day {t.day}, {t.role.replace('_', ' ')}, {t.channel.replace('_', '-')} | {t.why} |" for t in p.strategy.touches] + [""]
    L += [f"## Build it in {PLATFORM_LABEL[b.platform]}", ""] + [f"{i}. {s}" for i, s in enumerate(f.build_steps, 1)] + [""]
    L += ["## The copy", ""]
    for tc in p.copy_plan:
        L.append(f"### {tc.step_id} ({tc.channel.replace('_', '-')}, {tc.mode})")
        if tc.note:
            L.append(f"_{tc.note}_")
        if not tc.variants:
            L += ["", "Brief:", "", "```text", tc.copy_brief, "```", ""]
            continue
        L += ["", "| Variant | Copy | Framework | Hypothesis |", "|---|---|---|---|"]
        approved = p.approvals.get(tc.step_id)
        for v in tc.variants:
            if tc.channel == "email":
                copy_txt = f"**{v.fields.get('subject')}** / {v.fields.get('preheader')}".replace("{first_name}", "[first name]")
            elif tc.channel == "sms":
                copy_txt = v.fields.get("text", "")
            elif tc.channel == "push":
                copy_txt = f"**{v.fields.get('title')}** / {v.fields.get('body')}"
            else:
                copy_txt = f"**{v.fields.get('headline')}** / {v.fields.get('body')} [{v.fields.get('cta_label')}]"
            copy_txt = copy_txt.replace("{product_name}", b.product_name).replace("{first_name}", "[first name]").replace("|", "/")
            label = f"{v.variant_id} (approved)" if approved == v.variant_id else v.variant_id
            L.append(f"| {label} | {copy_txt} | {v.framework} | {v.hypothesis} |")
        files = sorted({k for v in tc.variants for k in v.rendered})
        L += ["", "Files: " + ", ".join(f"`copy/{x}`" for x in files), ""]
    L += ["## Measurement", "", "| | |", "|---|---|", f"| Holdout | {e.holdout_pct:g}%, {e.unit} level, persistent: {e.assignment_rule} |",
          f"| Primary metric | {e.primary_metric} |", f"| Sizing | {e.sizing} |", f"| Read | {e.read}: {e.read_after} |",
          f"| Decision rule | {e.decision_rule} |", f"| Sample ratio | {e.srm_rule} |", f"| Peeking | {e.peeking_policy} |",
          f"| Guardrails | {'; '.join(e.guardrail_metrics)} |", f"| Copy tests | {' '.join(e.variant_tests)} |", ""]
    if e.mde_by_horizon:
        L += ["| Months of entries | Holdout n | Treatment n | Smallest detectable lift |", "|---:|---:|---:|---|"]
        L += [f"| {m['months_of_entries']} | {m['holdout_n']:,} | {m['treatment_n']:,} | "
              + ("not reachable" if m["mde_abs_pts"] is None else f"{m['mde_abs_pts']} pts ({m['mde_rel']:.0%} relative)") + " |" for m in e.mde_by_horizon]
        L.append("")
    if e.sensitivity:
        L += [f"How long a {e.holdout_pct:g}% holdout takes to read a {b.min_detectable_lift:.0%} lift (months, incl. the {e.outcome_window_days}-day window):", "",
              "| Entrants a month | 5% come back alone | 10% | 20% |", "|---:|---:|---:|---:|"]
        for n in sorted({g["entrants_per_month"] for g in e.sensitivity}):
            row = [g for g in e.sensitivity if g["entrants_per_month"] == n]
            L.append(f"| {n:,} | " + " | ".join("never" if g["months_to_read"] is None else f"{g['months_to_read']}" for g in row) + " |")
        L.append("")
    L += ["## Deliverability guardrails", "", f"- Gate: {p.gate_policy['note']}. Audience: {p.gate_policy['audience']}."]
    L += [f"- Suppressed: {s}" for s in spec["suppressions"]]
    L += [f"- {r}" for r in guardrails.RE_ENGAGEMENT_RULES]
    L += ["- Every email carries the platform's unsubscribe token and a postal address; every SMS carries opt-out language; "
          "every template is linted in its dialect; image-only email is blocked.", ""]
    if p.compliance:
        cp = p.compliance
        L += ["## Compliance", "", f"**{cp.status.upper()}** for {', '.join(cp.jurisdictions)}. {cp.note}", "",
              "| Rule | Jurisdiction | Result | Detail | Source |", "|---|---|---|---|---|"]
        L += [f"| {x.rule} | {x.jurisdiction} | {x.status.upper()} | {x.detail.replace('|', '/')} | {x.source or ''} |" for x in cp.findings] + [""]
    L += ["## QA", "", "| Check | Result | Detail |", "|---|---|---|"]
    L += [f"| {k.name} | {k.status.upper()} | {k.detail.replace('|', '/')} |" for k in p.qa.checks]
    L += ["", "## Files", "", "- `flow_spec.json`: the flow, valid against `schemas/flow_spec.schema.json`",
          "- `simulation.json`: rules for `python3 engine/policy/simulator.py flow_spec.json`",
          "- `cohort/`: entry and history SQL for postgres, snowflake, bigquery and duckdb",
          "- `copy/`: rendered templates; `experiment.json`; `trigger_payload_example.json`; `plan.json` (everything)", ""]
    return "\n".join(L)


def default_out() -> str:
    """The workspace's runs/ folder inside a workspace (lmm.yaml here or above, or LCM_WORKSPACE), otherwise ./out."""
    from engine import workspace
    ws = workspace.find()
    return "out" if ws.fallback else str(ws.runs_dir)


def write_outputs(p: ExecutionPlan, out_root: Path, run_dir: Optional[Path] = None) -> Path:
    """Write the plan's files to out_root/<flow>/ (the newest run) and, with run_dir, a copy into run_dir/outputs/."""
    out = _write_files(p, out_root / p.brief.slug)
    if run_dir is not None:
        _write_files(p, Path(run_dir) / "outputs")
    return out


def _write_files(p: ExecutionPlan, out: Path) -> Path:
    (out / "cohort").mkdir(parents=True, exist_ok=True)
    (out / "copy").mkdir(exist_ok=True)
    (out / "plan.json").write_text(p.model_dump_json(indent=2), encoding="utf-8")
    (out / "plan.md").write_text(render_plan(p), encoding="utf-8")
    (out / "flow_spec.json").write_text(json.dumps(p.flow.flow_spec, indent=2), encoding="utf-8")
    (out / "simulation.json").write_text(json.dumps(p.flow.simulation, indent=2), encoding="utf-8")
    (out / "experiment.json").write_text(p.experiment.model_dump_json(indent=2), encoding="utf-8")
    if p.experiment.registry_entry and "error" not in p.experiment.registry_entry:
        (out / "experiment_registry_entry.json").write_text(json.dumps(p.experiment.registry_entry, indent=2), encoding="utf-8")
    (out / "brief.yaml").write_text(to_yaml(p.brief), encoding="utf-8")
    for d, sql in p.cohort.sql.items():
        (out / "cohort" / f"entry_{d}.sql").write_text(sql, encoding="utf-8")
    for d, sql in p.cohort.history_sql.items():
        (out / "cohort" / f"history_{d}.sql").write_text(sql, encoding="utf-8")
    if p.cohort.job_sql:
        (out / "cohort" / "job").mkdir(exist_ok=True)
        for d, steps in p.cohort.job_sql.items():
            for step, sql in steps.items():
                (out / "cohort" / "job" / f"{step}_{d}.sql").write_text(sql, encoding="utf-8")
    if p.cohort.trigger_payload_example:
        (out / "trigger_payload_example.json").write_text(json.dumps(p.cohort.trigger_payload_example, indent=2), encoding="utf-8")
    for tc in p.copy_plan:
        for v in tc.variants:
            for name, text in v.rendered.items():
                (out / "copy" / name).write_text(text, encoding="utf-8")
    if any(tc.mode == "handoff" for tc in p.copy_plan):
        (out / "copy_briefs.md").write_text(copywriter.handoff_markdown(p.brief, p.copy_plan), encoding="utf-8")
    return out


def _report(plan: ExecutionPlan, out: Path, run_dir: Optional[Path]) -> None:
    print(f"{plan.qa.status}: {plan.flow.flow_spec['flow']}")
    for k in plan.qa.checks:
        if k.status != "pass":
            print(f"  {k.status.upper():4}  {k.name}: {k.detail[:160]}")
    print(f"plan: {out / 'plan.md'}")
    if run_dir:
        print(f"run:  {run_dir}  (this run's own copy: {Path(run_dir) / 'outputs'})")
        print(f"next: lcm revise --run {run_dir} --set touches=4   |   lcm approve --run {run_dir} --step <step> --variant <id>")


def _value(v: str) -> Any:
    import yaml
    parsed = yaml.safe_load(v)
    return parsed


def _latest(path: Path) -> Path:
    """A run directory, or a flow folder (its newest run), or a runs folder."""
    path = Path(path)
    if (path / "state.json").exists():
        return path
    for runs in (path / "runs", path):
        latest = runs / "latest"
        if latest.exists():
            return runs / latest.read_text(encoding="utf-8").strip()
    raise FileNotFoundError(f"{path}: not a run directory (no state.json) and no runs/latest")


def _root(run_dir: Path) -> Path:
    """The output folder a run belongs to: <root>/<flow>/runs/<run_id>."""
    return Path(run_dir).resolve().parents[2]


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="lcm", description="LMM Engine teammate: one brief in, a build-ready plan out")
    sub = ap.add_subparsers(dest="cmd", required=True)
    out_help = "where plans are written (default: the workspace's runs/ folder, or ./out outside a workspace)"
    pl = sub.add_parser("plan", help="run every agent and write the plan")
    src = pl.add_mutually_exclusive_group(required=True)
    src.add_argument("--brief", help="a filled brief YAML (templates/brief.yaml shows every field)")
    src.add_argument("--text", help="the brief in one sentence, e.g. \"a 3-touch abandoned cart flow in Klaviyo\"")
    pl.add_argument("--product", help="product or brand name, with --text (default: brand.name in lmm.yaml)")
    pl.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE",
                    help="set a brief field, e.g. postal_address=\"1 Main St, Springfield, IL 62701\" or holdout_pct=20 (repeatable)")
    pl.add_argument("--data", help="folder or URL prefix with users, orders, track_events exports (.csv or .parquet)")
    pl.add_argument("--as-of", help="evaluate the cohort on this date (default: the latest complete day in the data)")
    pl.add_argument("--copy-mode", default="auto", choices=["auto", "claude", "handoff", "offline"],
                    help="who writes the copy: auto (Claude when ANTHROPIC_API_KEY is set, else the offline library), claude, "
                         "handoff (write copy_briefs.md for a person or a Claude chat), offline (the built-in library)")
    pl.add_argument("--drafts", help="drafts.json answering copy_briefs.md (from handoff mode)")
    pl.add_argument("--out", help=out_help)
    pl.add_argument("--sheet", action="store_true", help="log the plan to Google Sheets (needs LCM_SHEET_ID and service-account credentials)")
    br = sub.add_parser("brief", help="turn a sentence into an editable YAML brief")
    br.add_argument("--text", required=True, help="the brief in one sentence")
    br.add_argument("--product", help="product or brand name (default: brand.name in lmm.yaml)")
    br.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE", help="set a brief field (repeatable)")
    br.add_argument("--out", help="write the YAML here instead of printing it")
    rs = sub.add_parser("resume", help="continue a run that failed part way, from the failed stage")
    rs.add_argument("--run", required=True, help="a run directory, or a flow folder for its newest run")
    rs.add_argument("--data", help="replace the data location (when that was the cause of the failure)")
    rs.add_argument("--out", help="default: the folder the run lives in")
    rv = sub.add_parser("revise", help="start a new run from an old one with brief fields changed; unchanged copy is kept")
    rv.add_argument("--run", required=True, help="a run directory, or a flow folder for its newest run")
    rv.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE", help="e.g. touches=4, channels=[email,sms], holdout_pct=20")
    rv.add_argument("--out", help="default: the folder the run lives in")
    apv = sub.add_parser("approve", help="mark a copy variant as approved; revisions keep it while its touch is unchanged")
    apv.add_argument("--run", required=True, help="a run directory, or a flow folder for its newest run")
    apv.add_argument("--step", required=True, help="the step id from plan.md, e.g. email_1")
    apv.add_argument("--variant", required=True, help="the variant id from plan.md, e.g. A")
    apv.add_argument("--out", help="default: the folder the run lives in")
    st = sub.add_parser("status", help="show a run's stages")
    st.add_argument("--run", required=True, help="a run directory, or a flow folder for its newest run")
    a = ap.parse_args(argv)

    def sets(items: List[str]) -> Dict[str, Any]:
        out = {}
        for item in items:
            k, eq, v = item.partition("=")
            if not eq:
                raise ValueError(f"--set takes FIELD=VALUE (got {item!r})")
            out[k.strip()] = _value(v)
        return out

    try:
        if a.cmd == "brief":
            y = to_yaml(parse_text(a.text, a.product, sets(a.set) or None), thin=True)
            if a.out:
                Path(a.out).parent.mkdir(parents=True, exist_ok=True)
                Path(a.out).write_text(y, encoding="utf-8")
                print(f"wrote {a.out}: confirm the assumptions at the top, then run: lcm plan --brief {a.out}")
            else:
                print(y)
            return 0
        if a.cmd == "status":
            r = Run.load(_latest(Path(a.run)))
            print(f"run {r.state.run_id}: {r.state.status}" + (f" (failed at {r.state.failed_stage})" if r.state.failed_stage else "")
                  + (f", revision of {r.state.parent_run_id}" if r.state.parent_run_id else ""))
            for rec in r.state.stages.values():
                print(f"  {rec.name:11} {rec.status:8} {rec.seconds:6.2f}s attempts {rec.attempts}" + (f"  {rec.error}" if rec.error else ""))
            if r.state.approvals:
                print("  approved: " + ", ".join(f"{k}={v}" for k, v in r.state.approvals.items()))
            return 0
        if a.cmd == "approve":
            run_dir = _latest(Path(a.run))
            approve(run_dir, a.step, a.variant)
            r = Run.load(run_dir)
            write_outputs(r.plan(), Path(a.out) if a.out else _root(run_dir), run_dir)
            print(f"approved {a.step} variant {a.variant} in run {r.state.run_id}")
            return 0
        if a.cmd == "resume":
            run_dir = _latest(Path(a.run))
            r = Run.load(run_dir)
            if a.data:
                r.state.data = a.data
            plan = r.execute()
            _report(plan, write_outputs(plan, Path(a.out) if a.out else _root(run_dir), r.dir), r.dir)
            return 3 if plan.qa.status == "BLOCKED" else 0
        if a.cmd == "revise":
            run_dir = _latest(Path(a.run))
            out = Path(a.out) if a.out else _root(run_dir)
            r, plan = revise(run_dir, sets(a.set), out)
            _report(plan, write_outputs(plan, out, r.dir), r.dir)
            return 3 if plan.qa.status == "BLOCKED" else 0
        changes = sets(a.set)
        if a.brief:
            brief = load_brief(Path(a.brief))
            if changes:
                data = {**brief.model_dump(), **changes}
                data["assumptions"] = [x for x in data["assumptions"] if x["field"] not in changes]   # a value set is no longer assumed
                brief = Brief.model_validate(data)
        else:
            brief = parse_text(a.text, a.product, changes or None)
        drafts = json.loads(Path(a.drafts).read_text(encoding="utf-8")) if a.drafts else None
        a.out = a.out or default_out()
        r = Run.new(brief, Path(a.out), data=a.data, as_of=a.as_of, copy_mode=a.copy_mode, drafts=drafts)
        plan = r.execute()
    except PipelineError as e:
        print(f"BLOCKED: {e}", file=sys.stderr)
        return 4
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    out = write_outputs(plan, Path(a.out), r.dir)
    _report(plan, out, r.dir)
    if a.sheet:
        from adapters.destinations import sheets as sheets_sync
        print(sheets_sync.sync(plans=[plan.model_dump(mode='json')], dry_run=not os.environ.get("LCM_SHEET_ID")))
    return 3 if plan.qa.status == "BLOCKED" else 0


if __name__ == "__main__":
    sys.exit(_cli())
