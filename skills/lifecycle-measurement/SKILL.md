---
name: lifecycle-measurement
description: >
  Use when the user says "how much did this flow drive", "is this significant", "how big a sample
  do I need", "power calculation", "MDE", "holdout", "control group", "attributed revenue", "did it
  actually work", "read this test", "prioritize our test backlog", or is about to report a lifecycle
  result to a client or executive. Sizes tests honestly, reads holdouts with intervals and sample
  ratio checks, and reports attributed, incremental and the gap together.
metadata:
  version: "2.0.0"
---

# Measurement and incrementality

## Step 0: Load context and rules

Read `${CLAUDE_PLUGIN_ROOT}/prompts/core/system_prompt.md` and `lmm-engine/context.xml` (benchmarks with
their basis, live flows and holdouts, the transaction object).

**Before running a tool:** the checks need Python 3.12 or later and the packages in
`${CLAUDE_PLUGIN_ROOT}/requirements.txt`. If an import fails, install them once with
`python3 -m pip install -r ${CLAUDE_PLUGIN_ROOT}/requirements.txt`. If code cannot run here (no
shell, or the install is blocked), still deliver, and write "unchecked" next to every artifact a
tool would have checked; never describe an unchecked artifact as verified.

## Step 1: Pick the module

- Sizing a test or holdout: `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/measurement/ab_power_calculator.md`
- Reading a result: `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/measurement/holdout_incrementality.md`
- Ordering a backlog: `${CLAUDE_PLUGIN_ROOT}/prompts/jobs/measurement/test_hypothesis_matrix.md`

## Step 2: Compute

```text
python3 ${CLAUDE_PLUGIN_ROOT}/engine/measurement/stats.py size --baseline <rate> --mde-rel <lift>
python3 ${CLAUDE_PLUGIN_ROOT}/engine/measurement/stats.py readout --xt <n> --nt <n> --xc <n> --nc <n> --expected-holdout <share>
python3 ${CLAUDE_PLUGIN_ROOT}/engine/measurement/stats.py incrementality readout.json --markdown
```

With an analytics or warehouse connector, pull arm sizes and outcomes from the transaction object
(not platform-attributed revenue) and label them LIVE PULL.

## Rules

- Never report attributed revenue alone.
- Never compute a lift percentage on a sample too small to support one.
- Never change the attribution model or window between reporting periods.
- No forecasts: say what will be known and when.
