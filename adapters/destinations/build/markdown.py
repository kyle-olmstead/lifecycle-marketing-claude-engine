"""BUILD.md: the build pack in plain English, for the person who builds the flow (or reviews what the API will create)."""
from __future__ import annotations

from typing import List

from adapters.destinations.build.model import BuildPack

MARK = {"pass": "pass", "warn": "WARN", "fail": "FAIL"}


def _cell(text: str) -> str:
    return str(text).replace("|", "/").replace("\n", " ")


def render(p: BuildPack) -> str:
    t = p.trigger
    L: List[str] = [f"# Build pack: {p.flow} on {p.platform_label}", "",
                    f"**Status: {p.status}** | mode: {p.mode.replace('_', ' ')} | run {p.run_id or '-'} | slug `{p.slug}`", ""]
    if p.blocked_reasons:
        L += ["## Blocked", ""] + [f"- {r}" for r in p.blocked_reasons] + [""]
    L += ["## What is automatic", "", p.automatic + ".", "",
          "Nothing is created or sent without `lcm build --create --approve` and the platform's key in the environment; "
          "without them the command prints what it would send.", ""]
    L += ["## Trigger", "", f"- Type: **{t.type}**" + (f" on `{t.event}`" if t.event else ""), f"- {t.summary}"]
    L += [f"- Entry filter: {f}" for f in t.filters]
    L += [f"- Checked before every step: {f}" for f in t.flow_filters]
    L += [f"- Exit: {e}" for e in t.exit_criteria]
    L += [f"- Re-entry: {t.re_entry.get('setting', t.re_entry.get('plan'))} (plan: {t.re_entry.get('plan')})", f"- Holdout: {t.holdout}"]
    if t.confidence != "VERIFIED":
        L.append(f"- Confidence: {t.confidence} (the trigger type is verified on the vendor's documentation; this recipe is not)")
    if t.warehouse_alternative:
        L.append(f"- {t.warehouse_alternative}")
    if t.native_alternative:
        L.append(f"- {t.native_alternative}")
    L.append("")
    if t.segment:
        s = t.segment
        L += [f"### Segment to create: {s.name}", "", f"{s.kind}; {s.refresh}. Confidence: {s.confidence}.", ""]
        L += [f"1. {c}" for c in s.conditions] + [""]
        if s.data_prerequisites:
            L += [f"Needs: {d}" for d in s.data_prerequisites] + [""]
    L += ["## Steps", "", "Waits are relative to the previous step, in the platform's smallest delay unit.", "",
          "| # | Step | Action | Wait | Channel | Copy | Condition or split |", "|---|---|---|---|---|---|---|"]
    for n, s in enumerate(p.steps, 1):
        wait = f"{s.wait.value} {s.wait.unit} ({s.wait.label})" if s.wait else "-"
        copy = ", ".join(f"copy/{f}" for f in (s.content.variants.get(s.content.chosen, []) if s.content and s.content.chosen else [])
                         if f.endswith((".html", ".json"))) or "-"
        cond = (f"{s.split.kind}: {s.split.condition}; yes: {s.split.yes}; no: {s.split.no}" if s.split else (s.condition or s.note or "-"))
        if s.window:
            cond += f"; window: {s.window}"
        L.append(f"| {n} | `{s.id}`{' (by hand)' if s.manual else ''} | {_cell(s.platform_action)} | {_cell(wait)} | {s.channel or '-'} | "
                 f"{_cell(copy)} | {_cell(cond)} |")
    L.append("")
    st = p.settings
    L += ["## Goal, exit and settings", "", f"- Goal: {st.goal}", f"- Exit: {st.exit}", f"- Re-entry: {st.re_entry}",
          f"- Status when created: {st.status_on_create}"]
    L += [f"- Send window: {w}" for w in st.send_windows]
    L += [f"- Frequency: {f}" for f in st.frequency]
    L.append("")
    L += ["## Capability checks", "", "| Check | Result | Detail | Confidence |", "|---|---|---|---|"]
    L += [f"| `{c.id}` | {MARK[c.status]}{' (hard)' if c.hard else ''} | {_cell(c.detail)} | {c.confidence} ([source]({c.source})) |"
          if c.source.startswith("http") else f"| `{c.id}` | {MARK[c.status]} | {_cell(c.detail)} | {c.confidence} ({c.source}) |"
          for c in p.checks]
    L.append("")
    if p.artifacts:
        L += ["## Artifacts", "", "| File | What | Schema | Valid | Unverified fields |", "|---|---|---|---|---|"]
        for a in p.artifacts:
            what = f"{a.method} {a.url}" if a.method else a.kind.replace("_", " ")
            L.append(f"| `build/{a.path}` | {_cell(what)} | {a.schema_ref or '-'} | "
                     f"{'-' if a.valid is None else ('yes' if a.valid else 'NO: ' + _cell('; '.join(a.errors[:2])))} | "
                     f"{_cell('; '.join(a.unverified_fields)) or 'none'} |")
        L.append("")
    if p.inputs_required:
        L += ["## Inputs to supply", "", "Give each with `--id NAME=VALUE` (or esp.options.ids in lmm.yaml); `--create` resolves the rest.", ""]
        L += [f"- `{k}`: {v}" for k, v in sorted(p.inputs_required.items())] + [""]
    if p.platform in ("braze", "customerio", "iterable"):
        L += ["## Click by click", ""]
        L.append(f"1. Create {p.platform_label}'s {('Canvas' if p.platform == 'braze' else 'campaign' if p.platform == 'customerio' else 'journey')} "
                 f"named `LCM {p.flow}` and keep it {p.settings.status_on_create}.")
        L.append(f"2. Entry: {t.type}" + (f" on {t.event}" if t.event else "") + f". {t.summary}.")
        n = 3
        for s in p.steps:
            wait = f"after {s.wait.label}, " if s.wait else ""
            if s.split:
                L.append(f"{n}. {wait}add {s.platform_action}: {s.split.condition} goes to {s.split.yes}; everyone else to {s.split.no}.")
            elif s.kind == "update_profile":
                L.append(f"{n}. {wait}{s.platform_action}: {s.note}.")
            else:
                files = ", ".join(f"copy/{f}" for f in (s.content.variants.get(s.content.chosen, []) if s.content and s.content.chosen else []))
                L.append(f"{n}. {wait}add {s.platform_action} `{s.id}`" + (f" ({s.condition})" if s.condition else "") +
                         (f", content from {files}" if files else "") + (f"; {s.window}" if s.window else "") + ".")
            n += 1
        L.append(f"{n}. Exit: " + "; ".join(t.exit_criteria) + f". Re-entry: {p.settings.re_entry}.")
        L.append("")
    L += ["## By hand", ""] + [f"1. {m}" for m in p.manual_steps] + [""]
    if p.unverified:
        L += ["## Relied on, not verified", "", "Test these in a sandbox account before the first live send.", ""]
        L += [f"- {f.confidence}: {f.fact} ([source]({f.source}))" if f.source.startswith("http") else f"- {f.confidence}: {f.fact} ({f.source})"
              for f in p.unverified]
        L.append("")
    return "\n".join(L)
