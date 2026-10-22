---
id: lmm.measurement.test_hypothesis_matrix
version: 3.0.0
type: framework
tools: [lcm size, lcm experiments]
---

# Test Hypothesis Matrix

A test backlog is a list of questions, each worth some amount to answer, each costing audience and
time. This framework turns ideas into falsifiable hypotheses, kills the ones the volume cannot
answer, and orders the rest by the value of what the team will learn. It also enforces the change
budget: a program that tests everything at once can read nothing.

## 1. Write each idea as a falsifiable hypothesis

```text
Because <evidence or observed behavior>,
we believe <change> for <segment> at <position in the flow>
will move <primary metric> by at least <minimum effect worth acting on>,
measured against <control or holdout> over <window>.
It is falsified if <specific result>.
Guardrails: <metrics that must not get worse>.
```

"Variant B is more direct" is not a hypothesis. "Naming the purchased item in the subject will raise
second-order click rate among first-time buyers by at least 15%, because it reads as a person
following up; falsified if the item-name arm's click rate is equal or lower" is.

## 2. The matrix

| Field | What goes in it | Rule |
|---|---|---|
| ID | short code | stable for the life of the test |
| Flow and position | which message, which step | one position per test |
| Hypothesis | the full statement above | must contain a falsifying result |
| Mechanism | the behavioral reason it should work | honest mechanisms only; no fake urgency or scarcity |
| Primary metric | clicks, conversions, replies, revenue per recipient | never opens |
| Baseline | from the client's own history | labeled LIVE PULL or ESTIMATE |
| Minimum effect worth acting on | absolute or relative | set by the business before sizing |
| Required sample | `lcm size` output | per arm and total |
| Time to read | sample divided by eligible weekly volume | over one quarter means redesign or drop |
| Readable? | yes / directional only / no | "no" leaves the backlog |
| Value if true | what changes and what it is worth, as a range | labeled ESTIMATE, with the arithmetic |
| Cost | audience withheld, build time, risk | include holdout cost for holdout tests |
| Priority score | see section 3 | computed, not argued |
| Decision rule | ship / keep control / inconclusive plan | written before launch |
| Status | proposed / sized / running / read / archived | one owner |

## 3. Prioritize by the value of the answer

```text
priority = P(hypothesis true) x value if true x share of program reached
           ---------------------------------------------------------------
                         weeks to read x (1 + build cost in weeks)
```

- P(true) is a calibrated guess (0.1 to 0.5 for most ideas); write down why.
- Value if true is a range; use the low end.
- Tests that cannot read inside a quarter score zero regardless of value. Redesign them (a bigger
  change, a pooled test across sends, a different metric) or drop them.
- Ties go to the test closer to the money: post-purchase and second-order before welcome.

## 4. Change budget

| Cadence | Tests that may start |
|---|---|
| Weekly | none, except in response to a deliverability flag |
| Monthly | up to three substantive changes across the program, including tests |
| Quarterly | structural tests, after the quarterly review |

Two tests on overlapping audiences at the same time contaminate each other unless they are
explicitly crossed and sized for it. Default: one test per audience at a time; `lcm experiments check` finds
overlaps in the registry.

## 5. Example backlog

A filled matrix for a second-order flow, including a dropped idea and why:
`prompts/examples/test_hypothesis_matrix_backlog.md`. Read it only when a model answer would help.

## 6. After the read

- Read once, at the planned sample. Check the sample ratio first.
- Record the result whichever way it went, with the interval, in the matrix.
- A result that cannot be distinguished from zero is "inconclusive", not "no effect" and not
  "trending".
- Ship winners into the flow's control and re-baseline; the next test compares against the new
  control.
