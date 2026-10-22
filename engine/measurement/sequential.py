"""
Sequential testing: reading an experiment more than once without inflating the false-positive rate.

A fixed-horizon test is read once, on the planned date. Reading it every week and stopping the first time
p < 0.05 turns a 5% false-positive rate into 20% or more. This module offers two honest ways to look early.

1. Group-sequential designs (a few planned looks). Lan-DeMets alpha spending decides how much of the 5% each
   look may use, so the looks can fall at any information fraction and need not be scheduled exactly:

     O'Brien-Fleming type   a(t) = 2 - 2 Phi(z_{1-a/2} / sqrt(t))      very strict early, close to fixed at the end
     Pocock type            a(t) = a x ln(1 + (e - 1) t)                about the same bar at every look

   Each function is written for one side at level a. A two-sided test at total alpha spends a = alpha/2 on each
   side, the convention of Lan and DeMets (1983) as implemented in the ldbounds and gsDesign packages, which is
   what the published two-sided boundaries (4.877, 3.357, 2.680, 2.290, 2.031 for K = 5) use. For the Pocock type
   the convention makes no difference, because that function is linear in a.

   boundaries()        the z boundary at each look, by recursive numerical integration (Armitage, McPherson
                       and Rowe 1969; Jennison and Turnbull 2000, ch. 19: their grid and Simpson's rule)
   inflation_factor()  how much larger the maximum sample must be than the fixed-horizon sample for the same power
   sequential_plan()   per-arm sample sizes at each look for a two-proportion test, never below the readout floor
   look()              continue, stop for efficacy, or the final read, given the observed z at an information fraction
   look_from_counts()  the same from conversions and entrants, with the repeated confidence interval

2. Always-valid inference (look as often as you like). The mixture sequential probability ratio test (mSPRT)
   for a difference of two proportions gives a p-value and a confidence sequence that stay valid under
   continuous monitoring (Johari, Koomen, Pekelis and Walsh 2017, "Peeking at A/B tests: why it matters, and
   what to do about it", KDD; Johari, Pekelis and Walsh 2022, "Always valid inference: continuous monitoring
   of A/B tests", Operations Research 70(3):1806-1821). With the difference estimate d, its plug-in variance v
   and a normal mixing distribution N(theta0, tau^2) over the true difference:

     Lambda = sqrt(v / (v + tau^2)) x exp(tau^2 (d - theta0)^2 / (2 v (v + tau^2)))

   The always-valid p-value is the running minimum of 1 / Lambda; the confidence sequence is the running
   intersection of {theta: Lambda(theta) < 1 / alpha}. The price is width: at any single moment the interval
   is wider than a fixed-horizon interval. The variance is estimated from the data, so validity is asymptotic;
   the readout floors keep the earliest, noisiest looks out.

    python3 engine/measurement/sequential.py boundaries --looks 5 --spending obf
    python3 engine/measurement/sequential.py plan --baseline 0.10 --mde-abs 0.02 --looks 4 --spending obf --holdout 0.20
    python3 engine/measurement/sequential.py look --z 2.9 --t 0.5 --previous 0.25 --spending obf
    python3 engine/measurement/sequential.py look --xt 410 --nt 4000 --xc 80 --nc 1000 --planned-nt 8000 --planned-nc 2000 --previous 0.25
    python3 engine/measurement/sequential.py msprt --xt 600 --nt 5000 --xc 100 --nc 1000 --mde-abs 0.02

Other sources: Lan and DeMets (1983), "Discrete sequential boundaries for clinical trials", Biometrika
70:659-663; O'Brien and Fleming (1979), Biometrics 35:549-556; Pocock (1977), Biometrika 64:191-199;
repeated confidence intervals, Jennison and Turnbull (2000), Group Sequential Methods with Applications to
Clinical Trials, ch. 9.

Checks performed in tests/unit/test_sequential.py: K = 5 equally spaced looks at two-sided alpha 0.05 give the
published Lan-DeMets O'Brien-Fleming boundaries (4.877, 3.357, 2.680, 2.290, 2.031) and Pocock-type
boundaries near 2.41 to 2.44; the classic constant Pocock and O'Brien-Fleming constants and inflation factors
match Jennison and Turnbull's Tables 2.1 to 2.4; seeded Monte Carlo confirms the overall type I error; and the
mSPRT keeps its error rate at or below alpha under continuous monitoring where naive peeking does not.
"""
from __future__ import annotations

if __package__ in (None, ""):  # run as a file (python3 engine/measurement/sequential.py): make the repository importable
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse
import json
import math
import sys
from statistics import NormalDist
from typing import Any, Dict, List, Optional, Sequence, Tuple

from engine.measurement import stats
from engine.measurement.stats import MIN_ARM_FOR_LIFT, MIN_EVENTS_FOR_LIFT

N01 = NormalDist()
SPENDING = ("obf", "pocock")
_INV_SQRT_2PI = 1.0 / math.sqrt(2.0 * math.pi)
_TINY = 1e-15


def _phi(x: float) -> float:
    return _INV_SQRT_2PI * math.exp(-0.5 * x * x)


def _cdf(x: float) -> float:
    if x == math.inf:
        return 1.0
    if x == -math.inf:
        return 0.0
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def _sf(x: float) -> float:
    if x == math.inf:
        return 0.0
    if x == -math.inf:
        return 1.0
    return 0.5 * math.erfc(x / math.sqrt(2.0))


# ----------------------------------------------------------------------------------------------
# spending functions
# ----------------------------------------------------------------------------------------------

def alpha_spent(t: float, alpha: float = 0.05, spending: str = "obf", sides: int = 2) -> float:
    """Cumulative type I error (both sides together when sides = 2) the Lan-DeMets spending function allows by
    information fraction t (0 to 1). Each side spends a = alpha / sides with the one-sided function."""
    if spending not in SPENDING:
        raise ValueError(f"spending must be one of {SPENDING}")
    if sides not in (1, 2):
        raise ValueError("sides is 1 or 2")
    t = min(max(t, 0.0), 1.0)
    if t == 0:
        return 0.0
    a = alpha / sides
    if spending == "obf":
        return sides * 2.0 * _sf(N01.inv_cdf(1 - a / 2) / math.sqrt(t))
    return alpha * math.log(1 + (math.e - 1) * t)


# ----------------------------------------------------------------------------------------------
# recursive numerical integration (Jennison and Turnbull 2000, ch. 19)
# ----------------------------------------------------------------------------------------------

def _grid(mean: float, lo: float, hi: float, r: int) -> Tuple[List[float], List[float]]:
    """Jennison and Turnbull's grid for Z_k with mean `mean`, trimmed to the continuation region (lo, hi), with
    Simpson's rule weights. 6r - 1 base points: dense within 3 SD of the mean, log-spaced out to 3 + 4 log r SD."""
    base = []
    for i in range(1, 6 * r):
        if i < r:
            x = mean - 3 - 4 * math.log(r / i)
        elif i <= 5 * r:
            x = mean - 3 + 3 * (i - r) / (2 * r)
        else:
            x = mean + 3 + 4 * math.log(r / (6 * r - i))
        base.append(x)
    pts = [x for x in base if lo < x < hi]
    if not math.isinf(lo):
        pts.insert(0, lo)
    if not math.isinf(hi):
        pts.append(hi)
    if len(pts) < 2:   # continuation region far from the mean: two points spanning it carry negligible mass
        pts = [lo if not math.isinf(lo) else hi - 1.0, hi if not math.isinf(hi) else lo + 1.0]
    z: List[float] = []
    w: List[float] = []
    for j in range(len(pts) - 1):
        a, b = pts[j], pts[j + 1]
        z.extend((a, (a + b) / 2))
    z.append(pts[-1])
    w = [0.0] * len(z)
    for j in range(len(pts) - 1):
        d = pts[j + 1] - pts[j]
        w[2 * j] += d / 6
        w[2 * j + 1] += 4 * d / 6
        w[2 * j + 2] += d / 6
    return z, w


class _Recursion:
    """Holds the sub-density of Z_k on the continuation region and steps it forward one look at a time.

    Information is on the scale of the planned maximum (I_k = t_k). drift = theta x sqrt(I_max), so the mean of
    Z_k is drift x sqrt(t_k); drift 0 is the null hypothesis.
    """

    def __init__(self, drift: float, sides: int, r: int):
        self.drift, self.sides, self.r = drift, sides, r
        self.t_prev: Optional[float] = None
        self.z: List[float] = []
        self.h: List[float] = []

    def crossing(self, t: float, b: float) -> Tuple[float, float]:
        """First-crossing probabilities (upper, lower) at a look at information t with boundary b."""
        if self.t_prev is None:
            mean = self.drift * math.sqrt(t)
            return _sf(b - mean), (_cdf(-b - mean) if self.sides == 2 else 0.0)
        dt = t - self.t_prev
        sd, sk, skm = math.sqrt(dt), math.sqrt(t), math.sqrt(self.t_prev)
        shift = self.drift * dt
        up = lo = 0.0
        for zi, hi in zip(self.z, self.h):
            centre = zi * skm + shift
            up += hi * _sf((b * sk - centre) / sd)
            if self.sides == 2:
                lo += hi * _cdf((-b * sk - centre) / sd)
        return up, lo

    def advance(self, t: float, b: float) -> None:
        """Move the sub-density to the look at information t, keeping only paths inside the boundary b."""
        mean = self.drift * math.sqrt(t)
        lo_b = -b if self.sides == 2 else -math.inf
        z2, w2 = _grid(mean, lo_b, b, self.r)
        if self.t_prev is None:
            h2 = [w * _phi(z - mean) for z, w in zip(z2, w2)]
        else:
            dt = t - self.t_prev
            sd, sk, skm = math.sqrt(dt), math.sqrt(t), math.sqrt(self.t_prev)
            shift = self.drift * dt
            scale = sk / sd * _INV_SQRT_2PI
            src = [(zi * skm + shift, hi) for zi, hi in zip(self.z, self.h) if hi > 1e-300]
            exp = math.exp
            h2 = []
            for zj, wj in zip(z2, w2):
                a = zj * sk
                acc = 0.0
                for c, hi in src:
                    u = (a - c) / sd
                    acc += hi * exp(-0.5 * u * u)
                h2.append(wj * acc * scale)
        self.z, self.h, self.t_prev = z2, h2, t


def _check_fractions(fractions: Sequence[float]) -> List[float]:
    t = [float(x) for x in fractions]
    if not t:
        raise ValueError("at least one look")
    if any(x <= 0 for x in t) or any(b <= a for a, b in zip(t, t[1:])):
        raise ValueError("information fractions must be positive and strictly increasing")
    return t


def _solve_boundary(rec: _Recursion, t: float, target: float, sides: int) -> float:
    """The boundary whose first-crossing probability at this look equals target (bisection)."""
    if target <= _TINY:
        return math.inf
    if rec.t_prev is None:
        return N01.inv_cdf(1 - target / sides) if target / sides < 1 else 0.0
    lo, hi = 0.0, 40.0

    def f(b: float) -> float:
        u, l_ = rec.crossing(t, b)
        return u + l_

    if f(lo) <= target:
        return 0.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if f(mid) > target:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-10:
            break
    return (lo + hi) / 2


def _boundaries_for_spend(t: List[float], cumulative: List[float], sides: int, r: int) -> List[float]:
    rec = _Recursion(0.0, sides, r)
    bounds: List[float] = []
    prev = 0.0
    for k, (tk, ck) in enumerate(zip(t, cumulative)):
        b = _solve_boundary(rec, tk, max(0.0, ck - prev), sides)
        bounds.append(b)
        prev = ck
        if k < len(t) - 1:
            rec.advance(tk, b)
    return bounds


def boundaries(information_fractions: Sequence[float], alpha: float = 0.05, spending: str = "obf", sides: int = 2,
               r: int = 16) -> Dict[str, Any]:
    """Efficacy boundaries (z scale) for looks at the given information fractions, the last one spending all of alpha.

    sides = 2: reject when |Z_k| >= b_k (symmetric, either direction). sides = 1: reject when Z_k >= b_k.
    alpha is the total two-sided level for sides = 2 and the one-sided level for sides = 1.
    """
    if sides not in (1, 2):
        raise ValueError("sides is 1 or 2")
    t = _check_fractions(information_fractions)
    cum = [alpha_spent(x / t[-1] if x < t[-1] else 1.0, alpha, spending, sides) for x in t]
    cum[-1] = alpha
    b = _boundaries_for_spend(t, cum, sides, r)
    nominal = [(sides * _sf(x)) if not math.isinf(x) else 0.0 for x in b]
    return {"information_fractions": t, "boundaries": b, "nominal_p": nominal, "cumulative_alpha": cum, "alpha": alpha,
            "spending": spending, "sides": sides,
            "method": f"Lan-DeMets {'O-Brien-Fleming' if spending == 'obf' else 'Pocock'}-type spending, recursive integration "
                      f"(Jennison and Turnbull ch. 19, r = {r})"}


def crossing_probabilities(bounds: Sequence[float], information_fractions: Sequence[float], drift: float = 0.0,
                           sides: int = 2, r: int = 16) -> Dict[str, Any]:
    """Probability of first crossing each boundary under a drift (0 = no effect). Returns per-look upper/lower and totals."""
    t = _check_fractions(information_fractions)
    rec = _Recursion(drift, sides, r)
    up, lo = [], []
    for k, (tk, b) in enumerate(zip(t, bounds)):
        u, l_ = rec.crossing(tk, b)
        up.append(u)
        lo.append(l_)
        if k < len(t) - 1:
            rec.advance(tk, b)
    stop = [a + b for a, b in zip(up, lo)]
    reach_last = 1 - sum(stop[:-1])
    expected_t = sum(tk * s for tk, s in zip(t[:-1], stop[:-1])) + t[-1] * reach_last
    return {"upper": up, "lower": lo, "total": sum(stop), "expected_information": expected_t}


def constant_boundaries(looks: int, alpha: float = 0.05, kind: str = "pocock", sides: int = 2, r: int = 16) -> Dict[str, Any]:
    """The classic equally spaced designs: Pocock (1977) constant boundary c, or O'Brien-Fleming (1979) c x sqrt(K / k).

    Used to validate the integration against Jennison and Turnbull's Tables 2.1 and 2.3.
    """
    t = [(k + 1) / looks for k in range(looks)]

    def shape(c: float) -> List[float]:
        return [c] * looks if kind == "pocock" else [c / math.sqrt(x) for x in t]

    lo, hi = 1.0, 6.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if crossing_probabilities(shape(mid), t, 0.0, sides, r)["total"] > alpha:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-9:
            break
    c = (lo + hi) / 2
    return {"constant": c, "boundaries": shape(c), "information_fractions": t, "kind": kind}


def _power_drift(bounds: List[float], t: List[float], power: float, sides: int, r: int, alpha: float) -> float:
    """The drift at which the design rejects with probability `power` (secant, then bisection as a safeguard)."""
    za = N01.inv_cdf(1 - alpha / sides)
    zb = N01.inv_cdf(power)

    def g(d: float) -> float:
        return crossing_probabilities(bounds, t, d, sides, r)["total"] - power

    a, b = za + zb, (za + zb) * 1.15
    ga, gb = g(a), g(b)
    for _ in range(30):
        if gb == ga:
            break
        c = b - gb * (b - a) / (gb - ga)
        a, ga = b, gb
        b, gb = c, g(c)
        if abs(gb) < 1e-9:
            return b
    lo, hi = 0.0, 3 * (za + zb)   # safeguard
    for _ in range(80):
        mid = (lo + hi) / 2
        if g(mid) < 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def inflation_factor(information_fractions: Sequence[float], alpha: float = 0.05, power: float = 0.80, spending: str = "obf",
                     sides: int = 2, r: int = 16, bounds: Optional[Sequence[float]] = None) -> Dict[str, Any]:
    """Maximum sample of the group-sequential design divided by the fixed-horizon sample with the same alpha and power.

    R = (drift that gives the stated power with these boundaries / (z_{1-alpha/sides} + z_power))^2. Also returns the
    expected information (as a share of the maximum) under no effect and under the planned effect.
    """
    t = _check_fractions(information_fractions)
    b = list(bounds) if bounds is not None else boundaries(t, alpha, spending, sides, r)["boundaries"]
    fixed = N01.inv_cdf(1 - alpha / sides) + N01.inv_cdf(power)
    drift = _power_drift(b, t, power, sides, r, alpha)
    h1 = crossing_probabilities(b, t, drift, sides, r)
    h0 = crossing_probabilities(b, t, 0.0, sides, r)
    return {"inflation_factor": (drift / fixed) ** 2, "drift": drift, "fixed_drift": fixed, "boundaries": b,
            "information_fractions": t, "expected_information_h1": h1["expected_information"],
            "expected_information_h0": h0["expected_information"], "type_i_error": h0["total"], "power": h1["total"],
            "stop_probability_h1": [u + l_ for u, l_ in zip(h1["upper"], h1["lower"])]}


# ----------------------------------------------------------------------------------------------
# plans and looks for a two-proportion comparison
# ----------------------------------------------------------------------------------------------

def sequential_plan(p_control: float, p_treatment: float, looks: int = 5, alpha: float = 0.05, power: float = 0.80,
                    ratio: float = 1.0, spending: str = "obf", information_fractions: Optional[Sequence[float]] = None,
                    sides: int = 2, r: int = 16, design_effect: float = 1.0) -> Dict[str, Any]:
    """Per-arm entrants at each look of a group-sequential two-proportion test.

    Maximum per arm = fixed-horizon size x inflation factor x design effect, raised to the readout floor if
    needed. Looks whose arms would fall below the floor are marked: take them for sample ratio and guardrails
    only; they are not efficacy looks and spend no alpha (Lan-DeMets lets a skipped look carry its alpha forward).
    """
    t = _check_fractions(information_fractions or [(k + 1) / looks for k in range(looks)])
    if abs(t[-1] - 1.0) > 1e-9:
        raise ValueError("the last planned look is at information fraction 1")
    fixed = stats.sample_size(p_control, p_treatment, alpha, power, ratio, two_sided=(sides == 2), apply_readout_floor=False)
    inf = inflation_factor(t, alpha, power, spending, sides, r)
    pw_c = math.ceil(fixed["n_control"] * inf["inflation_factor"] * design_effect - 1e-9)
    pw_t = math.ceil(pw_c * ratio - 1e-9)
    n_c, n_t, by, floor = stats._apply_floor(pw_c, pw_t, p_control, p_treatment, ratio)
    rows = []
    for k, (tk, bk) in enumerate(zip(t, inf["boundaries"])):
        c_k, t_k = math.ceil(n_c * tk - 1e-9), math.ceil(n_t * tk - 1e-9)
        readable = c_k >= floor["n_control"] and t_k >= floor["n_treatment"]
        rows.append({"look": k + 1, "information_fraction": round(tk, 4), "holdout_n": c_k, "treatment_n": t_k,
                     "boundary_z": None if math.isinf(bk) else round(bk, 4),
                     "nominal_p": 0.0 if math.isinf(bk) else sides * _sf(bk),
                     "cumulative_alpha": round(alpha_spent(tk, alpha, spending, sides) if k < len(t) - 1 else alpha, 6),
                     "efficacy_look": readable,
                     "note": None if readable else "below the readout floor: check sample ratio and guardrails only, no efficacy decision"})
    out = {"design": "group_sequential", "spending": spending, "looks": len(t), "information_fractions": t, "alpha": alpha,
           "power": power, "sides": sides, "p_control": p_control, "p_treatment": p_treatment, "ratio": ratio,
           "design_effect": design_effect, "fixed_horizon_n_control": fixed["n_control"], "fixed_horizon_n_treatment": fixed["n_treatment"],
           "inflation_factor": round(inf["inflation_factor"], 4), "max_n_control": n_c, "max_n_treatment": n_t,
           "size_set_by": by, "looks_detail": rows,
           "expected_n_control_if_effect": round(n_c * inf["expected_information_h1"]),
           "expected_n_control_if_no_effect": round(n_c * inf["expected_information_h0"]),
           "rule": ("at each efficacy look compute z and the information fraction; stop for efficacy when |z| reaches the boundary "
                    "(engine/measurement/sequential.py look). Record every look in the experiment registry. Looks may move: the "
                    "boundary is recomputed from the information actually reached")}
    if by == "readout floor":
        out["sizing_note"] = (f"power alone needs {pw_c:,} holdout entrants at the maximum, but the readout needs {floor['rule']}; "
                              "the floor, not power, set the maximum")
    return out


def information_fraction(n_control: float, n_treatment: float, planned_n_control: float, planned_n_treatment: float) -> float:
    """Statistical information reached, as a share of the planned maximum: (1/n_c + 1/n_t) planned over observed."""
    return (1 / planned_n_control + 1 / planned_n_treatment) / (1 / n_control + 1 / n_treatment)


def look(z: float, information_fraction: float, previous_fractions: Sequence[float] = (), alpha: float = 0.05,
         spending: str = "obf", sides: int = 2, final: bool = False, r: int = 16) -> Dict[str, Any]:
    """The decision at one look of a group-sequential test.

    previous_fractions are the information fractions of the efficacy looks already taken (in order); the
    boundary now is recomputed from them and the current fraction, so looks need not fall on the planned
    fractions. The look is final when final=True or the fraction reaches 1; the final look spends all of alpha.
    Returns decision: "continue", "stop for efficacy" or "final read", with the boundary and the nominal p it implies.
    """
    t = _check_fractions(list(previous_fractions) + [information_fraction])
    is_final = final or information_fraction >= 1 - 1e-9
    cum = [alpha_spent(x, alpha, spending, sides) for x in t]
    if is_final:
        cum[-1] = alpha
    b = _boundaries_for_spend(t, cum, sides, r)[-1]
    crossed = (abs(z) >= b) if sides == 2 else (z >= b)
    direction = "treatment better" if z > 0 else "treatment worse"
    if crossed and not is_final:
        decision, detail = "stop for efficacy", f"|z| = {abs(z):.3f} reached the boundary {b:.3f}: {direction}. Stop and read the result"
    elif is_final:
        decision = "final read"
        detail = (f"significant at the final look: |z| = {abs(z):.3f} >= {b:.3f}, {direction}" if crossed else
                  f"not significant at the final look: |z| = {abs(z):.3f} < {b:.3f}; report as inconclusive, not as a win")
    else:
        decision, detail = "continue", f"|z| = {abs(z):.3f} is inside the boundary {b:.3f}: keep collecting; do not report a lift yet"
    return {"decision": decision, "significant": bool(crossed), "z": z, "boundary": None if math.isinf(b) else b,
            "nominal_p_threshold": 0.0 if math.isinf(b) else sides * _sf(b), "information_fraction": information_fraction,
            "look_number": len(t), "final": is_final, "cumulative_alpha": cum[-1], "spending": spending, "detail": detail}


def look_from_counts(x_t: int, n_t: int, x_c: int, n_c: int, planned_n_treatment: int, planned_n_control: int,
                     previous_fractions: Sequence[float] = (), alpha: float = 0.05, spending: str = "obf", sides: int = 2,
                     final: bool = False, expected_holdout: Optional[float] = None, r: int = 16) -> Dict[str, Any]:
    """A group-sequential look from counts: z (pooled, as stats.readout), the information fraction, the decision,
    and a repeated confidence interval (difference +/- boundary x unpooled SE; Jennison and Turnbull ch. 9) that is
    valid at every look. Arms below the readout floor give "continue" and are not counted as a look."""
    out: Dict[str, Any] = {"treatment": {"n": n_t, "conversions": x_t}, "holdout": {"n": n_c, "conversions": x_c}}
    if expected_holdout is not None:
        out["srm"] = stats.srm_check([n_t, n_c], [1 - expected_holdout, expected_holdout])
        if out["srm"]["mismatch"]:
            out.update({"decision": "stop: sample ratio mismatch", "counted_as_look": False,
                        "detail": "the split does not match the plan; fix assignment before any efficacy decision"})
            return out
    if min(n_t, n_c) < MIN_ARM_FOR_LIFT or min(x_t, x_c) < MIN_EVENTS_FOR_LIFT:
        out.update({"decision": "continue", "counted_as_look": False,
                    "detail": (f"fewer than {MIN_ARM_FOR_LIFT} entrants or {MIN_EVENTS_FOR_LIFT} conversions in an arm: not an "
                               "efficacy look, no alpha spent; do not record it as one")})
        return out
    p_t, p_c = x_t / n_t, x_c / n_c
    pool = (x_t + x_c) / (n_t + n_c)
    se0 = math.sqrt(pool * (1 - pool) * (1 / n_t + 1 / n_c))
    z = (p_t - p_c) / se0 if se0 > 0 else 0.0
    frac = information_fraction(n_c, n_t, planned_n_control, planned_n_treatment)
    d = look(z, frac, previous_fractions, alpha, spending, sides, final, r)
    se1 = math.sqrt(p_t * (1 - p_t) / n_t + p_c * (1 - p_c) / n_c)
    out.update(d)
    out.update({"counted_as_look": True, "rate_treatment": p_t, "rate_holdout": p_c, "diff_abs": p_t - p_c})
    if d["boundary"] is not None:
        out["repeated_ci"] = [p_t - p_c - d["boundary"] * se1, p_t - p_c + d["boundary"] * se1]
    if d["decision"] == "stop for efficacy":
        out["caveat"] = ("an estimate taken at an early stop overstates the effect on average; report the repeated confidence "
                         "interval, and treat the point estimate as an upper-leaning guess")
    return out


# ----------------------------------------------------------------------------------------------
# always-valid inference: the mixture SPRT
# ----------------------------------------------------------------------------------------------

def msprt(x_t: int, n_t: int, x_c: int, n_c: int, tau: float, alpha: float = 0.05, theta0: float = 0.0,
          previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Always-valid p-value and confidence sequence for p_t - p_c by the normal-mixture SPRT (Johari et al. 2017, 2022).

    tau is the spread of the mixing distribution over the true difference: set it to the smallest difference worth
    acting on (the MDE, absolute). previous is the output of the last call (or a registry record of it), from which
    the running minimum p-value and the running intersection of the confidence sequence are carried forward. Looks
    below the readout floor return "continue" without updating anything.
    """
    if tau <= 0:
        raise ValueError("tau must be positive (use the absolute MDE)")
    prev_p = 1.0 if not previous else float(previous.get("p_value", 1.0))
    prev_cs = None if not previous else previous.get("confidence_sequence")
    out: Dict[str, Any] = {"treatment": {"n": n_t, "conversions": x_t}, "holdout": {"n": n_c, "conversions": x_c},
                           "tau": tau, "alpha": alpha, "theta0": theta0, "method": "normal-mixture SPRT (Johari, Koomen, Pekelis and Walsh)"}
    if min(n_t, n_c) < MIN_ARM_FOR_LIFT or min(x_t, x_c) < MIN_EVENTS_FOR_LIFT:
        out.update({"decision": "continue", "read": "not readable", "p_value": prev_p, "confidence_sequence": prev_cs,
                    "note": f"fewer than {MIN_ARM_FOR_LIFT} entrants or {MIN_EVENTS_FOR_LIFT} conversions in an arm: nothing is updated"})
        return out
    p_t, p_c = x_t / n_t, x_c / n_c
    d = p_t - p_c
    v = p_t * (1 - p_t) / n_t + p_c * (1 - p_c) / n_c
    if v <= 0:
        out.update({"decision": "continue", "read": "not readable", "p_value": prev_p, "confidence_sequence": prev_cs,
                    "note": "no variation in either arm yet"})
        return out
    tau2 = tau * tau
    log_lr = 0.5 * math.log(v / (v + tau2)) + tau2 * (d - theta0) ** 2 / (2 * v * (v + tau2))
    p_now = min(1.0, math.exp(-log_lr)) if log_lr > -700 else 1.0
    p = min(prev_p, p_now)
    radius = math.sqrt(2 * v * (v + tau2) / tau2 * (math.log(1 / alpha) + 0.5 * math.log((v + tau2) / v)))
    lo, hi = d - radius, d + radius
    if prev_cs:
        lo, hi = max(lo, prev_cs[0]), min(hi, prev_cs[1])
    out.update({"rate_treatment": p_t, "rate_holdout": p_c, "diff_abs": d, "log_likelihood_ratio": log_lr,
                "p_value": p, "confidence_sequence": [lo, hi]})
    if lo > hi:
        out.update({"decision": "stop: inconsistent", "read": "not readable",
                    "note": "the confidence sequence is empty: the rates moved during the test (a launch, a seasonal swing); investigate"})
    elif p <= alpha:
        better = lo > theta0   # p <= alpha means theta0 left the (intersected) confidence sequence at some look
        out.update({"decision": "stop for efficacy", "read": "causal",
                    "note": f"always-valid p = {p:.4g} <= {alpha}: {'treatment better' if better else 'treatment worse'}. Valid no matter "
                            "how often the test was checked; report the confidence sequence as the interval"})
    else:
        out.update({"decision": "continue", "read": "causal, inconclusive",
                    "note": "keep collecting; the confidence sequence includes no effect"})
    return out


# ----------------------------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------------------------

def _floats(text: Optional[str]) -> List[float]:
    return [float(x) for x in text.split(",") if x.strip()] if text else []


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="lcm sequential", description="Group-sequential and always-valid testing")
    sub = ap.add_subparsers(dest="cmd", required=True, help="what to compute")
    spending_help = "obf (O'Brien-Fleming: very strict early, near fixed at the end) or pocock (same bar every look); default obf"
    alpha_help = "false positive rate across all looks (default 0.05)"
    count_help = {"xt": "conversions in treatment so far", "nt": "people in treatment so far",
                  "xc": "conversions in the holdout so far", "nc": "people in the holdout so far",
                  "planned-nt": "people planned in treatment at the final look", "planned-nc": "people planned in the holdout at the final look"}
    b = sub.add_parser("boundaries", help="Lan-DeMets efficacy boundaries",
                       description="The z value each planned look must clear to stop early for efficacy")
    b.add_argument("--looks", type=int, default=5, help="number of equally spaced looks, the last one final (default 5)")
    b.add_argument("--fractions", help="comma-separated information fractions, last = 1 (default equally spaced)")
    b.add_argument("--spending", choices=SPENDING, default="obf", help=spending_help)
    b.add_argument("--alpha", type=float, default=0.05, help="false positive rate across all looks (default 0.05, two-sided)")
    b.add_argument("--one-sided", action="store_true", help="test for a lift only, not a drop (default: two-sided)")
    p = sub.add_parser("plan", help="per-arm entrants at each look for a two-proportion test",
                       description="People each arm needs at each look of a group-sequential conversion test")
    p.add_argument("--baseline", type=float, required=True, help="the holdout's expected conversion rate, 0.10 = 10%%")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--mde-abs", type=float, help="smallest lift worth detecting, in rate points (0.02 = 2 points)")
    g.add_argument("--mde-rel", type=float, help="smallest lift worth detecting, relative to baseline (0.20 = 20%% lift)")
    p.add_argument("--looks", type=int, default=5, help="number of equally spaced looks, the last one final (default 5)")
    p.add_argument("--fractions", help="comma-separated information fractions, last = 1 (default: equally spaced --looks)")
    p.add_argument("--spending", choices=SPENDING, default="obf", help=spending_help)
    p.add_argument("--alpha", type=float, default=0.05, help=alpha_help)
    p.add_argument("--power", type=float, default=0.80, help="chance of detecting a real lift of the MDE (default 0.80)")
    p.add_argument("--holdout", type=float, help="holdout share, e.g. 0.20")
    p.add_argument("--design-effect", type=float, default=1.0,
                   help="sample inflation for account-level tests, from `lcm cluster deff` (default 1.0, people randomized)")
    lk = sub.add_parser("look", help="the decision at one look, from z or from counts",
                        description="Continue, stop for efficacy, or the final read at one look, from z or from counts")
    lk.add_argument("--z", type=float, help="the observed z statistic now (with --t), instead of counts")
    lk.add_argument("--t", type=float, help="information fraction now (with --z)")
    for k in ("xt", "nt", "xc", "nc", "planned-nt", "planned-nc"):
        lk.add_argument(f"--{k}", type=int, help=f"{count_help[k]} (a count)")
    lk.add_argument("--previous", help="comma-separated information fractions of the efficacy looks already taken")
    lk.add_argument("--spending", choices=SPENDING, default="obf", help=spending_help)
    lk.add_argument("--alpha", type=float, default=0.05, help=alpha_help)
    lk.add_argument("--final", action="store_true", help="this is the final look: spend all remaining alpha")
    lk.add_argument("--expected-holdout", type=float, help="planned holdout share, e.g. 0.20, to check the split first (default: no check)")
    m = sub.add_parser("msprt", help="always-valid p-value and confidence sequence",
                       description="An always-valid p-value and confidence sequence, safe to check as often as you like")
    for k in ("xt", "nt", "xc", "nc"):
        m.add_argument(f"--{k}", type=int, required=True, help=f"{count_help[k]} (a count)")
    gm = m.add_mutually_exclusive_group(required=True)
    gm.add_argument("--tau", type=float, help="spread of the prior over the true difference, in rate points; set it to the absolute MDE")
    gm.add_argument("--mde-abs", type=float, help="sets tau to the absolute MDE")
    m.add_argument("--alpha", type=float, default=0.05, help="false positive rate, however often you look (default 0.05)")
    m.add_argument("--previous-p", type=float, help="the always-valid p-value from the last look, to carry forward")
    m.add_argument("--previous-cs", help="lower,upper of the confidence sequence so far")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "boundaries":
            fr = _floats(a.fractions) or [(k + 1) / a.looks for k in range(a.looks)]
            print(json.dumps(boundaries(fr, a.alpha, a.spending, 1 if a.one_sided else 2), indent=2))
        elif a.cmd == "plan":
            p2 = a.baseline + (a.mde_abs if a.mde_abs is not None else a.baseline * a.mde_rel)
            ratio = (1 - a.holdout) / a.holdout if a.holdout else 1.0
            fr = _floats(a.fractions) or None
            print(json.dumps(sequential_plan(a.baseline, p2, len(fr) if fr else a.looks, a.alpha, a.power, ratio, a.spending, fr,
                                             design_effect=a.design_effect), indent=2))
        elif a.cmd == "look":
            prev = _floats(a.previous)
            if a.z is not None:
                if a.t is None:
                    raise ValueError("--z needs --t (the information fraction now)")
                print(json.dumps(look(a.z, a.t, prev, a.alpha, a.spending, final=a.final), indent=2))
            else:
                need = [a.xt, a.nt, a.xc, a.nc, a.planned_nt, a.planned_nc]
                if any(v is None for v in need):
                    raise ValueError("give --z and --t, or all of --xt --nt --xc --nc --planned-nt --planned-nc")
                print(json.dumps(look_from_counts(a.xt, a.nt, a.xc, a.nc, a.planned_nt, a.planned_nc, prev, a.alpha, a.spending,
                                                  final=a.final, expected_holdout=a.expected_holdout), indent=2))
        elif a.cmd == "msprt":
            prev: Optional[Dict[str, Any]] = None
            if a.previous_p is not None or a.previous_cs:
                prev = {"p_value": a.previous_p if a.previous_p is not None else 1.0,
                        "confidence_sequence": _floats(a.previous_cs) or None}
            print(json.dumps(msprt(a.xt, a.nt, a.xc, a.nc, a.tau if a.tau else a.mde_abs, a.alpha, previous=prev), indent=2))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
