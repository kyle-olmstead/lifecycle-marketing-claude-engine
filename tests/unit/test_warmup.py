import pytest

from engine.deliverability.warmup import plan

TIERS = {"engaged_30d": 30000, "engaged_90d": 45000, "engaged_180d": 50000, "unengaged": 220000}


def test_red_gate_produces_no_ladder():
    p = plan(40000, TIERS, "RED")
    assert p["schedule"] == [] and "No warming ladder" in p["summary"]


def test_amber_holds_flat_on_engaged_tiers():
    p = plan(40000, TIERS, "AMBER", current=9000)
    assert {r["volume"] for r in p["schedule"]} == {9000}
    assert all("unengaged" not in r["tiers"] for r in p["schedule"])


def test_green_ladder_doubles_never_uses_unengaged_and_respects_capacity():
    p = plan(40000, TIERS, "GREEN", max_per_week=3)
    vols = [r["volume"] for r in p["schedule"]]
    capacity = sum(int(TIERS[t] * 3 / 7) for t in ("engaged_30d", "engaged_90d", "engaged_180d"))
    assert vols[0] == 500 and max(vols) == 40000 <= capacity
    steps = sorted({v for v in vols})
    assert all(b <= 2 * a for a, b in zip(steps, steps[1:], strict=False))
    assert all("unengaged" not in r["tiers"] for r in p["schedule"])
    assert p["unwarmable_gap"] == 0


def test_target_above_engaged_capacity_is_reported_not_padded():
    p = plan(200000, TIERS, "GREEN")
    assert p["goal_daily_volume"] == p["engaged_daily_capacity"] < 200000
    assert p["unwarmable_gap"] == 200000 - p["engaged_daily_capacity"] and "not warmable" in p["summary"]


def test_invalid_gate_rejected():
    with pytest.raises(ValueError):
        plan(1000, TIERS, "YELLOW")


def test_zero_days_is_rejected_and_zero_engaged_capacity_schedules_nothing():
    with pytest.raises(ValueError):
        plan(40000, TIERS, "GREEN", days=0)
    p = plan(40000, {"engaged_30d": 0, "engaged_90d": 0, "engaged_180d": 0, "unengaged": 9000}, "GREEN")
    assert p["schedule"] == [] and p["goal_daily_volume"] == 0 and "Nothing is scheduled" in p["summary"]


def test_amber_without_a_current_volume_holds_the_new_infrastructure_start_not_tier_capacity():
    # v2.3 held an AMBER domain at the 30-day tier's capacity (12,857 a day here), far above any GREEN start
    p = plan(40000, TIERS, "AMBER")
    assert {r["volume"] for r in p["schedule"]} == {500}
    assert "no current volume was given" in p["summary"]
    green = plan(40000, TIERS, "GREEN")
    assert max(r["volume"] for r in p["schedule"]) <= green["schedule"][0]["volume"]


def test_amber_on_existing_infrastructure_asks_for_todays_volume():
    p = plan(40000, TIERS, "AMBER", new_infrastructure=False)
    assert p["schedule"] == [] and "--current" in p["summary"]
    assert {r["volume"] for r in plan(40000, TIERS, "AMBER", current=7000, new_infrastructure=False)["schedule"]} == {7000}


def test_every_plan_carries_the_machine_readable_abort_rules():
    from engine.deliverability.warmup import ABORT_CRITERIA, ABORT_RULES
    for gate in ("GREEN", "AMBER", "RED"):
        p = plan(40000, TIERS, gate, current=9000)
        assert p["abort_rules"] == ABORT_RULES and p["abort_criteria"] == ABORT_CRITERIA
    assert all(0 <= r["criterion"] < len(ABORT_CRITERIA) for r in ABORT_RULES)
