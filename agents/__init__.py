"""The LMM Engine teammate: one campaign brief in, a build-ready execution plan out.

Four agents run in sequence under agents/orchestrator.py:

  cohort_analyst         who enters, on which events, with which data dependencies (and the SQL)
  campaign_architect     the multi-channel logic tree: waits, splits, consent, exits, as a flow spec
  copywriter             direct-response copy variants per touch, rendered in the platform dialect
  experimentation_agent  the persistent holdout, its sizing and the read date

A QA pass then validates the flow spec against schemas/flow_spec.schema.json, simulates journeys
(scripts/campaign_simulator.py), lints every template (tools/template_lint.py) and applies the
deliverability guardrails (agents/guardrails.py).

The split of work follows the rest of the engine: Claude writes language; deterministic code does
anything that must never be guessed (dates, splits, consent, sizing, syntax). Only the copywriter
calls the Claude API, and only when ANTHROPIC_API_KEY is set; everything else runs offline.
"""

__version__ = "2.3.0"
