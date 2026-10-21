"""Today's priorities, tracking health, the retention curve, and the question bank."""
import json
import re
from pathlib import Path

import pytest

from tests.conftest import ROOT
from agents import cli, priorities, retention, tracking
from agents.brief import load_brief
from scripts import daily, sheets_sync
from tools import question_bank

FIXTURES = str(ROOT / "tests" / "fixtures")
EXAMPLE = ROOT / "briefs" / "examples" / "winback_14d.yaml"


# ----------------------------------------------------------------------------------------------
# priorities
# ----------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ranked(tmp_path_factory):
    out = tmp_path_factory.mktemp("prio")
    return priorities.run(FIXTURES, briefs=[load_brief(EXAMPLE)], live=[], out=out), out


def test_fixes_come_first_and_builds_are_ranked_by_score(ranked):
    r, _ = ranked
    kinds = [o.kind for o in r["top"] + r["others"]]
    assert kinds == sorted(kinds, key=lambda k: k != "fix")             # every fix before every build
    assert r["top"][0].title.startswith("Sunset the unengaged")         # the fixtures' list is RED
    scores = [o.score for o in r["top"] + r["others"] if o.kind == "build"]
    assert scores == sorted(scores, reverse=True) and len(scores) >= 3


def test_every_build_is_sized_from_measured_inputs_and_shows_its_assumptions(ranked):
    r, out = ranked
    for o in [x for x in r["top"] + r["others"] if x.kind == "build"]:
        assert o.people_per_month and o.baseline_rate is not None
        assert o.conversions_low == pytest.approx(o.people_per_month * o.baseline_rate * o.lift_low, rel=0.01, abs=0.06)
        assert o.revenue_low <= o.revenue_high and o.value_label
        assert any("assumption" in c for c in o.confirm)
    text = priorities.render(r)
    assert "(assumed)" in text and "estimates, not forecasts" in text
    winback = next(o for o in r["top"] + r["others"] if o.key == "ridgeview_winback_14d")
    assert winback.brief_path and Path(winback.brief_path).exists()
    assert load_brief(Path(winback.brief_path)).flow_type == "winback"
    assert any("'newsletter'" in w and "observed sends, not a test" in w for w in winback.why)


def test_a_live_flow_is_not_suggested_again():
    b = load_brief(EXAMPLE)
    r = priorities.run(FIXTURES, briefs=[b], live=[b])
    keys = {o.key for o in r["top"] + r["others"]}
    assert "ridgeview_winback_14d" not in keys and "ridgeview_activation" in keys


def test_value_and_lift_can_be_supplied_and_are_checked():
    r = priorities.run(FIXTURES, briefs=[load_brief(EXAMPLE)], live=[], value=100.0, lift_low=0.05, lift_high=0.05, top=10)
    build = next(o for o in r["top"] if o.kind == "build")
    assert build.value_label == "SUPPLIED" and build.revenue_low == build.revenue_high
    with pytest.raises(ValueError):
        priorities.run(FIXTURES, briefs=[load_brief(EXAMPLE)], live=[], lift_low=0.3, lift_high=0.1)


def test_a_red_domain_is_a_fix_that_outranks_everything():
    domain = {"gate": {"verdict": "RED", "reasons": ["authentication_state is RED"]},
              "findings": [{"severity": "critical", "title": "No DMARC record"}]}
    r = priorities.run(FIXTURES, briefs=[load_brief(EXAMPLE)], live=[], deliverability={"example.org": domain})
    assert r["top"][0].key == "fix_domain_example.org" and "No DMARC record" in " ".join(r["top"][0].why)


def test_priorities_reach_the_daily_summary_and_sheet(tmp_path):
    st = daily.run("demo", briefs_dir=tmp_path / "none", out=tmp_path / "out", dry_run=True)
    assert st["priorities"]["status"] == "ok" and len(st["priorities"]["top"]) == 4      # the fix, then three builds
    summary = (tmp_path / "out" / "summary.md").read_text()
    assert summary.index("Today's priorities") < summary.index("Engagement tiers")
    assert "### Fix first: Sunset the unengaged" in summary and "### 3. " in summary
    assert "## Tracking health" in summary and st["tracking"]["status"] == "ok"
    rows = (tmp_path / "out" / "sheets" / "priorities.csv").read_text().splitlines()
    assert rows[0].startswith("as_of,rank,kind,title") and len(rows) == 5
    assert set(sheets_sync.TABS["Priorities"]["header"]) >= {"rank", "title", "why", "next_step"}


# ----------------------------------------------------------------------------------------------
# tracking health and retention
# ----------------------------------------------------------------------------------------------

def test_tracking_health_flags_a_stopped_event_and_the_flow_that_needs_it(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    for t in ("users", "orders"):
        (data / f"{t}.csv").write_text((Path(FIXTURES) / f"{t}.csv").read_text())
    lines = (Path(FIXTURES) / "track_events.csv").read_text().splitlines()
    at = lines[0].split(",").index("event_at")
    kept = [ln for ln in lines[1:] if not (",feature_used," in ln and ln.split(",")[at] >= "2026-10-12")]
    (data / "track_events.csv").write_text("\n".join([lines[0]] + kept) + "\n")
    r = tracking.health(str(data), as_of="2026-10-18", briefs=[load_brief(EXAMPLE)])
    fu = next(e for e in r["events"] if e["event"] == "feature_used")
    assert fu["status"] == "stopped" and r["status"] == "problem" and not r["stale"]
    assert [(f["flow"], f["events"]) for f in r["flows_affected"]] == [("ridgeview_winback_14d", ["feature_used where feature = reports"])]
    assert "feature_used stopped" in tracking.render(r)


def test_tracking_health_is_clean_on_the_fixtures_and_lists_off_plan_events():
    r = tracking.health(FIXTURES)
    assert r["status"] == "ok" and {"event": "page_viewed", "last_7d": 3} in r["off_plan"]
    assert {e["event"] for e in r["events"] if e["status"] == "not tracked"} >= {"checkout_started"}


def test_tracking_reads_the_webhook_quarantine(tmp_path):
    q = tmp_path / "events" / "quarantine"
    q.mkdir(parents=True)
    (q / "2026-10-18.ndjson").write_text(json.dumps({"reason": "does not match the tracking plan: properties: 'plan' is a required property"}) + "\n")
    r = tracking.quarantine(tmp_path / "events", today=__import__("datetime").date(2026, 10, 18))
    assert r["quarantined"] == 1 and r["top_reasons"][0][0] == "does not match the tracking plan"


def test_retention_curve_counts_only_observed_weeks_and_only_real_cliffs():
    r = retention.curve(FIXTURES, weeks=8)
    pts = r["segments"]["all"]["points"]
    assert [p["week"] for p in pts] == list(range(9))
    assert all(a["observed"] >= b["observed"] for a, b in zip(pts, pts[1:]))      # later weeks are never over-counted
    by = retention.curve(FIXTURES, weeks=8, by="plan")
    assert set(by["segments"]) <= {"growth", "scale", "starter", "(none)"} and by["segments"]
    with pytest.raises(ValueError):
        retention.curve(FIXTURES, by="email")


def _steps(changes, n=400, churn=20):
    """Paired steps for week 1.. with a given net change in points (negative is a drop) and some two-way churn."""
    out = []
    for k, c in enumerate(changes, 1):
        moved = round(abs(c) * n)
        lost, gained = (churn + moved, churn) if c < 0 else (churn, churn + moved)
        out.append({"week": k, "n": n, "lost": lost, "gained": gained})
    return out


def test_noise_is_not_a_cliff_and_a_real_drop_is():
    assert retention._shape(_steps([0.01, -0.01, 0.0, 0.01, -0.01, 0.0]))["cliff_week"] is None
    steep = retention._shape(_steps([-0.05, -0.25, -0.01, 0.0, -0.01]))
    assert steep["cliff_week"] == 2 and steep["cliff_drop"] == pytest.approx(0.25, abs=0.01)
    bounced = retention._shape(_steps([0.0, -0.2, 0.15, 0.0]))         # a drop undone the next week is not a cliff
    assert bounced["cliff_week"] is None


# ----------------------------------------------------------------------------------------------
# the question bank
# ----------------------------------------------------------------------------------------------

def test_every_question_reaches_its_job_and_the_doc_is_current():
    bank = question_bank.load()
    assert question_bank.check(bank) == []
    assert sum(len(v) for v in bank.values()) >= 25 and all(len(v) >= 4 for v in bank.values())
    assert (ROOT / "docs" / "QUESTION_BANK.md").read_text() == question_bank.render(bank), "run python3 tools/question_bank.py --write"


def test_every_lcm_command_in_the_bank_routes_the_same_way():
    for items in question_bank.load().values():
        for q in items:
            if "job" in q["route"]:
                said = re.search(r'lcm "([^"]+)"', q["command"]).group(1)
                assert cli.classify(said) == q["route"]["job"], q["id"]


def test_new_jobs_ask_for_what_they_need(capsys):
    assert cli.main(["are our events firing correctly?"]) == 2 and "--data" in capsys.readouterr().out
    assert cli.route("when do customers stop using the product, by plan?", {"data": "x"}).args["by"] == "plan"
    assert cli.route("what are my top 5 priority builds?", {"data": "x"}).args["top"] == 5
    assert cli.main(["what is our lifecycle program worth next year?"]) == 0
