"""The ROI memo's figures come from engine/measurement/roi.py; if either changes, they must change together."""
import json
import subprocess
import sys

from tests.conftest import ROOT
from engine.measurement import roi as roi_model

MEMO = (ROOT / "docs" / "ROI_MEMO.md").read_text(encoding="utf-8")


def test_memo_figures_are_the_models_defaults():
    r = roi_model.run(roi_model.Inputs())
    c, t, e, inc, lg = r["cost_of_a_marketer"], r["time"], r["engine_cost"], r["inbox_incident"], r["legal_exposure"]
    usd = lambda v: f"${v:,.0f}"  # noqa: E731
    expected = [usd(c["loaded"]), usd(c["hour"]), f"{t['hours_returned']:,.0f} hours", f"{t['share_of_year']:.0%}".replace("%", " percent"),
                usd(t["value"]), usd(e["per_marketer_annual"]), usd(e["per_marketer_one_time"]), usd(e["annual"]), usd(e["one_time"]),
                f"{e['value_per_dollar']:.0f} dollars", f"{e['breakeven_hours_per_marketer']:.0f} hours a year",
                usd(inc["lost_without"]), usd(inc["lost_with"]), usd(inc["avoided"]), usd(inc["true_revenue_per_day_in_spam"]),
                "$2.5 million to $7.5 million", usd(3 * t["value"]), f"{3 * t['hours_returned']:,.0f} hours"]
    missing = [x for x in expected if x not in MEMO]
    assert not missing, missing
    assert lg["tcpa_low"] == 2_500_000 and lg["tcpa_high"] == 7_500_000


def test_every_task_row_in_the_memo_matches_the_model():
    for name, n, before, after, _ in roi_model.TASKS:
        assert f"| {int(n * (before - after))} |" in MEMO, name


def test_the_model_runs_with_other_inputs():
    out = subprocess.run([sys.executable, "engine/measurement/roi.py", "--pay", "130000", "--team", "4", "--json"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    r = json.loads(out)
    assert r["inputs"]["team"] == 4 and r["cost_of_a_marketer"]["pay"] == 130000
    assert r["engine_cost"]["per_marketer_annual"] < roi_model.run(roi_model.Inputs())["engine_cost"]["per_marketer_annual"]
