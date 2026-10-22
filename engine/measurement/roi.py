"""The arithmetic behind docs/ROI_MEMO.md: what the engine is worth to one lifecycle marketer, and to the company.

Every figure in the memo comes from this file, so anyone can rerun it with their own numbers. Defaults are either sourced
(listed in SOURCES) or labeled assumptions; the memo says which is which.

    python3 engine/measurement/roi.py                                   # the memo's numbers
    python3 engine/measurement/roi.py --pay 130000 --team 4             # your pay and team size
    python3 engine/measurement/roi.py --email-revenue 8000000 --incremental-share 0.3 --spam-share 0.25
    python3 engine/measurement/roi.py --json
"""
from __future__ import annotations

if __package__ in (None, ""):  # run as a file (python3 engine/measurement/roi.py): make the repository importable
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

SOURCES = {
    "pay": "BLS Occupational Outlook Handbook: median annual wage for marketing managers, $166,790 in May 2025",
    "benefits_bls": "BLS Employer Costs for Employee Compensation, June 2026: private industry benefits 30.0% of compensation, wages 70.0%",
    "flows_share": "Klaviyo email benchmarks: flows generate about 41% of email revenue from 5.3% of sends",
    "tcpa": "47 U.S.C. 227(b)(3): $500 per violation, up to three times that for willful or knowing violations",
    "can_spam": "FTC CAN-SPAM Act compliance guide: penalties up to $53,088 per violating email",
}

# (task, times a year, hours each today, hours each with the engine, what changes)
TASKS: List[Tuple[str, float, float, float, str]] = [
    ("Specifying a new or rebuilt flow", 12, 22.0, 8.0,
     "audience SQL, logic, copy in the platform's code, QA, test sizing and a legal check arrive drafted and checked; "
     "the marketer confirms assumptions and edits copy. Building it in the platform is the same either way and not counted"),
    ("Revising a live flow", 24, 3.0, 1.0, "one sentence re-runs only what changed; approved copy is kept"),
    ("Routine deliverability checks", 52, 1.5, 0.25, "the morning check replaces logging into Postmaster, DNS tools and bounce reports"),
    ("Investigating an inbox problem", 2, 16.0, 6.0, "the gate names the failing record and writes the fix"),
    ("Monthly flow performance reporting", 12, 6.0, 2.0, "the daily Sheet holds cohort health, benchmarks and anomalies"),
    ("Reading a holdout test", 8, 4.0, 1.0, "credited, true lift and the gap in one readout, with the read labeled"),
    ("List hygiene and sunset segments", 4, 6.0, 1.0, "engagement tiers and the sunset list are rebuilt daily"),
]


@dataclass
class Inputs:
    pay: float = 166_790.0             # sourced: BLS median for marketing managers, May 2025
    benefits_load: float = 0.20        # assumption (conservative); BLS private-industry average implies 0.43
    tools_and_equipment: float = 4_000.0   # assumption: laptop, software seats outside the ESP
    productive_hours: float = 1_800.0  # assumption: 2,080 paid hours less leave and holidays
    team: int = 3                      # assumption: lifecycle marketers sharing the engine
    claude_seat_month: float = 30.0    # assumption: use your plan's price
    api_copy_year: float = 300.0       # assumption: Claude API for copy outside a Claude session, per marketer
    hosting_year: float = 240.0        # assumption: scheduled runs (GitHub Actions minutes), per team
    upkeep_hours_quarter: float = 4.0  # assumption: review rules, thresholds and the sunset list, per team
    setup_engineer_hours: float = 8.0  # assumption: wiring the daily check to the warehouse and Sheets
    engineer_hour: float = 125.0       # assumption: loaded cost of an engineer hour
    setup_marketer_hours: float = 2.0  # assumption: per marketer
    # inbox protection (Part 3): a scenario, every input replaceable
    email_revenue: float = 3_000_000.0  # assumption: annual revenue your email platform attributes to email
    incremental_share: float = 0.38     # the worked readout in examples/stats (fictional): $15,880 of $41,900 was true lift
    spam_share: float = 0.20            # assumption: share of mail sent to spam during an incident
    detect_days_without: float = 21.0   # assumption: days until a slide in results is noticed and traced
    detect_days_with: float = 1.0       # the morning check
    fix_days: float = 7.0               # assumption: days to fix once found (DNS, list, re-warming)
    incidents_year: float = 1.0         # assumption
    # legal exposure (Part 5): an illustration of scale
    sms_send_size: int = 5_000          # assumption: one SMS send
    tcpa_per_text: float = 500.0        # sourced: 47 U.S.C. 227(b)(3)
    tcpa_willful_multiple: float = 3.0  # sourced: up to treble for willful or knowing violations
    can_spam_per_email: float = 53_088.0  # sourced: FTC
    tasks: List[Tuple[str, float, float, float, str]] = field(default_factory=lambda: list(TASKS))


def run(i: Inputs) -> Dict[str, Any]:
    benefits = i.pay * i.benefits_load
    loaded = i.pay + benefits + i.tools_and_equipment
    hour = loaded / i.productive_hours
    rows = []
    for name, n, before, after, how in i.tasks:
        saved = n * (before - after)
        rows.append({"task": name, "per_year": n, "hours_today": before, "hours_with_engine": after, "hours_returned": round(saved, 1), "how": how})
    hours = sum(r["hours_returned"] for r in rows)
    share = hours / i.productive_hours
    value = hours * hour

    seats = i.team * i.claude_seat_month * 12
    api = i.team * i.api_copy_year
    upkeep = 4 * i.upkeep_hours_quarter * hour
    annual = seats + api + i.hosting_year + upkeep
    one_time = i.setup_engineer_hours * i.engineer_hour + i.team * i.setup_marketer_hours * hour
    per_annual, per_once = annual / i.team, one_time / i.team
    breakeven_hours = per_annual / hour

    days_without = i.detect_days_without + i.fix_days
    days_with = i.detect_days_with + i.fix_days
    per_day = i.email_revenue * i.incremental_share * i.spam_share / 365
    incident = {"lost_without": per_day * days_without, "lost_with": per_day * days_with,
                "avoided": per_day * (days_without - days_with), "avoided_year": per_day * (days_without - days_with) * i.incidents_year,
                "true_revenue_per_day_in_spam": per_day}

    tcpa_low = i.sms_send_size * i.tcpa_per_text
    tcpa_high = tcpa_low * i.tcpa_willful_multiple

    return {
        "inputs": {k: v for k, v in asdict(i).items() if k != "tasks"},
        "cost_of_a_marketer": {"pay": i.pay, "benefits": benefits, "tools_and_equipment": i.tools_and_equipment, "loaded": loaded,
                               "hour": hour, "loaded_at_bls_benefits": i.pay / 0.70 + i.tools_and_equipment},
        "time": {"tasks": rows, "hours_returned": hours, "share_of_year": share, "value": value},
        "engine_cost": {"team": i.team, "claude_seats": seats, "api_copy": api, "hosting": i.hosting_year, "upkeep": upkeep,
                        "annual": annual, "one_time": one_time, "per_marketer_annual": per_annual, "per_marketer_one_time": per_once,
                        "value_per_dollar": value / per_annual if per_annual else None, "breakeven_hours_per_marketer": breakeven_hours},
        "inbox_incident": incident,
        "legal_exposure": {"sms_send": i.sms_send_size, "tcpa_low": tcpa_low, "tcpa_high": tcpa_high, "can_spam_per_email": i.can_spam_per_email},
        "sources": SOURCES,
    }


def _usd(v: float) -> str:
    return f"${v:,.0f}"


def render(r: Dict[str, Any]) -> str:
    c, t, e, inc, lg = r["cost_of_a_marketer"], r["time"], r["engine_cost"], r["inbox_incident"], r["legal_exposure"]
    L = ["ONE LIFECYCLE MARKETER, PER YEAR",
         f"  Loaded cost: {_usd(c['loaded'])} ({_usd(c['hour'])} an hour over {r['inputs']['productive_hours']:,.0f} productive hours)",
         f"  Time returned: {t['hours_returned']:,.0f} hours, {t['share_of_year']:.0%} of a working year, worth {_usd(t['value'])}"]
    for row in t["tasks"]:
        L.append(f"    {row['task']:<38} {row['per_year']:>4g} x ({row['hours_today']:g}h -> {row['hours_with_engine']:g}h) = {row['hours_returned']:,.0f}h")
    L += ["", f"ENGINE COST, TEAM OF {e['team']}",
          f"  Per year: {_usd(e['annual'])} (seats {_usd(e['claude_seats'])}, API {_usd(e['api_copy'])}, hosting {_usd(e['hosting'])}, "
          f"upkeep {_usd(e['upkeep'])}); once: {_usd(e['one_time'])}",
          f"  Per marketer: {_usd(e['per_marketer_annual'])} a year plus {_usd(e['per_marketer_one_time'])} once; "
          f"{e['value_per_dollar']:.0f} dollars of time per dollar; breaks even at {e['breakeven_hours_per_marketer']:.0f} hours a year",
          "", "ONE INBOX INCIDENT (scenario)",
          f"  True revenue lost per day while {r['inputs']['spam_share']:.0%} of mail is in spam: {_usd(inc['true_revenue_per_day_in_spam'])}",
          f"  Lost without the engine: {_usd(inc['lost_without'])}; with it: {_usd(inc['lost_with'])}; avoided: {_usd(inc['avoided'])}",
          "", "LEGAL EXPOSURE (illustration of scale, not an expected loss)",
          f"  One {lg['sms_send']:,}-person SMS send outside the rules: {_usd(lg['tcpa_low'])} to {_usd(lg['tcpa_high'])} in statutory damages",
          f"  One commercial email without a postal address or opt-out: up to {_usd(lg['can_spam_per_email'])} in penalties"]
    return "\n".join(L) + "\n"


def _cli(argv: Optional[List[str]] = None) -> int:
    d = Inputs()
    ap = argparse.ArgumentParser(prog="lcm roi", description="ROI model for the LMM Engine (docs/ROI_MEMO.md)")
    for name, text, spec in (
            ("pay", "one marketer's annual pay, in dollars", ",.0f"),
            ("benefits_load", "benefits as a share of pay, 0.20 = 20%%", "g"),
            ("tools_and_equipment", "tools and equipment per marketer a year, in dollars", ",.0f"),
            ("productive_hours", "productive hours per marketer a year", ",.0f"),
            ("claude_seat_month", "Claude seat price per marketer a month, in dollars", ",.0f"),
            ("api_copy_year", "Claude API spend on copy per marketer a year, in dollars", ",.0f"),
            ("hosting_year", "hosting for the scheduled runs per team a year, in dollars", ",.0f"),
            ("upkeep_hours_quarter", "hours a quarter to review rules, thresholds and the sunset list", "g"),
            ("setup_engineer_hours", "one-time engineer hours to wire the daily check to the warehouse and Sheets", "g"),
            ("engineer_hour", "loaded cost of one engineer hour, in dollars", ",.0f"),
            ("setup_marketer_hours", "one-time setup hours per marketer", "g"),
            ("email_revenue", "annual revenue your email platform attributes to email, in dollars", ",.0f"),
            ("incremental_share", "share of attributed email revenue that is truly incremental, 0.38 = 38%%", "g"),
            ("spam_share", "share of mail landing in spam during an inbox incident, 0.20 = 20%%", "g"),
            ("detect_days_without", "days until an inbox problem is noticed without the engine", "g"),
            ("detect_days_with", "days until it is noticed with the morning check", "g"),
            ("fix_days", "days to fix an inbox problem once found", "g"),
            ("incidents_year", "inbox incidents a year", "g")):
        ap.add_argument("--" + name.replace("_", "-"), type=float, default=getattr(d, name),
                        help=f"{text} (default {format(getattr(d, name), spec)})")
    ap.add_argument("--team", type=int, default=d.team, help=f"lifecycle marketers sharing the engine (default {d.team})")
    ap.add_argument("--sms-send-size", type=int, default=d.sms_send_size,
                    help=f"people in one SMS send, for the legal exposure illustration (default {d.sms_send_size:,})")
    ap.add_argument("--json", action="store_true", help="print the numbers as JSON instead of the memo text")
    a = ap.parse_args(argv)
    if a.team < 1:
        ap.error("--team must be at least 1")
    i = Inputs(**{k: v for k, v in vars(a).items() if k != "json"})
    r = run(i)
    print(json.dumps(r, indent=2) if a.json else render(r))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
