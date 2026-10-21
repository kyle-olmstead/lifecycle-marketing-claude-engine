"""The agent pipeline as a fault-tolerant, resumable run with saved state.

    intake -> analyst -> strategist -> architect -> copywriter -> experiment -> qa -> compliance

Each stage reads the outputs of the stages before it and writes typed outputs (agents/models.py).
A run directory keeps everything the agents know about a campaign, so work carries across steps:

  out/<flow>/runs/<run_id>/state.json         brief, settings, stage records, approvals, decisions
  out/<flow>/runs/<run_id>/stages/<stage>.json each stage's output
  out/<flow>/runs/latest                       the id of the newest run

Fault tolerance
  * The analyst stage is retried twice when loading remote data fails with a network or server error.
    (Calls to Claude are retried by the Anthropic SDK; if they still fail, the copywriter falls back to
    offline drafts and the plan says so.) A stage that still fails is recorded with its error, and the
    stages after it are skipped. Nothing is lost: `resume` re-runs from the failed stage once the cause
    is fixed.
  * Each stage records a hash of its inputs and reads only the outputs of the stages before it. On
    resume, a finished stage whose inputs have not changed is reused rather than recomputed.

Context across steps
  * `revise` starts a new run from an old one with some brief fields changed. Copy for any touch whose
    job, channel, audience and voice did not change is carried over (no new Claude call, hand edits
    kept), re-rendered and re-linted for the new plan.
  * `approve` records which variant of a touch the marketer approved; the plan marks it. An approval
    stands only while the approved words stand: when a revision or a resume rewrites them, it is
    dropped and the plan says so.
  * Every agent logs its decisions and the reasons; the plan prints them under "Why the plan looks
    like this".
"""
from __future__ import annotations

import hashlib
import json
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from agents import __version__
from agents.models import (Brief, CohortDefinition, ComplianceReport, Decision, ExecutionPlan, ExperimentDesign, FlowPlan, QAReport,
                           Strategy, TouchCopy)

STAGES = ("intake", "analyst", "strategist", "architect", "copywriter", "experiment", "qa", "compliance")
RETRIES = {"analyst": 2}
_now = lambda: datetime.now(timezone.utc).replace(microsecond=0).isoformat()  # noqa: E731

# what each stage writes, and how to read it back
OUTPUT_TYPES: Dict[str, Dict[str, Any]] = {
    "intake": {"brief": Brief, "gate_policy": dict},
    "analyst": {"cohort": CohortDefinition, "brief": Brief},
    "strategist": {"strategy": Strategy},
    "architect": {"flow": FlowPlan},
    "copywriter": {"copy_plan": List[TouchCopy]},
    "experiment": {"experiment": ExperimentDesign, "flow": FlowPlan},
    "qa": {"qa": QAReport},
    "compliance": {"compliance": ComplianceReport, "qa": QAReport},
}


class StageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    status: Literal["pending", "ok", "reused", "failed", "skipped"] = "pending"
    attempts: int = 0
    seconds: float = 0.0
    input_hash: Optional[str] = None
    error: Optional[str] = None
    finished_at: Optional[str] = None


class RunState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    parent_run_id: Optional[str] = None
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)
    engine_version: str = __version__
    brief: Brief
    data: Optional[str] = None
    as_of: Optional[str] = None
    copy_mode: str = "auto"
    drafts: Optional[Dict[str, Any]] = None
    stages: Dict[str, StageRecord] = Field(default_factory=lambda: {s: StageRecord(name=s) for s in STAGES})
    approvals: Dict[str, str] = Field(default_factory=dict)
    status: Literal["new", "running", "complete", "failed"] = "new"
    failed_stage: Optional[str] = None
    changes_from_parent: Dict[str, Any] = Field(default_factory=dict)


class PipelineError(RuntimeError):
    def __init__(self, stage: str, error: str, run_dir: Optional[Path] = None):
        self.stage, self.error, self.run_dir = stage, error, run_dir
        where = f"; fix the cause, then: python3 -m agents.orchestrator resume --run {run_dir}" if run_dir else ""
        super().__init__(f"stage '{stage}' failed: {error}{where}")


def _h(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _dump(v: Any) -> Any:
    if isinstance(v, BaseModel):
        return v.model_dump(mode="json")
    if isinstance(v, list):
        return [_dump(x) for x in v]
    return v


def _data_fingerprint(data: Optional[str]) -> Any:
    """Local exports: names, sizes and modification times, so new data invalidates the analyst's output."""
    if not data or data.startswith(("http://", "https://", "s3://", "gs://")):
        return data
    p = Path(data)
    if not p.exists():
        return data
    return [(f.name, f.stat().st_size, int(f.stat().st_mtime)) for f in sorted(p.iterdir()) if f.is_file()]


def copy_key(touch: Any, brief: Brief, mode: str) -> str:
    """Identity of one touch's words: its job and channel, the audience it is written to (flow type, qualifying and core
    events, threshold), and the brief fields copy is written from. The touch's position, links and the postal address are not
    part of it: reused words are re-rendered and re-linted for the new plan."""
    return _h(touch.role, touch.channel, touch.job, touch.goal, brief.flow_type, brief.qualifying_event.model_dump(),
              brief.core_conversion_event.model_dump(), brief.churn_threshold_days, brief.product_name, brief.offer_policy,
              brief.brand_voice, brief.sender_name, brief.copy_variants, brief.template_dialect, mode)


class Run:
    """One pipeline run. With a directory it persists after every stage; without one it runs in memory."""

    def __init__(self, state: RunState, run_dir: Optional[Path] = None, client: Any = None,
                 parent: Optional["Run"] = None, sleep: Callable[[float], None] = time.sleep):
        self.state, self.dir, self.client, self.parent, self.sleep = state, run_dir, client, parent, sleep
        self.stage_out: Dict[str, Dict[str, Any]] = {}       # each finished stage's own outputs
        self.stage_dec: Dict[str, List[Decision]] = {}       # and its decisions
        self.out: Dict[str, Any] = {}                        # what the running stage may read: the stages before it
        self.decisions: List[Decision] = []
        self.prior: Dict[str, Any] = {}                      # the running stage's own outputs from an earlier attempt, if any

    def _view(self, upto: Optional[str] = None) -> None:
        """Make out and decisions the merged outputs of the stages before `upto` (all stages when None), in order."""
        names = STAGES[: STAGES.index(upto)] if upto else STAGES
        self.out, self.decisions = {}, []
        for s in names:
            self.out.update(self.stage_out.get(s, {}))
            self.decisions += self.stage_dec.get(s, [])

    # ------------------------------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------------------------------

    @classmethod
    def new(cls, brief: Brief, root: Optional[Path] = None, **kw: Any) -> "Run":
        state = RunState(run_id=uuid.uuid4().hex[:12], brief=brief, data=kw.pop("data", None), as_of=kw.pop("as_of", None),
                         copy_mode=kw.pop("copy_mode", "auto"), drafts=kw.pop("drafts", None),
                         parent_run_id=kw.pop("parent_run_id", None), changes_from_parent=kw.pop("changes", {}),
                         approvals=kw.pop("approvals", {}))
        run_dir = (root / brief.slug / "runs" / state.run_id) if root else None
        return cls(state, run_dir, **kw)

    @classmethod
    def load(cls, run_dir: Path, load_parent: bool = True, **kw: Any) -> "Run":
        run_dir = Path(run_dir)
        state = RunState.model_validate_json((run_dir / "state.json").read_text(encoding="utf-8"))
        run = cls(state, run_dir, **kw)
        for stage, types in OUTPUT_TYPES.items():
            path = run_dir / "stages" / f"{stage}.json"
            if path.exists():   # kept even for a failed or skipped stage: a rerun compares against it (approvals)
                raw = json.loads(path.read_text(encoding="utf-8"))
                run.stage_out[stage] = {key: TypeAdapter(typ).validate_python(raw[key]) for key, typ in types.items()}
                run.stage_dec[stage] = [Decision(**d) for d in raw.get("_decisions", [])]
        # a revision keeps its parent, so resuming it still reuses the parent's copy and approvals
        parent_dir = run_dir.parent / state.parent_run_id if state.parent_run_id else None
        if load_parent and parent_dir is not None and (parent_dir / "state.json").exists():
            run.parent = cls.load(parent_dir, load_parent=False)
        run._view()
        return run

    def save(self) -> None:
        if not self.dir:
            return
        self.state.updated_at = _now()
        (self.dir / "stages").mkdir(parents=True, exist_ok=True)
        (self.dir / "state.json").write_text(self.state.model_dump_json(indent=2), encoding="utf-8")
        (self.dir.parent / "latest").write_text(self.state.run_id + "\n", encoding="utf-8")

    def _save_stage(self, stage: str, outputs: Dict[str, Any], decisions: List[Decision]) -> None:
        if not self.dir:
            return
        payload = {k: _dump(v) for k, v in outputs.items()}
        payload["_decisions"] = [d.model_dump() for d in decisions]
        (self.dir / "stages").mkdir(parents=True, exist_ok=True)
        (self.dir / "stages" / f"{stage}.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    # ------------------------------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------------------------------

    def _input_hash(self, stage: str) -> str:
        i = STAGES.index(stage)
        upstream = [self.state.stages[s].input_hash for s in STAGES[:i]]
        extra: Any = None
        if stage == "intake":
            extra = self.state.brief.model_dump(mode="json")
        elif stage == "analyst":
            extra = (self.state.data, self.state.as_of, _data_fingerprint(self.state.data))
        elif stage == "copywriter":
            extra = (self.state.copy_mode, self.state.drafts)
        return _h(stage, __version__, upstream, extra)

    def execute(self) -> ExecutionPlan:
        from agents import stages as stage_fns
        self.state.status, self.state.failed_stage = "running", None
        self.save()
        failed: Optional[str] = None
        for stage in STAGES:
            rec = self.state.stages[stage]
            if failed:
                rec.status, rec.error = "skipped", f"not run: {failed} failed"
                continue
            h = self._input_hash(stage)
            if rec.status in ("ok", "reused") and rec.input_hash == h and all(k in self.stage_out.get(stage, {}) for k in OUTPUT_TYPES[stage]):
                rec.status = "reused"
                continue
            rec.input_hash, rec.error = h, None
            started = time.monotonic()
            self.prior = self.stage_out.pop(stage, {})
            self.stage_dec.pop(stage, None)
            for attempt in range(1, RETRIES.get(stage, 0) + 2):
                rec.attempts = attempt
                self._view(stage)                 # a stage sees only the stages before it, never its own earlier output
                before = len(self.decisions)
                try:
                    outputs = stage_fns.FUNCS[stage](self)
                    self.stage_out[stage] = outputs
                    self.stage_dec[stage] = self.decisions[before:]
                    rec.status, rec.finished_at = "ok", _now()
                    self._save_stage(stage, outputs, self.stage_dec[stage])
                    break
                except Exception as e:  # noqa: BLE001  (every failure is recorded, nothing is swallowed silently)
                    del self.decisions[before:]
                    tb = traceback.extract_tb(e.__traceback__)[-1]
                    rec.error = f"{type(e).__name__}: {str(e)[:400]} (at {Path(tb.filename).name}:{tb.lineno})"
                    if attempt <= RETRIES.get(stage, 0) and _transient(e):
                        self.sleep(0.5 * attempt)
                        continue
                    rec.status = "failed"
                    failed = stage
                    break
            rec.seconds = round(time.monotonic() - started, 3)
            self.save()
        self.prior = {}
        self._view()
        if failed:
            self.state.status, self.state.failed_stage = "failed", failed
            self.save()
            raise PipelineError(failed, self.state.stages[failed].error or "", self.dir)
        self.state.status = "complete"
        self.save()
        return self.plan()

    def plan(self) -> ExecutionPlan:
        o = self.out
        return ExecutionPlan(run_id=self.state.run_id, engine_version=__version__, brief=o["brief"], gate_policy=o["gate_policy"],
                             cohort=o["cohort"], flow=o["flow"], copy_plan=o["copy_plan"], experiment=o["experiment"], qa=o["qa"],
                             strategy=o.get("strategy"), compliance=o.get("compliance"), decisions=list(self.decisions),
                             parent_run_id=self.state.parent_run_id, approvals=dict(self.state.approvals))

    # ------------------------------------------------------------------------------------------
    # revisions and approvals
    # ------------------------------------------------------------------------------------------

    def previous_copy(self) -> Dict[str, Tuple[TouchCopy, Optional[str], str]]:
        """Copy from the parent run, keyed by copy identity: (copy, the approval it carried, the parent run id)."""
        if not self.parent or "copy_plan" not in self.parent.out or "flow" not in self.parent.out:
            return {}
        touches = {t.step_id: t for t in self.parent.out["flow"].touches}
        pb = self.parent.out["brief"]
        out: Dict[str, Tuple[TouchCopy, Optional[str], str]] = {}
        for tc in self.parent.out["copy_plan"]:
            t = touches.get(tc.step_id)
            if tc.variants and t is not None and tc.mode != "handoff":
                out[copy_key(t, pb, self.parent.state.copy_mode)] = (tc, self.parent.state.approvals.get(tc.step_id), self.parent.state.run_id)
        return out


def _transient(e: Exception) -> bool:
    name = type(e).__name__
    if isinstance(e, (FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError, ValueError)):
        return False                       # a wrong path or a bad input does not fix itself on retry
    return isinstance(e, (TimeoutError, ConnectionError, OSError)) or name in ("APIConnectionError", "APITimeoutError", "RateLimitError",
                                                                              "InternalServerError", "HTTPException", "IOException")


def revise(old_dir: Path, changes: Dict[str, Any], root: Path, client: Any = None) -> Tuple[Run, ExecutionPlan]:
    parent = Run.load(old_dir)
    data = parent.state.brief.model_dump()
    unknown = [k for k in changes if k not in Brief.model_fields or k == "assumptions"]
    if unknown:
        raise ValueError(f"not brief fields: {unknown}")
    data.update(changes)
    data["assumptions"] = [a for a in data["assumptions"] if a["field"] not in changes]
    brief = Brief(**data)
    run = Run.new(brief, root, data=parent.state.data, as_of=parent.state.as_of, copy_mode=parent.state.copy_mode,
                  drafts=parent.state.drafts, parent_run_id=parent.state.run_id, changes=changes, client=client)
    run.parent = parent
    return run, run.execute()


def approve(run_dir: Path, step_id: str, variant_id: str) -> RunState:
    run = Run.load(run_dir)
    copy_plan = run.out.get("copy_plan") or []
    tc = next((c for c in copy_plan if c.step_id == step_id), None)
    if tc is None:
        raise ValueError(f"no step {step_id!r} in this run: {[c.step_id for c in copy_plan]}")
    if variant_id not in {v.variant_id for v in tc.variants}:
        raise ValueError(f"no variant {variant_id!r} for {step_id}: {[v.variant_id for v in tc.variants]}")
    run.state.approvals[step_id] = variant_id
    run.save()
    return run.state
