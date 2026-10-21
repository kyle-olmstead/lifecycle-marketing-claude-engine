"""
Experiment sizing and readout for modules/05_experimentation_and_analytics.

Pure standard library (statistics.NormalDist for the normal quantiles), so the numbers can be checked
by hand and reproduced anywhere:

  size      sample size per arm for a two-proportion test, any allocation ratio (holdouts are unequal)
  mde       the smallest lift a given sample can detect
  feasible  how long a flow's entry volume takes to reach a readable holdout, or that it never will
  readout   two-proportion test with a Newcombe score interval on the difference, plus an SRM check
  incrementality  the three numbers (attributed, incremental, gap) with the read labeled honestly

    python3 tools/stats.py size --baseline 0.10 --mde-abs 0.02
    python3 tools/stats.py feasible --entries-per-month 1200 --holdout 0.10 --baseline 0.04 --mde-rel 0.20 --outcome-window-days 60
    python3 tools/stats.py readout --xt 412 --nt 9000 --xc 38 --nc 1000 --expected-holdout 0.10
    python3 tools/stats.py incrementality examples/stats/winback_readout.json --markdown

The method is the incrementality-and-holdouts discipline: decide the design before the flow ships,
size honestly, never compute a lift percentage on a population too small to support one, and report
attributed, incremental and the gap together.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from statistics import NormalDist
from typing import Any, Dict, List, Optional, Tuple

Z = NormalDist()
MIN_ARM_FOR_LIFT = 30          # below this many entrants in either arm, no lift percentage is computed
MIN_EVENTS_FOR_LIFT = 5        # and below this many conversions in either arm


def _z(p: float) -> float:
    return Z.inv_cdf(p)


# ----------------------------------------------------------------------------------------------
# sizing
# ----------------------------------------------------------------------------------------------

def sample_size(p_control: float, p_treatment: float, alpha: float = 0.05, power: float = 0.80,
                ratio: float = 1.0, two_sided: bool = True) -> Dict[str, Any]:
    """
    Per-arm sample sizes for a two-proportion z-test (pooled variance under H0, unpooled under H1).
    ratio = n_treatment / n_control. A 10 percent holdout is ratio 9.
    """
    if not (0 < p_control < 1 and 0 < p_treatment < 1) or p_control == p_treatment:
        raise ValueError("rates must be in (0, 1) and differ")
    k = ratio
    za = _z(1 - alpha / 2) if two_sided else _z(1 - alpha)
    zb = _z(power)
    p_bar = (p_control + k * p_treatment) / (1 + k)
    num = za * math.sqrt(p_bar * (1 - p_bar) * (1 + 1 / k)) + zb * math.sqrt(p_control * (1 - p_control) + p_treatment * (1 - p_treatment) / k)
    n_c = math.ceil(num ** 2 / (p_treatment - p_control) ** 2)
    n_t = math.ceil(n_c * k)
    return {"n_control": n_c, "n_treatment": n_t, "n_total": n_c + n_t, "p_control": p_control, "p_treatment": p_treatment,
            "alpha": alpha, "power": power, "ratio": ratio, "two_sided": two_sided,
            "method": "two-proportion z-test, pooled variance under H0, unpooled under H1"}


def _power(n_c: int, n_t: int, p_c: float, p_t: float, alpha: float, two_sided: bool) -> float:
    k = n_t / n_c
    za = _z(1 - alpha / 2) if two_sided else _z(1 - alpha)
    p_bar = (p_c + k * p_t) / (1 + k)
    se0 = math.sqrt(p_bar * (1 - p_bar) * (1 / n_c + 1 / n_t))
    se1 = math.sqrt(p_c * (1 - p_c) / n_c + p_t * (1 - p_t) / n_t)
    return Z.cdf((abs(p_t - p_c) - za * se0) / se1)


def minimum_detectable_effect(n_control: int, n_treatment: int, p_control: float, alpha: float = 0.05,
                              power: float = 0.80, two_sided: bool = True) -> Dict[str, Any]:
    """Smallest absolute lift (treatment above control) detectable with the given arms, by bisection on power."""
    lo, hi = 1e-9, 1 - p_control - 1e-9
    if _power(n_control, n_treatment, p_control, p_control + hi, alpha, two_sided) < power:
        return {"mde_abs": None, "mde_rel": None, "note": "no lift within the possible range reaches the requested power at this sample size"}
    for _ in range(200):
        mid = (lo + hi) / 2
        if _power(n_control, n_treatment, p_control, p_control + mid, alpha, two_sided) >= power:
            hi = mid
        else:
            lo = mid
    return {"mde_abs": round(hi, 6), "mde_rel": round(hi / p_control, 4), "n_control": n_control, "n_treatment": n_treatment,
            "p_control": p_control, "alpha": alpha, "power": power}


def feasibility(entries_per_month: float, holdout_share: float, p_control: float, mde_abs: Optional[float] = None,
                mde_rel: Optional[float] = None, alpha: float = 0.05, power: float = 0.80, horizon_months: int = 12,
                outcome_window_days: float = 0, design_effect: float = 1.0) -> Dict[str, Any]:
    """How long a persistent holdout takes to read the stated lift, and the design to use if it never will.

    months_to_read = months of entries needed to fill the holdout + the outcome window, because the last
    entrant still has to finish the window before the read. design_effect inflates the sample when the
    randomization unit (an account, a workspace) holds more than one entrant: 1 + (m - 1) x ICC, where m
    is entrants per unit and ICC is how alike entrants in one unit behave. Leave it at 1.0 only when each
    unit contributes one entrant.
    """
    if mde_abs is None:
        if mde_rel is None:
            raise ValueError("give mde_abs or mde_rel")
        mde_abs = p_control * mde_rel
    if design_effect < 1:
        raise ValueError("design_effect is at least 1")
    window_months = outcome_window_days / 30.44

    def months_for(share: float) -> Tuple[int, int, float, float]:
        need = sample_size(p_control, p_control + mde_abs, alpha, power, (1 - share) / share)
        n_c, n_t = math.ceil(need["n_control"] * design_effect), math.ceil(need["n_treatment"] * design_effect)
        enroll = n_c / (entries_per_month * share) if entries_per_month > 0 else math.inf
        return n_c, n_t, enroll, enroll + window_months

    n_c, n_t, enroll, months = months_for(holdout_share)
    if entries_per_month < 10:
        design, read = "no statistical read exists; report qualitatively and do not compute a lift percentage", "not readable"
    elif months <= 3:
        design, read = f"persistent randomized holdout at {holdout_share:.0%}", "causal"
    elif months <= horizon_months:
        design = f"persistent randomized holdout at {holdout_share:.0%} reads in about {months:.1f} months"
        if holdout_share < 0.25:   # only suggest a larger holdout when there is a larger one to suggest
            design += (f"; a 25% holdout would read in about {months_for(0.25)[3]:.1f} months at the cost of withholding more audience")
        read = "causal, slow"
    else:
        design = ("the holdout will not reach a readable sample inside the horizon. Use a pre/post baseline, label it directional, and "
                  "watch an operational signal instead; do not report a lift percentage")
        read = "directional only"
    alternatives = []
    for share in (0.25, 0.50):
        if share <= holdout_share:
            continue
        m = months_for(share)[3]
        alternatives.append({"holdout_share": share, "months_to_read": None if math.isinf(m) else round(m, 1),
                             "fits_horizon": m <= horizon_months, "cost": f"withholds {share:.0%} of entrants from the flow while it reads"})
    return {"entries_per_month": entries_per_month, "holdout_share": holdout_share, "p_control": p_control, "mde_abs": mde_abs,
            "design_effect": design_effect, "required_holdout_n": n_c, "required_treatment_n": n_t,
            "enrollment_months": None if math.isinf(enroll) else round(enroll, 1), "outcome_window_days": outcome_window_days,
            "months_to_read": None if math.isinf(months) else round(months, 1), "recommended_design": design, "expected_read": read,
            "alternatives": alternatives, "horizon_months": horizon_months,
            "randomization_unit": ("account (company or workspace) where the buying unit is an account; entrants in one unit are not "
                                   "independent, so set design_effect = 1 + (entrants per unit - 1) x ICC")}


# ----------------------------------------------------------------------------------------------
# readout
# ----------------------------------------------------------------------------------------------

def wilson(x: int, n: int, alpha: float = 0.05) -> Tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    z = _z(1 - alpha / 2)
    p = x / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def newcombe_diff_ci(x_t: int, n_t: int, x_c: int, n_c: int, alpha: float = 0.05) -> Tuple[float, float]:
    """Newcombe (1998) hybrid score interval, method 10, for p_t - p_c."""
    p_t, p_c = x_t / n_t, x_c / n_c
    l1, u1 = wilson(x_t, n_t, alpha)
    l2, u2 = wilson(x_c, n_c, alpha)
    d = p_t - p_c
    lower = d - math.sqrt((p_t - l1) ** 2 + (u2 - p_c) ** 2)
    upper = d + math.sqrt((u1 - p_t) ** 2 + (p_c - l2) ** 2)
    return (lower, upper)


def _chi2_sf(x: float, df: int) -> float:
    """Upper tail of the chi-square distribution (regularized upper incomplete gamma)."""
    if x <= 0:
        return 1.0
    a, xx = df / 2.0, x / 2.0
    if xx < a + 1:  # series for the lower incomplete gamma
        term = total = 1.0 / a
        n = a
        for _ in range(10000):
            n += 1
            term *= xx / n
            total += term
            if abs(term) < abs(total) * 1e-15:
                break
        return max(0.0, 1.0 - total * math.exp(-xx + a * math.log(xx) - math.lgamma(a)))
    b = xx + 1 - a  # continued fraction for the upper incomplete gamma
    c, d = 1e300, 1 / b
    h = d
    for i in range(1, 10000):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-15:
            break
    return math.exp(-xx + a * math.log(xx) - math.lgamma(a)) * h


def srm_check(observed: List[int], expected_shares: List[float], threshold: float = 0.001) -> Dict[str, Any]:
    """Sample ratio mismatch: chi-square goodness of fit of the observed split to the planned allocation."""
    total = sum(observed)
    exp = [total * s / sum(expected_shares) for s in expected_shares]
    chi2 = sum((o - e) ** 2 / e for o, e in zip(observed, exp, strict=True))
    p = _chi2_sf(chi2, len(observed) - 1)
    return {"observed": observed, "expected": [round(e, 1) for e in exp], "chi2": round(chi2, 4), "p_value": p, "mismatch": p < threshold,
            "note": ("the split does not match the plan: assignment or logging is broken, and no lift from this test can be trusted until it is found"
                     if p < threshold else "split matches the planned allocation")}


def readout(x_t: int, n_t: int, x_c: int, n_c: int, alpha: float = 0.05, expected_holdout: Optional[float] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"treatment": {"n": n_t, "conversions": x_t}, "holdout": {"n": n_c, "conversions": x_c}}
    if expected_holdout is not None:
        out["srm"] = srm_check([n_t, n_c], [1 - expected_holdout, expected_holdout])
    if min(n_t, n_c) < MIN_ARM_FOR_LIFT or min(x_t, x_c) < MIN_EVENTS_FOR_LIFT:
        out["read"] = "not readable"
        out["note"] = (f"fewer than {MIN_ARM_FOR_LIFT} entrants or {MIN_EVENTS_FOR_LIFT} conversions in an arm: no lift percentage is computed. "
                       "Report counts qualitatively.")
        return out
    p_t, p_c = x_t / n_t, x_c / n_c
    p_pool = (x_t + x_c) / (n_t + n_c)
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / n_t + 1 / n_c))
    zstat = (p_t - p_c) / se if se > 0 else 0.0
    pval = 2 * (1 - Z.cdf(abs(zstat)))
    lo, hi = newcombe_diff_ci(x_t, n_t, x_c, n_c, alpha)
    out.update({
        "rate_treatment": p_t, "rate_holdout": p_c, "diff_abs": p_t - p_c, "diff_ci": [lo, hi],
        "lift_rel": (p_t - p_c) / p_c if p_c > 0 else None, "z": zstat, "p_value": pval, "alpha": alpha,
        "read": ("not readable" if out.get("srm", {}).get("mismatch") else "causal" if (lo > 0 or hi < 0) else "causal, inconclusive"),
    })
    if out.get("srm", {}).get("mismatch"):
        out["note"] = "sample ratio mismatch: fix assignment before reading any result"
    elif lo <= 0 <= hi:
        out["note"] = "the interval includes zero: the data are consistent with no effect. Keep collecting or report as inconclusive; do not round up to a win"
    return out


def incrementality(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Attributed, incremental and gap for a flow with a persistent randomized holdout."""
    t, c = doc["treatment"], doc["holdout"]
    r = readout(t["conversions"], t["n"], c["conversions"], c["n"], doc.get("alpha", 0.05), doc.get("planned_holdout_share"))
    rev_t, rev_c = t.get("revenue"), c.get("revenue")
    inc_rev, rev_ci = None, None
    if rev_t is not None and rev_c is not None and r.get("read") != "not readable":
        inc_rev = (rev_t / t["n"] - rev_c / c["n"]) * t["n"]
        sd_t, sd_c = t.get("revenue_sd"), c.get("revenue_sd")
        if sd_t is not None and sd_c is not None:  # per-entrant revenue SD in each arm (zeros included): Welch interval, scaled
            z = _z(1 - doc.get("alpha", 0.05) / 2)
            se = math.sqrt(sd_t ** 2 / t["n"] + sd_c ** 2 / c["n"])
            rev_ci = ((inc_rev / t["n"] - z * se) * t["n"], (inc_rev / t["n"] + z * se) * t["n"])
    attributed = doc.get("attributed_revenue")
    caveats: List[str] = list(doc.get("caveats", []))
    if attributed is not None and rev_t is not None and attributed > rev_t:
        raise ValueError(f"attributed revenue ({attributed:,.0f}) exceeds the treatment arm's total revenue ({rev_t:,.0f}) in the outcome window. "
                         "Attribution can only credit revenue the treatment arm produced: the attribution and outcome windows, the populations, "
                         "or the revenue definitions (gross vs net, order vs invoice) differ. Reconcile them before reporting.")
    largest = t.get("largest_transaction")
    if largest and rev_t and largest / rev_t > 0.25:
        caveats.append(f"one transaction is {largest / rev_t:.0%} of treatment revenue: report median and count, and say the result rests on one record")
    if doc.get("window_days") and doc.get("inter_purchase_days_p50") and doc["window_days"] < doc["inter_purchase_days_p50"]:
        caveats.append(f"the {doc['window_days']}-day window is shorter than the median inter-purchase interval ({doc['inter_purchase_days_p50']} days); "
                       "it measures who happened to be mid-cycle")
    if doc.get("design") == "pre_post":
        r["read"] = "directional"
        caveats.append("pre/post baseline, not a holdout: directional only")
    return {
        "flow": doc.get("flow"), "period": doc.get("period"),
        "entered": t["n"] + c["n"], "treatment_n": t["n"], "holdout_n": c["n"],
        "attributed_revenue": attributed, "incremental_revenue": inc_rev, "incremental_revenue_ci": rev_ci,
        "gap": (attributed - inc_rev) if (attributed is not None and inc_rev is not None) else None,
        "rates": {"treatment": r.get("rate_treatment"), "holdout": r.get("rate_holdout")},
        "conversion_readout": r, "read": r.get("read"),
        "window_days": doc.get("window_days"), "window_reason": doc.get("window_reason"),
        "attribution_model": doc.get("attribution_model"),
        "caveats": caveats or ["none material"],
    }


def render_incrementality(x: Dict[str, Any]) -> str:
    def money(v: Optional[float]) -> str:
        return "not computed" if v is None else f"${v:,.0f}"
    r = x["conversion_readout"]
    level = f"{1 - r.get('alpha', 0.05):.0%}"   # the interval level follows the readout's alpha, not a fixed 95%
    verdict = (f"; not distinguishable from zero at {level}" if x["read"] == "causal, inconclusive" else
               "; directional only" if x["read"] == "directional" else "")
    conv = (f"{r['rate_treatment']:.2%} vs {r['rate_holdout']:.2%}; difference {r['diff_abs'] * 100:+.2f} pts, {level} CI "
            f"{r['diff_ci'][0] * 100:+.2f} to {r['diff_ci'][1] * 100:+.2f} pts{verdict}") if "rate_treatment" in r else "not readable at this sample"
    if x.get("incremental_revenue_ci"):
        lo, hi = x["incremental_revenue_ci"]
        rev_note = f"({level} CI {money(lo)} to {money(hi)}, from per-entrant revenue SDs)"
    elif x["incremental_revenue"] is not None:
        rev_note = "(point estimate: revenue per entrant, treatment minus holdout, times treatment entrants; supply revenue_sd per arm for an interval)"
    else:
        rev_note = ""
    lines = [
        f"{x['flow']} | {x['period']}",
        "",
        f"  Entered:        {x['entered']:,} (treatment {x['treatment_n']:,} / holdout {x['holdout_n']:,})",
        f"  Conversion:     {conv}",
        f"  Attributed:     {money(x['attributed_revenue'])}" + (f"   ({x['attribution_model']})" if x.get("attribution_model") else ""),
        f"  Incremental:    {money(x['incremental_revenue'])}   {rev_note}".rstrip(),
        f"  Gap:            {money(x['gap'])}",
        "",
        f"  Read:           {x['read']}",
        f"  Window:         {x['window_days']} days" + (f", chosen because {x['window_reason']}" if x.get("window_reason") else ""),
        "  Caveats:        " + "; ".join(x["caveats"]),
    ]
    if r.get("srm"):
        lines.append(f"  Sample ratio:   chi2 {r['srm']['chi2']}, p={r['srm']['p_value']:.3g}: {r['srm']['note']}")
    return "\n".join(lines) + "\n"


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Experiment sizing and readout")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("size")
    s.add_argument("--baseline", type=float, required=True)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--mde-abs", type=float)
    g.add_argument("--mde-rel", type=float)
    s.add_argument("--alpha", type=float, default=0.05)
    s.add_argument("--power", type=float, default=0.80)
    s.add_argument("--holdout", type=float, help="holdout share, e.g. 0.10 (sets the allocation ratio)")
    s.add_argument("--one-sided", action="store_true")
    m = sub.add_parser("mde")
    m.add_argument("--baseline", type=float, required=True)
    m.add_argument("--nc", type=int, required=True)
    m.add_argument("--nt", type=int, required=True)
    m.add_argument("--alpha", type=float, default=0.05)
    m.add_argument("--power", type=float, default=0.80)
    f = sub.add_parser("feasible")
    f.add_argument("--entries-per-month", type=float, required=True)
    f.add_argument("--holdout", type=float, default=0.10)
    f.add_argument("--baseline", type=float, required=True)
    gf = f.add_mutually_exclusive_group(required=True)
    gf.add_argument("--mde-abs", type=float)
    gf.add_argument("--mde-rel", type=float)
    f.add_argument("--outcome-window-days", type=float, default=0, help="days each entrant is followed before the metric is final")
    f.add_argument("--design-effect", type=float, default=1.0, help="1 + (entrants per randomized unit - 1) x ICC; 1.0 when one entrant per unit")
    r = sub.add_parser("readout")
    for k in ("xt", "nt", "xc", "nc"):
        r.add_argument(f"--{k}", type=int, required=True)
    r.add_argument("--expected-holdout", type=float)
    i = sub.add_parser("incrementality")
    i.add_argument("input")
    i.add_argument("--markdown", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "size":
        p2 = a.baseline + (a.mde_abs if a.mde_abs is not None else a.baseline * a.mde_rel)
        ratio = (1 - a.holdout) / a.holdout if a.holdout else 1.0
        print(json.dumps(sample_size(a.baseline, p2, a.alpha, a.power, ratio, not a.one_sided), indent=2))
    elif a.cmd == "mde":
        print(json.dumps(minimum_detectable_effect(a.nc, a.nt, a.baseline, a.alpha, a.power), indent=2))
    elif a.cmd == "feasible":
        print(json.dumps(feasibility(a.entries_per_month, a.holdout, a.baseline, a.mde_abs, a.mde_rel,
                                     outcome_window_days=a.outcome_window_days, design_effect=a.design_effect), indent=2))
    elif a.cmd == "readout":
        print(json.dumps(readout(a.xt, a.nt, a.xc, a.nc, expected_holdout=a.expected_holdout), indent=2))
    elif a.cmd == "incrementality":
        try:
            from tools.contracts import InputError, validate_readout
        except ImportError:  # run as a script from tools/
            from contracts import InputError, validate_readout  # type: ignore
        try:
            doc = validate_readout(json.loads(Path(a.input).read_text(encoding="utf-8")))
        except InputError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        x = incrementality(doc)
        print(render_incrementality(x) if a.markdown else json.dumps(x, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
