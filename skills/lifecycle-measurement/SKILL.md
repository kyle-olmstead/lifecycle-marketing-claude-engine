---
name: lifecycle-measurement
description: >
  Use when the user says "how much did this flow drive", "is this significant", "how big a sample
  do I need", "power calculation", "MDE", "holdout", "control group", "attributed revenue", "did it
  actually work", "read this test", "prioritize our test backlog", or is about to report a lifecycle
  result to a client or executive. Sizes tests honestly, reads holdouts with intervals and sample
  ratio checks, and reports attributed, incremental and the gap together.
metadata:
  version: "3.0.0"
---

# Measurement and incrementality

Always read (once per conversation; skip it if you already read it in this conversation):
`${CLAUDE_PLUGIN_ROOT}/prompts/core/principles.md`.

Workspace: `lmm.yaml` `measurement` (randomization unit, global holdout, stopping rule); older setups keep benchmarks,
live flows and holdouts in `lmm-engine/context.xml`.

`lcm` below is the installed command, or `python3 ${CLAUDE_PLUGIN_ROOT}/engine/cli/main.py` (Python 3.12 or later and
`${CLAUDE_PLUGIN_ROOT}/requirements.txt`; `lcm doctor` checks the install). If code cannot run, show the arithmetic
and label every number ESTIMATE and the analysis "unchecked".

## 1. Compute: the engine does the statistics

```text
lcm size --baseline <rate> --mde-rel <lift> [--holdout <share>] [--entries-per-month <n>] [--outcome-window-days <d>]
lcm size mde --baseline <rate> --nc <n> --nt <n>
lcm readout results.json                                   # attributed against incremental, with the gap
lcm readout --xt <n> --nt <n> --xc <n> --nc <n> --expected-holdout <share>   # one holdout, with the sample-ratio check
```

A flow's holdout starts from its flow type's default share (`engine/flows/library.py`). Account-level tests: `lcm
cluster` (design effect). Stopping early: `lcm sequential`. The program: `lcm global-holdout`, `lcm experiments`
(registry and overlaps), `lcm discounts`, `lcm roi`. With an analytics or warehouse connector, pull arm sizes and
outcomes from the transaction object (not platform-attributed revenue) and label them LIVE PULL.

## 2. Judgment

Route (read the one job prompt that matches):
- Sizing a test or holdout: `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/measurement/ab_power_calculator.md`
- Reading a result: `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/measurement/holdout_incrementality.md`
- Ordering a backlog: `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/measurement/test_hypothesis_matrix.md`

A result that looks too good: `${CLAUDE_PLUGIN_ROOT}/prompts/core/diagnostics/result_looks_too_good.md` first.

## Rules

- Never report attributed revenue alone, or a lift percentage on a sample too small to support one.
- Never change the attribution model or window between reporting periods.
- No forecasts: say what will be known and when.
