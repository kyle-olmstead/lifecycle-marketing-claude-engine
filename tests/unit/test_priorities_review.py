"""Regression tests for the independent review of 2.3: each test names the finding it pins."""
import csv
import json
import random
import re
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from tests.conftest import ROOT
from engine.cli import main as cli
from engine.agents import cohort_analyst, priorities, retention, tracking
from engine.brief import load_brief
from engine.agents.campaign_architect import outcome_window
from engine.models import Brief

FIXTURES = ROOT / "tests" / "fixtures" / "ridgeview_small" / "data"   # the 400-person world the numbers below are pinned to
EXAMPLE = ROOT / "examples" / "brands" / "ridgeview_saas" / "briefs" / "winback_14d.yaml"


def _copy(tmp: Path, tables=("users", "orders", "track_events", "message_events"), keep=None) -> Path:
    """A copy of the fixtures (labeled LIVE PULL, not DEMO DATA), optionally without some tables or filtering events."""
    tmp.mkdir(parents=True, exist_ok=True)
    for t in tables:
        src = FIXTURES / f"{t}.csv"
        if t == "track_events" and keep is not None:
            lines = src.read_text().splitlines()
            at = lines[0].split(",").index("event_at")
            (tmp / f"{t}.csv").write_text("\n".join([lines[0]] + [ln for ln in lines[1:] if keep(ln, ln.split(",")[at])]) + "\n")
        else:
            shutil.copy(src, tmp / f"{t}.csv")
    return tmp


def _brief(**changes) -> Brief:
    return Brief.model_validate({**load_brief(EXAMPLE).model_dump(), **changes})


# ----------------------------------------------------------------------------------------------
# tracking health
# ----------------------------------------------------------------------------------------------

def test_h1_a_total_outage_reads_as_stale_and_stopped_and_today_quarantine_counts(tmp_path):
    data = _copy(tmp_path / "data", keep=lambda ln, at: at < "2026-09-20")
    q = tmp_path / "events" / "quarantine"
    q.mkdir(parents=True)
    for d in ("2026-10-19", "2026-10-20"):
        (q / f"{d}.ndjson").write_text("\n".join(json.dumps({"reason": "track call without an event name"}) for _ in range(50)) + "\n")
    r = tracking.health(str(data), briefs=[load_brief(EXAMPLE)], events_dir=tmp_path / "events", today=date(2026, 10, 20))
    assert r["as_of"] == "2026-10-19" and r["status"] == "problem"
    assert r["stale"] == {"newest_event": "2026-09-19", "days_behind": 30}
    assert next(e for e in r["events"] if e["event"] == "session_started")["status"] == "stopped"
    assert r["quarantine"]["quarantined"] == 100
    assert "No new events since 2026-09-19" in tracking.render(r)


def test_m8_a_renamed_property_puts_the_flow_at_risk_even_when_the_event_still_arrives(tmp_path):
    data = _copy(tmp_path / "data")
    text = (data / "track_events.csv").read_text().splitlines()
    at = text[0].split(",").index("event_at")
    out = [text[0]] + [ln.replace("reports", "report_builder") if ln.split(",")[at] >= "2026-10-01" else ln for ln in text[1:]]
    (data / "track_events.csv").write_text("\n".join(out) + "\n")
    r = tracking.health(str(data), as_of="2026-10-18", briefs=[load_brief(EXAMPLE)])
    assert next(e for e in r["events"] if e["event"] == "feature_used")["status"] == "ok"
    assert r["status"] == "problem" and r["flows_affected"][0]["events"] == ["feature_used where feature = reports"]
    assert r["flows_affected"][0]["detail"][0]["status"] == "stopped"


def test_m9_a_quiet_week_for_a_rare_event_is_not_called_stopped():
    b = load_brief(EXAMPLE)
    r = tracking.health(str(FIXTURES), as_of="2026-09-06", briefs=[b])
    assert r["status"] == "ok" and r["flows_affected"] == []
    assert next(e for e in r["events"] if e["event"] == "activation_reached")["status"] in ("ok", "too few to judge")
    days = [(date(2026, 10, 18) - timedelta(days=k)).isoformat() for k in range(0, 40, 3)]
    assert all(tracking.health(str(FIXTURES), as_of=d, briefs=[b])["status"] == "ok" for d in days)


def test_l6_order_completed_is_tracked_from_the_orders_table():
    b = _brief(qualifying_event={"event": "signed_up"}, core_conversion_event={"event": "order_completed", "label": "placed an order"})
    r = tracking.health(str(FIXTURES), as_of="2026-10-18", briefs=[b])
    oc = next(e for e in r["events"] if e["event"] == "order_completed")
    assert oc["source"] == "orders" and oc["status"] == "ok" and r["flows_needing_untracked_events"] == []
    curve = retention.curve(str(FIXTURES), brief=b, weeks=4)
    assert any(p["active"] for p in curve["segments"]["all"]["points"])


def test_l11_bad_quarantine_lines_are_counted_not_crashed_on(tmp_path):
    q = tmp_path / "quarantine"
    q.mkdir()
    (q / "2026-10-18.ndjson").write_text('{"reason": null}\n[1, 2]\nnot json\n\n{"reason": "bad: x"}\n')
    r = tracking.quarantine(tmp_path, today=date(2026, 10, 18))
    assert r["quarantined"] == 4 and dict(r["top_reasons"]) == {"no reason recorded": 3, "bad": 1}


# ----------------------------------------------------------------------------------------------
# priorities
# ----------------------------------------------------------------------------------------------

def test_h2_newsletter_evidence_comes_from_real_sends_or_not_at_all(tmp_path):
    b = load_brief(EXAMPLE)
    r = cohort_analyst.blast_vs_trigger(b, str(FIXTURES))
    real = {"2026-08-10", "2026-08-24", "2026-09-07"}           # the fixtures' biweekly Monday sends with a full window
    assert r["observed"] and r["campaign"] == "newsletter" and {x["date"] for x in r["blasts"]} == real
    back = cohort_analyst.blast_vs_trigger(b, str(FIXTURES), as_of="2026-08-31")
    assert back["as_of"] == "2026-08-31" and all(x["date"] <= "2026-08-01" for x in back["blasts"])
    no_msgs = _copy(tmp_path / "nomsg", tables=("users", "orders", "track_events"))
    sim = cohort_analyst.blast_vs_trigger(b, str(no_msgs))
    assert not sim["observed"] and sim["source"].startswith("simulated")
    pr = priorities.run(str(no_msgs), briefs=[b], live=[], top=10)
    wb = next(o for o in pr["top"] if o.key == "ridgeview_winback_14d")
    assert not any("newsletter" in w or "send" in w for w in wb.why)
    assert any("no message_events export" in n for n in pr["notes"])                        # L12


def test_m2_lcm_passes_the_value_through():
    job = cli.route("what are my top 3 priority builds today and why?", {"data": str(FIXTURES), "value": 120.0})
    assert job.args["value"] == 120.0


def test_m3_a_supplied_brief_does_not_hide_the_live_flows(tmp_path, monkeypatch, capsys):
    root = tmp_path / "repo"
    (root / "briefs" / "active").mkdir(parents=True)
    shutil.copy(EXAMPLE, root / "briefs" / "active" / "winback.yaml")
    monkeypatch.setenv("LCM_WORKSPACE", str(root))
    assert priorities._cli(["--data", str(FIXTURES), "--brief", str(EXAMPLE), "--json", "--out", str(tmp_path / "out"), "--top", "10"]) == 0
    r = json.loads(capsys.readouterr().out)
    keys = {o["key"] for o in r["top"] + r["others"]}
    assert "ridgeview_winback_14d" not in keys and "ridgeview_activation" in keys
    assert not any("no live flows" in n for n in r["notes"])


def test_m4_no_post_purchase_build_when_a_flow_already_works_on_orders():
    b = _brief(product_name="Ridgeview", churn_threshold_days=30, touches=3,
               qualifying_event={"event": "order_completed", "label": "placed a first order"},
               core_conversion_event={"event": "order_completed", "label": "placed an order"})
    for live in ([b], []):
        r = priorities.run(str(FIXTURES), briefs=[b], live=live, top=10)
        assert "post_purchase_second_order" not in {o.key for o in r["top"] + r["others"]}
        assert any("no separate post-purchase build" in n for n in r["notes"])


def test_m5_two_core_actions_for_one_product_get_their_own_keys_and_briefs(tmp_path):
    exports = _brief(core_conversion_event={"event": "feature_used", "where": {"feature": "exports"}, "label": "exported data"})
    r = priorities.run(str(FIXTURES), briefs=[load_brief(EXAMPLE), exports], live=[], top=10, out=tmp_path)
    builds = [o for o in r["top"] + r["others"] if o.brief_path]
    assert len({o.key for o in builds}) == len(builds) == 4
    for o in builds:
        where = load_brief(Path(o.brief_path)).core_conversion_event.where["feature"]
        assert o.key.endswith(f"_feature_used_{where}")


def test_m6_the_why_states_the_window_the_baseline_was_measured_over():
    b = _brief(churn_threshold_days=30)
    r = priorities.run(str(FIXTURES), briefs=[b], live=[], top=10)
    wb = next(o for o in r["top"] + r["others"] if o.key == "ridgeview_winback_30d")
    assert f"within {outcome_window(b)} days" in wb.why[0] and outcome_window(b) == 60
    act = next(o for o in r["top"] + r["others"] if o.key == "ridgeview_activation")
    assert "within 14 days" in act.why[0]


def test_m7_ready_briefs_carry_todays_gate_and_no_borrowed_goal(tmp_path):
    base = _brief(goal="Build a 5-touch automated win-back flow for Ridgeview", sending_gate="GREEN")
    r = priorities.run(str(FIXTURES), briefs=[base], live=[], out=tmp_path)
    assert r["sending_gate"] == "RED"                                  # the fixtures' list is RED on engagement
    for o in [x for x in r["top"] + r["others"] if x.brief_path]:
        made = load_brief(Path(o.brief_path))
        assert made.goal is None and made.sending_gate == "RED"
    assert priorities.todays_gate(None, None) == "UNKNOWN"


def test_l1_one_stale_feed_is_one_fix():
    audits = [{"flow": f, "anomalies": [{"metric": "data_freshness", "reason": "newest event is 9 days old"}]} for f in ("a", "b", "c")]
    fixes, _ = priorities._fix_candidates(audits, None, None)
    assert [f.key for f in fixes] == ["fix_data"] and "a, b, c" in fixes[0].why[1]


def test_l3_l4_dollars_follow_the_printed_formula_and_proof_includes_the_window():
    r = priorities.run(str(FIXTURES), briefs=[load_brief(EXAMPLE)], live=[], top=10)
    for o in [x for x in r["top"] + r["others"] if x.revenue_low is not None]:
        assert o.revenue_low == round(o.people_per_month * o.baseline_rate * o.lift_low * o.value_per_conversion)
    months = lambda s: float(re.search(r"about (\d+) months", s).group(1))  # noqa: E731
    assert months(priorities._measurable(800, 0.2, 0.25, 60)) > months(priorities._measurable(800, 0.2, 0.25, 0))


def test_l10_domains_reach_lcm_daily_and_priorities(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LCM_DOMAINS", "example.org")
    assert cli.route("what broke overnight?", {"data": "x"}).args["domains"] == "example.org"
    red = {"gate": {"verdict": "RED", "reasons": ["authentication_state is RED"]}, "findings": [{"severity": "critical", "title": "No DMARC record"}]}
    seen = []
    monkeypatch.setattr(priorities, "check_domains", lambda ds: seen.extend(ds) or {d: red for d in ds})
    assert priorities._cli(["--data", str(FIXTURES), "--json", "--out", str(tmp_path)]) == 0
    r = json.loads(capsys.readouterr().out)
    assert seen == ["example.org"] and r["top"][0]["key"] == "fix_domain_example.org"


# ----------------------------------------------------------------------------------------------
# the router
# ----------------------------------------------------------------------------------------------

ROUTES = [
    ("plan a win-back campaign for our high-priority customers", "plan"),
    ("build a nurture sequence for open opportunities in HubSpot", "plan"),
    ("create a re-engagement flow for closed-lost opportunities", "plan"),
    ("design an onboarding flow for priority accounts", "plan"),
    ("Plan a welcome flow that highlights our top 3 features", "plan"),
    ("I need a 5-touch win-back flow with a 15% holdout for users inactive 30 days, Klaviyo", "plan"),
    ("win-back for lapsed subscribers with a 10% holdout", "plan"),
    ("nurture sequence for dormant leads", "plan"),
    ("onboarding sequence for users who drop off in the funnel", "plan"),
    ("re-engage customers who stop using reports", "plan"),
    ("is a welcome series worth it for a B2B product?", "plan"),
    ("what are my top 3 priority builds today and why?", "priorities"),
    ("what should I build next?", "priorities"),
    ("which flow should I build first?", "priorities"),
    ("give me campaign ideas", "priorities"),
    ("where are we leaving money on the table?", "priorities"),
    ("what holdout size should I use?", "size"),
    ("what's the ROI of the welcome flow?", "readout"),
    ("did the win-back flow actually work?", "readout"),
    ("read the holdout results in results/winback_q3.json", "readout"),
    ("is our win-back flattening the churn curve?", "retention"),
    ("show me our retention curve by country", "retention"),
    ("what is our lifecycle program worth next year?", "roi"),
    ("did the last release break our tracking?", "tracking"),
    ("Which flows should we retire?", None),
    ("Which compliance questions are open across our flows?", None),
]


@pytest.mark.parametrize("text,job", ROUTES)
def test_m1_m12_the_router(text, job):
    assert cli.classify(text) == job


def test_m12_a_question_lcm_cannot_answer_points_to_claude(capsys):
    assert cli.main(["Which flows should we retire?"]) == 0
    assert "Ask it in a Claude conversation" in capsys.readouterr().err
    doc = (ROOT / "docs" / "QUESTION_BANK.md").read_text()
    assert "After `lcm`, type the plain version" in doc and "either works" in doc


def test_l9_segments_by_country_and_short_curves():
    assert cli.route("show me the retention curve by country segment", {"data": "x"}).args["by"] == "country"
    r = retention.curve(str(FIXTURES), weeks=3, by="plan")
    assert r["segments"] and all(len(s["points"]) == 4 for s in r["segments"].values())


# ----------------------------------------------------------------------------------------------
# the retention curve
# ----------------------------------------------------------------------------------------------

def _mix(folder: Path) -> Path:
    """300 old signups active 20% of weeks and 900 recent ones active 60%: nobody's retention declines."""
    rng = random.Random(11)
    folder.mkdir(parents=True)
    users, events = [], []
    for i in range(1200):
        old = i < 300
        signup = (datetime(2026, 3, 1) if old else datetime(2026, 9, 1)) + timedelta(days=i % 28 if old else i % 14)
        users.append([f"u{i}", f"u{i}@example.com", signup.isoformat(sep=" "), "US", "starter", "customer", "", ""])
        for k in range(0, 34):
            day = signup + timedelta(days=7 * k + 2)
            if day.date() <= date(2026, 10, 18) and rng.random() < (0.2 if old else 0.6):
                events.append([f"e{len(events)}", f"u{i}", "", "feature_used", day.isoformat(sep=" "), json.dumps({"feature": "reports"})])
    for name, header, rows in (("users", ["user_id", "email", "created_at", "country", "plan", "lifecycle_stage", "email_consent_at", "sms_consent_at"], users),
                               ("track_events", ["event_id", "user_id", "anonymous_id", "event_name", "event_at", "properties"], events)):
        with open(folder / f"{name}.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
    return folder


def test_m10_a_change_in_cohort_mix_is_not_a_cliff(tmp_path):
    r = retention.curve(str(_mix(tmp_path / "mix")), as_of="2026-10-18", weeks=6)
    pts = r["segments"]["all"]["points"]
    assert pts[0]["rate"] - pts[-1]["rate"] > 0.15                     # the pooled curve falls: the mix changes...
    assert r["segments"]["all"]["cliff_week"] is None                  # ...but no one's retention does


def test_m11_flat_curves_rarely_show_a_cliff():
    rng = random.Random(7)
    false = 0
    for _ in range(200):
        prev = [rng.random() < 0.3 for _ in range(300)]
        steps = []
        for k in range(1, 13):
            cur = [rng.random() < 0.3 for _ in range(300)]
            steps.append({"week": k, "n": 300, "lost": sum(a and not b for a, b in zip(prev, cur)),
                          "gained": sum(b and not a for a, b in zip(prev, cur))})
            prev = cur
        false += retention._shape(steps)["cliff_week"] is not None
    assert false / 200 <= 0.05


def test_l8_a_steady_decline_or_one_small_last_step_is_not_a_floor():
    from tests.unit.test_priorities_and_questions import _steps
    assert retention._shape(_steps([-0.015] * 8))["floor_week"] is None
    assert retention._shape(_steps([-0.2, -0.1, -0.08, -0.01]))["floor_week"] is None
    assert retention._shape(_steps([-0.2, -0.1, 0.005, -0.005, 0.0]))["floor_week"] == 2


def test_l7_weeks_before_the_export_starts_are_not_counted(tmp_path):
    data = _copy(tmp_path / "late", tables=("users", "orders", "track_events"), keep=lambda ln, at: at >= "2026-06-01")
    r = retention.curve(str(data), weeks=4)
    full = retention.curve(str(FIXTURES), weeks=4)
    p, f = r["segments"]["all"]["points"], full["segments"]["all"]["points"]
    assert all(a["observed"] < b["observed"] for a, b in zip(p, f))   # only people whose weeks the export covers
