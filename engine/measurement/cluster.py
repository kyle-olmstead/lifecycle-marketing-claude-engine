"""
Clustered experiments: when the randomized unit is an account, not a person.

A B2B flow that holds out whole accounts (every contact at a company lands in the same arm) has entrants
that are not independent: two people at one firm share a budget, a champion and a renewal date, so they
convert together. Treating them as independent understates the standard error and turns noise into
"significant" lifts. This module supplies the statistics for that case:

  icc_anova            intraclass correlation of a 0/1 outcome by the one-way ANOVA estimator, unequal
                       account sizes allowed, with an approximate F-based interval
  design_effect        1 + ((1 + cv^2) x m_bar - 1) x ICC: the variance inflation of the pooled rate when
                       accounts hold m_bar entrants on average with coefficient of variation cv
  cluster_sample_size  accounts and entrants per arm for a two-proportion comparison, with Student t
                       quantiles when accounts are few, never below the readout floor
  cluster_readout      treatment vs holdout from account-level totals: ratio-estimator (delta method)
                       standard error, Satterthwaite degrees of freedom, SRM tested on accounts
  icc_from_duckdb      the ICC straight from the canonical tables (users.account_id plus a per-user 0/1
                       outcome), with event_outcome_sql() to build the outcome from track_events

    python3 engine/measurement/cluster.py icc --data examples/brands/ridgeview_saas/data --event report_created
    python3 engine/measurement/cluster.py deff --icc 0.05 --mean-size 8 --cv 0.6
    python3 engine/measurement/cluster.py size --baseline 0.10 --mde-abs 0.03 --icc 0.05 --mean-size 8 --cv 0.6 --holdout 0.20
    python3 engine/measurement/cluster.py readout clusters.json --expected-holdout 0.20

clusters.json holds account-level totals per arm: {"treatment": [[entrants, conversions], ...], "holdout": [...]}.

Sources
  ICC estimator: the one-way ANOVA estimator with the m0 adjustment for unequal cluster sizes, Donner (1986),
    "A review of inference procedures for the intraclass correlation coefficient in the one-way random effects
    model", International Statistical Review 54:67-82; Donner and Klar, Design and Analysis of Cluster
    Randomization Trials in Health Research (2000), ch. 5 and 6; for binary outcomes, Ridout, Demetrio and
    Firth (1999), "Estimating intraclass correlation for binary data", Biometrics 55:137-148.
  ICC interval: Searle, Linear Models (1971), the F-based interval with m0 in place of the common size;
    exact for balanced normal data, approximate here, and labeled so.
  Design effect with unequal sizes: Kerry and Bland (2001), "Unequal cluster sizes for trials in English and
    Welsh general practice", Statistics in Medicine 20:377-390; Eldridge, Ashby and Kerry (2006), "Sample
    size for cluster randomized trials: effect of coefficient of variation of cluster size and analysis
    method", International Journal of Epidemiology 35:1292-1300. With cv the population coefficient of
    variation, 1 + ((1 + cv^2) m_bar - 1) ICC equals 1 + (sum m_i^2 / sum m_i - 1) ICC, which is exact for
    the entrant-weighted (pooled) rate this module reads.
  Readout: the sample estimate of the variance of a ratio estimator, Cochran, Sampling Techniques, 3rd ed.
    (1977), ch. 6: v(r) = (1 / (k x m_bar^2)) x sum (y_i - r m_i)^2 / (k - 1), which is k/(k-1) x
    sum (y_i - r m_i)^2 / N^2 (no finite population correction), as used for clustered binary data by Rao and
    Scott (1992), "A simple method for the analysis of clustered binary data", Biometrics 48:577-585; degrees
    of freedom by Satterthwaite (1946), Biometrics Bulletin 2:110-114, which for a small holdout of accounts is
    far fewer than accounts minus 2 and so protects the error rate.
"""
from __future__ import annotations

if __package__ in (None, ""):  # run as a file (python3 engine/measurement/cluster.py): make the repository importable
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from engine.measurement import stats
from engine.measurement.stats import MIN_ARM_FOR_LIFT, MIN_CLUSTERS_PER_ARM, MIN_EVENTS_FOR_LIFT

Cluster = Tuple[float, float]   # (entrants in the account, conversions or revenue total in the account)
FEW_CLUSTERS = 30               # below this many accounts in an arm, say that the t reference is doing real work


# ----------------------------------------------------------------------------------------------
# F distribution (for the ICC interval)
# ----------------------------------------------------------------------------------------------

def _f_cdf(x: float, d1: float, d2: float) -> float:
    if x <= 0:
        return 0.0
    return stats.betainc(d1 / 2.0, d2 / 2.0, d1 * x / (d1 * x + d2))


def _f_ppf(q: float, d1: float, d2: float) -> float:
    lo, hi = 0.0, 1.0
    while _f_cdf(hi, d1, d2) < q:
        hi *= 2
        if hi > 1e12:
            return math.inf
    for _ in range(200):
        mid = (lo + hi) / 2
        if _f_cdf(mid, d1, d2) < q:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-12 * max(1.0, hi):
            break
    return (lo + hi) / 2


# ----------------------------------------------------------------------------------------------
# ICC and design effect
# ----------------------------------------------------------------------------------------------

def _clean(clusters: Sequence[Sequence[float]]) -> List[Cluster]:
    out: List[Cluster] = []
    for c in clusters:
        m, y = float(c[0]), float(c[1])
        if m <= 0:
            continue
        if y < 0:
            raise ValueError("a cluster total cannot be negative")
        out.append((m, y))
    return out


def size_summary(sizes: Sequence[float]) -> Dict[str, float]:
    """Mean account size, population coefficient of variation, and sum m^2 / sum m (the size that drives the design effect)."""
    sizes = [float(s) for s in sizes if s > 0]
    if not sizes:
        raise ValueError("no clusters")
    k, n = len(sizes), sum(sizes)
    m_bar = n / k
    var = sum((s - m_bar) ** 2 for s in sizes) / k
    return {"clusters": k, "entrants": n, "mean_size": m_bar, "cv": math.sqrt(var) / m_bar,
            "size_weighted_mean": sum(s * s for s in sizes) / n}


def icc_anova(clusters: Sequence[Sequence[float]], alpha: float = 0.05) -> Dict[str, Any]:
    """Intraclass correlation of a binary outcome by the one-way ANOVA estimator (Donner 1986; Donner and Klar 2000).

    clusters: (entrants, conversions) per account. With k accounts, N entrants, p_i the account rate and p the
    overall rate:
        MSB = sum m_i (p_i - p)^2 / (k - 1)
        MSW = sum m_i p_i (1 - p_i) / (N - k)
        m0  = (N - sum m_i^2 / N) / (k - 1)
        ICC = (MSB - MSW) / (MSB + (m0 - 1) MSW)
    The estimate can be negative by chance; icc_for_design truncates it at 0 for sizing.
    """
    cl = _clean(clusters)
    k = len(cl)
    n = sum(m for m, _ in cl)
    out: Dict[str, Any] = {"clusters": k, "entrants": n, "method": "one-way ANOVA estimator with m0 for unequal sizes (Donner 1986)"}
    if k < 2:
        out.update({"icc": None, "icc_for_design": None, "note": "fewer than two accounts: no between-account information"})
        return out
    if any(y > m for m, y in cl):
        raise ValueError("conversions exceed entrants in an account: icc_anova takes a 0/1 outcome")
    if n - k <= 0:
        out.update({"icc": None, "icc_for_design": 0.0, "note": "every account has one entrant: there is no clustering to correct for, design effect 1"})
        return out
    p = sum(y for _, y in cl) / n
    msb = sum(m * (y / m - p) ** 2 for m, y in cl) / (k - 1)
    msw = sum(m * (y / m) * (1 - y / m) for m, y in cl) / (n - k)
    m0 = (n - sum(m * m for m, _ in cl) / n) / (k - 1)
    sizes = size_summary([m for m, _ in cl])
    out.update({"overall_rate": p, "msb": msb, "msw": msw, "m0": m0, "mean_size": sizes["mean_size"], "cv": sizes["cv"]})
    if msw == 0 and msb == 0:
        out.update({"icc": None, "icc_for_design": None, "note": "the outcome does not vary at all: the ICC is undefined"})
        return out
    icc = (msb - msw) / (msb + (m0 - 1) * msw)
    out["icc"] = icc
    out["icc_for_design"] = max(0.0, icc)
    if msw > 0:
        f_obs = msb / msw
        f_hi = _f_ppf(1 - alpha / 2, k - 1, n - k)
        f_lo = _f_ppf(alpha / 2, k - 1, n - k)
        fl, fu = f_obs / f_hi, (f_obs / f_lo if f_lo > 0 else math.inf)
        lo = (fl - 1) / (fl + m0 - 1)
        hi = 1.0 if math.isinf(fu) else (fu - 1) / (fu + m0 - 1)
        out["icc_ci"] = [max(-1.0 / (m0 - 1) if m0 > 1 else -1.0, lo), min(1.0, hi)]
        out["icc_ci_method"] = ("approximate: Searle's F interval with m0 for the common account size; exact only for balanced "
                                "normal data. Size with the upper limit when the decision is expensive")
    notes = []
    if k < FEW_CLUSTERS:
        notes.append(f"estimated from {k} accounts: an ICC from fewer than {FEW_CLUSTERS} accounts is imprecise, read the interval")
    if icc < 0:
        notes.append("the estimate is below zero, which is chance at this sample; it is set to 0 for sizing")
    if notes:
        out["note"] = "; ".join(notes)
    return out


def design_effect(icc: float, mean_cluster_size: Optional[float] = None, cluster_sizes: Optional[Sequence[float]] = None,
                  cv: float = 0.0) -> Dict[str, Any]:
    """Variance inflation of the pooled rate under account randomization.

    DE = 1 + ((1 + cv^2) x m_bar - 1) x ICC (Kerry and Bland 2001; Eldridge, Ashby and Kerry 2006). With equal
    account sizes (cv = 0) this is the familiar 1 + (m - 1) x ICC. Give cluster_sizes to compute m_bar and the
    population cv from the accounts themselves, or mean_cluster_size and cv from a plan.
    """
    if icc < 0:
        icc = 0.0
    if icc > 1:
        raise ValueError("ICC is at most 1")
    if cluster_sizes is not None:
        s = size_summary(cluster_sizes)
        m_bar, cv = s["mean_size"], s["cv"]
    elif mean_cluster_size is not None:
        m_bar = float(mean_cluster_size)
    else:
        raise ValueError("give mean_cluster_size or cluster_sizes")
    if m_bar < 1:
        raise ValueError("mean_cluster_size is at least 1")
    if cv < 0:
        raise ValueError("cv cannot be negative")
    m_eff = (1 + cv * cv) * m_bar
    de = 1 + (m_eff - 1) * icc
    return {"design_effect": de, "icc": icc, "mean_cluster_size": m_bar, "cv": cv, "size_weighted_mean": m_eff,
            "formula": "1 + ((1 + cv^2) x mean size - 1) x ICC (Kerry and Bland 2001; Eldridge, Ashby and Kerry 2006)",
            "effective_entrants_per_entrant": 1 / de}


# ----------------------------------------------------------------------------------------------
# sizing
# ----------------------------------------------------------------------------------------------

def _satterthwaite_equal(k_c: float, k_t: float) -> float:
    """Satterthwaite df for two arms whose variances are proportional to 1 / accounts (equal per-account variance)."""
    v_c, v_t = 1.0 / k_c, 1.0 / k_t
    return (v_c + v_t) ** 2 / (v_c ** 2 / (k_c - 1) + v_t ** 2 / (k_t - 1))


def cluster_sample_size(p_control: float, p_treatment: float, icc: float, mean_cluster_size: float, cv: float = 0.0,
                        alpha: float = 0.05, power: float = 0.80, ratio: float = 1.0, two_sided: bool = True,
                        small_sample: bool = True) -> Dict[str, Any]:
    """Accounts and entrants per arm for a two-proportion comparison when whole accounts are randomized.

    Entrants = the individually randomized size x the design effect; accounts = entrants / mean account size,
    rounded up. With small_sample (the default) the normal quantiles are replaced by Student t quantiles at the
    Satterthwaite degrees of freedom of the planned accounts, iterated until the account counts settle, because
    the readout uses that t reference. Neither arm is planned below MIN_CLUSTERS_PER_ARM accounts or the
    entrant and event floor of the readout. ratio = treatment accounts / holdout accounts.
    """
    if not (0 < p_control < 1 and 0 < p_treatment < 1) or p_control == p_treatment:
        raise ValueError("rates must be in (0, 1) and differ")
    de = design_effect(icc, mean_cluster_size, cv=cv)
    d = de["design_effect"]
    m_bar = de["mean_cluster_size"]
    qa = stats._z(1 - alpha / 2) if two_sided else stats._z(1 - alpha)
    qb = stats._z(power)
    n_ind = stats._n_control_for(p_control, p_treatment, ratio, qa, qb)
    k_c = max(2, math.ceil(n_ind * d / m_bar - 1e-9))
    k_t = max(2, math.ceil(n_ind * d * ratio / m_bar - 1e-9))
    df = None
    if small_sample:
        for _ in range(100):
            df = _satterthwaite_equal(k_c, k_t)
            ta = stats.t_ppf(1 - alpha / 2, df) if two_sided else stats.t_ppf(1 - alpha, df)
            tb = stats.t_ppf(power, df)
            n_ind = stats._n_control_for(p_control, p_treatment, ratio, ta, tb)
            new_c = max(2, math.ceil(n_ind * d / m_bar - 1e-9))
            new_t = max(2, math.ceil(n_ind * d * ratio / m_bar - 1e-9))
            if (new_c, new_t) == (k_c, k_t):
                break
            k_c, k_t = new_c, new_t
    power_k_c, power_k_t = k_c, k_t
    k_c = max(k_c, MIN_CLUSTERS_PER_ARM)
    k_t = max(k_t, MIN_CLUSTERS_PER_ARM)
    n_c, n_t = math.ceil(k_c * m_bar - 1e-9), math.ceil(k_t * m_bar - 1e-9)
    floor = stats.readout_floor(p_control, p_treatment, ratio)
    if n_c < floor["n_control"] or n_t < floor["n_treatment"]:
        k_c = max(k_c, math.ceil(floor["n_control"] / m_bar - 1e-9))
        k_t = max(k_t, math.ceil(floor["n_treatment"] / m_bar - 1e-9))
        n_c, n_t = math.ceil(k_c * m_bar - 1e-9), math.ceil(k_t * m_bar - 1e-9)
    size_set_by = "power" if (k_c, k_t) == (power_k_c, power_k_t) else "readout floor"
    if small_sample:
        df = _satterthwaite_equal(k_c, k_t)
    out = {"holdout_clusters": k_c, "treatment_clusters": k_t, "holdout_entrants": n_c, "treatment_entrants": n_t,
           "individual_n_control": math.ceil(n_ind - 1e-9), "design_effect": round(d, 4), "icc": de["icc"],
           "mean_cluster_size": m_bar, "cv": cv, "p_control": p_control, "p_treatment": p_treatment, "alpha": alpha, "power": power,
           "ratio": ratio, "two_sided": two_sided, "df": None if df is None else round(df, 1), "size_set_by": size_set_by,
           "method": ("two-proportion formula x design effect, accounts = entrants / mean size"
                      + ("; Student t quantiles at the Satterthwaite df of the planned accounts" if small_sample else ""))}
    if size_set_by == "readout floor":
        out["sizing_note"] = (f"power alone needs {power_k_c} holdout / {power_k_t} treatment accounts, but the readout needs at least "
                              f"{MIN_CLUSTERS_PER_ARM} accounts, {MIN_ARM_FOR_LIFT} entrants and {MIN_EVENTS_FOR_LIFT} expected conversions "
                              f"per arm; the floor, not power, set this size")
    return out


# ----------------------------------------------------------------------------------------------
# readout
# ----------------------------------------------------------------------------------------------

def _ratio_arm(cl: List[Cluster]) -> Dict[str, float]:
    """Pooled rate and its ratio-estimator variance from account totals: k/(k-1) x sum (y_i - r m_i)^2 / N^2."""
    k = len(cl)
    n = sum(m for m, _ in cl)
    y = sum(v for _, v in cl)
    r = y / n
    var = (k / (k - 1)) * sum((v - r * m) ** 2 for m, v in cl) / (n * n) if k > 1 else math.inf
    return {"clusters": k, "n": n, "total": y, "rate": r, "var": var}


def ratio_difference(treatment: Sequence[Sequence[float]], holdout: Sequence[Sequence[float]], alpha: float = 0.05) -> Dict[str, Any]:
    """Treatment minus holdout per-entrant mean (a rate for 0/1 outcomes, revenue per entrant for money) from account totals.

    Standard error from the ratio-estimator (delta method) variance in each arm, t reference with Satterthwaite
    degrees of freedom. No floors here: cluster_readout() applies them for conversion outcomes.
    """
    a, b = _ratio_arm(_clean(treatment)), _ratio_arm(_clean(holdout))
    diff = a["rate"] - b["rate"]
    se = math.sqrt(a["var"] + b["var"])
    if a["clusters"] < 2 or b["clusters"] < 2 or se == 0 or math.isinf(se):
        return {"treatment": a, "holdout": b, "diff": diff, "se": None, "df": None, "t": None, "p_value": None, "ci": None}
    df = (a["var"] + b["var"]) ** 2 / ((a["var"] ** 2 / (a["clusters"] - 1) if a["var"] else 0.0)
                                       + (b["var"] ** 2 / (b["clusters"] - 1) if b["var"] else 0.0))
    t = diff / se
    q = stats.t_ppf(1 - alpha / 2, df)
    return {"treatment": a, "holdout": b, "diff": diff, "se": se, "df": df, "t": t, "p_value": 2 * stats.t_sf(abs(t), df),
            "ci": [diff - q * se, diff + q * se]}


def cluster_readout(treatment: Sequence[Sequence[float]], holdout: Sequence[Sequence[float]], alpha: float = 0.05,
                    expected_holdout: Optional[float] = None) -> Dict[str, Any]:
    """Account-randomized treatment vs holdout on a conversion outcome, read from account-level totals.

    treatment, holdout: (entrants, conversions) per account. Reports the pooled rates, the difference with a
    t interval from the cluster-robust standard error, each arm's observed design effect, and the naive
    (independent entrants) standard error for comparison. The sample ratio check counts accounts, because
    accounts, not people, were assigned. Refuses to compute a lift with fewer than MIN_CLUSTERS_PER_ARM accounts,
    MIN_ARM_FOR_LIFT entrants or MIN_EVENTS_FOR_LIFT conversions in either arm.
    """
    t_cl, c_cl = _clean(treatment), _clean(holdout)
    if any(y > m for m, y in t_cl + c_cl):
        raise ValueError("conversions exceed entrants in an account: cluster_readout takes 0/1 outcomes; use ratio_difference for money")
    n_t, n_c = sum(m for m, _ in t_cl), sum(m for m, _ in c_cl)
    x_t, x_c = sum(y for _, y in t_cl), sum(y for _, y in c_cl)
    out: Dict[str, Any] = {"unit": "account", "treatment": {"clusters": len(t_cl), "n": n_t, "conversions": x_t},
                           "holdout": {"clusters": len(c_cl), "n": n_c, "conversions": x_c}}
    if expected_holdout is not None:
        out["srm"] = stats.srm_check([len(t_cl), len(c_cl)], [1 - expected_holdout, expected_holdout])
        out["srm"]["counted"] = "accounts (the unit that was assigned), not entrants"
    if min(len(t_cl), len(c_cl)) < MIN_CLUSTERS_PER_ARM or min(n_t, n_c) < MIN_ARM_FOR_LIFT or min(x_t, x_c) < MIN_EVENTS_FOR_LIFT:
        out["read"] = "not readable"
        out["note"] = (f"fewer than {MIN_CLUSTERS_PER_ARM} accounts, {MIN_ARM_FOR_LIFT} entrants or {MIN_EVENTS_FOR_LIFT} conversions in an "
                       "arm: no lift percentage is computed. Report counts qualitatively.")
        return out
    r = ratio_difference(t_cl, c_cl, alpha)
    p_t, p_c = r["treatment"]["rate"], r["holdout"]["rate"]
    naive_se = math.sqrt(p_t * (1 - p_t) / n_t + p_c * (1 - p_c) / n_c)

    def deff(arm: Dict[str, float]) -> Optional[float]:
        base = arm["rate"] * (1 - arm["rate"]) / arm["n"]
        return None if base == 0 else round(arm["var"] / base, 3)

    out.update({"rate_treatment": p_t, "rate_holdout": p_c, "diff_abs": r["diff"], "alpha": alpha,
                "observed_design_effect": {"treatment": deff(r["treatment"]), "holdout": deff(r["holdout"])},
                "naive_se": naive_se, "method": "ratio estimator (delta method) SE from account totals, t with Satterthwaite df"})
    if r["se"] is None:
        out["read"] = "not readable"
        out["note"] = "the outcome does not vary between accounts in either arm: no interval can be formed"
        return out
    lo, hi = r["ci"]
    out.update({"se": r["se"], "df": round(r["df"], 1), "t": r["t"], "p_value": r["p_value"], "diff_ci": [lo, hi],
                "lift_rel": (p_t - p_c) / p_c if p_c > 0 else None,
                "se_inflation_vs_naive": round(r["se"] / naive_se, 3) if naive_se > 0 else None})
    if out.get("srm", {}).get("mismatch"):
        out["read"] = "not readable"
        out["note"] = "sample ratio mismatch in accounts: fix assignment before reading any result"
        return out
    out["read"] = "causal" if (lo > 0 or hi < 0) else "causal, inconclusive"
    notes = []
    if lo <= 0 <= hi:
        notes.append("the interval includes zero: the data are consistent with no effect. Keep collecting or report as inconclusive")
    if min(len(t_cl), len(c_cl)) < FEW_CLUSTERS:
        notes.append(f"fewer than {FEW_CLUSTERS} accounts in an arm: the t reference with {r['df']:.1f} degrees of freedom widens the "
                     "interval on purpose, and one account can still move the result; name the largest accounts in the report")
    if notes:
        out["note"] = "; ".join(notes)
    return out


# ----------------------------------------------------------------------------------------------
# the canonical tables
# ----------------------------------------------------------------------------------------------

def _q(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def event_outcome_sql(event_name: str, start: Optional[str] = None, end: Optional[str] = None) -> str:
    """Per-user 0/1 outcome: did the person fire event_name (optionally between two ISO dates, end exclusive)."""
    cond = [f"e.event_name = {_q(event_name)}"]
    if start:
        cond.append(f"e.event_at >= CAST({_q(start)} AS TIMESTAMP)")
    if end:
        cond.append(f"e.event_at < CAST({_q(end)} AS TIMESTAMP)")
    return ("SELECT u.user_id, MAX(CASE WHEN " + " AND ".join(cond) + " THEN 1 ELSE 0 END) AS outcome "
            "FROM users u LEFT JOIN track_events e ON e.user_id = u.user_id GROUP BY u.user_id")


def cluster_totals_sql(outcome_sql: str, cluster_column: str = "account_id") -> str:
    """Account-level totals from a per-user outcome query (columns user_id, outcome 0/1), joined to users."""
    if not cluster_column.replace("_", "").isalnum():
        raise ValueError("cluster_column must be a plain column name")
    return (f"WITH outcome AS ({outcome_sql}) "
            f"SELECT u.{cluster_column} AS cluster_id, COUNT(*) AS m, SUM(CAST(o.outcome AS INTEGER)) AS y "
            f"FROM users u JOIN outcome o ON o.user_id = u.user_id "
            f"WHERE u.{cluster_column} IS NOT NULL AND u.{cluster_column} <> '' "
            f"GROUP BY u.{cluster_column} ORDER BY cluster_id")


def icc_from_duckdb(con: Any, outcome_sql: str, cluster_column: str = "account_id", alpha: float = 0.05) -> Dict[str, Any]:
    """ICC, account sizes and the design effect from the canonical tables in a DuckDB connection.

    outcome_sql returns one row per user with columns user_id and outcome (0/1); event_outcome_sql() builds the
    common case. People with no account are left out of the estimate and counted in unassigned_users.
    """
    rows = con.execute(cluster_totals_sql(outcome_sql, cluster_column)).fetchall()
    unassigned = con.execute(f"SELECT COUNT(*) FROM users WHERE {cluster_column} IS NULL OR {cluster_column} = ''").fetchone()[0]
    clusters = [(int(m), int(y)) for _cid, m, y in rows]
    est = icc_anova(clusters, alpha)
    est["unassigned_users"] = int(unassigned)
    if not clusters:
        est["note"] = (f"no user has {cluster_column}: every person is their own unit, so a person-level holdout has design effect 1. "
                       f"Load {cluster_column} on users before randomizing accounts")
        est["design_effect"] = 1.0
        return est
    if est.get("icc_for_design") is not None:
        de = design_effect(est["icc_for_design"], cluster_sizes=[m for m, _ in clusters])
        est["design_effect"] = de["design_effect"]
        est["design_effect_formula"] = de["formula"]
    return est


# ----------------------------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------------------------

def _load_clusters(path: str) -> Dict[str, List[Cluster]]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or "treatment" not in doc or "holdout" not in doc:
        raise ValueError(f"{path}: expected {{\"treatment\": [[entrants, conversions], ...], \"holdout\": [...]}}")
    return {k: [(float(c[0]), float(c[1])) for c in doc[k]] for k in ("treatment", "holdout")}


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Clustered (account-randomized) experiments: ICC, design effect, sizing and readout")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("icc", help="estimate the ICC from users.account_id and a per-user 0/1 outcome")
    i.add_argument("--data", required=True, help="a data folder with users (account_id) and track_events")
    i.add_argument("--event", required=True, help="the outcome: did the person fire this event")
    i.add_argument("--start")
    i.add_argument("--end")
    d = sub.add_parser("deff", help="design effect from ICC and account sizes")
    d.add_argument("--icc", type=float, required=True)
    d.add_argument("--mean-size", type=float, required=True)
    d.add_argument("--cv", type=float, default=0.0, help="coefficient of variation of account size (sd / mean)")
    s = sub.add_parser("size", help="accounts and entrants per arm")
    s.add_argument("--baseline", type=float, required=True)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--mde-abs", type=float)
    g.add_argument("--mde-rel", type=float)
    s.add_argument("--icc", type=float, required=True)
    s.add_argument("--mean-size", type=float, required=True)
    s.add_argument("--cv", type=float, default=0.0)
    s.add_argument("--holdout", type=float, help="holdout share of accounts, e.g. 0.20")
    s.add_argument("--alpha", type=float, default=0.05)
    s.add_argument("--power", type=float, default=0.80)
    r = sub.add_parser("readout", help="treatment vs holdout from account-level totals")
    r.add_argument("input", help='JSON: {"treatment": [[entrants, conversions], ...], "holdout": [...]}')
    r.add_argument("--expected-holdout", type=float)
    r.add_argument("--alpha", type=float, default=0.05)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "icc":
            from engine.data.load import connect
            con, _notes = connect(a.data)
            print(json.dumps(icc_from_duckdb(con, event_outcome_sql(a.event, a.start, a.end)), indent=2))
        elif a.cmd == "deff":
            print(json.dumps(design_effect(a.icc, a.mean_size, cv=a.cv), indent=2))
        elif a.cmd == "size":
            p2 = a.baseline + (a.mde_abs if a.mde_abs is not None else a.baseline * a.mde_rel)
            ratio = (1 - a.holdout) / a.holdout if a.holdout else 1.0
            print(json.dumps(cluster_sample_size(a.baseline, p2, a.icc, a.mean_size, a.cv, a.alpha, a.power, ratio), indent=2))
        elif a.cmd == "readout":
            doc = _load_clusters(a.input)
            print(json.dumps(cluster_readout(doc["treatment"], doc["holdout"], a.alpha, a.expected_holdout), indent=2))
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
