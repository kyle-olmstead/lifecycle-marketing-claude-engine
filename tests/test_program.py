"""The program simulator: flows that each respect a cap can break it together; the collision rule and the ESP limit decide."""
from engine.brief import parse_text
from engine.policy import program
from engine.policy.contracts import Cap, FrequencyPolicy


def _briefs(**overrides):
    over = {"platform": "klaviyo", **overrides}
    return [parse_text(s, "Northfield", dict(over)) for s in ("A 3-email abandoned cart flow", "browse abandonment within 2 hours",
                                                               "post-purchase series for first-time buyers")]


def test_flows_that_pass_alone_can_break_the_weekly_cap_together():
    r = program.simulate(_briefs())
    alone = [c for c in r["checks"] if c["name"].endswith("alone respects the program caps")]
    assert alone and all(c["status"] == "pass" for c in alone)
    together = next(c for c in r["checks"] if c["name"].startswith("caps hold across flows"))
    assert together["status"] == "warn" and "Smart Sending" in together["detail"]
    assert any(s["without_rule"] for s in r["scenarios"])


def test_preemption_skips_lower_priority_sends_while_a_higher_flow_runs():
    cart, browse, _ = _briefs()
    policy = program.policy_for([cart, browse])
    sends = program.combine([(cart, 0.0, [(1.0, "email"), (24.0, "email"), (72.0, "email")]),
                             (browse, 2.0, [(2.0, "email"), (24.0, "email")])], policy)
    assert [s["flow"] for s in sends] == [cart.slug] * 3       # browse outranked by cart for the whole cart journey
    dropped = program.combine([(cart, 0.0, [(1.0, "email")]), (browse, 0.5, [(2.0, "email")])],
                              policy.model_copy(update={"collision_rule": "drop_lower_priority"}))
    assert [s["flow"] for s in dropped] == [cart.slug]


def test_a_workspace_policy_overrides_the_briefs():
    tight = FrequencyPolicy(caps=[Cap(channels=["email"], max=1, window="P1D")])
    r = program.simulate(_briefs(), tight)
    assert r["policy"]["caps"][0]["window"] == "P1D"
    assert any(c["status"] != "pass" for c in r["checks"])
