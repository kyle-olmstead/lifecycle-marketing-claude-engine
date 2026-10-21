"""
Experiment sizing and readout for prompts/jobs/measurement.

Pure standard library (statistics.NormalDist for the normal quantiles), so the numbers can be checked
by hand and reproduced anywhere:

  size      sample size per arm for a two-proportion test, any allocation ratio (holdouts are unequal)
  mde       the smallest lift a given sample can detect
  feasible  how long a flow's entry volume takes to reach a readable holdout, or that it never will
  readout   two-proportion test with a Newcombe score interval on the difference, plus an SRM check
  incrementality  the three numbers (attributed, incremental, gap) with the read labeled honestly

    python3 engine/measurement/stats.py size --baseline 0.10 --mde-abs 0.02
    python3 engine/measurement/stats.py feasible --entries-per-month 1200 --holdout 0.10 --baseline 0.04 --mde-rel 0.20 --outcome-window-days 60
    python3 engine/measurement/stats.py readout --xt 412 --nt 9000 --xc 38 --nc 1000 --expected-holdout 0.10
    python3 engine/measurement/stats.py incrementality examples/stats/winback_readout.json --markdown

The method is the incrementality-and-holdouts discipline: decide the design before the flow ships,
size honestly, never compute a lift percentage on a population too small to support one, and report
attributed, incremental and the gap together.

The sizer and the readout share one floor. readout() refuses any arm with fewer than MIN_ARM_FOR_LIFT
entrants or MIN_EVENTS_FOR_LIFT conversions, so sample_size() and feasibility() never plan an arm below
it: each arm gets at least 30 entrants and enough entrants that its expected conversions reach 5 at the
baseline rate and at the treatment rate. When that floor, not power, sets the size, the output says so
(size_set_by: "readout floor") and reports the chance that both arms actually clear the event floor.

Sources: the sample size formula is the pooled-under-H0, unpooled-under-H1 normal approximation in
Fleiss, Levin and Paik, Statistical Methods for Rates and Proportions, 3rd ed. (2003), ch. 4; the
difference interval is Newcombe (1998), Statistics in Medicine 17:873-890, method 10; the Student t
distribution used by the clustered and trend readouts is evaluated through the regularized incomplete
beta function by the continued fraction in Press et al., Numerical Recipes, 3rd ed. (2007), section 6.4.
"""
from __future__ import annotations

if __package__ in (None, ""):  # run as a file (python3 engine/measurement/stats.py): make the repository importable
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

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
MIN_CLUSTERS_PER_ARM = 10      # and, when accounts are randomized, below this many accounts in either arm


def _z(p: float) -> float:
    return Z.inv_cdf(p)


# ----------------------------------------------------------------------------------------------
# Student t and binomial tails (pure Python; used by the clustered readout and the trend tests)
# ----------------------------------------------------------------------------------------------

def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (modified Lentz, Numerical Recipes 6.4)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = tiny if abs(d) < tiny else d
    d = 1.0 / d
    h = d
    for m in range(1, 10000):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = tiny if abs(d) < tiny else d
        c = 1.0 + aa / c
        c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = tiny if abs(d) < tiny else d
        c = 1.0 + aa / c
        c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1) / (a + b + 2):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1 - x) / b


def t_sf(t: float, df: float) -> float:
    """Upper tail P(T > t) of Student's t with df degrees of freedom (df may be fractional)."""
    if df <= 0:
        raise ValueError("df must be positive")
    if math.isinf(df):
        return 1 - Z.cdf(t)
    tail = 0.5 * betainc(df / 2.0, 0.5, df / (df + t * t))
    return tail if t >= 0 else 1.0 - tail


def t_cdf(t: float, df: float) -> float:
    return 1.0 - t_sf(t, df)


def t_ppf(q: float, df: float) -> float:
    """Quantile of Student's t: the value with P(T <= value) = q. Bisection on t_cdf, accurate to about 1e-10."""
    if not 0 < q < 1:
        raise ValueError("q must be in (0, 1)")
    if math.isinf(df):
        return _z(q)
    if q < 0.5:
        return -t_ppf(1 - q, df)
    lo, hi = 0.0, max(1.0, _z(q))
    while t_cdf(hi, df) < q:
        hi *= 2
    for _ in range(200):
        mid = (lo + hi) / 2
        if t_cdf(mid, df) < q:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-12:
            break
    return (lo + hi) / 2


def binom_prob_at_least(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p), summed exactly from the lower tail."""
    if k <= 0:
        return 1.0
    if n < k:
        return 0.0
    if p <= 0:
        return 0.0
    if p >= 1:
        return 1.0
    lower = 0.0
    for j in range(k):
        lower += math.exp(math.lgamma(n + 1) - math.lgamma(j + 1) - math.lgamma(n - j + 1) + j * math.log(p) + (n - j) * math.log1p(-p))
    return max(0.0, 1.0 - lower)


# ----------------------------------------------------------------------------------------------
# sizing
# ----------------------------------------------------------------------------------------------

def _n_control_for(p_control: float, p_treatment: float, ratio: float, q_alpha: float, q_beta: float) -> float:
    """Unrounded control-arm size for the pooled-under-H0 / unpooled-under-H1 formula, given the two quantiles.

    sample_size() passes normal quantiles; engine/measurement/cluster.py passes Student t quantiles when the
    number of randomized accounts is small.
    """
    k = ratio
    p_bar = (p_control + k * p_treatment) / (1 + k)
    num = (q_alpha * math.sqrt(p_bar * (1 - p_bar) * (1 + 1 / k))
           + q_beta * math.sqrt(p_control * (1 - p_control) + p_treatment * (1 - p_treatment) / k))
    return num ** 2 / (p_treatment - p_control) ** 2


def readout_floor(p_control: float, p_treatment: float, ratio: float = 1.0) -> Dict[str, Any]:
    """The smallest arms readout() will read: every arm at least MIN_ARM_FOR_LIFT entrants and enough entrants
    that its expected conversions reach MIN_EVENTS_FOR_LIFT.

    The holdout always converts at the baseline. The treatment arm converts at the treatment rate if the flow
    works and at the baseline if it does not, so it is sized for the lower of the two. ratio = n_treatment / n_control.
    """
    if not (0 < p_control < 1 and 0 < p_treatment < 1):
        raise ValueError("rates must be in (0, 1)")
    k = ratio
    low_t = min(p_control, p_treatment)
    n_c = max(MIN_ARM_FOR_LIFT, math.ceil(MIN_EVENTS_FOR_LIFT / p_control - 1e-9))
    need_t = max(MIN_ARM_FOR_LIFT, math.ceil(MIN_EVENTS_FOR_LIFT / low_t - 1e-9))
    n_c = max(n_c, math.ceil(need_t / k - 1e-9))
    n_t = max(need_t, math.ceil(n_c * k - 1e-9))
    return {"n_control": n_c, "n_treatment": n_t,
            "rule": (f"at least {MIN_ARM_FOR_LIFT} entrants and {MIN_EVENTS_FOR_LIFT} expected conversions per arm, at the baseline "
                     f"and at the treatment rate (the readout refuses smaller arms)")}


def p_readable(n_control: int, n_treatment: int, p_control: float, p_treatment: float) -> float:
    """Probability that both arms clear the readout's event floor at these sizes, if the treatment rate is true."""
    return (binom_prob_at_least(MIN_EVENTS_FOR_LIFT, n_control, p_control)
            * binom_prob_at_least(MIN_EVENTS_FOR_LIFT, n_treatment, p_treatment))


def _apply_floor(n_c: int, n_t: int, p_control: float, p_treatment: float, ratio: float,
                 min_clusters: Optional[float] = None) -> Tuple[int, int, str, Dict[str, Any]]:
    """Raise power-based arms to the readout floor. Returns (n_c, n_t, size_set_by, floor)."""
    floor = readout_floor(p_control, p_treatment, ratio)
    f_c, f_t = floor["n_control"], floor["n_treatment"]
    if min_clusters:   # mean entrants per randomized account: each arm also needs MIN_CLUSTERS_PER_ARM accounts
        f_c = max(f_c, math.ceil(MIN_CLUSTERS_PER_ARM * min_clusters - 1e-9))
        f_t = max(f_t, math.ceil(MIN_CLUSTERS_PER_ARM * min_clusters - 1e-9), math.ceil(f_c * ratio - 1e-9))
        floor = dict(floor, rule=floor["rule"] + f", and at least {MIN_CLUSTERS_PER_ARM} randomized accounts per arm")
    floor = dict(floor, n_control=f_c, n_treatment=f_t)
    if n_c >= f_c and n_t >= f_t:
        return n_c, n_t, "power", floor
    return max(n_c, f_c), max(n_t, f_t, math.ceil(max(n_c, f_c) * ratio - 1e-9)), "readout floor", floor


def sample_size(p_control: float, p_treatment: float, alpha: float = 0.05, power: float = 0.80,
                ratio: float = 1.0, two_sided: bool = True, apply_readout_floor: bool = True) -> Dict[str, Any]:
    """
    Per-arm sample sizes for a two-proportion z-test (pooled variance under H0, unpooled under H1).
    ratio = n_treatment / n_control. A 10 percent holdout is ratio 9.

    With apply_readout_floor (the default) neither arm is planned below what readout() will read; size_set_by
    says whether power or that floor set the size, and power_n_control / power_n_treatment keep the power-only
    numbers. Pass apply_readout_floor=False only to get the raw formula (feasibility() does, because it inflates
    by the design effect before applying the floor, which is counted in entrants).
    """
    if not (0 < p_control < 1 and 0 < p_treatment < 1) or p_control == p_treatment:
        raise ValueError("rates must be in (0, 1) and differ")
    k = ratio
    za = _z(1 - alpha / 2) if two_sided else _z(1 - alpha)
    zb = _z(power)
    n_c = math.ceil(_n_control_for(p_control, p_treatment, k, za, zb))
    n_t = math.ceil(n_c * k)
    out: Dict[str, Any] = {"n_control": n_c, "n_treatment": n_t, "n_total": n_c + n_t, "p_control": p_control, "p_treatment": p_treatment,
                           "alpha": alpha, "power": power, "ratio": ratio, "two_sided": two_sided,
                           "method": "two-proportion z-test, pooled variance under H0, unpooled under H1"}
    if apply_readout_floor:
        f_c, f_t, by, floor = _apply_floor(n_c, n_t, p_control, p_treatment, k)
        out.update({"n_control": f_c, "n_treatment": f_t, "n_total": f_c + f_t, "power_n_control": n_c, "power_n_treatment": n_t,
                    "size_set_by": by, "readout_floor": floor, "p_readable": round(p_readable(f_c, f_t, p_control, p_treatment), 4)})
        if by == "readout floor":
            out["sizing_note"] = (f"power alone needs {n_c:,} control / {n_t:,} treatment, but the readout refuses arms with fewer than "
                                  f"{MIN_ARM_FOR_LIFT} entrants or {MIN_EVENTS_FOR_LIFT} conversions, so the plan is raised to "
                                  f"{f_c:,} / {f_t:,}. The floor, not power, set this size; power at the planned size is higher than asked")
        note = _readable_note(out["p_readable"])
        if note:
            out["readable_note"] = note
    return out


def _readable_note(p: float) -> Optional[str]:
    if p >= 0.9:
        return None
    return (f"expected conversions meet the floor, but with counts this small there is a {1 - p:.0%} chance an arm ends below "
            f"{MIN_EVENTS_FOR_LIFT} conversions and the read is refused; a larger sample makes the read more certain")


def _power(n_c: int, n_t: int, p_c: float, p_t: float, alpha: float, two_sided: bool) -> float:
    k = n_t / n_c
    za = _z(1 - alpha / 2) if two_sided else _z(1 - alpha)
    p_bar = (p_c + k * p_t) / (1 + k)
    se0 = math.sqrt(p_bar * (1 - p_bar) * (1 / n_c + 1 / n_t))
    se1 = math.sqrt(p_c * (1 - p_c) / n_c + p_t * (1 - p_t) / n_t)
    return Z.cdf((abs(p_t - p_c) - za * se0) / se1)


def minimum_detectable_effect(n_control: int, n_treatment: int, p_control: float, alpha: float = 0.05,
                              power: float = 0.80, two_sided: bool = True) -> Dict[str, Any]:
    """Smallest absolute lift (treatment above control) detectable with the given arms, by bisection on power.

    Arms the readout would refuse (fewer than MIN_ARM_FOR_LIFT entrants, or fewer than MIN_EVENTS_FOR_LIFT
    expected conversions at the baseline) get no MDE: no lift of any size could be read from them.
    """
    if min(n_control, n_treatment) < MIN_ARM_FOR_LIFT or min(n_control, n_treatment) * p_control < MIN_EVENTS_FOR_LIFT:
        return {"mde_abs": None, "mde_rel": None, "n_control": n_control, "n_treatment": n_treatment, "p_control": p_control,
                "note": (f"the readout refuses arms with fewer than {MIN_ARM_FOR_LIFT} entrants or {MIN_EVENTS_FOR_LIFT} expected "
                         f"conversions at the baseline, so no lift is detectable at this sample size")}
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
                outcome_window_days: float = 0, design_effect: float = 1.0,
                mean_cluster_size: Optional[float] = None) -> Dict[str, Any]:
    """How long a persistent holdout takes to read the stated lift, and the design to use if it never will.

    months_to_read = months of entries needed to fill the holdout + the outcome window, because the last
    entrant still has to finish the window before the read. design_effect inflates the sample when the
    randomization unit (an account, a workspace) holds more than one entrant: 1 + (m - 1) x ICC, where m
    is entrants per unit and ICC is how alike entrants in one unit behave (engine/measurement/cluster.py
    estimates both and adds the correction for unequal account sizes). Leave it at 1.0 only when each unit
    contributes one entrant. mean_cluster_size (entrants per randomized account) adds the account counts to
    the plan and the floor of MIN_CLUSTERS_PER_ARM accounts per arm.

    No plan falls below what readout() reads: each arm is raised to at least MIN_ARM_FOR_LIFT entrants and
    MIN_EVENTS_FOR_LIFT expected conversions (at the baseline and the treatment rate); size_set_by says
    whether power or that floor set the size.
    """
    if mde_abs is None:
        if mde_rel is None:
            raise ValueError("give mde_abs or mde_rel")
        mde_abs = p_control * mde_rel
    if design_effect < 1:
        raise ValueError("design_effect is at least 1")
    if mean_cluster_size is not None and mean_cluster_size < 1:
        raise ValueError("mean_cluster_size is at least 1")
    window_months = outcome_window_days / 30.44
    p_t = p_control + mde_abs

    def months_for(share: float) -> Tuple[int, int, float, float, int, int, str, Dict[str, Any]]:
        ratio = (1 - share) / share
        need = sample_size(p_control, p_t, alpha, power, ratio, apply_readout_floor=False)
        pw_c, pw_t = math.ceil(need["n_control"] * design_effect), math.ceil(need["n_treatment"] * design_effect)
        n_c, n_t, by, floor = _apply_floor(pw_c, pw_t, p_control, p_t, ratio, mean_cluster_size)
        enroll = n_c / (entries_per_month * share) if entries_per_month > 0 else math.inf
        return n_c, n_t, enroll, enroll + window_months, pw_c, pw_t, by, floor

    n_c, n_t, enroll, months, pw_c, pw_t, size_set_by, floor = months_for(holdout_share)
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
    out = {"entries_per_month": entries_per_month, "holdout_share": holdout_share, "p_control": p_control, "mde_abs": mde_abs,
           "design_effect": design_effect, "required_holdout_n": n_c, "required_treatment_n": n_t,
           "power_holdout_n": pw_c, "power_treatment_n": pw_t, "size_set_by": size_set_by, "readout_floor": floor,
           "p_readable": round(p_readable(n_c, n_t, p_control, p_t), 4),
           "enrollment_months": None if math.isinf(enroll) else round(enroll, 1), "outcome_window_days": outcome_window_days,
           "months_to_read": None if math.isinf(months) else round(months, 1), "recommended_design": design, "expected_read": read,
           "alternatives": alternatives, "horizon_months": horizon_months,
           "randomization_unit": ("account (company or workspace) where the buying unit is an account; entrants in one unit are not "
                                  "independent, so set design_effect = 1 + (entrants per unit - 1) x ICC")}
    if size_set_by == "readout floor":
        out["sizing_note"] = (f"power alone needs {pw_c:,} holdout entrants, but the readout needs {floor['rule']}; "
                              f"the floor, not power, set the size at {n_c:,} holdout / {n_t:,} treatment entrants")
    note = _readable_note(out["p_readable"])
    if note:
        out["readable_note"] = note
    if mean_cluster_size:
        out["mean_cluster_size"] = mean_cluster_size
        out["required_holdout_clusters"] = math.ceil(n_c / mean_cluster_size - 1e-9)
        out["required_treatment_clusters"] = math.ceil(n_t / mean_cluster_size - 1e-9)
    return out


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
    level = f"{round((1 - r.get('alpha', 0.05)) * 100, 2):g}%"   # the interval level follows the readout's alpha: 95%, 90%, 97.5%
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


def _n(x: float) -> str:
    return f"{x:,.0f}"


def render_size(r: Dict[str, Any], holdout: Optional[float]) -> str:
    """The sample-size answer in sentences a marketer can repeat."""
    pc, pt = r["p_control"], r["p_treatment"]
    lines = [f"To detect a move from {pc:.1%} to {pt:.1%} ({(pt - pc) / pc:+.0%} relative, {(pt - pc) * 100:.1f} points) "
             f"with {r['power']:.0%} power at a {r['alpha']:.0%} false-alarm rate ({'two' if r['two_sided'] else 'one'}-sided):", "",
             f"  holdout   {_n(r['n_control']):>10} people",
             f"  treatment {_n(r['n_treatment']):>10} people",
             f"  total     {_n(r['n_total']):>10} entrants" + (f" at a {holdout:.0%} holdout" if holdout else " split evenly"), ""]
    if r.get("size_set_by") == "readout_floor":
        lines.append(f"The readout floor set the size, not power: {r['readout_floor']['rule']}.")
    lines.append(f"Method: {r['method']}. Divide the total by monthly entrants to get months to read, or run "
                 f"`lcm size --entries-per-month N` for that and the alternatives.")
    return "\n".join(lines) + "\n"


def render_feasible(r: Dict[str, Any]) -> str:
    lines = [f"{_n(r['entries_per_month'])} entrants a month, {r['holdout_share']:.0%} holdout, {r['p_control']:.1%} baseline, "
             f"detecting {r['mde_abs'] * 100:.1f} points:", "",
             f"  needs     {_n(r['required_holdout_n'])} in the holdout and {_n(r['required_treatment_n'])} in treatment",
             f"  enrolls   in {r['enrollment_months']} months" if r.get("enrollment_months") is not None else "  enrolls   never at this volume",
             (f"  reads     in about {r['months_to_read']} months, including the {r['outcome_window_days']:g}-day outcome window"
              if r.get("months_to_read") is not None else "  reads     not within the horizon"), "",
             f"{r['recommended_design']}. Expected read: {r['expected_read']}."]
    alts = r.get("alternatives") or []
    if alts:
        lines += ["", "  Holdout  Months to read  Cost"]
        lines += [f"  {x['holdout_share']:>6.0%}  {('never' if x['months_to_read'] is None else x['months_to_read']):>14}  {x['cost']}" for x in alts]
    if r.get("design_effect", 1) != 1:
        lines.append(f"\nDesign effect {r['design_effect']:g} applied (people clustered in accounts).")
    return "\n".join(lines) + "\n"


def render_mde(r: Dict[str, Any]) -> str:
    return (f"With {_n(r['n_control'])} in the holdout and {_n(r['n_treatment'])} in treatment at a {r['p_control']:.1%} baseline, the "
            f"smallest lift you can reliably detect is {r['mde_abs'] * 100:.1f} points ({r['mde_rel']:.0%} relative), at "
            f"{r['power']:.0%} power and a {r['alpha']:.0%} false-alarm rate. A smaller true lift will usually read as no difference.\n")


def render_readout(r: Dict[str, Any]) -> str:
    if "diff_ci" not in r:
        return (f"Treatment {_n(r['treatment']['conversions'])} of {_n(r['treatment']['n'])}, holdout {_n(r['holdout']['conversions'])} of "
                f"{_n(r['holdout']['n'])}: {r.get('read', 'not readable')}. {r.get('note') or r.get('error') or ''}".rstrip() + "\n")
    lo, hi = r["diff_ci"]
    sig = r["p_value"] < r["alpha"]
    return (f"Treatment {r['rate_treatment']:.1%} ({_n(r['treatment']['conversions'])} of {_n(r['treatment']['n'])}) vs holdout "
            f"{r['rate_holdout']:.1%} ({_n(r['holdout']['conversions'])} of {_n(r['holdout']['n'])}): "
            f"{r['diff_abs'] * 100:+.1f} points ({r['lift_rel']:+.0%} relative), {1 - r['alpha']:.0%} interval "
            f"{lo * 100:+.1f} to {hi * 100:+.1f} points, p = {r['p_value']:.3f}. "
            + ("The difference is unlikely to be chance." if sig else "This could be chance: do not report it as a lift.")
            + (f" Read: {r['read']}." if r.get("read") else "") + "\n")


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="lcm size", description="Experiment sizing and readout. Rates are shares (0.10 means 10%%).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("size", help="people needed in each arm to detect a lift")
    s.add_argument("--baseline", type=float, required=True, help="share who convert with no flow (the holdout's rate), e.g. 0.10")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--mde-abs", type=float, help="smallest lift worth detecting, in points as a share (0.02 = 2 points)")
    g.add_argument("--mde-rel", type=float, help="smallest lift worth detecting, relative to the baseline (0.20 = 20%%)")
    s.add_argument("--alpha", type=float, default=0.05, help="false-alarm rate (default 0.05)")
    s.add_argument("--power", type=float, default=0.80, help="chance of detecting a true lift of that size (default 0.80)")
    s.add_argument("--holdout", type=float, help="holdout share, e.g. 0.10 (sets the allocation ratio; default an even split)")
    s.add_argument("--one-sided", action="store_true", help="test for a lift only, not a drop (rarely right for lifecycle)")
    s.add_argument("--json", action="store_true", help="print the full result as JSON")
    m = sub.add_parser("mde", help="the smallest lift a given sample can detect")
    m.add_argument("--baseline", type=float, required=True, help="share who convert with no flow, e.g. 0.10")
    m.add_argument("--nc", type=int, required=True, help="people in the holdout")
    m.add_argument("--nt", type=int, required=True, help="people in treatment")
    m.add_argument("--alpha", type=float, default=0.05, help="false-alarm rate (default 0.05)")
    m.add_argument("--power", type=float, default=0.80, help="detection probability (default 0.80)")
    m.add_argument("--json", action="store_true", help="print the full result as JSON")
    f = sub.add_parser("feasible", help="months to read at your entry volume, with faster alternatives")
    f.add_argument("--entries-per-month", type=float, required=True, help="people entering the flow each month")
    f.add_argument("--holdout", type=float, default=0.10, help="holdout share (default 0.10)")
    f.add_argument("--baseline", type=float, required=True, help="share who convert with no flow, e.g. 0.10")
    gf = f.add_mutually_exclusive_group(required=True)
    gf.add_argument("--mde-abs", type=float, help="smallest lift worth detecting, in points as a share")
    gf.add_argument("--mde-rel", type=float, help="smallest lift worth detecting, relative to the baseline")
    f.add_argument("--outcome-window-days", type=float, default=0, help="days each entrant is followed before the metric is final")
    f.add_argument("--design-effect", type=float, default=1.0, help="1 + (entrants per randomized unit - 1) x ICC; 1.0 when one entrant per unit")
    f.add_argument("--json", action="store_true", help="print the full result as JSON")
    r = sub.add_parser("readout", help="read a finished holdout from four counts")
    for k, h in (("xt", "conversions in treatment"), ("nt", "people in treatment"), ("xc", "conversions in the holdout"), ("nc", "people in the holdout")):
        r.add_argument(f"--{k}", type=int, required=True, help=h)
    r.add_argument("--expected-holdout", type=float, help="the holdout share you set, to check the split (sample ratio mismatch)")
    r.add_argument("--json", action="store_true", help="print the full result as JSON")
    i = sub.add_parser("incrementality", help="incremental vs attributed conversions from a results file")
    i.add_argument("input", help="results JSON (schemas/readout_input.schema.json)")
    i.add_argument("--markdown", action="store_true", help="print a marketer-readable table instead of JSON")
    a = ap.parse_args(argv)
    if a.cmd == "size":
        p2 = a.baseline + (a.mde_abs if a.mde_abs is not None else a.baseline * a.mde_rel)
        ratio = (1 - a.holdout) / a.holdout if a.holdout else 1.0
        res = sample_size(a.baseline, p2, a.alpha, a.power, ratio, not a.one_sided)
        print(json.dumps(res, indent=2) if a.json else render_size(res, a.holdout), end="" if not a.json else "\n")
    elif a.cmd == "mde":
        res = minimum_detectable_effect(a.nc, a.nt, a.baseline, a.alpha, a.power)
        print(json.dumps(res, indent=2) if a.json else render_mde(res), end="" if not a.json else "\n")
    elif a.cmd == "feasible":
        res = feasibility(a.entries_per_month, a.holdout, a.baseline, a.mde_abs, a.mde_rel,
                          outcome_window_days=a.outcome_window_days, design_effect=a.design_effect)
        print(json.dumps(res, indent=2) if a.json else render_feasible(res), end="" if not a.json else "\n")
    elif a.cmd == "readout":
        res = readout(a.xt, a.nt, a.xc, a.nc, expected_holdout=a.expected_holdout)
        print(json.dumps(res, indent=2) if a.json else render_readout(res), end="" if not a.json else "\n")
    elif a.cmd == "incrementality":
        from engine.contracts import InputError, validate_readout
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
