"""Every flow in the library plans on every platform: not blocked, a valid flow spec, a passing simulation, and offline
copy with nothing to fill in and no lint or guardrail warning."""
import pytest

from engine.brief import parse_text
from engine.flows.library import FLOWS
from engine.pipeline import orchestrator
from engine.schemas import errors

SENTENCES = {
    "welcome": "a welcome series",
    "activation": "an activation flow for users who signed up but have not created a report",
    "winback": "a win-back for customers who have not ordered in 60 days",
    "cart_abandon": "an abandoned cart flow with email and sms",
    "browse_abandon": "a browse abandonment flow",
    "post_purchase": "a post-purchase flow",
    "replenishment": "a replenishment flow every 30 days",
    "dunning": "a failed payment flow",
    "trial_convert": "a trial conversion flow for a 14 day trial",
    "sunset": "a sunset flow for subscribers inactive 180 days",
}
PLATFORMS = ("klaviyo", "hubspot", "braze", "customerio", "sfmc")


def test_the_sentences_cover_the_library():
    assert set(SENTENCES) == set(FLOWS)


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("flow", sorted(SENTENCES))
def test_every_flow_plans_cleanly_on_every_platform(flow, platform):
    b = parse_text(SENTENCES[flow], "Acme", defaults={}, overrides={
        "platform": platform, "postal_address": "1 Example Way, Springfield, OR 97477", "cta_url": "https://app.acme.example/start"})
    assert b.flow_type == flow
    p = orchestrator.run(b, None, copy_mode="offline")
    assert p.qa.status != "BLOCKED", [(c.name, c.detail[:160]) for c in p.qa.checks if c.status == "fail"]
    assert not errors("flow_spec.schema.json", p.flow.flow_spec)
    sim = [c for c in p.qa.checks if "simulat" in c.name]
    assert sim and all(c.status == "pass" for c in sim), [(c.name, c.detail[:160]) for c in sim]
    copy = [c for c in p.qa.checks if c.name.startswith("copy")]
    assert copy and all(c.status == "pass" for c in copy), [(c.name, c.detail[:200]) for c in copy if c.status != "pass"]
    assert all(tc.variants for tc in p.copy_plan)
