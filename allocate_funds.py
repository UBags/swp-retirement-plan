# Copyright (c) 2025 Uddipan Bagchi. All rights reserved.
# See LICENSE in the project root for license information.package com.costheta.cortexa.action


"""
allocate_funds.py
=================
Portfolio allocation optimiser for Indian mutual funds.

Reads Fund_Metrics_Output.csv (produced by GetFundData.py / fund_analyser_v4.py)
and allocates a lump-sum across eligible funds to **maximise quality-adjusted
weighted-average return** subject to std_dev, max_drawdown, history, per-fund,
per-type constraints.

The objective is:  maximise dot(w, adj_ret + λ * quality_norm)
where quality_norm is Combined_Ratio normalised to [0, 1], and λ is 10% of
the return spread.  This makes the optimizer prefer higher-quality funds
(better Sortino × Calmar) when returns are similar, without overriding the
return signal.  All constraints use pure adj_ret (quality is objective-only).

Ten configurable inputs (all have defaults):
  1. total_money_L      – Total corpus to allocate (Rs lakhs)              [required]
  2. min_return         – Min weighted-avg expected return (Worst_Exp_Ret_%) [6.85%]
  3. max_std_dev        – Max weighted-avg std dev (Std_Dev_5Y / 10Y)       [0.99%]
  4. max_dd             – Max weighted-avg Max_Drawdown (abs value)          [0.75%]
  5. min_history_years  – Min fund age required, in years                    [5]
  6. max_per_fund_pct   – Max allocation to any single fund (%)              [7%]
  7. max_per_type_pct   – Max allocation to any fund type (%)                [20%]
  8. min_per_fund_pct   – Min allocation to any selected fund (%)            [1%]
                          Funds below this are dropped iteratively (mixed-integer
                          constraint handled via iterative pruning).
  9. max_fund_std_pct   – Exclude individual funds with std_dev >= this (%)  [1.5%]
 10. max_fund_dd_pct    – Exclude individual funds with |max_dd| >= this (%)  [1.5%]

Constraint relaxation order (outermost → innermost):
  The two per-fund eligibility filters (9, 10) are relaxed first and together,
  in 0.25% increments (1.5% → 1.75% → 2.0% → ...) up to a ceiling of 5%.
  For each universe, the portfolio-level constraints are then relaxed:
    Step 0: All constraints tight.
    Step 1: Relax per-type cap (remove entirely).
    Step 2: Also relax max_dd.
    Step 3: Also relax max_std_dev.
    Step 4: Also lower min_return.
    Step 5: Also raise max_per_fund.

Usage
-----
  # Interactive (prompts for inputs):
  python allocate_funds.py

  # Command-line (all flags optional):
  python allocate_funds.py \\
    --total 350 \\
    --min-return 6.85 \\
    --max-std-dev 0.99 \\
    --max-dd 0.75 \\
    --min-history 5 \\
    --max-per-fund 7 \\
    --max-per-type 20 \\
    --min-per-fund 1 \\
    --max-fund-std 1.5 \\
    --max-fund-dd  1.5 \\
    --input Fund_Metrics_Output.csv \\
    --output allocation_result.csv

Dependencies
------------
  pip install pandas numpy scipy
"""

import argparse
import json
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import milp, minimize, LinearConstraint, Bounds as ScipyBounds

warnings.filterwarnings("ignore", category=RuntimeWarning)


# ═══════════════════════════════════════════════════════════════════════════════
# AMC EXTRACTION UTILITY
# ═══════════════════════════════════════════════════════════════════════════════

def extract_amc(fund_name: str) -> str:
    """
    Extract the AMC (Asset Management Company) identifier from a fund name
    by taking the first word, title-cased for consistency.

    This is intentionally simple — the user confirmed that the first word
    of the fund name reliably identifies the AMC in the AMFI dataset:
      "ICICI Prudential Money Market Fund"  →  "Icici"
      "HDFC Short Duration Fund"            →  "Hdfc"
      "Aditya Birla Sun Life Savings Fund"  →  "Aditya"
      "Nippon India Floater Fund"           →  "Nippon"
      "SBI SAVINGS FUND"                    →  "Sbi"

    Using the first word consistently means all ICICI funds group together,
    all HDFC funds group together, etc. — which is exactly what the AMC
    concentration constraint needs.
    """
    s = fund_name.strip()
    if not s:
        return "Unknown"
    return s.split()[0].title()


# ═══════════════════════════════════════════════════════════════════════════════
# DEFAULT PATHS
# ═══════════════════════════════════════════════════════════════════════════════

from configuration import config, get_project_root

DEFAULT_INPUT  = str(get_project_root() / config.allocator_default_input)
DEFAULT_OUTPUT = str(get_project_root() / config.allocator_default_output)


# ═══════════════════════════════════════════════════════════════════════════════
# DATA LOADING & PREPARATION
# ═══════════════════════════════════════════════════════════════════════════════

def load_and_filter(csv_path: str, min_history_months: int) -> pd.DataFrame:
    """
    Load Fund_Metrics_Output.csv and return eligible funds with working columns:
      adj_ret  – Worst_Exp_Ret_% as decimal (e.g. 0.0685)
      adj_std  – Std_Dev_5Y if available, else Std_Dev_10Y
      adj_dd   – Max_DD_5Y  if available, else Max_DD_10Y  (negative fraction)

    Eligibility (hard, non-relaxable):
      - History_Months >= min_history_months
      - adj_ret  is finite and > 0
      - adj_std  is finite and > 0
      - adj_dd   is finite
    """
    df = pd.read_csv(csv_path)
    print(f"  Input CSV: {Path(csv_path).resolve()}")
    df.columns = [c.strip() for c in df.columns]

    # History filter
    df = df[df["History_Months"] >= min_history_months].copy()

    # Working columns: expected return = min of 10Y/5Y/3Y CAGRs (excludes 1Y as too short-term)
    _ret_cols = [c for c in ["10Y_CAGR", "5Y_CAGR", "3Y_CAGR"] if c in df.columns]
    for c in _ret_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["adj_ret"] = df[_ret_cols].min(axis=1)

    # adj_std: use the WORST (highest) std dev across available periods.
    # Philosophy: pessimistic on risk — if the fund was more volatile over
    # 10 years than over 5 years, the solver should see the worse number.
    sd3 = pd.to_numeric(df.get("Std_Dev_3Y", np.nan), errors="coerce")
    sd5 = pd.to_numeric(df.get("Std_Dev_5Y", np.nan), errors="coerce")
    sd10 = pd.to_numeric(df.get("Std_Dev_10Y", np.nan), errors="coerce")
    # Take the max of available values (worst-case std); NaN cells are skipped by max(axis=1)
    _sd_all = pd.concat([sd3, sd5, sd10], axis=1)
    df["adj_std"] = _sd_all.max(axis=1)  # max = worst-case std

    # adj_dd: use the WORST (most negative) drawdown across available periods.
    # Max_DD values are typically negative or zero (worst = most negative).
    dd3 = pd.to_numeric(df.get("Max_DD_3Y", np.nan), errors="coerce")
    dd5 = pd.to_numeric(df.get("Max_DD_5Y", np.nan), errors="coerce")
    dd10 = pd.to_numeric(df.get("Max_DD_10Y", np.nan), errors="coerce")
    _dd_all = pd.concat([dd3, dd5, dd10], axis=1)
    df["adj_dd"] = _dd_all.min(axis=1)  # min = most negative = worst-case dd

    # Quality score: Combined_Ratio (prefer 5Y → 10Y → 3Y fallback)
    # Combined_Ratio = sqrt(Sortino × Calmar); higher = better risk-adjusted quality.
    # Used as a tiebreaker in the objective when funds have similar returns.
    cr5  = pd.to_numeric(df.get("Combined_Ratio_5Y",  np.nan), errors="coerce")
    cr10 = pd.to_numeric(df.get("Combined_Ratio_10Y", np.nan), errors="coerce")
    cr3  = pd.to_numeric(df.get("Combined_Ratio_3Y",  np.nan), errors="coerce")
    df["adj_quality"] = cr5.where(cr5.notna() & (cr5 > 0),
                         cr10.where(cr10.notna() & (cr10 > 0), cr3))
    # Fill any remaining NaN with 0 (no quality bonus for funds with no ratio)
    df["adj_quality"] = df["adj_quality"].fillna(0.0)

    before = len(df)
    df = df[
        df["adj_ret"].notna() &
        df["adj_std"].notna() &
        df["adj_dd"].notna()
    ].copy().reset_index(drop=True)

    print(f"  Eligible funds (history + metrics): {len(df)} / {before}")

    # Derive AMC column (used by the per-AMC concentration constraint)
    if "AMC" not in df.columns:
        df["AMC"] = df["Fund Name"].apply(
            lambda n: extract_amc(str(n)) if pd.notna(n) else "Unknown"
        )

    return df


def apply_fund_filters(
    df: pd.DataFrame,
    max_fund_std: float,   # exclude funds with adj_std >= this (fraction, e.g. 0.015)
    max_fund_dd:  float,   # exclude funds with |adj_dd| >= this (fraction, e.g. 0.015)
) -> pd.DataFrame:
    """
    Apply per-fund eligibility filters for individual std_dev and max_dd limits.

    These are universe-narrowing filters, not portfolio-level weighted constraints.
    A fund is excluded if ANY of:
      - adj_std >= max_fund_std   (std_dev too high)
      - |adj_dd| >= max_fund_dd   (drawdown too deep)

    When max_fund_std or max_fund_dd is None / 0 / very large, that filter is skipped.
    Returns a filtered copy (reset index).
    """
    filtered = df.copy()
    n_before = len(filtered)

    if max_fund_std and max_fund_std < 1.0:   # sanity: skip if unreasonably large
        excluded_std = filtered["adj_std"] >= max_fund_std
        n_std = excluded_std.sum()
        filtered = filtered[~excluded_std]
    else:
        n_std = 0

    if max_fund_dd and max_fund_dd < 1.0:
        excluded_dd = filtered["adj_dd"].abs() >= max_fund_dd
        n_dd = excluded_dd.sum()
        filtered = filtered[~excluded_dd]
    else:
        n_dd = 0

    filtered = filtered.reset_index(drop=True)
    n_excluded = n_before - len(filtered)
    if n_excluded > 0:
        print(f"  Fund filters (std<{max_fund_std*100:.2f}%, |dd|<{max_fund_dd*100:.2f}%): "
              f"excluded {n_excluded} funds "
              f"({n_std} by std_dev, {n_dd} by max_dd) → {len(filtered)} remain")
    return filtered


# ═══════════════════════════════════════════════════════════════════════════════
# OPTIMISATION CORE — MILP (Mixed-Integer Linear Programming)
# ═══════════════════════════════════════════════════════════════════════════════
#
# Architecture:
#   For n candidate funds, the solver optimises 2n variables:
#     w_i  : continuous weight of fund i   (0 ≤ w_i ≤ 1)
#     y_i  : binary inclusion indicator    (y_i ∈ {0, 1})
#
#   Semi-continuous linking:
#     w_i ≤ max_per_fund × y_i    (if not selected, weight = 0)
#     w_i ≥ min_per_fund × y_i    (if selected, at least the floor)
#
#   This handles the "if selected, allocate at least X%" constraint natively —
#   no iterative pruning needed.  The HiGHS Branch-and-Cut solver guarantees
#   the global maximum within the configured optimality gap (default 0.01%).
#
# ═══════════════════════════════════════════════════════════════════════════════

def _solve(
    df:           pd.DataFrame,
    min_return:   float,
    max_std_dev:  float,
    max_dd:       float,
    max_per_fund: float,
    type_caps:    dict,
    min_per_fund: float = 0.0,
    n_restarts:   int = 5,      # kept for API compat; ignored by MILP
    time_limit:   float = 120.0,
    mode:         str = "fine",  # "coarse" = minimise risk, "fine" = maximise return
    blend_alpha:  float = -1.0,  # -1 = use mode; 0..1 = α×risk + (1-α)×(-return)
    max_per_amc:  float = 1.0,   # max total allocation fraction per AMC (1.0 = no limit)
) -> tuple:
    """
    Find the globally optimal portfolio allocation using MILP.

    Two modes:

    **fine** (default — original behaviour):
        Objective: maximise  dot(w, adj_ret + λ × quality_norm)
        std_dev and max_dd are hard ceiling constraints.

    **coarse** (risk-minimisation):
        Objective: minimise  dot(w, std + |dd|)  with small quality tiebreaker
        min_return is a hard floor constraint.  std_dev and max_dd ceilings
        are still enforced as safety backstops but the solver actively pushes
        risk below them.

    **blend_alpha** (0.0–1.0):
        When >= 0, overrides ``mode`` with a blended objective:
            objective = α × risk_obj  +  (1-α) × (-return_obj)
        where risk_obj  = std + |dd|  (normalised to [0, 1])
              return_obj = adj_ret    (normalised to [0, 1])
        α = 1.0 is pure risk minimisation; α = 0.0 is pure return maximisation.
        Quality tiebreaker (5% of range) is added to both components.
        The min_return constraint is ALWAYS enforced regardless of α.

    Common constraints (both modes):
      C1:  sum(w)          = 1                   (full investment)
      C2:  dot(w, ret)    >= min_return           (return floor)
      C3:  dot(w, std)    <= max_std_dev          (volatility ceiling)
      C4:  dot(w, |dd|)   <= max_dd               (drawdown ceiling)
      C5+: sum(w[type=T]) <= cap_T  for each type (per-type caps)
      C6:  w_i <= max_per_fund × y_i              (upper link)
      C7:  w_i >= min_per_fund × y_i              (lower link / semi-continuous)

    Returns (weights, feasible: bool).
    """
    n = len(df)
    if n == 0:
        return None, False

    ret = df["adj_ret"].values
    std = df["adj_std"].values
    dd  = df["adj_dd"].values     # negative fractions
    types = df["Fund Type"].values

    # ── Quick pre-feasibility check ───────────────────────────────────────
    if ret.max() < min_return - 1e-6:
        return None, False
    if std.min() > max_std_dev + 1e-6:
        return None, False
    if dd.max() < -max_dd - 1e-6:
        return None, False

    # ── Build objective vector ────────────────────────────────────────────
    raw_q = df["adj_quality"].values.copy()
    q_max = raw_q.max()
    quality_norm = (raw_q / q_max) if q_max > 1e-9 else np.zeros(n)

    dd_abs = np.abs(dd)

    if blend_alpha >= 0.0:
        # ── BLENDED: α × risk + (1-α) × (-return) ───────────────────────
        # Normalise both signals to [0, 1] so α blends meaningfully.
        risk_raw = std + dd_abs
        r_min, r_max = risk_raw.min(), risk_raw.max()
        risk_range = r_max - r_min if r_max > r_min else 1e-6
        risk_norm = (risk_raw - r_min) / risk_range

        ret_min, ret_max = ret.min(), ret.max()
        ret_range = ret_max - ret_min if ret_max > ret_min else 1e-6
        ret_norm = (ret - ret_min) / ret_range

        # Quality tiebreaker: 5% of blended signal range
        q_lambda = 0.05

        alpha = float(np.clip(blend_alpha, 0.0, 1.0))
        # milp MINIMISES: risk_norm is already "minimise", ret_norm needs negation
        blended = (alpha * (risk_norm - q_lambda * quality_norm)
                   + (1.0 - alpha) * (-ret_norm - q_lambda * quality_norm))
        c = np.concatenate([blended, np.zeros(n)])

    elif mode == "coarse":
        # ── COARSE: minimise risk with quality tiebreaker ─────────────────
        # Primary: minimise weighted-avg std_dev + weighted-avg |max_dd|.
        # Tiebreaker: prefer higher-quality funds when risk is comparable.
        # We scale the quality tiebreaker to ~10% of the risk signal range
        # so it nudges but never overrides the risk objective.
        risk_obj = std + dd_abs   # both are positive fractions
        risk_spread = risk_obj.max() - risk_obj.min() if n > 1 else 0.01
        quality_lambda = 0.10 * risk_spread
        # Minimise risk − λ·quality  (subtract quality to *prefer* higher quality)
        c = np.concatenate([risk_obj - quality_lambda * quality_norm,
                            np.zeros(n)])
    else:
        # ── FINE: maximise return with quality tiebreaker ─────────────────
        ret_spread = ret.max() - ret.min() if n > 1 else 0.01
        quality_lambda = 0.10 * ret_spread
        obj_ret = ret + quality_lambda * quality_norm
        # scipy.milp MINIMISES, so negate for maximisation.
        c = np.concatenate([-obj_ret, np.zeros(n)])

    # Integrality: w_i = continuous (0), y_i = integer (1)
    integrality = np.concatenate([np.zeros(n), np.ones(n)])

    # Bounds: w_i ∈ [0, 1], y_i ∈ [0, 1]  (integrality forces y to 0/1)
    bounds = ScipyBounds(lb=np.zeros(2 * n), ub=np.ones(2 * n))

    # ── Build constraint matrix rows ──────────────────────────────────────
    A_rows = []
    lb_vals = []
    ub_vals = []

    # C1: sum(w) = 1.0
    row_sum = np.concatenate([np.ones(n), np.zeros(n)])
    A_rows.append(row_sum)
    lb_vals.append(1.0)
    ub_vals.append(1.0)

    # C2: dot(w, ret) >= min_return
    row_ret = np.concatenate([ret, np.zeros(n)])
    A_rows.append(row_ret)
    lb_vals.append(min_return)
    ub_vals.append(np.inf)

    # C3: dot(w, std) <= max_std_dev
    row_std = np.concatenate([std, np.zeros(n)])
    A_rows.append(row_std)
    lb_vals.append(-np.inf)
    ub_vals.append(max_std_dev)

    # C4: dot(w, |dd|) <= max_dd   (dd values are negative, use abs)
    row_dd = np.concatenate([dd_abs, np.zeros(n)])
    A_rows.append(row_dd)
    lb_vals.append(-np.inf)
    ub_vals.append(max_dd)

    # C5+: per-type caps
    for ft, cap in type_caps.items():
        mask = (types == ft).astype(float)
        if mask.sum() == 0:
            continue
        row_type = np.concatenate([mask, np.zeros(n)])
        A_rows.append(row_type)
        lb_vals.append(-np.inf)
        ub_vals.append(cap)

    # C8+: per-AMC concentration caps  sum(w_i for i in AMC_k) <= max_per_amc
    # Only added when max_per_amc < 1.0 (i.e. the user specified a real limit).
    if max_per_amc < 1.0 - 1e-6 and "AMC" in df.columns:
        amc_col = df["AMC"].values
        for amc in np.unique(amc_col):
            amc_mask = (amc_col == amc).astype(float)
            if amc_mask.sum() < 2:
                # Only one fund from this AMC — the per-fund cap already
                # enforces the limit; no need for an extra row.
                continue
            row_amc = np.concatenate([amc_mask, np.zeros(n)])
            A_rows.append(row_amc)
            lb_vals.append(-np.inf)
            ub_vals.append(max_per_amc)

    # C6 & C7: Semi-continuous linking  w_i ↔ y_i
    for i in range(n):
        # Upper bound:  w_i - max_per_fund × y_i ≤ 0
        row_ub = np.zeros(2 * n)
        row_ub[i] = 1.0
        row_ub[n + i] = -max_per_fund
        A_rows.append(row_ub)
        lb_vals.append(-np.inf)
        ub_vals.append(0.0)

        # Lower bound:  w_i - min_per_fund × y_i ≥ 0
        if min_per_fund > 1e-6:
            row_lb = np.zeros(2 * n)
            row_lb[i] = 1.0
            row_lb[n + i] = -min_per_fund
            A_rows.append(row_lb)
            lb_vals.append(0.0)
            ub_vals.append(np.inf)

    # ── Assemble and solve ────────────────────────────────────────────────
    A_matrix = np.array(A_rows)
    constraints = LinearConstraint(A_matrix, lb_vals, ub_vals)

    options = {
        'disp': False,
        'time_limit': time_limit,
        'mip_rel_gap': 1e-4,       # 0.01% optimality gap
    }

    try:
        res = milp(
            c=c,
            integrality=integrality,
            bounds=bounds,
            constraints=constraints,
            options=options,
        )
    except Exception as e:
        print(f"    MILP solver error: {e}", flush=True)
        return None, False

    if not res.success:
        return None, False

    # ── Extract and clean weights ─────────────────────────────────────────
    weights = res.x[:n].copy()
    weights[weights < 1e-5] = 0.0

    if weights.sum() < 1e-10:
        return None, False

    weights /= weights.sum()

    # ── Verify feasibility ────────────────────────────────────────────────
    tol = 1e-4
    ok = (
        abs(weights.sum() - 1.0) < tol
        and np.dot(weights, ret) >= min_return - tol
        and np.dot(weights, std) <= max_std_dev + tol
        and np.dot(weights, dd_abs) <= max_dd + tol
        and np.all(weights >= -tol)
        and np.all(weights <= max_per_fund + tol)
    )
    if ok and type_caps:
        for ft, cap in type_caps.items():
            if weights[types == ft].sum() > cap + tol:
                ok = False
                break

    # Check min_per_fund compliance
    if ok and min_per_fund > 1e-6:
        active = weights > 1e-5
        below_floor = active & (weights < min_per_fund - tol)
        if below_floor.any():
            ok = False

    if not ok:
        # Solver reported success but post-check failed — rare but possible
        # with floating-point tolerances.  Clean up: zero out sub-floor funds.
        if min_per_fund > 1e-6:
            active = weights > 1e-5
            weights[active & (weights < min_per_fund * 0.5)] = 0.0
            if weights.sum() > 1e-10:
                weights /= weights.sum()
                # Re-check after cleanup
                ok = (
                    np.dot(weights, ret) >= min_return - tol
                    and np.dot(weights, std) <= max_std_dev + tol
                )
            else:
                ok = False

    return weights if ok else None, ok


def _solve_with_min_alloc(
    df:           pd.DataFrame,
    min_return:   float,
    max_std_dev:  float,
    max_dd:       float,
    max_per_fund: float,
    type_caps:    dict,
    min_per_fund: float,
    max_prune_rounds: int = 15,  # kept for API compat; MILP handles natively
    verbose: bool = True,
    mode:    str = "fine",
    blend_alpha: float = -1.0,
    max_per_amc:  float = 1.0,   # max total allocation fraction per AMC (1.0 = no limit)
) -> tuple:
    """
    MILP-native enforcement of minimum-per-fund allocation.

    The MILP formulation handles the semi-continuous "if selected, allocate
    at least min_per_fund" constraint directly via binary indicator variables.
    No iterative pruning is needed — this is a thin wrapper for API compat.

    Returns (weights_on_original_index | None, feasible: bool).
    """
    if verbose:
        alpha_str = f", α={blend_alpha:.3f}" if blend_alpha >= 0 else ""
        print(f"    MILP solving ({len(df)} funds, min_alloc={min_per_fund*100:.1f}%"
              f", mode={mode}{alpha_str}) ...",
              end=" ", flush=True)

    w, ok = _solve(
        df, min_return, max_std_dev, max_dd,
        max_per_fund, type_caps,
        min_per_fund=min_per_fund,
        mode=mode,
        blend_alpha=blend_alpha,
        max_per_amc=max_per_amc,
    )

    if verbose:
        if ok:
            n_sel = int((w > 1e-5).sum()) if w is not None else 0
            print(f"optimal ({n_sel} funds selected).", flush=True)
        else:
            print("infeasible.", flush=True)

    if not ok or w is None:
        return None, False

    return w, True


def _check_feasibility(
    w:           np.ndarray,
    df:          pd.DataFrame,
    min_return:  float,
    max_std_dev: float,
    max_dd:      float,
    max_per_fund: float,
    type_caps:   dict,
    tol:         float = 1e-4,
) -> bool:
    """Return True if w satisfies all active constraints within tol."""
    ret   = df["adj_ret"].values
    std   = df["adj_std"].values
    dd    = df["adj_dd"].values
    types = df["Fund Type"].values

    ok = (
        abs(w.sum() - 1.0)        < tol
        and np.dot(w, ret) >= min_return   - tol
        and np.dot(w, std) <= max_std_dev  + tol
        and np.dot(w, dd)  >= -max_dd      - tol
        and np.all(w >= -tol)
        and np.all(w <= max_per_fund + tol)
    )
    if ok and type_caps:
        for ft, cap in type_caps.items():
            if w[types == ft].sum() > cap + tol:
                return False
    return ok


# ═══════════════════════════════════════════════════════════════════════════════
# RELAXATION ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def optimise_with_relaxation(
    df:            pd.DataFrame,
    min_return:    float,
    max_std_dev:   float,
    max_dd:        float,
    max_per_fund:  float,
    max_per_type:  float,
    min_per_fund:  float,
    max_fund_std:  float,   # per-fund std_dev ceiling (fraction); 0 = no filter
    max_fund_dd:   float,   # per-fund |max_dd| ceiling (fraction); 0 = no filter
    mode:          str = "fine",   # "coarse" = minimise risk, "fine" = maximise return
    blend_alpha:   float = -1.0,   # -1 = use mode; 0..1 = blended objective
    max_per_amc:   float = 1.0,    # max total allocation fraction per AMC (1.0 = no limit)
) -> tuple:
    """
    Find a feasible allocation with two levels of relaxation.

    The ``mode`` parameter controls the MILP objective:
      - "coarse": minimise risk (std + |dd|) subject to a return floor.
      - "fine":   maximise return subject to risk ceilings (original behaviour).

    OUTER LOOP – fund-universe filters (relaxed first):
      Start at max_fund_std / max_fund_dd = initial values.
      If infeasible at all inner steps, widen both thresholds by +0.25%
      and retry, up to a ceiling of 5% (or no filter, whichever comes first).

    INNER LOOP – portfolio-level constraints (relaxed in order):
      Step 0: All portfolio constraints tight.
      Step 1: Remove per-type cap.
      Step 2: Relax max_dd.
      Step 3: Relax max_std_dev.
      Step 4: Lower min_return.
      Step 5: Raise max_per_fund.

    min_per_fund is enforced via iterative pruning at every step (never relaxed).

    Returns (weights_array | None, info_dict).
    """
    fund_types_all = df["Fund Type"].unique()

    def type_caps(cap):
        return {ft: cap for ft in fund_types_all}

    def solve(df_, mr, ms, md, mf, tc, verbose=False):
        if min_per_fund > 1e-6:
            return _solve_with_min_alloc(df_, mr, ms, md, mf, tc, min_per_fund,
                                         verbose=verbose, mode=mode,
                                         blend_alpha=blend_alpha,
                                         max_per_amc=max_per_amc)
        else:
            w, ok = _solve(df_, mr, ms, md, mf, tc, min_per_fund=0.0, mode=mode,
                           blend_alpha=blend_alpha, max_per_amc=max_per_amc)
            return (w, True) if ok else (None, False)

    def _try_inner(df_filtered, relaxations_so_far):
        """
        Interleaved relaxation: expand std+dd by 0.05% increments, then drop
        min_return by 0.1%, then expand again, alternating until feasible or
        ceiling is hit.

        Cycle:
          4 rounds of std+0.05%, dd+0.05%
          2 rounds of ret−0.10%     (std/dd stay where they are)
          4 rounds of std+0.05%, dd+0.05%
          2 rounds of ret−0.10%
          ... repeat

        Per-type cap: removed permanently after the first failure at Step 0.
        Ceiling: std+2.0%, dd+2.0%, ret−2.0% — give up beyond this.
        min_per_fund: never relaxed.

        Returns (weights, relaxations_list) or (None, None).
        """
        STD_DD_STEP  = 0.0005   # 0.05% per std/dd round
        RET_STEP     = 0.001    # 0.10% per return round
        STD_DD_CEIL  = 0.020    # +2.0% max expansion
        RET_CEIL     = 0.020    # −2.0% max reduction
        STD_DD_PER_CYCLE = 4    # expand std/dd this many times before touching ret
        RET_PER_CYCLE    = 2    # drop ret this many times before expanding again

        n = len(df_filtered)
        relaxations = list(relaxations_so_far)
        type_cap_removed = False

        # Running offsets from user targets
        d_std = 0.0   # cumulative std expansion (positive = looser)
        d_dd  = 0.0   # cumulative dd expansion  (positive = looser)
        d_ret = 0.0   # cumulative ret reduction  (positive = tighter ask removed)

        def _attempt(label, mr, ms, md):
            nonlocal type_cap_removed
            tc = {} if type_cap_removed else type_caps(max_per_type)

            # First try with type cap (if not already removed)
            if not type_cap_removed:
                print(f"    {label} [+type cap] ... ", end="", flush=True)
                w, ok = solve(df_filtered, mr, ms, md, max_per_fund, tc,
                              verbose=False)
                if ok:
                    print("ok", flush=True)
                    return w, ok
                print("✗", flush=True)

                # Then try without type cap
                print(f"    {label} [no type cap] ... ", end="", flush=True)
                w, ok = solve(df_filtered, mr, ms, md, max_per_fund, {},
                              verbose=False)
                if ok:
                    print("ok", flush=True)
                    type_cap_removed = True
                    relaxations.append(
                        f"per-type cap removed (was {max_per_type*100:.0f}%)")
                    return w, ok
                print("✗", flush=True)
                # From now on always try without cap — it never helped
                type_cap_removed = True
                return None, False
            else:
                print(f"    {label} ... ", end="", flush=True)
                w, ok = solve(df_filtered, mr, ms, md, max_per_fund, {},
                              verbose=False)
                print("ok" if ok else "✗", flush=True)
                return w, ok

        # ── Step 0: original constraints ──────────────────────────────────────
        cur_ret = min_return
        cur_std = max_std_dev
        cur_dd  = max_dd
        w, ok = _attempt(
            f"Step 0  ({n} funds, ret≥{cur_ret*100:.2f}%"
            f" std≤{cur_std*100:.3f}% dd≤{cur_dd*100:.3f}%)",
            cur_ret, cur_std, cur_dd)
        if ok:
            return w, relaxations

        # ── Interleaved relaxation loop ───────────────────────────────────────
        cycle_std_rounds_done = 0
        cycle_ret_rounds_done = 0
        phase = "std_dd"   # alternate between "std_dd" and "ret"
        std_dd_exhausted = False   # once True, never switch back to std_dd
        step_num = 1

        while True:
            if phase == "std_dd":
                # Expand std and dd by one step
                new_d_std = d_std + STD_DD_STEP
                new_d_dd  = d_dd  + STD_DD_STEP
                if new_d_std > STD_DD_CEIL + 1e-9:
                    # Ceiling hit on std/dd — switch permanently to ret drops
                    phase = "ret"
                    std_dd_exhausted = True
                    continue
                d_std = round(new_d_std, 6)
                d_dd  = round(new_d_dd,  6)
                cur_std = max_std_dev + d_std
                cur_dd  = max_dd      + d_dd
                cur_ret = min_return  - d_ret
                label = (f"Step {step_num}  std+{d_std*100:.2f}%"
                         f" dd+{d_dd*100:.2f}%"
                         f" → std≤{cur_std*100:.3f}%"
                         f" dd≤{cur_dd*100:.3f}%"
                         f" ret≥{cur_ret*100:.2f}%")
                cycle_std_rounds_done += 1

            else:  # phase == "ret"
                new_d_ret = d_ret + RET_STEP
                if new_d_ret > RET_CEIL + 1e-9:
                    # Ceiling hit on ret — give up
                    break
                d_ret   = round(new_d_ret, 6)
                cur_ret = max(0.01, min_return - d_ret)
                cur_std = max_std_dev + d_std
                cur_dd  = max_dd      + d_dd
                label = (f"Step {step_num}  ret−{d_ret*100:.2f}%"
                         f" → ret≥{cur_ret*100:.2f}%"
                         f" (std≤{cur_std*100:.3f}% dd≤{cur_dd*100:.3f}%)")
                cycle_ret_rounds_done += 1

            w, ok = _attempt(label, cur_ret, cur_std, cur_dd)
            step_num += 1

            if ok:
                # Record what was relaxed relative to original targets
                if d_std > 1e-9:
                    relaxations.append(
                        f"max_std_dev relaxed: {max_std_dev*100:.3f}%"
                        f" → {cur_std*100:.3f}%")
                if d_dd > 1e-9:
                    relaxations.append(
                        f"max_dd relaxed: {max_dd*100:.3f}%"
                        f" → {cur_dd*100:.3f}%")
                if d_ret > 1e-9:
                    relaxations.append(
                        f"min_return lowered: {min_return*100:.2f}%"
                        f" → {cur_ret*100:.2f}%")
                return w, relaxations

            # Phase transition logic
            if phase == "std_dd" and cycle_std_rounds_done >= STD_DD_PER_CYCLE:
                phase = "ret"
                cycle_std_rounds_done = 0
                cycle_ret_rounds_done = 0
            elif phase == "ret" and cycle_ret_rounds_done >= RET_PER_CYCLE:
                if std_dd_exhausted:
                    # std/dd ceiling already reached — stay in ret phase,
                    # just reset the counter to keep going
                    cycle_ret_rounds_done = 0
                else:
                    phase = "std_dd"
                    cycle_ret_rounds_done = 0
                    cycle_std_rounds_done = 0

        print(f"    All relaxation steps exhausted "
              f"(std+{d_std*100:.2f}% dd+{d_dd*100:.2f}%"
              f" ret−{d_ret*100:.2f}% ceiling reached).", flush=True)
        return None, None

    # ── Outer loop: fund-universe filters ─────────────────────────────────────
    # Build the sequence of (std_threshold, dd_threshold) pairs to try.
    # Start with the user-specified values, then widen by 0.25% each step.
    # A value of 0 means "no filter" — skip that axis entirely.
    FILTER_STEP    = 0.0025    # 0.25%
    FILTER_CEILING = 0.05      # stop widening at 5% (effectively no filter)

    def _filter_steps(initial: float) -> list:
        """Generate threshold values from initial up to ceiling, then no-filter.

        Always starts with the user-specified value, then widens by FILTER_STEP
        increments up to max(initial, FILTER_CEILING).  If initial > ceiling,
        the only step with an actual filter is [initial], then [0.0] fallback.
        """
        if initial <= 1e-6:
            return [0.0]   # no filter; just one iteration
        steps = [round(initial, 6)]
        effective_ceil = max(initial, FILTER_CEILING)
        v = initial + FILTER_STEP
        while v <= effective_ceil + 1e-9:
            steps.append(round(v, 6))
            v += FILTER_STEP
        steps.append(0.0)  # final fallback: no filter at all
        return steps

    std_steps = _filter_steps(max_fund_std)
    dd_steps  = _filter_steps(max_fund_dd)

    # Zip the two step sequences together; pad with 0 if one runs out first
    n_outer = max(len(std_steps), len(dd_steps))
    std_seq = std_steps + [0.0] * (n_outer - len(std_steps))
    dd_seq  = dd_steps  + [0.0] * (n_outer - len(dd_steps))

    prev_n_filtered = -1   # track fund count to skip redundant outer iterations

    for outer_i, (cur_std, cur_dd) in enumerate(zip(std_seq, dd_seq)):
        # Build fund universe for this threshold pair
        outer_relaxations = []

        if outer_i == 0:
            # First attempt: initial (tightest) filters
            label = (f"std_dev < {cur_std*100:.2f}%, |dd| < {cur_dd*100:.2f}%"
                     if (cur_std > 0 or cur_dd > 0) else "no fund filters")
            print(f"\n  [Outer 0] Fund filters: {label}", flush=True)
        else:
            parts = []
            if cur_std > 0:
                parts.append(f"std_dev < {cur_std*100:.2f}%")
            else:
                parts.append("std_dev: no limit")
            if cur_dd > 0:
                parts.append(f"|dd| < {cur_dd*100:.2f}%")
            else:
                parts.append("|dd|: no limit")
            label = ", ".join(parts)
            print(f"\n  [Outer {outer_i}] Widening fund filters → {label}", flush=True)
            outer_relaxations.append(
                f"fund filters widened: {label}"
            )

        df_filtered = apply_fund_filters(df, cur_std, cur_dd)
        if len(df_filtered) < 2:
            print(f"    Only {len(df_filtered)} fund(s) pass filters — skipping.")
            continue

        # Skip if widening didn't add any new funds — same universe means same
        # inner-loop result, so re-running is pure waste.
        if len(df_filtered) == prev_n_filtered and outer_i > 0:
            print(f"    Same {len(df_filtered)} funds as previous filter — skipping.",
                  flush=True)
            continue
        prev_n_filtered = len(df_filtered)

        w, relaxations = _try_inner(df_filtered, outer_relaxations)

        if w is not None:
            n_selected = int((w > 1e-5).sum())
            print(f"  ✓ Feasible solution found: {n_selected} funds selected.", flush=True)
            # Remap weights from filtered df back to full df index
            full_w = np.zeros(len(df))
            name_to_orig = {name: pos for pos, name in enumerate(df["Fund Name"])}
            for i, name in enumerate(df_filtered["Fund Name"]):
                if name in name_to_orig:
                    full_w[name_to_orig[name]] = w[i]

            # Determine effective portfolio constraints from the winning relaxations
            # (parse them back; simpler to just re-derive from what _try_inner returned)
            eff_std = max_std_dev
            eff_dd  = max_dd
            eff_ret = min_return
            eff_mf  = max_per_fund
            eff_tc  = max_per_type
            tc_active = True
            for r in relaxations:
                if "per-type cap removed" in r:
                    tc_active = False; eff_tc = None
                if "max_dd relaxed" in r:
                    eff_dd = float(r.split("→")[1].strip().rstrip("%")) / 100
                if "max_std_dev relaxed" in r:
                    eff_std = float(r.split("→")[1].strip().rstrip("%")) / 100
                if "min_return lowered" in r:
                    eff_ret = float(r.split("→")[1].strip().rstrip("%")) / 100
                if "max_per_fund raised" in r:
                    eff_mf = float(r.split("→")[1].strip().rstrip("%")) / 100

            return full_w, _info(
                relaxations, eff_ret, eff_std, eff_dd,
                eff_mf, eff_tc, tc_active,
                min_per_fund=min_per_fund,
                eff_fund_std=cur_std,
                eff_fund_dd=cur_dd,
                max_per_amc=max_per_amc,
            )

    return None, {}


def _info(relaxations, min_return, max_std_dev, max_dd,
          max_per_fund, max_per_type, type_caps_active,
          min_per_fund=0.0, eff_fund_std=0.0, eff_fund_dd=0.0,
          max_per_amc=1.0) -> dict:
    return {
        "relaxations": relaxations,
        "final": {
            "min_return":       min_return,
            "max_std_dev":      max_std_dev,
            "max_dd":           max_dd,
            "max_per_fund":     max_per_fund,
            "max_per_type":     max_per_type,
            "type_caps_active": type_caps_active,
            "min_per_fund":     min_per_fund,
            "eff_fund_std":     eff_fund_std,
            "eff_fund_dd":      eff_fund_dd,
            "max_per_amc":      max_per_amc,
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# FINE-TUNING  (quality-aware rebalance after initial allocation)
# ═══════════════════════════════════════════════════════════════════════════════

def fine_tune(
    df:           pd.DataFrame,
    weights:      np.ndarray,
    min_return:   float,
    max_std_dev:  float,
    max_dd:       float,
    max_per_fund: float,
    min_per_fund: float,
    max_per_type: float = 0.0,
    tol:          float = 1e-6,
    mode:         str   = "coarse",
    max_fund_std: float = 0.0,   # 0 = no per-fund std filter
    max_fund_dd:  float = 0.0,   # 0 = no per-fund dd filter
) -> tuple:
    """
    After the main allocation is found, run a targeted re-optimisation on only
    the selected funds to shift weight toward higher-quality funds (by
    Combined_Ratio) while respecting all constraints.

    Objective on selected-fund subset:

        maximise  dot(w, ret + λ * quality_norm)

    where quality_norm is Combined_Ratio normalised to [0, 1] across selected
    funds, and λ = 20% of the return spread (stronger than the main solver's
    10% because fine-tuning operates on a smaller, already-feasible subset
    where aggressive quality steering is safe).

    All original portfolio-level constraints (return floor, std cap, dd cap,
    per-type cap) are preserved.  Per-fund bounds are also preserved.

    Returns (refined_weights, improved: bool, summary_str).
    """
    ret  = df["adj_ret"].values
    std  = df["adj_std"].values
    dd   = df["adj_dd"].values
    types = df["Fund Type"].values
    quality = df["adj_quality"].values

    w_orig = weights.copy()
    selected_mask = w_orig > tol
    n_selected = selected_mask.sum()

    if n_selected < 2:
        return w_orig, False, "Fine-tuning skipped: fewer than 2 funds selected."

    # ── Build sub-problem on selected funds only ──────────────────────
    sel_idx   = np.where(selected_mask)[0]
    df_sel    = df.iloc[sel_idx].reset_index(drop=True)
    w_sel     = w_orig[sel_idx]                  # initial weights in sub-problem

    ret_sel     = ret[sel_idx]
    std_sel     = std[sel_idx]
    dd_sel      = dd[sel_idx]
    quality_sel = quality[sel_idx]
    types_sel   = types[sel_idx]

    n_sel = len(sel_idx)

    # ── Per-fund upper bounds — enforce P0-anchored risk caps ──────
    # Funds exceeding the per-fund std/dd cap get ub=0 (excluded from
    # fine-tuning).  This prevents fine_tune from shifting weight toward
    # volatile funds that the frontier walk deliberately kept out.
    ub_sel = np.full(n_sel, max_per_fund)
    if max_fund_std > 0:
        for i in range(n_sel):
            if std_sel[i] > max_fund_std + 1e-6:
                ub_sel[i] = 0.0
    if max_fund_dd > 0:
        for i in range(n_sel):
            if abs(dd_sel[i]) > max_fund_dd + 1e-6:
                ub_sel[i] = 0.0
    n_capped = int((ub_sel == 0.0).sum())
    if n_capped > 0:
        capped_names = [str(df_sel.iloc[i]["Fund Name"]) for i in range(n_sel) if ub_sel[i] == 0.0]
        print(f"  Fine-tuning: {n_capped} fund(s) excluded by P0 risk cap: "
              f"{', '.join(capped_names)}")

    # Quality-adjusted objective coefficients (used for logging only)
    q_max = quality_sel.max()
    quality_norm = (quality_sel / q_max) if q_max > 1e-9 else np.zeros(n_sel)
    ret_spread   = ret_sel.max() - ret_sel.min() if n_sel > 1 else 0.01
    quality_lambda = 0.20 * ret_spread  # 20% — stronger than main solver's 10%

    n_high_q = int((quality_sel > q_max * 0.5).sum()) if q_max > 1e-9 else 0
    n_low_q  = n_sel - n_high_q
    print(f"\n  Fine-tuning: {n_high_q} high-quality fund(s), {n_low_q} lower-quality fund(s)"
          f" (λ={quality_lambda*100:.3f}%, q_max={q_max:.2f})")

    # Per-fund bounds: [0, max_per_fund].  SLSQP doesn't support semi-continuous
    # (either 0 or ≥ min_per_fund) natively, so we handle it via iterative
    # drop-and-re-solve: solve, check for violators, drop them, repeat.
    bounds_sel = ScipyBounds(lb=0.0, ub=max_per_fund)

    def _solve_ft(n_s, r_s, s_s, d_s, q_s, t_s, w_init, obj_c, ub_s=None):
        """Inner SLSQP solve for fine-tuning.  Returns (w_best, obj) or (None, inf).

        Objective depends on mode:
          coarse → MINIMISE risk (std + dd) with return as tiebreaker.
                   Return is enforced by constraints; risk is the primary goal.
          fine   → MAXIMISE return+quality with risk as tiebreaker.
                   Both std and dd penalties push solutions toward lower risk
                   when return+quality are comparable.
        """
        if ub_s is None:
            ub_s = np.full(n_s, max_per_fund)
        ret_range = float(r_s.max() - r_s.min()) if n_s > 1 else 0.01
        dd_abs_s  = np.abs(d_s)
        s_sq      = s_s ** 2

        if mode == "coarse":
            # Primary: minimise risk.  10% of obj_c as tiebreaker for quality.
            def _obj(w):
                port_std = np.sqrt(float(np.dot(w, s_sq)) + 1e-18)
                port_dd  = float(np.dot(w, dd_abs_s))
                return port_std + port_dd + 0.10 * float(np.dot(w, obj_c))

            def _jac(w):
                port_std = np.sqrt(float(np.dot(w, s_sq)) + 1e-18)
                g_std    = s_sq / (2.0 * port_std)
                g_dd     = dd_abs_s
                return g_std + g_dd + 0.10 * obj_c
        else:
            # Primary: maximise return+quality.  Risk as tiebreaker (~15%).
            lambda_std = 0.15 * ret_range / (float(s_s.max()) + 1e-12)
            lambda_dd  = 0.15 * ret_range / (float(dd_abs_s.max()) + 1e-12)

            def _obj(w):
                primary  = float(np.dot(w, obj_c))
                port_std = np.sqrt(float(np.dot(w, s_sq)) + 1e-18)
                port_dd  = float(np.dot(w, dd_abs_s))
                return primary + lambda_std * port_std + lambda_dd * port_dd

            def _jac(w):
                port_std = np.sqrt(float(np.dot(w, s_sq)) + 1e-18)
                g_std    = lambda_std * s_sq / (2.0 * port_std)
                g_dd     = lambda_dd * dd_abs_s
                return obj_c.copy() + g_std + g_dd
        # Constraints on sub-problem
        cs = []
        cs.append({"type": "eq",
                   "fun":  lambda w: w.sum() - 1.0,
                   "jac":  lambda w: np.ones(n_s)})
        cs.append({"type": "ineq",
                   "fun":  lambda w, r=r_s, m=min_return: np.dot(w, r) - m,
                   "jac":  lambda w, r=r_s: r})
        cs.append({"type": "ineq",
                   "fun":  lambda w, s=s_s, m=max_std_dev: m - np.sqrt(np.dot(w, s**2)),
                   "jac":  lambda w, s=s_s: -s**2 / (2 * np.sqrt(np.dot(w, s**2)) + 1e-12)})
        cs.append({"type": "ineq",
                   "fun":  lambda w, d=d_s, m=max_dd: np.dot(w, d) + m,
                   "jac":  lambda w, d=d_s: d})
        if max_per_type > 0:
            for ft in np.unique(t_s):
                mask_ft = (t_s == ft).astype(float)
                if mask_ft.sum() == 0:
                    continue
                cs.append({"type": "ineq",
                           "fun":  lambda w, m=mask_ft, c=max_per_type: c - np.dot(w, m),
                           "jac":  lambda w, m=mask_ft: -m})

        bnd = ScipyBounds(lb=np.zeros(n_s), ub=ub_s)
        best_o, best_w = np.inf, None

        rng_seeds = [0, 42, 7, 123, 999, 31415]
        for seed in rng_seeds:
            if seed == 0:
                w0 = w_init.copy()
            else:
                rng = np.random.default_rng(seed)
                w0  = w_init + rng.uniform(-0.01, 0.01, n_s)
                w0  = np.clip(w0, 0.0, ub_s)
                if w0.sum() < 1e-10:
                    w0 = w_init.copy()
                else:
                    w0 /= w0.sum()
            # Enforce upper bounds on initial point
            w0 = np.minimum(w0, ub_s)
            try:
                res = minimize(
                    fun=_obj, jac=_jac,
                    x0=w0, method="SLSQP", bounds=bnd, constraints=cs,
                    options={"maxiter": 3000, "ftol": 1e-10, "disp": False})
            except Exception:
                continue
            w_c = np.clip(res.x, 0.0, None)
            if w_c.sum() < 1e-10:
                continue
            w_c /= w_c.sum()
            ok = (abs(w_c.sum() - 1.0) < 1e-4
                  and np.dot(w_c, r_s) >= min_return - 1e-4
                  and np.sqrt(np.dot(w_c, s_s**2)) <= max_std_dev + 1e-4
                  and np.dot(w_c, d_s) >= -max_dd - 1e-4
                  and np.all(w_c >= -1e-4)
                  and np.all(w_c <= ub_s + 1e-4))
            if ok and max_per_type > 0:
                for ft in np.unique(t_s):
                    if w_c[t_s == ft].sum() > max_per_type + 1e-4:
                        ok = False; break
            if ok:
                ov = _obj(w_c)
                if ov < best_o:
                    best_o, best_w = ov, w_c.copy()

        return best_w, best_o

    # ── Iterative solve-and-drop loop ─────────────────────────────────
    # Solve, then drop any fund that ended up in the (0, min_per_fund)
    # "dead zone", re-normalise survivors, and re-solve.  Repeat until
    # clean or max iterations.
    active_mask = np.ones(n_sel, dtype=bool)    # which of the n_sel funds are still in play
    # Pre-exclude funds that are capped at 0 by the P0 risk filter
    active_mask &= (ub_sel > 0)
    MAX_DROP_ROUNDS = 5
    best_w_sel = None                       # initialise before loop

    for drop_round in range(MAX_DROP_ROUNDS + 1):
        a_idx = np.where(active_mask)[0]
        n_a   = len(a_idx)
        if n_a < 2:
            break

        r_a = ret_sel[a_idx];  s_a = std_sel[a_idx];  d_a = dd_sel[a_idx]
        q_a = quality_sel[a_idx];  t_a = types_sel[a_idx]
        ub_a = ub_sel[a_idx]

        # Re-compute objective for active subset
        q_max_a = q_a.max()
        q_norm_a = (q_a / q_max_a) if q_max_a > 1e-9 else np.zeros(n_a)
        rs_a = r_a.max() - r_a.min() if n_a > 1 else 0.01
        ql_a = 0.20 * rs_a
        obj_a = -(r_a + ql_a * q_norm_a)

        # Initial weights for this subset: re-normalise from w_sel
        w0_a = w_sel[a_idx].copy()
        w0_a = np.minimum(w0_a, ub_a)         # respect per-fund caps
        if w0_a.sum() > 1e-10:
            w0_a /= w0_a.sum()
        else:
            w0_a = np.ones(n_a) / n_a
            w0_a = np.minimum(w0_a, ub_a)
            if w0_a.sum() > 1e-10:
                w0_a /= w0_a.sum()

        w_a, _ = _solve_ft(n_a, r_a, s_a, d_a, q_a, t_a, w0_a, obj_a, ub_a)
        if w_a is None:
            break

        # Check for min_per_fund violators
        violators = (w_a > tol) & (w_a < min_per_fund - 1e-6)
        if not violators.any():
            # Clean solution — map back to n_sel space
            best_w_sel = np.zeros(n_sel)
            for li, gi in enumerate(a_idx):
                best_w_sel[gi] = w_a[li]
            break
        else:
            # Drop violators and retry
            for li in np.where(violators)[0]:
                active_mask[a_idx[li]] = False
    else:
        # Exhausted rounds — use last result if we have one
        if w_a is not None:
            best_w_sel = np.zeros(n_sel)
            for li, gi in enumerate(a_idx):
                best_w_sel[gi] = w_a[li]
        else:
            best_w_sel = None

    if best_w_sel is None:
        return w_orig, False, "Fine-tuning: re-optimisation found no feasible improvement."

    # Final cleanup: zero out any sub-threshold weights and re-normalise
    best_w_sel[best_w_sel < tol] = 0.0
    if best_w_sel.sum() < 1e-10:
        return w_orig, False, "Fine-tuning: result collapsed after min_per_fund cleanup."
    best_w_sel /= best_w_sel.sum()

    # Check if we actually improved portfolio return
    orig_ret = np.dot(w_sel,      ret_sel)
    new_ret  = np.dot(best_w_sel, ret_sel)

    # Also compare quality-weighted composition
    orig_quality_wt = np.dot(w_sel,      quality_sel)
    new_quality_wt  = np.dot(best_w_sel, quality_sel)

    if new_ret < orig_ret - 1e-5 and new_quality_wt <= orig_quality_wt + 1e-5:
        # Neither return nor quality improved — keep original
        return w_orig, False, "Fine-tuning: no improvement over original allocation."

    # Map back to full weight vector
    w_new = w_orig.copy()
    for local_i, global_i in enumerate(sel_idx):
        w_new[global_i] = best_w_sel[local_i]
    # Zero out any sub-threshold weights
    w_new[w_new < tol] = 0.0
    if w_new.sum() > 0:
        w_new /= w_new.sum()

    # Build summary
    grew   = np.where((w_new - w_orig) >  1e-4)[0]
    shrank = np.where((w_orig - w_new) >  1e-4)[0]
    ret_gain     = (new_ret - orig_ret) * 100
    quality_gain = new_quality_wt - orig_quality_wt

    lines = []
    if abs(ret_gain) > 0.001:
        lines.append(f"return {'+' if ret_gain>0 else ''}{ret_gain:.3f}%")
    if abs(quality_gain) > 0.01:
        lines.append(f"wtd quality {'+' if quality_gain>0 else ''}{quality_gain:.2f}")

    # Risk comparison
    orig_std = np.sqrt(np.dot(w_sel,      std_sel**2)) * 100
    new_std  = np.sqrt(np.dot(best_w_sel, std_sel**2)) * 100
    orig_dd  = np.dot(w_sel,      dd_sel) * 100
    new_dd   = np.dot(best_w_sel, dd_sel) * 100
    std_delta = new_std - orig_std
    dd_delta  = new_dd - orig_dd
    if abs(std_delta) > 0.001:
        lines.append(f"std {'+' if std_delta>0 else ''}{std_delta:.3f}%")
    if abs(dd_delta) > 0.001:
        lines.append(f"dd {'+' if dd_delta>0 else ''}{dd_delta:.3f}%")
    detail = ", ".join(lines) if lines else "marginal quality improvement"

    summary = (
        f"Fine-tuning: {len(grew)} fund(s) increased, {len(shrank)} fund(s) reduced "
        f"({detail})."
    )

    # Print fund-level changes
    for i in grew:
        name = df.iloc[i]["Fund Name"] if i < len(df) else f"fund[{i}]"
        q    = quality[i]
        print(f"    ↑ {str(name)[:50]:<50}  {w_orig[i]*100:5.2f}% → {w_new[i]*100:5.2f}%  [CR={q:.2f}]")
    for i in shrank:
        name = df.iloc[i]["Fund Name"] if i < len(df) else f"fund[{i}]"
        q    = quality[i]
        print(f"    ↓ {str(name)[:50]:<50}  {w_orig[i]*100:5.2f}% → {w_new[i]*100:5.2f}%  [CR={q:.2f}]")

    return w_new, True, summary


# ═══════════════════════════════════════════════════════════════════════════════
# REPORTING
# ═══════════════════════════════════════════════════════════════════════════════

def report(
    df:              pd.DataFrame,
    weights:         np.ndarray,
    total_money:     float,
    original_params: dict,
    info:            dict,
    output_path:     str,
    fine_tune_info:  str = "",
) -> pd.DataFrame:
    """Print a formatted report and save results to CSV."""

    allocated = df.copy()
    allocated["Weight_%"]     = (weights * 100).round(4)
    allocated["Allocation_L"] = (weights * total_money).round(2)

    result = allocated[allocated["Allocation_L"] > 0.001].copy()
    result = result.sort_values("Allocation_L", ascending=False).reset_index(drop=True)

    # Portfolio-level metrics (weighted avg)
    mask_a = weights > 1e-5
    w_a    = weights[mask_a]
    df_a   = df[mask_a]

    wtd_ret  = float(np.dot(w_a, df_a["adj_ret"].values))
    wtd_std  = float(np.sqrt(np.dot(w_a, df_a["adj_std"].values ** 2)))
    wtd_dd   = float(np.dot(w_a, df_a["adj_dd"].values))
    worst_L  = wtd_ret * total_money

    # ── Portfolio risk-adjusted ratios ────────────────────────────────────────
    # Sharpe  = weighted avg of fund Sharpe ratios (using 5Y, fall back to 10Y)
    # Sortino = weighted avg of fund Sortino ratios (5Y → 10Y)
    # Calmar  = portfolio CAGR / |portfolio max-drawdown|  (directly computed)
    #
    # Weighted-avg Sharpe/Sortino is the standard approach when individual fund
    # ratios are already risk-free-rate adjusted (which AMFI-derived ratios are).
    def _wtd_ratio(col5, col10):
        """Weighted avg of a ratio column; 5Y preferred, 10Y fallback."""
        s5  = pd.to_numeric(df_a.get(col5,  np.nan), errors="coerce")
        s10 = pd.to_numeric(df_a.get(col10, np.nan), errors="coerce")
        vals = s5.where(s5.notna(), s10).values
        # If a fund has NaN for both windows, treat its contribution as 0
        valid = np.where(np.isfinite(vals), vals, 0.0)
        w_sum = w_a[np.isfinite(vals)].sum()
        return float(np.dot(w_a, valid) / w_sum) if w_sum > 1e-9 else float("nan")

    port_sharpe  = _wtd_ratio("Sharpe_5Y",  "Sharpe_10Y")
    port_sortino = _wtd_ratio("Sortino_5Y", "Sortino_10Y")
    # Calmar = annualised return / |max drawdown|  (both already as fractions)
    port_calmar  = (wtd_ret / abs(wtd_dd)) if abs(wtd_dd) > 1e-9 else float("nan")

    sep = "─" * 82

    # ── Header ────────────────────────────────────────────────────────────────
    print(f"\n{'═'*82}")
    print(f"  ALLOCATION RESULT")
    print(f"{'═'*82}")
    print(f"  Total corpus  : ₹{total_money:,.2f} L")
    print(f"  Funds selected: {len(result)}")

    if info.get("relaxations"):
        print(f"\n  ⚠  Constraints were relaxed to find a feasible solution:")
        for r in info["relaxations"]:
            print(f"      • {r}")
    else:
        print(f"\n  ✓  All original constraints satisfied")

    if fine_tune_info:
        print(f"  ↻  {fine_tune_info}")

    # ── Constraint check table ────────────────────────────────────────────────
    final = info["final"]
    op    = original_params

    print(f"\n  {'Constraint':<30} {'Requested':>12}  {'Effective':>12}  {'Achieved':>12}  Status")
    print(f"  {'─'*30} {'─'*12}  {'─'*12}  {'─'*12}  {'─'*6}")

    def _row(label, req, effective, achieved, better_fn):
        r_s = f"{req:.2f}%"      if req       is not None else "—"
        e_s = f"{effective:.2f}%" if effective is not None else "(removed)"
        a_s = f"{achieved:.3f}%"
        ok  = better_fn(achieved, effective) if effective is not None else "~"
        flag = "✓" if ok is True or ok is np.bool_(True) or (isinstance(ok, bool) and ok) else ("~" if ok == "~" else "✗")
        print(f"  {label:<30} {r_s:>12}  {e_s:>12}  {a_s:>12}  {flag}")

    _row("Min avg return",
         op["min_return"],   final["min_return"]*100,   wtd_ret*100,
         lambda a, e: a >= e - 0.001)
    _row("Max avg std dev",
         op["max_std_dev"],  final["max_std_dev"]*100,  wtd_std*100,
         lambda a, e: a <= e + 0.001)
    _row("Max avg drawdown",
         op["max_dd"],       final["max_dd"]*100,       abs(wtd_dd)*100,
         lambda a, e: a <= e + 0.001)
    _row("Max per-fund",
         op["max_per_fund"], final["max_per_fund"]*100, result["Weight_%"].max(),
         lambda a, e: a <= e + 0.05)
    _row("Min per-fund (if selected)",
         op["min_per_fund"], final["min_per_fund"]*100,
         result["Weight_%"].min(),
         lambda a, e: a >= e - 0.05)

    type_summary = result.groupby("Fund Type")["Weight_%"].sum()
    type_max_val = type_summary.max()
    if final["type_caps_active"]:
        _row("Max per-type",
             op["max_per_type"], final["max_per_type"]*100, type_max_val,
             lambda a, e: a <= e + 0.05)
    else:
        _row("Max per-type",
             op["max_per_type"], None, type_max_val,
             lambda a, e: True)

    # Fund-filter rows — show actual max std/dd across selected funds
    sel_std_max = result["adj_std"].max() * 100
    sel_dd_max  = result["adj_dd"].abs().max() * 100
    eff_fstd = final.get("eff_fund_std", 0.0)
    eff_fdd  = final.get("eff_fund_dd",  0.0)
    _row("Max fund std_dev (per fund)",
         op.get("max_fund_std"), eff_fstd * 100 if eff_fstd > 0 else None,
         sel_std_max,
         lambda a, e: a <= e + 0.05)
    _row("Max fund |max_dd| (per fund)",
         op.get("max_fund_dd"),  eff_fdd  * 100 if eff_fdd  > 0 else None,
         sel_dd_max,
         lambda a, e: a <= e + 0.05)

    # AMC concentration row — show worst-case AMC total across selected funds
    eff_amc = final.get("max_per_amc", 1.0)
    req_amc = op.get("max_per_amc")
    if req_amc is not None and req_amc < 100.0 and "AMC" in result.columns:
        amc_max_val = result.groupby("AMC")["Weight_%"].sum().max()
        _row("Max per-AMC",
             req_amc, eff_amc * 100 if eff_amc < 1.0 else None,
             amc_max_val,
             lambda a, e: a <= e + 0.05)
    elif req_amc is not None and req_amc < 100.0:
        # AMC column not present (e.g. single-chunk mode without CSV)
        pass

    print(f"\n  Expected worst-case annual return: "
          f"₹{worst_L:,.2f} L  ({wtd_ret*100:.3f}% on ₹{total_money:,.2f} L)")

    def _ratio_str(v):
        return f"{v:.3f}" if np.isfinite(v) else "n/a"

    print(f"\n  Portfolio risk-adjusted ratios:")
    print(f"    Sharpe  (wtd avg 5Y/10Y) : {_ratio_str(port_sharpe)}")
    print(f"    Sortino (wtd avg 5Y/10Y) : {_ratio_str(port_sortino)}")
    print(f"    Calmar  (ret / |max_dd|) : {_ratio_str(port_calmar)}"
          f"  ({wtd_ret*100:.3f}% / {abs(wtd_dd)*100:.3f}%)")

    # ── Fund detail table ─────────────────────────────────────────────────────
    print(f"\n{sep}")
    print(f"  FUND-LEVEL ALLOCATION  (sorted by allocation)")
    print(sep)
    hdr = f"  {'Fund Name':<44} {'Type':<24} {'₹ L':>8} {'Wt%':>7} {'Ret%':>7} {'Std%':>7} {'DD%':>8}"
    print(hdr)
    print(f"  {'─'*44} {'─'*24} {'─'*8} {'─'*7} {'─'*7} {'─'*7} {'─'*8}")

    for _, r in result.iterrows():
        print(
            f"  {str(r['Fund Name'])[:44]:<44} "
            f"  {str(r['Fund Type'])[:24]:<24} "
            f"  {r['Allocation_L']:>7.2f} "
            f"  {r['Weight_%']:>6.2f} "
            f"  {r['adj_ret']*100:>6.2f} "
            f"  {r['adj_std']*100:>6.3f} "
            f"  {r['adj_dd']*100:>7.3f}"
        )

    print(f"  {'─'*44} {'─'*24} {'─'*8} {'─'*7} {'─'*7} {'─'*7} {'─'*8}")
    print(
        f"  {'PORTFOLIO (wtd avg)':<44} "
        f"  {'':24} "
        f"  {total_money:>7.2f} "
        f"  {'100.00':>6} "
        f"  {wtd_ret*100:>6.3f} "
        f"  {wtd_std*100:>6.3f} "
        f"  {wtd_dd*100:>7.3f}"
    )

    # ── Type breakdown ────────────────────────────────────────────────────────
    type_tbl = (
        result.groupby("Fund Type")
        .agg(N=("Fund Name", "count"),
             Alloc_L=("Allocation_L", "sum"),
             Wt_pct=("Weight_%", "sum"))
        .sort_values("Alloc_L", ascending=False)
    )
    print(f"\n{sep}")
    print(f"  FUND-TYPE BREAKDOWN")
    print(sep)
    print(f"  {'Fund Type':<36} {'N':>3} {'₹ Lakhs':>9} {'Wt%':>7}")
    print(f"  {'─'*36} {'─'*3} {'─'*9} {'─'*7}")
    for ft, r in type_tbl.iterrows():
        flag = ""
        if not final["type_caps_active"]:
            flag = " *"
        elif final["max_per_type"] and r["Wt_pct"] > final["max_per_type"]*100 + 0.01:
            flag = " !"
        print(f"  {ft:<36} {int(r['N']):>3} {r['Alloc_L']:>9.2f} {r['Wt_pct']:>7.2f}{flag}")
    if not final["type_caps_active"]:
        print(f"  * per-type cap was relaxed")
    print()

    # ── Save CSV ──────────────────────────────────────────────────────────────
    keep = [
        "Fund Type", "Fund Name", "AMFI Code",
        "Allocation_L", "Weight_%",
        "adj_ret", "adj_std", "adj_dd",
        "1Y_CAGR", "3Y_CAGR", "5Y_CAGR", "10Y_CAGR",
        "History_Months",
    ]
    save_cols = [c for c in keep if c in result.columns]
    out = result[save_cols].copy()
    out = out.rename(columns={
        "adj_ret": "Worst_Exp_Ret_%",
        "adj_std": "Std_Dev_used",
        "adj_dd":  "Max_DD_used",
    })
    # Convert to %
    for col in ["Worst_Exp_Ret_%", "Std_Dev_used", "Max_DD_used",
                "1Y_CAGR", "3Y_CAGR", "5Y_CAGR", "10Y_CAGR"]:
        if col in out.columns:
            out[col] = (out[col] * 100).round(4)

    # Portfolio summary row
    summary = {c: "" for c in out.columns}
    summary.update({
        "Fund Type":       "PORTFOLIO TOTAL",
        "Allocation_L":    round(total_money, 2),
        "Weight_%":        100.0,
        "Worst_Exp_Ret_%": round(wtd_ret * 100, 4),
        "Std_Dev_used":    round(wtd_std * 100, 4),
        "Max_DD_used":     round(wtd_dd  * 100, 4),
        "Port_Sharpe":     round(port_sharpe,  4) if np.isfinite(port_sharpe)  else "",
        "Port_Sortino":    round(port_sortino, 4) if np.isfinite(port_sortino) else "",
        "Port_Calmar":     round(port_calmar,  4) if np.isfinite(port_calmar)  else "",
    })
    out = pd.concat([out, pd.DataFrame([summary])], ignore_index=True)
    out.to_csv(output_path, index=False)
    print(f"  Allocation saved to: {output_path}")
    print()

    return out


# ═══════════════════════════════════════════════════════════════════════════════
# MULTI-CHUNK ALLOCATION
# ═══════════════════════════════════════════════════════════════════════════════

class _ChunkProxy:
    """Lightweight duck-type wrapper so plain dicts work with run_aim_pass_multi,
    score_combinations, and select_best_combination (which expect attribute access
    like chunk.min_return, chunk.year_from, etc.).

    The JSON chunks store constraints as percentages (e.g. 8.0 for 8%).
    run_aim_pass_multi expects fractions (e.g. 0.08), so we convert on init.
    """
    __slots__ = (
        "year_from", "year_to",
        "min_return", "max_std_dev", "max_dd",
        "max_per_fund", "min_per_fund", "max_per_type",
        "min_history", "max_fund_std", "max_fund_dd",
        "max_per_amc",
        "target_weights", "_type_ratios", "constraint_slack_used",
        "_orig_dict",
        "p0_max_fund_std", "p0_max_fund_dd",     # frontier-walk P0 reference
    )

    def __init__(self, d: dict):
        self.year_from    = d["year_from"]
        self.year_to      = d["year_to"]
        # Convert % → fraction for the optimizer
        self.min_return   = d["min_return"]   / 100.0
        self.max_std_dev  = d["max_std_dev"]  / 100.0
        self.max_dd       = d["max_dd"]       / 100.0
        self.max_per_fund = d["max_per_fund"] / 100.0
        self.min_per_fund = d["min_per_fund"] / 100.0
        self.max_per_type = d["max_per_type"] / 100.0
        self.min_history  = int(d["min_history"])
        self.max_fund_std = d.get("max_fund_std", 1.5) / 100.0
        self.max_fund_dd  = d.get("max_fund_dd",  1.5) / 100.0
        self.max_per_amc  = d.get("max_per_amc",  16.0) / 100.0  # default 16%
        # Writeable by select_best_combination
        self.target_weights       = {}
        self._type_ratios         = {}
        self.constraint_slack_used = {}
        self._orig_dict           = d   # keep reference for report()
        # P0 reference — set by run_frontier_walk
        self.p0_max_fund_std = None
        self.p0_max_fund_dd  = None


def allocate_chunks(
    input_csv:         str,
    chunks:            list,        # list of dicts — see below
    total_money:       float,       # Rs lakhs (same corpus for every chunk)
    output_dir:        str,         # directory to write per-chunk CSVs
    commonality_bonus: float = 0.002,  # (ignored — kept for backward compat)
    mode:              str   = "coarse",  # "coarse" = minimise risk, "fine" = maximise return
    frontier_walk:     bool  = False,    # use frontier-walk instead of α-blending
    risk_ref:          str   = "portfolio",  # "portfolio" or "pct75"
    pulp_commonality:  bool  = False,    # use PuLP commonality-optimising walk
    pulp_max_portfolios: int = 0,        # candidates per chunk; 0 = auto from N
    pulp_max_std:      float = 2.5,      # stop ceiling for PuLP walk (%)
) -> list:
    """
    Multi-chunk portfolio allocation using the generate-and-select approach:

    1. Load the fund universe from the CSV.
    2. For each chunk, generate up to N candidate portfolios via one of:
       - λ-blending (run_aim_pass_multi)     — default
       - frontier-walk (run_frontier_walk)    — frontier_walk=True
       - PuLP commonality walk               — pulp_commonality=True
    3. Score all cross-chunk combinations for rebalancing efficiency
       (score_combinations / find_best_commonality_combination).
    4. Select the best combination and write per-chunk CSVs
       (select_best_combination → report).

    The PuLP commonality walk (pulp_commonality=True) uses PuLP's CBC MILP
    solver to minimise portfolio std_dev while iterating with increasing
    std_dev floors.  The combination scorer targets ~60% of unique funds
    common to ALL chunks and ≥20% in (N-1) chunks — maximising portfolio
    overlap to minimise rebalancing.  This also inherently minimises max_dd
    due to its strong correlation with std_dev.

    Each element of ``chunks`` must be a dict with keys:
        year_from, year_to,
        min_return   (% e.g. 6.85),
        max_std_dev  (% e.g. 0.97),
        max_dd       (% e.g. 0.75),
        max_per_fund (% e.g. 8.0),
        max_per_type (% e.g. 24.0),
        min_per_fund (% e.g. 2.0),
        min_history  (years, int e.g. 7),
        max_fund_std (% e.g. 1.5),
        max_fund_dd  (% e.g. 1.5),

    Returns a list of result dicts, one per chunk.
    """
    import os
    import numpy as np
    os.makedirs(output_dir, exist_ok=True)

    # ── Guard: reject more than 5 chunks ─────────────────────────────────
    if len(chunks) > 5:
        msg = (
            f"Too many chunks ({len(chunks)}). "
            "Define 5 or fewer chunks of periods, because the reliability "
            "of results will be highly suspect if it is more. Since we are "
            "using long-term data, we need long-term chunks."
        )
        print(f"\n  ERROR: {msg}")
        return [{"chunk": c, "chunk_num": i + 1,
                 "success": False, "error": msg} for i, c in enumerate(chunks)]

    # ── Build _ChunkProxy objects (duck-type for run_aim_pass_multi) ──────
    proxies = [_ChunkProxy(c) for c in chunks]

    # When using frontier walk, the objective already minimises risk —
    # tight std/dd ceilings just force unnecessary relaxation.
    # Open them up so only the return floor and the frontier walk's
    # progressive risk floor do the real work.
    if frontier_walk:
        for p in proxies:
            p.max_std_dev  = 1.0     # 100% — effectively unconstrained
            p.max_dd       = 1.0
            p.max_fund_std = 1.0     # per-fund filters also opened
            p.max_fund_dd  = 1.0

    # ── Load universe (use strictest history requirement) ─────────────────
    min_hist_months = max(int(c["min_history"]) * 12 for c in chunks)
    df = load_and_filter(input_csv, min_hist_months)

    if len(df) == 0:
        msg = "No eligible funds after history filter."
        print(f"  ERROR: {msg}")
        return [{"chunk": c, "chunk_num": i + 1,
                 "success": False, "error": msg} for i, c in enumerate(chunks)]

    sep = "═" * 82
    print(f"\n{sep}")
    print(f"  MULTI-PORTFOLIO GENERATE-AND-SELECT  ({len(chunks)} chunks)")
    print(f"  Universe: {len(df)} funds  |  Corpus: ₹{total_money:,.2f} L")
    print(sep)

    # ── Pass 1: Generate candidates per chunk ────────────────────────────
    if pulp_commonality:
        # Auto-compute portfolios-per-chunk from N if not explicitly set
        n_chunks = len(proxies)
        if pulp_max_portfolios <= 0:
            _pulp_auto = {1: 1, 2: 30, 3: 25, 4: 20, 5: 15}
            pulp_max_portfolios = _pulp_auto[n_chunks]  # N>5 rejected above

        # PuLP uses the full universe (no per-fund std/dd filters), matching
        # the portfolio_allocator.py reference implementation.
        for p in proxies:
            p.max_fund_std = 0.0
            p.max_fund_dd  = 0.0

        print(f"\n─── Pass 1: PuLP Commonality Walk "
              f"({n_chunks} chunk(s), "
              f"up to {pulp_max_portfolios} candidates/chunk, "
              f"max_rms_std≤{pulp_max_std:.1f}%) ───")

        all_candidates = run_pulp_commonality_walk(
            chunks         = proxies,
            universe       = df,
            n_portfolios   = pulp_max_portfolios,
            max_overall_std= pulp_max_std / 100.0,
            progress_cb    = None,
        )

    elif frontier_walk:
        print(f"\n─── Pass 1: Frontier Walk "
              f"(up to 10 candidates/chunk, risk_step=0.05%) ───")

        all_candidates = run_frontier_walk(
            chunks       = proxies,
            universe     = df,
            n_portfolios = 10,
            risk_step    = 0.0005,
            progress_cb  = None,
            risk_ref     = risk_ref,
        )
    else:
        print(f"\n─── Pass 1: Multi-Portfolio Aim "
              f"(up to 10 candidates/chunk, α step=0.025) ───")

        all_candidates = run_aim_pass_multi(
            chunks       = proxies,
            universe     = df,
            n_portfolios = 10,
            alpha_step   = 0.025,
            progress_cb  = None,
            mode         = mode,
        )

    # ── Pass 2: Score cross-chunk combinations ────────────────────────────
    if len(chunks) > 1:
        if pulp_commonality:
            # Use the target-distribution fitness scorer
            print(f"\n─── Pass 2: PuLP Commonality Combination Scoring ───")

            best_indices, best_combo, penalty, sum_std, best_stats = \
                find_best_commonality_combination(
                    chunks         = proxies,
                    all_candidates = all_candidates,
                    progress_cb    = None,
                )

            if best_combo is not None:
                print(f"\n─── Pass 3: Applying Best Commonality Combination ───")
                _apply_pulp_commonality_result(
                    chunks         = proxies,
                    all_candidates = all_candidates,
                    best_indices   = best_indices,
                    best_combo     = best_combo,
                    best_stats     = best_stats,
                )
            else:
                # Fallback: use quality-weighted overlap scorer
                print(f"\n  ⚠ PuLP commonality scorer failed — "
                      f"falling back to overlap scorer.")
                _fq = {str(row["Fund Name"]): float(row["adj_quality"])
                       for _, row in df.iterrows()
                       if pd.notna(row.get("adj_quality")) and float(row["adj_quality"]) > 0}
                scored = score_combinations(
                    chunks=proxies, all_candidates=all_candidates,
                    total_money=total_money, fund_quality=_fq,
                )
                if scored:
                    select_best_combination(
                        chunks=proxies, all_candidates=all_candidates, scored=scored,
                    )
                else:
                    for ci, cands in enumerate(all_candidates):
                        if cands:
                            proxies[ci].target_weights = dict(cands[0]["weights"])
                            proxies[ci]._type_ratios   = dict(cands[0]["type_ratios"])

        else:
            print(f"\n─── Pass 2: Cross-Chunk Combination Scoring ───")

            # Build fund quality lookup from universe for quality-weighted scoring
            _fq = {str(row["Fund Name"]): float(row["adj_quality"])
                   for _, row in df.iterrows()
                   if pd.notna(row.get("adj_quality")) and float(row["adj_quality"]) > 0}

            scored = score_combinations(
                chunks      = proxies,
                all_candidates = all_candidates,
                total_money = total_money,
                fund_quality = _fq,
            )

            # ── Pass 3: Select best combination ───────────────────────────────
            if scored:
                print(f"\n─── Pass 3: Best Combination Selection ───")
                select_best_combination(
                    chunks         = proxies,
                    all_candidates = all_candidates,
                    scored         = scored,
                )
            else:
                # Fallback: use first candidate per chunk
                print(f"\n  ⚠ Combination scoring returned no results — "
                      f"using best single candidate per chunk.")
                for ci, cands in enumerate(all_candidates):
                    if cands:
                        proxies[ci].target_weights = dict(cands[0]["weights"])
                        proxies[ci]._type_ratios   = dict(cands[0]["type_ratios"])
    else:
        # Single chunk: just pick the best candidate (first = lowest risk)
        if all_candidates and all_candidates[0]:
            proxies[0].target_weights = dict(all_candidates[0][0]["weights"])
            proxies[0]._type_ratios   = dict(all_candidates[0][0]["type_ratios"])

    # ── Generate reports from the winning combination ─────────────────────
    results = []

    for idx, (chunk, proxy) in enumerate(zip(chunks, proxies)):
        chunk_num = idx + 1
        print(f"\n{sep}")
        print(f"  CHUNK {chunk_num} / {len(chunks)}   "
              f"(Years {chunk['year_from']}–{chunk['year_to']})")
        print(sep)

        if not proxy.target_weights:
            msg = f"Chunk {chunk_num}: no feasible portfolio found."
            print(f"  ✗  {msg}")
            results.append({"chunk": chunk, "chunk_num": chunk_num,
                            "success": False, "error": msg})
            continue

        # Convert target_weights dict → numpy array aligned to df
        weights = np.zeros(len(df))
        for fund_name, w in proxy.target_weights.items():
            matches = df.index[df["Fund Name"] == fund_name]
            if len(matches) > 0:
                weights[matches[0]] = w

        # Build info dict compatible with report()
        # Since run_aim_pass_multi handles relaxation internally, the effective
        # constraints are whatever the winning candidate actually achieved under.
        # We set them to the proxy's original constraints; report() uses these
        # to compare "Requested" vs "Effective" vs "Achieved".
        info = {
            "relaxations": [],   # λ-blending doesn't track relaxation steps
            "final": {
                "min_return":       proxy.min_return,
                "max_std_dev":      proxy.max_std_dev,
                "max_dd":           proxy.max_dd,
                "max_per_fund":     proxy.max_per_fund,
                "max_per_type":     proxy.max_per_type,
                "type_caps_active": True,
                "min_per_fund":     proxy.min_per_fund,
                "eff_fund_std":     proxy.max_fund_std,
                "eff_fund_dd":      proxy.max_fund_dd,
            },
        }

        # ── Fine-tune ─────────────────────────────────────────────────────
        # In coarse mode with frontier walk, the iterative P0 + combination
        # scorer already produces well-optimised portfolios.  Fine-tuning
        # can introduce riskier funds (equity savings, dynamic allocation)
        # that undermine the risk discipline.  Skip it for coarse mode.
        # In fine mode, fine_tune steers toward higher return and may still
        # be useful — kept for future evaluation.
        if mode == "fine":
            ft_max_fund_std = getattr(proxy, "p0_max_fund_std", 0.0) or 0.0
            ft_max_fund_dd  = getattr(proxy, "p0_max_fund_dd",  0.0) or 0.0
            weights, _, ft_summary = fine_tune(
                df           = df,
                weights      = weights,
                min_return   = proxy.min_return,
                max_std_dev  = proxy.max_std_dev,
                max_dd       = proxy.max_dd,
                max_per_fund = proxy.max_per_fund,
                min_per_fund = proxy.min_per_fund,
                max_per_type = proxy.max_per_type,
                mode         = mode,
                max_fund_std = ft_max_fund_std,
                max_fund_dd  = ft_max_fund_dd,
            )
        else:
            ft_summary = "Fine-tuning: skipped in coarse mode (frontier walk handles optimisation)."

        # ── Build original_params for report ──────────────────────────────
        original_params = {
            "min_return":   chunk["min_return"],       # show what the user asked (%)
            "max_std_dev":  chunk["max_std_dev"],
            "max_dd":       chunk["max_dd"],
            "max_per_fund": chunk["max_per_fund"],
            "max_per_type": chunk["max_per_type"],
            "min_per_fund": chunk["min_per_fund"],
            "max_fund_std": chunk.get("max_fund_std", 1.5),
            "max_fund_dd":  chunk.get("max_fund_dd",  1.5),
            "max_per_amc":  chunk.get("max_per_amc",  16.0),
        }

        # In frontier walk mode, std/dd ceilings and per-fund filters were
        # not user-specified — show "—" in the "Requested" column and set
        # "Effective" to the achieved values so the check always passes.
        if frontier_walk:
            original_params["max_std_dev"]  = None
            original_params["max_dd"]       = None
            original_params["max_fund_std"] = None
            original_params["max_fund_dd"]  = None
            # Effective = achieved (so status always shows ✓)
            ret_vals = df["adj_ret"].values
            std_vals = df["adj_std"].values
            dd_vals  = np.abs(df["adj_dd"].values)
            achieved_std = float(np.dot(weights, std_vals))
            achieved_dd  = float(np.dot(weights, dd_vals))
            info["final"]["max_std_dev"] = achieved_std
            info["final"]["max_dd"]      = achieved_dd
            info["final"]["eff_fund_std"] = 0.0  # signal "no filter"
            info["final"]["eff_fund_dd"]  = 0.0

        # ── Report + CSV ──────────────────────────────────────────────────
        csv_path = str(Path(output_dir) /
                       f"allocation_chunk_{chunk_num}"
                       f"_yr{chunk['year_from']}-{chunk['year_to']}.csv")
        df_result = report(
            df              = df,
            weights         = weights,
            total_money     = total_money,
            original_params = original_params,
            info            = info,
            output_path     = csv_path,
            fine_tune_info  = ft_summary,
        )

        # Extract portfolio ratios from the summary row
        port_sharpe = port_sortino = port_calmar = None
        summary_row = df_result[df_result["Fund Type"].fillna("") == "PORTFOLIO TOTAL"]
        if not summary_row.empty:
            def _try_float(val):
                try:
                    v = float(val)
                    return v if np.isfinite(v) else None
                except (TypeError, ValueError):
                    return None
            port_sharpe  = _try_float(summary_row.iloc[0].get("Port_Sharpe"))
            port_sortino = _try_float(summary_row.iloc[0].get("Port_Sortino"))
            port_calmar  = _try_float(summary_row.iloc[0].get("Port_Calmar"))

        results.append({
            "chunk":        chunk,
            "chunk_num":    chunk_num,
            "df_result":    df_result,
            "weights":      weights,
            "df_input":     df,
            "info":         info,
            "port_sharpe":  port_sharpe,
            "port_sortino": port_sortino,
            "port_calmar":  port_calmar,
            "csv_path":     csv_path,
            "success":      True,
            "error":        None,
        })

    # ── Write master summary CSV ──────────────────────────────────────────
    _write_chunk_summary(results, output_dir, total_money)

    # ── Substitution advisor: find & suggest replacements for outlier funds ─
    sub_advice = []
    try:
        sub_advice = _substitution_advisor(results, total_money)
    except Exception as e:
        print(f"\n  ⚠ Substitution advisor failed: {e}")

    # ── Generate portfolio risk-return visualization ───────────────────────
    try:
        viz_path = _generate_portfolio_viz(results, output_dir, total_money,
                                           sub_advice=sub_advice)
        if viz_path:
            print(f"\n  Portfolio visualization → {viz_path}")
    except Exception as e:
        print(f"\n  ⚠ Visualization generation failed: {e}")

    return results


def _write_chunk_summary(results: list, output_dir: str, total_money: float):
    """
    Write allocation_summary.csv — one section per chunk, separated by blank
    rows, so the user can review all chunk allocations in one file.
    """
    import os
    rows = []

    for r in results:
        chunk     = r["chunk"]
        chunk_num = r["chunk_num"]

        # Header row for this chunk
        rows.append({
            "Chunk":       chunk_num,
            "Years":       f"{chunk['year_from']}–{chunk['year_to']}",
            "Fund Type":   "",
            "Fund Name":   f"── CHUNK {chunk_num}  "
                           f"(Yr {chunk['year_from']}–{chunk['year_to']}) ──",
            "Allocation_L": "",
            "Weight_%":    "",
            "Worst_Exp_Ret_%": "",
            "Std_Dev_used": "",
            "Max_DD_used":  "",
            "1Y_CAGR": "", "3Y_CAGR": "", "5Y_CAGR": "", "10Y_CAGR": "",
            "Port_Sharpe": "", "Port_Sortino": "", "Port_Calmar": "",
        })

        if not r.get("success"):
            rows.append({
                "Chunk": chunk_num, "Years": "",
                "Fund Name": f"  ERROR: {r.get('error', 'unknown')}",
            })
            rows.append({})   # blank separator
            continue

        df_result = r["df_result"]
        for _, row in df_result.iterrows():
            rows.append({
                "Chunk":       chunk_num,
                "Years":       f"{chunk['year_from']}–{chunk['year_to']}",
                "Fund Type":   row.get("Fund Type", ""),
                "Fund Name":   row.get("Fund Name", ""),
                "Allocation_L":  row.get("Allocation_L", ""),
                "Weight_%":      row.get("Weight_%", ""),
                "Worst_Exp_Ret_%": row.get("Worst_Exp_Ret_%", ""),
                "Std_Dev_used":  row.get("Std_Dev_used", ""),
                "Max_DD_used":   row.get("Max_DD_used", ""),
                "1Y_CAGR":    row.get("1Y_CAGR", ""),
                "3Y_CAGR":    row.get("3Y_CAGR", ""),
                "5Y_CAGR":    row.get("5Y_CAGR", ""),
                "10Y_CAGR":   row.get("10Y_CAGR", ""),
                "Port_Sharpe":  row.get("Port_Sharpe", ""),
                "Port_Sortino": row.get("Port_Sortino", ""),
                "Port_Calmar":  row.get("Port_Calmar", ""),
            })

        rows.append({})   # blank separator between chunks

    summary_path = str(Path(output_dir) / "allocation_summary.csv")
    all_keys = [
        "Chunk", "Years", "Fund Type", "Fund Name",
        "Allocation_L", "Weight_%",
        "Worst_Exp_Ret_%", "Std_Dev_used", "Max_DD_used",
        "1Y_CAGR", "3Y_CAGR", "5Y_CAGR", "10Y_CAGR",
        "Port_Sharpe", "Port_Sortino", "Port_Calmar",
    ]
    df_summary = pd.DataFrame(rows, columns=all_keys)
    df_summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    print(f"\n  Master summary saved → {summary_path}")


# ═══════════════════════════════════════════════════════════════════════════════
# SUBSTITUTION ADVISOR
# ═══════════════════════════════════════════════════════════════════════════════

def _safe_float(row, col):
    """Extract a float from a DataFrame row, returning None for NaN/missing."""
    import numpy as np
    try:
        v = row.get(col, None)
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return None
        return round(float(v), 6)
    except (ValueError, TypeError):
        return None


def _substitution_advisor(
    results: list,
    total_money: float,
) -> list:
    """
    For each chunk, identify funds that skew portfolio std_dev, find
    replacement candidates from the full universe, and print a
    substitution suggestion with projected portfolio metrics.

    Returns a list of dicts (one per chunk) with substitution details,
    used by the visualization to display suggestions.

    Algorithm:
      a) Identify "outlier" funds: std > 2× portfolio wtd_std
      b) Compute core portfolio metrics (excluding outliers)
      c) Find the minimum core std across all chunks → reference floor
      d) Search full universe for candidates with:
         - std ≤ 1.5 × min_core_std  (reasonably close to the cleanest core)
         - |dd| ≤ max of core portfolio's fund-level |dd| values
         - not already in the chunk's portfolio
         - Fund Type not already at 24% cap
         - highest return among qualifying candidates
      e) Hypothetically substitute (same weight), compute new portfolio metrics
      f) Print suggestion with before/after comparison
    """
    sep = "─" * 82

    # ── Step (a): For each chunk, identify outliers and compute core metrics ──
    chunk_analysis = []
    for r in results:
        if not r.get("success"):
            continue
        weights = r["weights"]
        df_in   = r["df_input"]
        mask    = weights > 1e-5
        w_sel   = weights[mask]
        df_sel  = df_in[mask].copy()
        df_sel["_w"] = w_sel

        # Portfolio-level metrics
        wtd_std = float(np.dot(w_sel, df_sel["adj_std"].values))
        wtd_ret = float(np.dot(w_sel, df_sel["adj_ret"].values))
        wtd_dd  = float(np.dot(w_sel, np.abs(df_sel["adj_dd"].values)))

        # Outlier threshold: std > 2× portfolio wtd_std
        std_threshold = 2.0 * wtd_std
        dd_threshold  = max(5.0 * wtd_dd, 0.005)  # 0.5% floor

        outliers = df_sel[
            (df_sel["adj_std"] > std_threshold) |
            (df_sel["adj_dd"].abs() > dd_threshold)
        ].copy()
        core = df_sel[~df_sel.index.isin(outliers.index)].copy()

        # Core metrics
        if len(core) > 0:
            core_w   = core["_w"].values
            core_std = float(np.dot(core_w, core["adj_std"].values))
            core_ret = float(np.dot(core_w, core["adj_ret"].values))
            core_dd  = float(np.dot(core_w, np.abs(core["adj_dd"].values)))
            # Max per-fund dd in the core (as a ceiling for candidates)
            core_max_fund_dd = float(core["adj_dd"].abs().max())
            # Max per-fund std in the core
            core_max_fund_std = float(core["adj_std"].max())
        else:
            core_std = wtd_std
            core_ret = wtd_ret
            core_dd  = wtd_dd
            core_max_fund_dd  = dd_threshold
            core_max_fund_std = std_threshold

        # Fund type weights for checking 24% cap
        type_weights = {}
        for _, row in df_sel.iterrows():
            ft = row["Fund Type"]
            type_weights[ft] = type_weights.get(ft, 0.0) + row["_w"]

        chunk_analysis.append({
            "result":       r,
            "df_sel":       df_sel,
            "outliers":     outliers,
            "core":         core,
            "wtd_std":      wtd_std,
            "wtd_ret":      wtd_ret,
            "wtd_dd":       wtd_dd,
            "core_std":     core_std,
            "core_ret":     core_ret,
            "core_dd":      core_dd,
            "core_max_fund_std": core_max_fund_std,
            "core_max_fund_dd":  core_max_fund_dd,
            "type_weights": type_weights,
            "std_threshold": std_threshold,
            "dd_threshold":  dd_threshold,
        })

    if not chunk_analysis:
        return []

    # ── Step (b): Find minimum core std across all chunks ──
    min_core_std = min(ca["core_max_fund_std"] for ca in chunk_analysis)

    print(f"\n{'═' * 82}")
    print(f"  SUBSTITUTION ADVISOR")
    print(f"{'═' * 82}")

    # ── Steps (c)-(f): For each chunk, find candidates and suggest ──
    sub_advice = []

    for ca in chunk_analysis:
        r       = ca["result"]
        chunk   = r["chunk"]
        df_full = r["df_input"]   # full 543-fund universe
        df_sel  = ca["df_sel"]
        outliers = ca["outliers"]
        chunk_label = f"C{r['chunk_num']} (Yr {chunk['year_from']}–{chunk['year_to']})"
        target_ret  = chunk["min_return"] / 100.0   # e.g. 0.08
        ret_floor   = target_ret - 0.001            # 10 bps below target (≤0.1% return drop)

        print(f"\n{sep}")
        print(f"  {chunk_label}  (target ret: {target_ret*100:.2f}%, "
              f"floor: {ret_floor*100:.2f}%)")
        print(f"{sep}")

        if len(outliers) == 0:
            print(f"  ✓ No outlier funds — portfolio is harmonious.")
            sub_advice.append({
                "chunk_label": chunk_label,
                "chunk_num":   r["chunk_num"],
                "has_outliers": False,
                "return_floor": round(ret_floor * 100, 2),
                "current": {
                    "ret": round(ca["wtd_ret"] * 100, 3),
                    "std": round(ca["wtd_std"] * 100, 3),
                    "dd":  round(ca["wtd_dd"] * 100, 3),
                },
                "substitutions": [],
            })
            continue

        # Print outliers
        print(f"  Outlier funds (std > {ca['std_threshold']*100:.2f}% "
              f"or |dd| > {ca['dd_threshold']*100:.2f}%):")
        for _, orow in outliers.iterrows():
            print(f"    • {orow['Fund Name']:<45} "
                  f"wt={orow['_w']*100:.1f}%  ret={orow['adj_ret']*100:.2f}%  "
                  f"std={orow['adj_std']*100:.2f}%  |dd|={abs(orow['adj_dd'])*100:.2f}%")

        print(f"\n  Core portfolio (excluding outliers):")
        print(f"    ret={ca['core_ret']*100:.2f}%  std={ca['core_std']*100:.2f}%  "
              f"|dd|={ca['core_dd']*100:.2f}%")
        print(f"    Max fund std in core: {ca['core_max_fund_std']*100:.2f}%")
        print(f"    Max fund |dd| in core: {ca['core_max_fund_dd']*100:.2f}%")

        # Set of fund names already in this chunk
        existing_names = set(df_sel["Fund Name"].values)

        # Candidate search criteria
        cand_std_cap = 1.5 * ca["core_max_fund_std"]
        cand_dd_cap  = max(ca["core_max_fund_dd"], 0.003)  # floor 0.3%

        print(f"\n  Searching replacements: std ≤ {cand_std_cap*100:.2f}%, "
              f"|dd| ≤ {cand_dd_cap*100:.2f}%")

        substitutions = []

        for _, orow in outliers.iterrows():
            outlier_name = orow["Fund Name"]
            outlier_wt   = orow["_w"]
            outlier_type = orow["Fund Type"]

            # How much room does this fund's type have?
            type_wt_without = ca["type_weights"].get(outlier_type, 0) - outlier_wt

            # Find candidates from full universe
            candidates = df_full[
                (df_full["adj_std"] <= cand_std_cap) &
                (df_full["adj_dd"].abs() <= cand_dd_cap) &
                (~df_full["Fund Name"].isin(existing_names)) &
                (df_full["adj_ret"] > 0)
            ].copy()

            # Check per-type cap: candidate's type must have room
            # (either same type as outlier — room freed, or different type with room)
            def _type_has_room(fund_type):
                if fund_type == outlier_type:
                    # We're removing outlier, so room = 24% - (current - outlier_wt)
                    return (type_wt_without + outlier_wt) <= 0.24 + 1e-6
                else:
                    current_type_wt = ca["type_weights"].get(fund_type, 0.0)
                    return (current_type_wt + outlier_wt) <= 0.24 + 1e-6

            candidates = candidates[
                candidates["Fund Type"].apply(_type_has_room)
            ]

            if len(candidates) == 0:
                print(f"\n    {outlier_name}: no qualifying replacements found.")
                substitutions.append({
                    "outlier_name": outlier_name,
                    "outlier_wt":   round(outlier_wt * 100, 2),
                    "outlier_ret":  round(orow["adj_ret"] * 100, 2),
                    "outlier_std":  round(orow["adj_std"] * 100, 2),
                    "outlier_dd":   round(abs(orow["adj_dd"]) * 100, 2),
                    "candidate": None,
                })
                continue

            # Pick highest return candidate
            best = candidates.sort_values("adj_ret", ascending=False).iloc[0]

            # Hypothetical portfolio: swap outlier for candidate at same weight
            new_sel = df_sel.copy()
            # Remove outlier
            new_sel = new_sel[new_sel["Fund Name"] != outlier_name]
            # Add candidate with outlier's weight
            cand_row = best.copy()
            cand_row["_w"] = outlier_wt
            new_sel = pd.concat([new_sel, cand_row.to_frame().T], ignore_index=True)

            new_w   = new_sel["_w"].values.astype(float)
            new_ret = float(np.dot(new_w, new_sel["adj_ret"].values.astype(float)))
            new_std = float(np.dot(new_w, new_sel["adj_std"].values.astype(float)))
            new_dd  = float(np.dot(new_w, np.abs(new_sel["adj_dd"].values.astype(float))))
            new_calmar = (new_ret / new_dd) if new_dd > 1e-9 else float("inf")

            sub_info = {
                "outlier_name": outlier_name,
                "outlier_wt":   round(outlier_wt * 100, 2),
                "outlier_ret":  round(orow["adj_ret"] * 100, 2),
                "outlier_std":  round(orow["adj_std"] * 100, 2),
                "outlier_dd":   round(abs(orow["adj_dd"]) * 100, 2),
                "candidate": {
                    "name":  str(best["Fund Name"]),
                    "type":  str(best["Fund Type"]),
                    "ret":   round(float(best["adj_ret"]) * 100, 2),
                    "std":   round(float(best["adj_std"]) * 100, 2),
                    "dd":    round(abs(float(best["adj_dd"])) * 100, 2),
                    # Per-fund metrics for CSV reconstruction after substitution
                    "sharpe_5y":  _safe_float(best, "Sharpe_5Y"),
                    "sharpe_10y": _safe_float(best, "Sharpe_10Y"),
                    "sortino_5y": _safe_float(best, "Sortino_5Y"),
                    "sortino_10y":_safe_float(best, "Sortino_10Y"),
                    "calmar_5y":  _safe_float(best, "Calmar_5Y"),
                    "calmar_10y": _safe_float(best, "Calmar_10Y"),
                    "combined_ratio_10y": _safe_float(best, "Combined_Ratio_10Y"),
                    "cagr_1y":  _safe_float(best, "1Y_CAGR"),
                    "cagr_3y":  _safe_float(best, "3Y_CAGR"),
                    "cagr_5y":  _safe_float(best, "5Y_CAGR"),
                    "cagr_10y": _safe_float(best, "10Y_CAGR"),
                },
                "new_portfolio": {
                    "ret":    round(new_ret * 100, 3),
                    "std":    round(new_std * 100, 3),
                    "dd":     round(new_dd * 100, 3),
                    "calmar": round(new_calmar, 2),
                },
            }
            substitutions.append(sub_info)

            # Also add candidate to existing_names so next outlier doesn't pick same fund
            existing_names.add(str(best["Fund Name"]))

            # Print
            c = sub_info["candidate"]
            np_ = sub_info["new_portfolio"]
            print(f"\n    SWAP: {outlier_name}")
            print(f"      Remove → wt={sub_info['outlier_wt']:.1f}%  "
                  f"ret={sub_info['outlier_ret']:.2f}%  "
                  f"std={sub_info['outlier_std']:.2f}%  "
                  f"|dd|={sub_info['outlier_dd']:.2f}%")
            print(f"      Add    → {c['name']}")
            print(f"               wt={sub_info['outlier_wt']:.1f}%  "
                  f"ret={c['ret']:.2f}%  std={c['std']:.2f}%  |dd|={c['dd']:.2f}%")
            print(f"               Type: {c['type']}")

        # Compute cumulative hypothetical (all swaps applied) with return floor
        active_subs = [s for s in substitutions if s.get("candidate")]
        if active_subs:
            # ── Helper: compute hypothetical portfolio metrics ──
            def _compute_hyp(subs_list, rebalances=None):
                """Compute portfolio metrics after applying swaps + optional rebalances.
                rebalances: list of {"from": name, "to": name, "shift": fraction} dicts
                """
                hyp = df_sel.copy()
                for s in subs_list:
                    hyp = hyp[hyp["Fund Name"] != s["outlier_name"]]
                    cd = df_full[df_full["Fund Name"] == s["candidate"]["name"]].iloc[0].copy()
                    cd["_w"] = s["outlier_wt"] / 100.0
                    hyp = pd.concat([hyp, cd.to_frame().T], ignore_index=True)
                # Apply weight rebalances (shift weight from one fund to another)
                if rebalances:
                    for rb in rebalances:
                        fm = hyp["Fund Name"] == rb["from"]
                        to = hyp["Fund Name"] == rb["to"]
                        if fm.any() and to.any():
                            hyp.loc[fm, "_w"] = hyp.loc[fm, "_w"].astype(float) - rb["shift"]
                            hyp.loc[to, "_w"] = hyp.loc[to, "_w"].astype(float) + rb["shift"]
                hw = hyp["_w"].values.astype(float)
                hr = float(np.dot(hw, hyp["adj_ret"].values.astype(float)))
                hs = float(np.dot(hw, hyp["adj_std"].values.astype(float)))
                hd = float(np.dot(hw, np.abs(hyp["adj_dd"].values.astype(float))))
                return hr, hs, hd

            h_ret, h_std, h_dd = _compute_hyp(active_subs)

            # ── Return floor enforcement ──
            # Drop the swap with largest weighted return loss until floor is met
            dropped = []
            while h_ret < ret_floor and len(active_subs) > 0:
                worst_idx = max(
                    range(len(active_subs)),
                    key=lambda i: (
                        active_subs[i]["outlier_ret"] / 100.0
                        - active_subs[i]["candidate"]["ret"] / 100.0
                    ) * active_subs[i]["outlier_wt"] / 100.0,
                )
                dropped_sub = active_subs.pop(worst_idx)
                dropped.append(dropped_sub)
                print(f"\n    ⚠ Dropping swap of {dropped_sub['outlier_name']}"
                      f" — return would breach {ret_floor*100:.2f}% floor")
                if active_subs:
                    h_ret, h_std, h_dd = _compute_hyp(active_subs)
                else:
                    h_ret, h_std, h_dd = ca["wtd_ret"], ca["wtd_std"], ca["wtd_dd"]

            # ── Weight rebalancing for dropped outliers ──
            # If a swap was dropped (outlier stays), but a swapped-in candidate
            # has room below max_per_fund, shift weight from the outlier to that
            # candidate to reduce risk, subject to the return floor.
            rebalances = []
            mf_max = 0.08  # max per-fund weight
            if dropped and active_subs:
                for ds in dropped:
                    outlier_name = ds["outlier_name"]
                    outlier_wt   = ds["outlier_wt"] / 100.0  # fraction

                    # Find candidates that were successfully swapped in
                    for asub in active_subs:
                        cand_name = asub["candidate"]["name"]
                        cand_wt   = asub["outlier_wt"] / 100.0  # current weight of candidate
                        # Include any prior rebalance shifts already assigned to this candidate
                        prior_shift = sum(rb["shift"] for rb in rebalances
                                          if rb["to"] == cand_name)
                        room = mf_max - (cand_wt + prior_shift)
                        if room < 0.005:  # less than 0.5% room — skip
                            continue

                        # Also check: candidate's ret must be < outlier's ret
                        # (otherwise shifting weight would increase return, not decrease risk)
                        cand_ret = asub["candidate"]["ret"] / 100.0
                        outlier_ret_frac = ds["outlier_ret"] / 100.0
                        if cand_ret >= outlier_ret_frac:
                            continue  # candidate is higher return — no risk reduction

                        # Max shift = min(room, outlier_wt - min_per_fund floor)
                        min_outlier_wt = 0.02  # min per-fund if selected
                        max_shift = min(room, outlier_wt - min_outlier_wt)
                        if max_shift < 0.005:
                            continue

                        # Binary search for max shift that keeps return ≥ floor
                        best_shift = 0.0
                        lo, hi = 0.0, max_shift
                        for _ in range(20):  # ~20 iterations gives <0.01% precision
                            mid = (lo + hi) / 2.0
                            test_rb = rebalances + [{"from": outlier_name,
                                                     "to": cand_name,
                                                     "shift": mid}]
                            tr, ts, td = _compute_hyp(active_subs, test_rb)
                            if tr >= ret_floor:
                                best_shift = mid
                                lo = mid
                            else:
                                hi = mid

                        if best_shift >= 0.005:  # at least 0.5% shift to be meaningful
                            rebalances.append({
                                "from": outlier_name,
                                "to":   cand_name,
                                "shift": round(best_shift, 4),
                            })
                            outlier_wt -= best_shift  # reduce available for next candidate
                            print(f"\n    ↻ REBALANCE: shift {best_shift*100:.1f}% "
                                  f"from {outlier_name}")
                            print(f"      → to {cand_name} "
                                  f"(now {(cand_wt + prior_shift + best_shift)*100:.1f}%)")

            # Recompute final metrics with rebalances applied
            if rebalances:
                h_ret, h_std, h_dd = _compute_hyp(active_subs, rebalances)

            # Mark dropped subs
            for ds in dropped:
                for s in substitutions:
                    if s["outlier_name"] == ds["outlier_name"]:
                        s["_dropped_for_floor"] = True
                        break

            h_cal = (h_ret / h_dd) if h_dd > 1e-9 else float("inf")

            remaining = [s for s in substitutions
                         if s.get("candidate") and not s.get("_dropped_for_floor")]

            # ── DD guard: if max_dd worsened, reject ALL substitutions ──
            if h_dd > ca["wtd_dd"] + 1e-6:
                print(f"\n  ⚠ |Max DD| would increase "
                      f"({ca['wtd_dd']*100:.3f}% → {h_dd*100:.3f}%) "
                      f"— substitutions rejected for {chunk_label}.")
                sub_advice.append({
                    "chunk_label":    chunk_label,
                    "chunk_num":      r["chunk_num"],
                    "has_outliers":   True,
                    "return_floor":   round(ret_floor * 100, 2),
                    "current": {
                        "ret": round(ca["wtd_ret"] * 100, 3),
                        "std": round(ca["wtd_std"] * 100, 3),
                        "dd":  round(ca["wtd_dd"] * 100, 3),
                    },
                    "substitutions": [],
                    "dropped": [{"outlier_name": s["outlier_name"],
                                 "reason": "|dd| would increase"}
                                for s in substitutions if s.get("candidate")],
                })
            elif remaining:
                print(f"\n  {'─' * 60}")
                if dropped or rebalances:
                    extras = []
                    if dropped:
                        extras.append(f"{len(remaining)} of "
                                      f"{len(remaining)+len(dropped)} swaps kept")
                    if rebalances:
                        extras.append(f"{len(rebalances)} rebalance(s)")
                    print(f"  {chunk_label} — Portfolio comparison "
                          f"({', '.join(extras)}, floor {ret_floor*100:.2f}%):")
                else:
                    print(f"  {chunk_label} — Portfolio comparison (all swaps applied):")
                print(f"    {'':30} {'Current':>12} {'After Swap':>12} {'Change':>12}")
                print(f"    {'Return':30} {ca['wtd_ret']*100:>11.2f}% {h_ret*100:>11.2f}% "
                      f"{(h_ret - ca['wtd_ret'])*100:>+11.2f}%")
                print(f"    {'Std Dev':30} {ca['wtd_std']*100:>11.3f}% {h_std*100:>11.3f}% "
                      f"{(h_std - ca['wtd_std'])*100:>+11.3f}%")
                print(f"    {'|Max DD|':30} {ca['wtd_dd']*100:>11.3f}% {h_dd*100:>11.3f}% "
                      f"{(h_dd - ca['wtd_dd'])*100:>+11.3f}%")
                print(f"    {'Calmar':30} "
                      f"{(ca['wtd_ret']/ca['wtd_dd'] if ca['wtd_dd']>1e-9 else 0):>11.1f}"
                      f"  {h_cal:>11.1f}")

                sub_advice.append({
                    "chunk_label":    chunk_label,
                    "chunk_num":      r["chunk_num"],
                    "has_outliers":   True,
                    "return_floor":   round(ret_floor * 100, 2),
                    "current": {
                        "ret": round(ca["wtd_ret"] * 100, 3),
                        "std": round(ca["wtd_std"] * 100, 3),
                        "dd":  round(ca["wtd_dd"] * 100, 3),
                    },
                    "after_swap": {
                        "ret":    round(h_ret * 100, 3),
                        "std":    round(h_std * 100, 3),
                        "dd":     round(h_dd * 100, 3),
                        "calmar": round(h_cal, 2),
                    },
                    "substitutions": [s for s in substitutions
                                      if not s.get("_dropped_for_floor")],
                    "rebalances": [{"from": rb["from"],
                                    "to": rb["to"],
                                    "shift_pct": round(rb["shift"] * 100, 2)}
                                   for rb in rebalances],
                    "dropped": [{"outlier_name": d["outlier_name"],
                                 "reason": "return floor breach"}
                                for d in dropped],
                })
            else:
                # All swaps dropped
                print(f"\n  ⚠ All swaps breach the {ret_floor*100:.2f}% return floor "
                      f"— no substitutions recommended.")
                sub_advice.append({
                    "chunk_label":    chunk_label,
                    "chunk_num":      r["chunk_num"],
                    "has_outliers":   True,
                    "return_floor":   round(ret_floor * 100, 2),
                    "current": {
                        "ret": round(ca["wtd_ret"] * 100, 3),
                        "std": round(ca["wtd_std"] * 100, 3),
                        "dd":  round(ca["wtd_dd"] * 100, 3),
                    },
                    "substitutions": [],
                    "dropped": [{"outlier_name": d["outlier_name"],
                                 "reason": "return floor breach"}
                                for d in dropped],
                })
        else:
            sub_advice.append({
                "chunk_label":    chunk_label,
                "chunk_num":      r["chunk_num"],
                "has_outliers":   True,
                "return_floor":   round(ret_floor * 100, 2),
                "current": {
                    "ret": round(ca["wtd_ret"] * 100, 3),
                    "std": round(ca["wtd_std"] * 100, 3),
                    "dd":  round(ca["wtd_dd"] * 100, 3),
                },
                "substitutions": substitutions,
            })

    return sub_advice


# ═══════════════════════════════════════════════════════════════════════════════
# INPUT HELPERS
# ═══════════════════════════════════════════════════════════════════════════════



def _generate_portfolio_viz(
    results: list,
    output_dir: str,
    total_money: float,
    sub_advice: list = None,
) -> str:
    """
    Generate a standalone HTML file with an interactive portfolio
    risk-return visualization.  Pure HTML/CSS/JS with inline SVG
    charts — no CDN dependencies, works fully offline.

    Returns the path to the generated HTML file.
    """
    import json as _json
    import os

    # ── Extract chunk data from results ──────────────────────────────────
    chunks_data = []
    for r in results:
        if not r.get("success"):
            continue
        chunk  = r["chunk"]
        weights = r["weights"]
        df_in   = r["df_input"]

        mask   = weights > 1e-5
        w_sel  = weights[mask]
        df_sel = df_in[mask]

        wtd_ret = float(np.dot(w_sel, df_sel["adj_ret"].values))
        wtd_std = float(np.dot(w_sel, df_sel["adj_std"].values))
        wtd_dd  = float(np.dot(w_sel, np.abs(df_sel["adj_dd"].values)))

        funds_list = []
        for i in range(len(df_sel)):
            funds_list.append({
                "name": str(df_sel.iloc[i]["Fund Name"]),
                "wt":   round(float(w_sel[i]) * 100, 2),
                "ret":  round(float(df_sel.iloc[i]["adj_ret"]) * 100, 2),
                "std":  round(float(df_sel.iloc[i]["adj_std"]) * 100, 2),
                "dd":   round(abs(float(df_sel.iloc[i]["adj_dd"])) * 100, 2),
                "type": str(df_sel.iloc[i]["Fund Type"]),
            })

        chunks_data.append({
            "label":        f"C{r['chunk_num']} (Yr {chunk['year_from']}\u2013{chunk['year_to']})",
            "target_ret":   chunk["min_return"],
            "achieved_ret": round(wtd_ret * 100, 3),
            "wtd_std":      round(wtd_std * 100, 3),
            "wtd_dd":       round(wtd_dd * 100, 3),
            "calmar":       round(r.get("port_calmar") or 0.0, 2),
            "sharpe":       round(r.get("port_sharpe") or 0.0, 3),
            "sortino":      round(r.get("port_sortino") or 0.0, 3),
            "n_funds":      int(mask.sum()),
            "funds":        funds_list,
        })

    if not chunks_data:
        return ""

    chunks_json = _json.dumps(chunks_data, indent=2)

    # The HTML template uses {{ / }} for literal braces in JS/CSS
    # and { } for Python f-string interpolation.
    html_content = (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="UTF-8">\n'
        '<title>Portfolio Risk \u00b7 Return Landscape</title>\n'
        '<style>\n'
        '* {margin:0;padding:0;box-sizing:border-box;}\n'
        ':root{--bg:#0a0f1a;--sf:#111827;--sl:#1a2235;--bd:#2a3650;'
        '--tx:#e2e8f0;--tm:#8896b0;--a1:#60a5fa;--a2:#34d399;--a3:#f59e0b;'
        '--wr:#f87171;--gd:#4ade80;}\n'
        'body{background:var(--bg);color:var(--tx);'
        "font-family:'JetBrains Mono','Fira Code','Consolas',monospace;padding:24px;}\n"
        '.hd{margin-bottom:24px;border-bottom:1px solid var(--bd);padding-bottom:16px;}\n'
        '.hd h1{font-size:22px;font-weight:600;letter-spacing:-0.5px;}\n'
        '.hd p{color:var(--tm);font-size:12px;margin-top:6px;}\n'
        '.gr{display:grid;grid-template-columns:1fr 400px;gap:20px;align-items:start;}\n'
        '.cw{background:var(--sf);border-radius:12px;border:1px solid var(--bd);padding:24px;}\n'
        '.ct{color:var(--tm);font-size:11px;text-transform:uppercase;letter-spacing:1px;margin-bottom:16px;}\n'
        '.cr2{display:flex;align-items:center;gap:0;margin-bottom:4px;position:relative;}\n'
        '.cl{width:130px;text-align:right;padding-right:12px;font-size:10px;color:var(--tm);'
        'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}\n'
        '.bg{display:flex;gap:3px;flex:1;align-items:center;}\n'
        '.br{height:20px;border-radius:3px;transition:all 0.3s;cursor:pointer;min-width:2px;}\n'
        '.br:hover{opacity:0.85;filter:brightness(1.2);}\n'
        '.bv{font-size:10px;color:var(--tx);padding-left:6px;white-space:nowrap;}\n'
        '.lg{display:flex;gap:20px;margin-top:16px;justify-content:center;}\n'
        '.li{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--tm);}\n'
        '.ld{width:12px;height:12px;border-radius:3px;}\n'
        '.sw{position:relative;}\n'
        '.ss{width:100%;}\n'
        '.sp{cursor:pointer;transition:all 0.2s;}\n'
        '.sp:hover{filter:brightness(1.3);}\n'
        ".al{font-size:11px;fill:var(--tm);font-family:'JetBrains Mono',monospace;}\n"
        '.gl{stroke:var(--bd);stroke-width:0.5;}\n'
        '.pn{display:flex;flex-direction:column;gap:16px;}\n'
        '.cc{background:var(--sf);border:1px solid var(--bd);border-radius:10px;'
        'padding:14px 16px;cursor:pointer;transition:all 0.2s;}\n'
        '.cc:hover,.cc.act{background:var(--sl);}\n'
        '.hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;}\n'
        '.lb{font-weight:600;font-size:14px;}\n'
        '.ba{font-size:10px;padding:2px 8px;border-radius:10px;font-weight:500;}\n'
        '.ba.w{background:rgba(248,113,113,0.15);color:var(--wr);}\n'
        '.ba.g{background:rgba(74,222,128,0.15);color:var(--gd);}\n'
        '.me{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:6px;font-size:11px;}\n'
        '.ml{color:var(--tm);font-size:9px;text-transform:uppercase;letter-spacing:0.5px;}\n'
        '.mv{color:var(--tx);font-weight:500;}\n'
        '.dt{margin-top:12px;padding-top:12px;border-top:1px solid var(--bd);display:none;}\n'
        '.cc.act .dt{display:block;}\n'
        '.cf{background:rgba(248,113,113,0.08);border-radius:6px;padding:8px 10px;'
        'margin-bottom:6px;border:1px solid rgba(248,113,113,0.15);}\n'
        '.cf .fn{color:var(--tx);font-size:11px;font-weight:500;margin-bottom:3px;}\n'
        '.cf .fm{color:var(--tm);font-size:10px;}\n'
        '.cf .hi{color:var(--wr);}\n'
        '.su{margin-top:8px;padding:8px 10px;background:rgba(96,165,250,0.1);'
        'border-radius:6px;border:1px solid rgba(96,165,250,0.2);}\n'
        '.su .st{color:var(--a1);font-size:11px;font-weight:500;}\n'
        '.su .sd{color:var(--tm);font-size:10px;margin-top:3px;}\n'
        '.op{padding:10px 12px;background:rgba(74,222,128,0.08);border-radius:6px;'
        'border:1px solid rgba(74,222,128,0.15);}\n'
        '.op .ot{color:var(--gd);font-size:12px;font-weight:500;}\n'
        '.op .od{color:var(--tm);font-size:10px;margin-top:3px;}\n'
        '.cm{background:var(--sf);border:1px solid var(--bd);border-radius:10px;padding:14px 16px;}\n'
        '.cm h3{color:var(--tx);font-weight:600;font-size:13px;margin-bottom:10px;}\n'
        '.cmr{display:flex;justify-content:space-between;align-items:center;'
        'padding:6px 0;border-bottom:1px solid var(--bd);}\n'
        '.ci{width:24px;height:24px;border-radius:50%;display:flex;'
        'align-items:center;justify-content:center;font-size:11px;font-weight:600;}\n'
        '.cbar{display:flex;border-radius:6px;overflow:hidden;height:8px;margin-top:12px;}\n'
        '.cap{color:var(--tm);font-size:9px;margin-top:4px;text-align:center;}\n'
        '.tip{position:absolute;background:var(--sl);border:1px solid var(--bd);'
        'border-radius:8px;padding:10px 12px;font-size:11px;pointer-events:none;'
        'z-index:100;display:none;min-width:200px;box-shadow:0 4px 20px rgba(0,0,0,0.4);}\n'
        '</style>\n</head>\n<body>\n'
    )

    html_content += (
        '<div class="hd">\n'
        '  <h1>Portfolio Risk \u00b7 Return Landscape</h1>\n'
        f'  <p>{len(chunks_data)}-chunk allocation \u00b7 '
        f'\u20b9{total_money:.0f}L corpus \u00b7 '
        'Click a card for fund-level analysis</p>\n'
        '</div>\n'
        '<div class="gr">\n'
        '  <div>\n'
        '    <div class="cw">\n'
        '      <div class="ct">Return vs Std Dev (bubble size = |Max DD|)</div>\n'
        '      <div class="sw" id="scatter"></div>\n'
        '    </div>\n'
        '    <div class="cw" style="margin-top:20px;">\n'
        '      <div class="ct" id="bar-title">Click a chunk card to see fund breakdown</div>\n'
        '      <div id="bar-chart"></div>\n'
        '      <div class="lg" id="bar-legend" style="display:none;">\n'
        '        <div class="li"><div class="ld" style="background:#4ade80;"></div>Std Dev %</div>\n'
        '        <div class="li"><div class="ld" style="background:#f87171;"></div>|Max DD| %</div>\n'
        '        <div class="li"><div class="ld" style="background:#60a5fa;"></div>Return %</div>\n'
        '      </div>\n'
        '    </div>\n'
        '  </div>\n'
        '  <div class="pn" id="panel"></div>\n'
        '</div>\n'
        '<div class="tip" id="tip"></div>\n'
    )

    # JavaScript — written without f-string {{ }} by using string concatenation
    html_content += '<script>\n'
    html_content += f'const C = {chunks_json};\n'
    sub_json = _json.dumps(sub_advice or [], indent=2)
    html_content += f'const SUB = {sub_json};\n'
    html_content += "const COLS = ['#60a5fa','#34d399','#f59e0b','#f472b6','#a78bfa'];\n"

    html_content += r"""
// ── Analysis ──
function getConcerns(c) {
  var st=2.0*c.wtd_std, dt=Math.max(5.0*c.wtd_dd,0.5);
  return c.funds.filter(function(f){return f.std>st||f.dd>dt;}).sort(function(a,b){return b.std-a.std;});
}
function harmRet(c) {
  var con=getConcerns(c); if(!con.length) return null;
  var cn={}; con.forEach(function(f){cn[f.name]=1;});
  var sf=c.funds.filter(function(f){return !cn[f.name];});
  var tw=0,wr=0; sf.forEach(function(f){tw+=f.wt;wr+=(f.wt/100)*f.ret;});
  return tw>0?Math.floor((wr/tw)*100*20)/20:0;
}
function getCom() {
  var sets=C.map(function(c){var s={};c.funds.forEach(function(f){s[f.name]=1;});return s;});
  var all={};C.forEach(function(c){c.funds.forEach(function(f){all[f.name]=1;});});
  var names=Object.keys(all);
  var r=[];
  for(var k=C.length;k>=1;k--) {
    var inK=names.filter(function(n){var cnt=0;sets.forEach(function(s){if(s[n])cnt++;});return cnt>=k;});
    var exK=names.filter(function(n){var cnt=0;sets.forEach(function(s){if(s[n])cnt++;});return cnt===k;});
    r.push({k:k,count:inK.length,exc:exK.length,names:exK});
  }
  return r;
}

// ── Scatter Plot (SVG) ──
(function() {
  var W=700,H=400,ML=60,MR=30,MT=30,MB=50;
  var pw=W-ML-MR,ph=H-MT-MB;
  var rets=[],stds=[],dds=[];
  C.forEach(function(c){rets.push(c.achieved_ret);stds.push(c.wtd_std);dds.push(c.wtd_dd);});
  var rMin=Math.min.apply(null,rets)-0.3,rMax=Math.max.apply(null,rets)+0.3;
  var sMin=0,sMax=Math.max.apply(null,stds)*1.3;
  var ddMax=Math.max.apply(null,dds);
  function sx(v){return (v-rMin)/(rMax-rMin)*pw;}
  function sy(v){return ph-(v-sMin)/(sMax-sMin)*ph;}
  function bub(v){return Math.max(12,Math.min(40,(v/Math.max(ddMax,0.01))*35));}

  var svg='<svg class="ss" viewBox="0 0 '+W+' '+H+'" xmlns="http://www.w3.org/2000/svg">';
  for(var i=0;i<=5;i++) {
    var y=MT+ph*i/5, x=ML+pw*i/5;
    svg+='<line x1="'+ML+'" y1="'+y+'" x2="'+(W-MR)+'" y2="'+y+'" class="gl"/>';
    svg+='<line x1="'+x+'" y1="'+MT+'" x2="'+x+'" y2="'+(H-MB)+'" class="gl"/>';
    var sv=(sMax-sMin)*(1-i/5)+sMin;
    svg+='<text x="'+(ML-8)+'" y="'+(y+4)+'" text-anchor="end" class="al">'+sv.toFixed(1)+'%</text>';
    var rv=(rMax-rMin)*i/5+rMin;
    svg+='<text x="'+x+'" y="'+(H-MB+18)+'" text-anchor="middle" class="al">'+rv.toFixed(1)+'%</text>';
  }
  svg+='<text x="'+(ML+pw/2)+'" y="'+(H-5)+'" text-anchor="middle" class="al" style="font-size:12px;">Return %</text>';
  svg+='<text x="14" y="'+(MT+ph/2)+'" text-anchor="middle" class="al" style="font-size:12px;" transform="rotate(-90 14 '+(MT+ph/2)+')">Std Dev %</text>';

  C.forEach(function(c,i) {
    var cx=ML+sx(c.achieved_ret),cy=MT+sy(c.wtd_std),r=bub(c.wtd_dd);
    svg+='<circle class="sp" cx="'+cx+'" cy="'+cy+'" r="'+r+'" fill="'+COLS[i]+'" fill-opacity="0.25" stroke="'+COLS[i]+'" stroke-width="2" data-idx="'+i+'"';
    svg+=' onmouseenter="showTip(event,'+i+')" onmouseleave="hideTip()" onclick="toggleCard('+i+')"/>';
    svg+='<text x="'+cx+'" y="'+(cy-r-6)+'" text-anchor="middle" style="font-size:12px;fill:'+COLS[i]+";font-family:'JetBrains Mono',monospace;font-weight:600;\">"+c.label+'</text>';
  });
  svg+='</svg>';
  document.getElementById('scatter').innerHTML=svg;
})();

// ── Tooltip ──
var tip=document.getElementById('tip');
function showTip(evt,i) {
  var c=C[i];
  tip.style.display='block';
  tip.innerHTML='<div style="color:'+COLS[i]+';font-weight:600;margin-bottom:6px;">'+c.label+'</div>'+
    '<div style="color:var(--tm);">Return: <span style="color:var(--tx)">'+c.achieved_ret.toFixed(2)+'%</span></div>'+
    '<div style="color:var(--tm);">Std Dev: <span style="color:var(--tx)">'+c.wtd_std.toFixed(3)+'%</span></div>'+
    '<div style="color:var(--tm);">|Max DD|: <span style="color:var(--tx)">'+c.wtd_dd.toFixed(3)+'%</span></div>'+
    '<div style="color:var(--tm);">Calmar: <span style="color:var(--tx)">'+c.calmar.toFixed(1)+'</span></div>'+
    '<div style="color:var(--tm);">Sharpe: <span style="color:var(--tx)">'+c.sharpe.toFixed(3)+'</span></div>'+
    '<div style="color:var(--tm);">Funds: <span style="color:var(--tx)">'+c.n_funds+'</span></div>';
  var rect=evt.target.getBoundingClientRect();
  tip.style.left=(rect.right+12)+'px';
  tip.style.top=(rect.top-20+window.scrollY)+'px';
}
function hideTip() { tip.style.display='none'; }

// ── Bar chart for selected chunk ──
function showBars(i) {
  var c=C[i];
  document.getElementById('bar-title').textContent=c.label+' \u2014 Fund Risk Breakdown';
  document.getElementById('bar-legend').style.display='flex';
  var maxVal=0;
  c.funds.forEach(function(f){maxVal=Math.max(maxVal,f.std,f.dd,f.ret);});
  function scale(v){return Math.max(2,v/maxVal*300);}
  var stdTh=2.0*c.wtd_std, ddTh=Math.max(5.0*c.wtd_dd,0.5);
  var html='';
  var sorted=c.funds.slice().sort(function(a,b){return b.std-a.std;});
  sorted.forEach(function(f) {
    var isC=f.std>stdTh||f.dd>ddTh;
    var nm=f.name.length>35?f.name.substring(0,33)+'..':f.name;
    html+='<div class="cr2">';
    html+='<div class="cl" title="'+f.name+'" style="'+(isC?'color:var(--wr);':'')+'">'+nm+'</div>';
    html+='<div class="bg">';
    html+='<div class="br" style="width:'+scale(f.std)+'px;background:'+(f.std>stdTh?'#f87171':'#4ade80')+';opacity:0.8;" title="Std: '+f.std.toFixed(2)+'%"></div>';
    html+='<div class="br" style="width:'+scale(f.dd)+'px;background:'+(f.dd>ddTh?'#f87171':'#fb923c')+';opacity:0.6;" title="|DD|: '+f.dd.toFixed(2)+'%"></div>';
    html+='<div class="br" style="width:'+scale(f.ret)+'px;background:#60a5fa;opacity:0.5;" title="Ret: '+f.ret.toFixed(2)+'%"></div>';
    html+='<div class="bv">'+f.wt.toFixed(1)+'%</div>';
    html+='</div></div>';
  });
  document.getElementById('bar-chart').innerHTML=html;
}

// ── Panel cards ──
var active=null;
var panel=document.getElementById('panel');

C.forEach(function(c,i) {
  var con=getConcerns(c),hr=harmRet(c),hc=con.length>0;
  var card=document.createElement('div');
  card.className='cc'; card.style.borderLeft='4px solid '+COLS[i]; card.dataset.idx=i;
  card.onclick=function(){toggleCard(i);};

  var det='';
  if(hc) {
    var st2=(2*c.wtd_std).toFixed(2), dt2=Math.max(5*c.wtd_dd,0.5).toFixed(2);
    det+='<div style="color:var(--wr);font-size:11px;font-weight:500;margin-bottom:8px;">\u26a0 Funds of concern (std > '+st2+'% or |dd| > '+dt2+'%):</div>';
    con.forEach(function(f) {
      var sh=f.std>2*c.wtd_std, dh=f.dd>Math.max(5*c.wtd_dd,0.5);
      det+='<div class="cf"><div class="fn">'+f.name+'</div><div class="fm">Wt: '+f.wt.toFixed(1)+'% \u00b7 Ret: '+f.ret.toFixed(2)+'% \u00b7 Std: <span class="'+(sh?'hi':'')+'">'+f.std.toFixed(2)+'%</span> \u00b7 |DD|: <span class="'+(dh?'hi':'')+'">'+f.dd.toFixed(2)+'%</span></div></div>';
    });
    if(hr!==null) det+='<div class="su"><div class="st">\ud83d\udca1 For a harmonious portfolio, target \u2264 '+hr.toFixed(2)+'% return</div><div class="sd">Excluding concern funds, the safe universe yields ~'+hr.toFixed(2)+'%</div></div>';
    // Show substitution advice if available
    var sa=SUB.find(function(s){return s.chunk_num===(i+1);});
    if(sa && sa.substitutions && sa.substitutions.length>0) {
      det+='<div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--bd);">';
      var floorStr = sa.return_floor ? ' (ret floor: '+sa.return_floor.toFixed(2)+'%)' : '';
      det+='<div style="color:var(--a2);font-size:11px;font-weight:600;margin-bottom:8px;">\ud83d\udd04 Suggested Substitutions'+floorStr+':</div>';
      sa.substitutions.forEach(function(s) {
        if(!s.candidate) {
          det+='<div style="color:var(--tm);font-size:10px;margin-bottom:4px;">\u2022 '+s.outlier_name+': no qualifying replacement found</div>';
          return;
        }
        det+='<div style="background:rgba(52,211,153,0.08);border:1px solid rgba(52,211,153,0.15);border-radius:6px;padding:8px 10px;margin-bottom:6px;">';
        det+='<div style="font-size:10px;color:var(--wr);margin-bottom:4px;">\u2796 Remove: '+s.outlier_name+'</div>';
        det+='<div style="font-size:10px;color:var(--tm);margin-bottom:2px;">    wt='+s.outlier_wt.toFixed(1)+'% \u00b7 ret='+s.outlier_ret.toFixed(2)+'% \u00b7 std='+s.outlier_std.toFixed(2)+'% \u00b7 |dd|='+s.outlier_dd.toFixed(2)+'%</div>';
        det+='<div style="font-size:10px;color:var(--gd);margin-bottom:4px;">\u2795 Add: '+s.candidate.name+'</div>';
        det+='<div style="font-size:10px;color:var(--tm);">    wt='+s.outlier_wt.toFixed(1)+'% \u00b7 ret='+s.candidate.ret.toFixed(2)+'% \u00b7 std='+s.candidate.std.toFixed(2)+'% \u00b7 |dd|='+s.candidate.dd.toFixed(2)+'%</div>';
        det+='</div>';
      });
      if(sa.dropped && sa.dropped.length>0) {
        det+='<div style="background:rgba(251,191,36,0.1);border:1px solid rgba(251,191,36,0.25);border-radius:6px;padding:6px 10px;margin-bottom:6px;">';
        det+='<div style="font-size:10px;font-weight:500;color:#b45309;margin-bottom:3px;">\u26a0 Dropped:</div>';
        sa.dropped.forEach(function(d){det+='<div style="font-size:10px;color:var(--tm);">\u2022 '+d.outlier_name+' <span style="color:#b45309;font-style:italic;">— '+d.reason+'</span></div>';});
        det+='</div>';
      }
      if(sa.rebalances && sa.rebalances.length>0) {
        det+='<div style="background:rgba(168,85,247,0.08);border:1px solid rgba(168,85,247,0.2);border-radius:6px;padding:6px 10px;margin-bottom:6px;">';
        det+='<div style="font-size:10px;font-weight:500;color:#a855f7;margin-bottom:3px;">\u21bb Weight Rebalances:</div>';
        sa.rebalances.forEach(function(rb){det+='<div style="font-size:10px;color:var(--tm);">\u2022 Shift '+rb.shift_pct.toFixed(1)+'% from '+rb.from+' \u2192 '+rb.to+'</div>';});
        det+='</div>';
      }
      if(sa.after_swap) {
        var cur=sa.current, aft=sa.after_swap;
        det+='<div style="background:rgba(96,165,250,0.1);border:1px solid rgba(96,165,250,0.2);border-radius:6px;padding:8px 10px;margin-top:6px;">';
        det+='<div style="color:var(--a1);font-size:11px;font-weight:500;margin-bottom:6px;">\ud83d\udcca After all swaps:</div>';
        det+='<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px;font-size:10px;">';
        det+='<div><span style="color:var(--tm);">Ret:</span> <span style="color:var(--tx);">'+cur.ret.toFixed(2)+'%</span> \u2192 <span style="color:'+(aft.ret>=cur.ret?'var(--gd)':'var(--wr)')+'">'+aft.ret.toFixed(2)+'%</span></div>';
        det+='<div><span style="color:var(--tm);">Std:</span> <span style="color:var(--tx);">'+cur.std.toFixed(3)+'%</span> \u2192 <span style="color:'+(aft.std<=cur.std?'var(--gd)':'var(--wr)')+'">'+aft.std.toFixed(3)+'%</span></div>';
        det+='<div><span style="color:var(--tm);">|DD|:</span> <span style="color:var(--tx);">'+cur.dd.toFixed(3)+'%</span> \u2192 <span style="color:'+(aft.dd<=cur.dd?'var(--gd)':'var(--wr)')+'">'+aft.dd.toFixed(3)+'%</span></div>';
        det+='</div></div>';
      }
      det+='</div>';
    }
  } else {
    var ms=0; c.funds.forEach(function(f){ms=Math.max(ms,f.std);});
    det='<div class="op"><div class="ot">\u2713 Good optimal point found</div><div class="od">All '+c.n_funds+' funds within acceptable risk bounds. Max fund std: '+ms.toFixed(2)+'%</div></div>';
  }

  var mets=[['Ret',c.achieved_ret.toFixed(2)+'%'],['Std',c.wtd_std.toFixed(3)+'%'],['|DD|',c.wtd_dd.toFixed(3)+'%'],['Calmar',c.calmar.toFixed(1)]];
  var mhtml='';mets.forEach(function(m){mhtml+='<div><div class="ml">'+m[0]+'</div><div class="mv">'+m[1]+'</div></div>';});

  card.innerHTML='<div class="hdr"><span class="lb" style="color:'+COLS[i]+'">'+c.label+'</span><span class="ba '+(hc?'w':'g')+'">'+(hc?con.length+' concern'+(con.length>1?'s':''):'\u2713 Optimal')+'</span></div>'+
    '<div class="me">'+mhtml+'</div><div class="dt">'+det+'</div>';
  panel.appendChild(card);
});

function toggleCard(idx) {
  var cards=document.querySelectorAll('.cc');
  for(var i=0;i<cards.length;i++) {
    if(i===idx && !cards[i].classList.contains('act')) {
      cards[i].classList.add('act'); cards[i].style.borderColor=COLS[i]; active=i; showBars(i);
    } else {
      cards[i].classList.remove('act'); cards[i].style.borderColor='var(--bd)';
    }
  }
}

// ── Commonality ──
var com=getCom(),cd=document.createElement('div');cd.className='cm';
var ch='<h3>Fund Commonality</h3>';
com.forEach(function(r) {
  var isA=r.k===C.length, is1=r.k===1;
  var bg=isA?'rgba(74,222,128,0.2)':is1?'rgba(248,113,113,0.15)':'rgba(96,165,250,0.15)';
  var cl=isA?'var(--gd)':is1?'var(--wr)':'var(--a1)';
  ch+='<div class="cmr"><div style="display:flex;align-items:center;gap:8px;"><div class="ci" style="background:'+bg+';color:'+cl+'">'+(isA?'ALL':r.k)+'</div><span style="color:var(--tm);font-size:11px;">In '+(isA?'ALL':r.k)+' chunk'+(r.k>1?'s':'')+'</span></div><div style="display:flex;align-items:center;gap:8px;"><span style="color:var(--tx);font-weight:600;font-size:13px;">'+r.count+'</span><span style="color:var(--tm);font-size:10px;">fund'+(r.count!==1?'s':'')+'</span></div></div>';
});
var tot=0,ac=0;com.forEach(function(r){if(r.k===1)tot=r.count;if(r.k===C.length)ac=r.count;});
ch+='<div class="cbar">';
var rev=com.slice().reverse().filter(function(r){return r.exc>0;});
rev.forEach(function(r) {
  var cl2=r.k===C.length?'var(--gd)':r.k===1?'var(--wr)':'var(--a1)';
  ch+='<div style="flex:'+r.exc+';background:'+cl2+';opacity:'+(0.6+r.k/C.length*0.4)+'"></div>';
});
ch+='</div><div class="cap">'+ac+' / '+tot+' funds shared across all chunks</div>';
cd.innerHTML=ch;panel.appendChild(cd);
"""

    html_content += '</script>\n</body>\n</html>'

    viz_path = os.path.join(output_dir, "portfolio_viz.html")
    with open(viz_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    return viz_path



# ═══════════════════════════════════════════════════════════════════════════════
# STICKY-PORTFOLIO OPTIMIZER  (Mode B backward-induction + Mode A singular)
# ═══════════════════════════════════════════════════════════════════════════════
#
# Public entry point:  optimize_sticky_portfolio(state, all_funds, progress_cb)
#
# Internal call order:
#   build_expanded_universe  →  run_aim_pass  →  run_track_pass
#   (Mode A skips the last two and uses merge_chunks_strict instead)
# ═══════════════════════════════════════════════════════════════════════════════


def build_expanded_universe(
    chunks:        list,          # list[AllocationChunk]
    all_funds:     list,          # list[FundEntry]  (full scored database)
    max_additions: int = 50,
) -> list:
    """
    Build the fund universe for the sticky-portfolio optimizer.

    1. Seed  = union of all FundEntry objects already present across all chunks
               (de-duplicated by name, preserving the FundEntry with the highest
               combined_ratio when a name appears in multiple chunks).
    2. Candidates = funds in all_funds not in the seed whose metrics are
               "similar" to at least one seed fund:
                 |cagr_5_diff|  <= 0.2 pp
                 |std_dev_diff| <= 0.1 pp
                 |max_dd_diff|  <= 0.2 pp   (absolute values compared)
               CAGRs in FundEntry are stored as percentages (e.g. 7.05).
    3. Sort candidates descending by AUM (FundEntry has no AUM field, so we
               proxy with combined_ratio as a quality-AUM stand-in, which is the
               best available ordinal on FundEntry objects).
    4. Append the top max_additions candidates.

    Returns a deduplicated list[FundEntry] (seed + up to 50 additions).
    """
    from models import FundEntry, _first_available  # local import to avoid circular at module level

    # ── Step 1: seed set ─────────────────────────────────────────────────────
    seed_map: dict[str, FundEntry] = {}
    for chunk in chunks:
        for f in chunk.funds:
            key = f.name.strip().lower()
            if key not in seed_map or f.combined_ratio > seed_map[key].combined_ratio:
                seed_map[key] = f
    seed_list = list(seed_map.values())
    seed_keys  = set(seed_map.keys())

    # ── Step 2: similarity expansion ─────────────────────────────────────────
    def _cagr(f: FundEntry) -> float:
        return _first_available(f.cagr_5, f.cagr_3, f.cagr_1, default=0.0)

    candidates: list[FundEntry] = []
    for candidate in all_funds:
        ckey = candidate.name.strip().lower()
        if ckey in seed_keys:
            continue
        c_cagr   = _cagr(candidate)
        c_std    = candidate.std_dev
        c_dd     = abs(candidate.max_dd)
        # Test against every seed fund — include if similar to ANY seed fund
        for seed in seed_list:
            s_cagr = _cagr(seed)
            s_std  = seed.std_dev
            s_dd   = abs(seed.max_dd)
            if (abs(c_cagr - s_cagr) <= 0.2
                    and abs(c_std - s_std) <= 0.1
                    and abs(c_dd  - s_dd)  <= 0.2):
                candidates.append(candidate)
                break  # no need to test further seed funds

    # ── Step 3 & 4: sort by combined_ratio proxy, take top max_additions ─────
    candidates.sort(key=lambda f: f.combined_ratio, reverse=True)
    selected_additions = candidates[:max_additions]

    return seed_list + selected_additions


def _fund_list_to_df(funds: list) -> pd.DataFrame:
    """
    Convert a list[FundEntry] to a minimal DataFrame compatible with the
    existing _solve() / optimise_with_relaxation() interface.

    Column mapping:
      Fund Name  ← name
      Fund Type  ← fund_type
      adj_ret    ← (worst_exp_ret / 100) if set, else _first_available(cagr_5, cagr_3, cagr_1, 7.0) / 100
      adj_std    ← std_dev / 100   (FundEntry stores as %)
      adj_dd     ← max_dd / 100    (FundEntry stores as negative %, e.g. -0.75)
                   BUT the optimizer expects a negative fraction, so keep sign.
      adj_quality← combined_ratio
      History_Months ← 120 (10 years synthetic; the universe was already filtered
                        upstream by the user's min_history setting)
    """
    from models import FundEntry, _first_available
    rows = []
    for f in funds:
        # Return: prefer worst_exp_ret, fall back to best CAGR available
        if f.worst_exp_ret is not None and f.worst_exp_ret > 0:
            ret = f.worst_exp_ret / 100.0
        else:
            cagr = _first_available(f.cagr_5, f.cagr_3, f.cagr_1, default=7.0)
            ret  = cagr / 100.0

        # std_dev and max_dd stored as % in FundEntry
        std = f.std_dev / 100.0 if f.std_dev > 0 else 0.01
        dd  = f.max_dd  / 100.0 if f.max_dd  != 0 else -0.01  # negative fraction

        rows.append({
            "Fund Name":      f.name,
            "Fund Type":      f.fund_type,
            "AMC":            extract_amc(f.name),
            "adj_ret":        ret,
            "adj_std":        std,
            "adj_dd":         dd,
            "adj_quality":    f.combined_ratio,
            "History_Months": 120,   # assume adequate; filtered upstream
        })
    return pd.DataFrame(rows).reset_index(drop=True)



# ─────────────────────────────────────────────────────────────────────────────
# TWO-PASS "AIM & TRACK" OPTIMIZER
#
# Background:
#   A single-pass penalized optimizer suffers the "Binary Cliff":
#   if the penalty is too high, the optimizer holds safe assets early to
#   avoid future turnover (Conservative Drag — decades of lost compounding).
#   If the penalty is too low, the "sticky" logic does nothing.
#   There is no middle ground because the LP snaps to one corner.
#
#   The fix is to DECOUPLE asset allocation from fund selection:
#     Pass 1 (Aim)   — solve each chunk with zero turnover penalty to get
#                      the optimal equity/debt/other ratios per chunk.
#                      These ratios capture the full risk/return potential.
#     Pass 2 (Track) — step backward and pick the best FUNDS that satisfy
#                      those locked ratios while minimising turnover vs the
#                      next chunk.  Asset class transitions are mandatory
#                      (healthy); fund-swapping within a class is penalised.
# ─────────────────────────────────────────────────────────────────────────────

def run_aim_pass(
    chunks:     list,   # list[AllocationChunk]  — modified in place
    universe:   list,   # list[FundEntry]
    progress_cb = None,
    mode:       str = "fine",   # "coarse" = minimise risk, "fine" = maximise return
) -> None:
    """
    Pass 1 — AIM: Solve each chunk independently with ZERO turnover penalty
    using the existing MILP + relaxation engine.

    Writes into each chunk:
      chunk.target_weights   — optimal weights (fund_name -> float, sum=1)
      chunk._type_ratios     — {'debt': float, 'equity': float, 'other': float}
                               The mandated asset-class totals for Pass 2.

    These type ratios are derived purely from return/risk optimisation and
    capture the full growth potential of each chunk.  Pass 2 is forbidden
    from changing them.
    """
    import numpy as np

    def _emit(msg: str):
        print(msg, flush=True)
        if progress_cb:
            progress_cb(msg)

    df_universe = _fund_list_to_df(universe)
    n = len(df_universe)
    if n == 0:
        _emit("  Aim pass: empty universe — nothing to optimise.")
        return

    names     = list(df_universe["Fund Name"].values)
    ret_vals  = df_universe["adj_ret"].values
    std_vals  = df_universe["adj_std"].values
    fund_types_col = df_universe["Fund Type"].values

    for chunk_idx, chunk in enumerate(chunks):
        _emit(f"\n  [Aim] Chunk {chunk_idx + 1} (Yr {chunk.year_from}–{chunk.year_to})")

        r_target  = getattr(chunk, "min_return",   0.0685)
        s_limit   = getattr(chunk, "max_std_dev",  0.0097)
        d_limit   = getattr(chunk, "max_dd",       0.0075)
        mf_max    = getattr(chunk, "max_per_fund", 0.08)   # matches _DEFAULT_CHUNK 8%
        mf_min    = getattr(chunk, "min_per_fund", 0.02)   # matches _DEFAULT_CHUNK 2%
        mt_max    = getattr(chunk, "max_per_type", 0.24)   # matches _DEFAULT_CHUNK 24%
        ma_max    = getattr(chunk, "max_per_amc",  0.16)   # matches _DEFAULT_CHUNK 16%

        # Full MILP solve — NO turnover penalty, pure return/risk optimisation
        w, info = optimise_with_relaxation(
            df           = df_universe,
            min_return   = r_target,
            max_std_dev  = s_limit,
            max_dd       = d_limit,
            max_per_fund = mf_max,
            max_per_type = mt_max,
            min_per_fund = mf_min,
            max_fund_std = 0.0,
            max_fund_dd  = 0.0,
            mode         = mode,
            max_per_amc  = ma_max,
        )

        if w is None:
            _emit(f"    Aim solve failed — chunk {chunk_idx+1} left empty.")
            chunk.target_weights = {}
            chunk._type_ratios   = {"debt": 0.0, "equity": 0.0, "other": 0.0}
            continue

        # Threshold & normalise
        w[w < mf_min * 0.5] = 0.0
        total = w.sum()
        if total < 1e-10:
            chunk.target_weights = {}
            chunk._type_ratios   = {"debt": 0.0, "equity": 0.0, "other": 0.0}
            continue
        w /= total

        chunk.target_weights = {names[i]: float(w[i]) for i in range(n) if w[i] > 1e-5}

        # Derive mandated type ratios from the solution
        type_ratios = {"debt": 0.0, "equity": 0.0, "other": 0.0}
        for i, nm in enumerate(names):
            if w[i] > 1e-5:
                ft = str(fund_types_col[i]).lower()
                if ft in type_ratios:
                    type_ratios[ft] += float(w[i])
        chunk._type_ratios = type_ratios

        wtd_ret = float(np.dot(w, ret_vals))
        wtd_std = float(np.dot(w, std_vals))
        _emit(
            f"    → {len(chunk.target_weights)} funds | "
            f"return={wtd_ret*100:.3f}% std={wtd_std*100:.3f}% | "
            f"D={type_ratios['debt']*100:.1f}% "
            f"E={type_ratios['equity']*100:.1f}% "
            f"O={type_ratios['other']*100:.1f}%"
        )


def run_track_pass(
    chunks:      list,   # list[AllocationChunk]  — target_weights set by run_aim_pass
    universe:    list,   # list[FundEntry]
    tolerance:   dict  = None,
    progress_cb         = None,
) -> None:
    """
    Pass 2 — TRACK: Backward induction that minimises fund-level turnover
    while holding asset-class ratios FIXED at the values found in Pass 1.

    Key design principle — intra-asset stickiness only:
      The equity/debt/other weight TOTALS for each chunk are locked (hard
      equality constraints) to chunk._type_ratios set in Pass 1.
      The optimizer can only change WHICH funds fill each bucket, not HOW
      MUCH of each bucket exists.  This eliminates the "Conservative Drag"
      entirely: the optimizer cannot hold debt early to avoid a future sell.

    Objective:
        minimise  Σᵢ sqrt((wᵢ - anchorᵢ)² + ε)   [soft-abs total turnover]
              +   Σᵢ P(i) · max(0, anchorᵢ - wᵢ)  [absence penalty for new funds]

    where anchor = next chunk's weights (chunk k+1), and P(i) is scaled to
    the return spread of the universe so it is always proportionate to the
    objective scale.

    Soft tolerances on risk constraints (defaults):
        return  ±0.0025 pp   std_dev ±0.0025 pp   max_dd ±0.0050 pp
    """
    from scipy.optimize import minimize, LinearConstraint, Bounds as ScipyBounds
    import numpy as np
    from models import _first_available

    if tolerance is None:
        tolerance = {"return": 0.0025, "std_dev": 0.0025, "max_dd": 0.0050}

    # Auto-scale absence penalty to ~10× return spread of the universe
    if universe:
        _rets = []
        for _f in universe:
            if _f.worst_exp_ret is not None and _f.worst_exp_ret > 0:
                _rets.append(_f.worst_exp_ret / 100.0)
            else:
                _cagr = _first_available(_f.cagr_5, _f.cagr_3, _f.cagr_1, default=7.0)
                _rets.append(_cagr / 100.0)
        _spread = (max(_rets) - min(_rets)) if len(_rets) >= 2 else 0.05
        absence_penalty = max(0.5, 10.0 * _spread)
    else:
        absence_penalty = 2.5

    def _emit(msg: str):
        print(msg, flush=True)
        if progress_cb:
            progress_cb(msg)

    _emit(f"  [Track] absence_penalty = {absence_penalty:.4f} (10× return spread)")

    if len(chunks) < 2:
        _emit("  Track pass: only one chunk — nothing to do.")
        return

    df_universe = _fund_list_to_df(universe)
    n = len(df_universe)
    if n == 0:
        return

    ret_vals  = df_universe["adj_ret"].values
    std_vals  = df_universe["adj_std"].values
    dd_vals   = np.abs(df_universe["adj_dd"].values)
    names     = list(df_universe["Fund Name"].values)
    name_idx  = {nm: i for i, nm in enumerate(names)}
    types_col = [str(t).lower() for t in df_universe["Fund Type"].values]

    # Build type masks (boolean index arrays)
    mask_debt   = np.array([t == "debt"   for t in types_col], dtype=float)
    mask_equity = np.array([t == "equity" for t in types_col], dtype=float)
    mask_other  = np.array([t == "other"  for t in types_col], dtype=float)

    def _weights_vec(chunk) -> np.ndarray:
        v = np.zeros(n)
        for nm, w in (chunk.target_weights or {}).items():
            if nm in name_idx:
                v[name_idx[nm]] = w
        return v

    EPS = 1e-8

    # Step backward: chunk N-2 → chunk 0
    for k in range(len(chunks) - 2, -1, -1):
        chunk  = chunks[k]
        anchor = _weights_vec(chunks[k + 1])

        type_ratios = getattr(chunk, "_type_ratios",
                              {"debt": 0.5, "equity": 0.4, "other": 0.1})
        t_debt   = type_ratios.get("debt",   0.0)
        t_equity = type_ratios.get("equity", 0.0)
        t_other  = type_ratios.get("other",  0.0)

        _emit(
            f"\n  [Track] Chunk {k+1} (Yr {chunk.year_from}–{chunk.year_to}) "
            f"← anchor Chunk {k+2} | "
            f"locked D={t_debt*100:.1f}% E={t_equity*100:.1f}% O={t_other*100:.1f}%"
        )

        # Relaxed risk/return bounds (soft tolerance)
        r_target = getattr(chunk, "min_return",   0.0685) - tolerance["return"]
        s_limit  = getattr(chunk, "max_std_dev",  0.0097) + tolerance["std_dev"]
        d_limit  = getattr(chunk, "max_dd",       0.0075) + tolerance["max_dd"]
        mf_max   = getattr(chunk, "max_per_fund", 0.08)   # matches _DEFAULT_CHUNK 8%
        mf_min   = getattr(chunk, "min_per_fund", 0.02)   # matches _DEFAULT_CHUNK 2%

        # Absence penalty: penalise funds not present in the anchor
        penalty = np.where(anchor < 1e-6, absence_penalty, 0.0)

        def objective(w):
            diff     = w - anchor
            soft_abs = np.sqrt(diff ** 2 + EPS)
            absence  = penalty * np.maximum(0.0, anchor - w)
            return float(np.sum(soft_abs) + np.sum(absence))

        def jac(w):
            diff     = w - anchor
            g_abs    = diff / np.sqrt(diff ** 2 + EPS)
            g_abs2   = np.where(anchor > 1e-6, 0.0,
                                penalty * np.where(w < anchor, -1.0, 0.0))
            return g_abs + g_abs2

        # ── Constraints ──────────────────────────────────────────────────────
        # Hard constraints:
        #   [0] sum(w) = 1
        #   [1] dot(w, mask_debt)   = t_debt    ← LOCKED asset-class ratio
        #   [2] dot(w, mask_equity) = t_equity  ← LOCKED asset-class ratio
        #   [3] dot(w, mask_other)  = t_other   ← LOCKED asset-class ratio
        # Soft risk constraints:
        #   [4] dot(w, ret) >= r_target
        #   [5] dot(w, std) <= s_limit
        #   [6] dot(w, |dd|) <= d_limit

        TOL_EQ = 0.005   # ±0.5pp tolerance on type ratios (handles rounding)

        A_hard = np.vstack([
            np.ones(n),    # sum = 1
            mask_debt,     # debt total
            mask_equity,   # equity total
            mask_other,    # other total
        ])
        lb_hard = [1.0,
                   t_debt   - TOL_EQ,
                   t_equity - TOL_EQ,
                   t_other  - TOL_EQ]
        ub_hard = [1.0,
                   t_debt   + TOL_EQ,
                   t_equity + TOL_EQ,
                   t_other  + TOL_EQ]

        A_soft = np.vstack([ret_vals, std_vals, dd_vals])
        lb_soft = [r_target, -np.inf, -np.inf]
        ub_soft = [np.inf,   s_limit,  d_limit]

        con_hard = LinearConstraint(A_hard, lb_hard, ub_hard)
        con_soft = LinearConstraint(A_soft, lb_soft, ub_soft)
        bounds   = ScipyBounds(lb=np.zeros(n), ub=np.full(n, mf_max))

        # Warm start: current (baseline/aim) weights for this chunk
        w0 = _weights_vec(chunk)
        if w0.sum() < 1e-10:
            # Fall back to anchor if baseline is empty
            w0 = anchor.copy()
        if w0.sum() < 1e-10:
            w0 = np.full(n, 1.0 / n)
        else:
            w0 /= w0.sum()

        res = minimize(
            objective, w0, jac=jac,
            method="SLSQP",
            bounds=bounds,
            constraints=[con_hard, con_soft],
            options={"maxiter": 3000, "ftol": 1e-10},
        )

        if not res.success or res.x is None:
            _emit(
                f"    Track solve failed ({res.message}); "
                f"keeping Aim-pass weights."
            )
            chunk.constraint_slack_used = {
                "return": 0.0, "std_dev": 0.0, "max_dd": 0.0
            }
            continue

        w = res.x.copy()
        w[w < mf_min * 0.5] = 0.0
        total = w.sum()
        if total < 1e-10:
            _emit("    Weights collapsed — keeping Aim-pass weights.")
            continue
        w /= total

        # Record constraint slack
        orig_r = getattr(chunk, "min_return",  0.0685)
        orig_s = getattr(chunk, "max_std_dev", 0.0099)
        orig_d = getattr(chunk, "max_dd",      0.0075)
        act_r  = float(np.dot(w, ret_vals))
        act_s  = float(np.dot(w, std_vals))
        act_d  = float(np.dot(w, dd_vals))
        chunk.constraint_slack_used = {
            "return":  max(0.0, orig_r - act_r),
            "std_dev": max(0.0, act_s  - orig_s),
            "max_dd":  max(0.0, act_d  - orig_d),
        }

        chunk.target_weights = {
            names[i]: float(w[i])
            for i in range(n)
            if w[i] > 1e-5
        }

        # Diagnostics
        turnover = float(np.sum(np.abs(w - anchor)))
        # Verify type ratios were preserved
        actual_d = float(np.dot(w, mask_debt))
        actual_e = float(np.dot(w, mask_equity))
        actual_o = float(np.dot(w, mask_other))
        slack    = chunk.constraint_slack_used
        _emit(
            f"    → {len(chunk.target_weights)} funds | "
            f"turnover vs anchor = {turnover*100:.2f}pp | "
            f"D={actual_d*100:.1f}% E={actual_e*100:.1f}% O={actual_o*100:.1f}% | "
            f"slack: ret={slack['return']*100:.3f}pp "
            f"std={slack['std_dev']*100:.3f}pp "
            f"dd={slack['max_dd']*100:.3f}pp"
        )


# ─────────────────────────────────────────────────────────────────────────────
# MULTI-PORTFOLIO AIM PASS  (λ-blending)
#
# Generates multiple candidate portfolios per chunk by varying the risk/return
# tradeoff coefficient α.  Each portfolio satisfies the chunk's min_return
# constraint but occupies a different point on the efficient frontier.
#
# This feeds into the combination-scoring step which selects the globally
# best set of portfolios across all chunks to minimise rebalancing cost.
# ─────────────────────────────────────────────────────────────────────────────

def run_aim_pass_multi(
    chunks:         list,   # list[AllocationChunk] or duck-typed objects with attribute access
    universe,               # list[FundEntry] or pd.DataFrame (already formatted)
    n_portfolios:   int  = 10,
    alpha_step:     float = 0.025,
    progress_cb           = None,
    mode:           str  = "fine",
) -> list:
    """
    Pass 1 (Multi): Generate ``n_portfolios`` candidate portfolios per chunk
    using λ-blending.

    For each chunk, we solve the MILP with blend_alpha values:
        α₀ = 1.0  (pure risk minimisation)
        α₁ = 1.0 - alpha_step
        α₂ = 1.0 - 2 × alpha_step
        ...up to n_portfolios distinct solutions.

    All candidates satisfy return ≥ min_return (hard constraint).
    Candidates that produce duplicate fund selections (identical fund sets)
    are de-duplicated; the one with lower risk is kept.

    Parameters
    ----------
    chunks       : list[AllocationChunk] or duck-typed objects
    universe     : list[FundEntry] or pd.DataFrame with columns:
                   Fund Name, Fund Type, adj_ret, adj_std, adj_dd, adj_quality
    n_portfolios : target number of candidate portfolios per chunk (default 10)
    alpha_step   : step size for α (default 0.025, giving α from 1.0 down to 0.775)
    progress_cb  : optional progress callback
    mode         : base mode ("fine" or "coarse") — used as fallback if blend fails

    Returns
    -------
    list of lists: candidates[chunk_idx] = [
        {
            "weights":       dict[str, float],   # fund_name → weight (sum=1)
            "type_ratios":   dict[str, float],   # debt/equity/other → ratio
            "alpha":         float,               # the α value used
            "wtd_ret":       float,               # portfolio weighted return
            "wtd_std":       float,               # portfolio weighted std_dev
            "wtd_dd":        float,               # portfolio weighted |max_dd|
            "calmar":        float,               # portfolio Calmar ratio
        },
        ...
    ]
    """
    import numpy as np

    def _emit(msg: str):
        print(msg, flush=True)
        if progress_cb:
            progress_cb(msg)

    # Accept either list[FundEntry] or a pre-built DataFrame
    df_universe = (universe if isinstance(universe, pd.DataFrame)
                   else _fund_list_to_df(universe))
    n = len(df_universe)
    if n == 0:
        _emit("  Aim-Multi: empty universe — nothing to optimise.")
        return [[] for _ in chunks]

    names       = list(df_universe["Fund Name"].values)
    ret_vals    = df_universe["adj_ret"].values
    std_vals    = df_universe["adj_std"].values
    dd_abs_vals = np.abs(df_universe["adj_dd"].values)
    fund_types  = df_universe["Fund Type"].values

    all_candidates = []

    for chunk_idx, chunk in enumerate(chunks):
        _emit(f"\n  [Aim-Multi] Chunk {chunk_idx + 1} "
              f"(Yr {chunk.year_from}–{chunk.year_to})  "
              f"— generating up to {n_portfolios} candidates")

        r_target = getattr(chunk, "min_return",   0.0685)
        s_limit  = getattr(chunk, "max_std_dev",  0.0097)
        d_limit  = getattr(chunk, "max_dd",       0.0075)
        mf_max   = getattr(chunk, "max_per_fund", 0.08)
        mf_min   = getattr(chunk, "min_per_fund", 0.02)
        mt_max   = getattr(chunk, "max_per_type", 0.24)
        ma_max   = getattr(chunk, "max_per_amc",  0.16)

        candidates = []
        seen_fund_sets = set()   # de-duplicate by fund selection

        # Generate α values from 1.0 (pure risk-min) downward
        # Try more α values than needed to account for duplicates/failures
        max_attempts = n_portfolios * 3
        alpha_val = 1.0

        for attempt in range(max_attempts):
            if len(candidates) >= n_portfolios:
                break

            alpha_val_clamped = max(0.0, min(1.0, alpha_val))

            w, info = optimise_with_relaxation(
                df           = df_universe,
                min_return   = r_target,
                max_std_dev  = s_limit,
                max_dd       = d_limit,
                max_per_fund = mf_max,
                max_per_type = mt_max,
                min_per_fund = mf_min,
                max_fund_std = 0.0,
                max_fund_dd  = 0.0,
                mode         = mode,
                blend_alpha  = alpha_val_clamped,
                max_per_amc  = ma_max,
            )

            alpha_val -= alpha_step

            if w is None:
                _emit(f"    α={alpha_val_clamped:.3f}: infeasible — skipping")
                continue

            # Threshold & normalise
            w[w < mf_min * 0.5] = 0.0
            total = w.sum()
            if total < 1e-10:
                continue
            w /= total

            # Build weight dict
            weights_dict = {
                names[i]: float(w[i]) for i in range(n) if w[i] > 1e-5
            }

            # De-duplicate: check if the same set of funds was already found
            fund_set_key = frozenset(weights_dict.keys())
            if fund_set_key in seen_fund_sets:
                _emit(f"    α={alpha_val_clamped:.3f}: duplicate fund set — skipping")
                continue
            seen_fund_sets.add(fund_set_key)

            # Compute portfolio metrics
            wtd_ret = float(np.dot(w, ret_vals))
            wtd_std = float(np.dot(w, std_vals))
            wtd_dd  = float(np.dot(w, dd_abs_vals))
            calmar  = (wtd_ret / wtd_dd) if wtd_dd > 1e-9 else float("nan")

            # Derive type ratios
            type_ratios = {"debt": 0.0, "equity": 0.0, "other": 0.0}
            for i in range(n):
                if w[i] > 1e-5:
                    ft = str(fund_types[i]).lower()
                    if ft in type_ratios:
                        type_ratios[ft] += float(w[i])

            candidates.append({
                "weights":     weights_dict,
                "type_ratios": type_ratios,
                "alpha":       alpha_val_clamped,
                "wtd_ret":     wtd_ret,
                "wtd_std":     wtd_std,
                "wtd_dd":      wtd_dd,
                "calmar":      calmar,
            })

            _emit(
                f"    α={alpha_val_clamped:.3f}: "
                f"{len(weights_dict)} funds | "
                f"ret={wtd_ret*100:.3f}% std={wtd_std*100:.3f}% "
                f"|dd|={wtd_dd*100:.3f}% calmar={calmar:.2f} | "
                f"D={type_ratios['debt']*100:.0f}% "
                f"E={type_ratios['equity']*100:.0f}% "
                f"O={type_ratios['other']*100:.0f}%"
            )

        if not candidates:
            # Fallback: run original single-mode aim pass for this chunk
            _emit(f"    No candidates found via λ-blending — "
                  f"falling back to single-mode solve")
            w, info = optimise_with_relaxation(
                df=df_universe, min_return=r_target, max_std_dev=s_limit,
                max_dd=d_limit, max_per_fund=mf_max, max_per_type=mt_max,
                min_per_fund=mf_min, max_fund_std=0.0, max_fund_dd=0.0,
                mode=mode, max_per_amc=ma_max,
            )
            if w is not None:
                w[w < mf_min * 0.5] = 0.0
                total = w.sum()
                if total > 1e-10:
                    w /= total
                    weights_dict = {names[i]: float(w[i])
                                    for i in range(n) if w[i] > 1e-5}
                    wtd_ret = float(np.dot(w, ret_vals))
                    wtd_std = float(np.dot(w, std_vals))
                    wtd_dd  = float(np.dot(w, dd_abs_vals))
                    calmar  = (wtd_ret / wtd_dd) if wtd_dd > 1e-9 else float("nan")
                    type_ratios = {"debt": 0.0, "equity": 0.0, "other": 0.0}
                    for i in range(n):
                        if w[i] > 1e-5:
                            ft = str(fund_types[i]).lower()
                            if ft in type_ratios:
                                type_ratios[ft] += float(w[i])
                    candidates.append({
                        "weights": weights_dict, "type_ratios": type_ratios,
                        "alpha": -1.0, "wtd_ret": wtd_ret, "wtd_std": wtd_std,
                        "wtd_dd": wtd_dd, "calmar": calmar,
                    })

        _emit(f"    → {len(candidates)} unique candidate(s) for Chunk {chunk_idx+1}")
        all_candidates.append(candidates)

    return all_candidates


# ═══════════════════════════════════════════════════════════════════════════════
# FRONTIER-WALK PORTFOLIO GENERATOR
#
# Alternative to run_aim_pass_multi's α-blending approach.
#
# Instead of varying the objective blend (which causes the LP to snap to the
# same vertex repeatedly — "duplicate fund set"), this approach:
#
#   P0:  minimise(std + |dd|)  s.t. return ≥ r_target
#        → gives (std₀, dd₀) — the minimum-risk feasible portfolio
#
#   Pk:  minimise(std + |dd|)  s.t. return ≥ r_target,
#                                   std + |dd| ≥ risk_{k-1} + ε
#        → forced to explore a higher-risk region each step
#
# This generates portfolios along the efficient frontier, each with strictly
# increasing risk.  The key insight: the LP MUST change its solution because
# the previous optimum is now infeasible.
# ═══════════════════════════════════════════════════════════════════════════════

def _solve_frontier(
    df:             pd.DataFrame,
    min_return:     float,
    max_std_dev:    float,
    max_dd:         float,
    max_per_fund:   float,
    type_caps:      dict,
    min_per_fund:   float = 0.0,
    min_risk_floor: float = 0.0,   # dot(w, std + |dd|) >= this value
    time_limit:     float = 120.0,
    fund_mask:      object = None,  # optional bool array — True = eligible
    max_per_amc:    float = 1.0,    # max total allocation fraction per AMC (1.0 = no limit)
) -> tuple:
    """
    Same LP as _solve(mode='coarse') but with an additional constraint:
        dot(w, std + |dd|) >= min_risk_floor

    This forces the solver away from previously-found low-risk solutions.
    If fund_mask is provided, ineligible funds (False) have their weight
    and selection indicator forced to 0.

    Returns (weights | None, feasible: bool).
    """
    from scipy.optimize import milp, LinearConstraint, Bounds as ScipyBounds
    import numpy as np

    n = len(df)
    if n == 0:
        return None, False

    ret   = df["adj_ret"].values
    std   = df["adj_std"].values
    dd    = df["adj_dd"].values
    types = df["Fund Type"].values

    dd_abs = np.abs(dd)

    # Quick pre-feasibility
    if ret.max() < min_return - 1e-6:
        return None, False

    # ── Objective: minimise risk with quality tiebreaker (same as coarse) ──
    raw_q = df["adj_quality"].values.copy()
    q_max = raw_q.max()
    quality_norm = (raw_q / q_max) if q_max > 1e-9 else np.zeros(n)

    risk_obj = std + dd_abs
    risk_spread = risk_obj.max() - risk_obj.min() if n > 1 else 0.01
    quality_lambda = 0.10 * risk_spread

    c = np.concatenate([risk_obj - quality_lambda * quality_norm,
                        np.zeros(n)])

    integrality = np.concatenate([np.zeros(n), np.ones(n)])

    # ── Bounds — force ineligible funds to 0 ──────────────────────────────
    w_ub = np.ones(n)
    z_ub = np.ones(n)
    if fund_mask is not None:
        w_ub[~fund_mask] = 0.0
        z_ub[~fund_mask] = 0.0
    bounds = ScipyBounds(lb=np.zeros(2 * n),
                         ub=np.concatenate([w_ub, z_ub]))

    # ── Constraints ───────────────────────────────────────────────────────
    A_rows = []
    lb_vals = []
    ub_vals = []

    # C1: sum(w) = 1.0
    A_rows.append(np.concatenate([np.ones(n), np.zeros(n)]))
    lb_vals.append(1.0)
    ub_vals.append(1.0)

    # C2: dot(w, ret) >= min_return
    A_rows.append(np.concatenate([ret, np.zeros(n)]))
    lb_vals.append(min_return)
    ub_vals.append(np.inf)

    # C3: dot(w, std) <= max_std_dev
    A_rows.append(np.concatenate([std, np.zeros(n)]))
    lb_vals.append(-np.inf)
    ub_vals.append(max_std_dev)

    # C4: dot(w, |dd|) <= max_dd
    A_rows.append(np.concatenate([dd_abs, np.zeros(n)]))
    lb_vals.append(-np.inf)
    ub_vals.append(max_dd)

    # C_new: dot(w, std + |dd|) >= min_risk_floor  (frontier walk constraint)
    if min_risk_floor > 1e-9:
        A_rows.append(np.concatenate([std + dd_abs, np.zeros(n)]))
        lb_vals.append(min_risk_floor)
        ub_vals.append(np.inf)

    # C5+: per-type caps
    for ft, cap in type_caps.items():
        mask = (types == ft).astype(float)
        if mask.sum() == 0:
            continue
        A_rows.append(np.concatenate([mask, np.zeros(n)]))
        lb_vals.append(-np.inf)
        ub_vals.append(cap)

    # C8+: per-AMC concentration caps (mirrors _solve)
    if max_per_amc < 1.0 - 1e-6 and "AMC" in df.columns:
        amc_col = df["AMC"].values
        for amc in np.unique(amc_col):
            amc_mask = (amc_col == amc).astype(float)
            if amc_mask.sum() < 2:
                continue
            A_rows.append(np.concatenate([amc_mask, np.zeros(n)]))
            lb_vals.append(-np.inf)
            ub_vals.append(max_per_amc)

    # C6 & C7: Semi-continuous linking
    for i in range(n):
        row_ub = np.zeros(2 * n)
        row_ub[i] = 1.0
        row_ub[n + i] = -max_per_fund
        A_rows.append(row_ub)
        lb_vals.append(-np.inf)
        ub_vals.append(0.0)

        if min_per_fund > 1e-6:
            row_lb = np.zeros(2 * n)
            row_lb[i] = 1.0
            row_lb[n + i] = -min_per_fund
            A_rows.append(row_lb)
            lb_vals.append(0.0)
            ub_vals.append(np.inf)

    A_matrix = np.array(A_rows)
    constraints = LinearConstraint(A_matrix, lb_vals, ub_vals)

    options = {'disp': False, 'time_limit': time_limit, 'mip_rel_gap': 1e-4}

    try:
        res = milp(
            c=c, integrality=integrality, bounds=bounds,
            constraints=constraints, options=options,
        )
    except Exception as e:
        return None, False

    if not res.success:
        return None, False

    weights = res.x[:n].copy()
    weights[weights < 1e-5] = 0.0
    if weights.sum() < 1e-10:
        return None, False
    weights /= weights.sum()

    # Verify feasibility
    tol = 1e-4
    ok = (
        abs(weights.sum() - 1.0) < tol
        and np.dot(weights, ret) >= min_return - tol
        and np.dot(weights, std) <= max_std_dev + tol
        and np.dot(weights, dd_abs) <= max_dd + tol
    )
    if ok and min_per_fund > 1e-6:
        active = weights > 1e-5
        if (active & (weights < min_per_fund - tol)).any():
            # Clean up sub-floor weights
            weights[active & (weights < min_per_fund * 0.5)] = 0.0
            if weights.sum() > 1e-10:
                weights /= weights.sum()
            else:
                ok = False

    return weights if ok else None, ok


def _solve_frontier_with_relaxation(
    df:             pd.DataFrame,
    min_return:     float,
    max_std_dev:    float,
    max_dd:         float,
    max_per_fund:   float,
    max_per_type:   float,
    min_per_fund:   float,
    min_risk_floor: float = 0.0,
    fund_mask:      object = None,
    max_per_amc:    float = 1.0,
) -> tuple:
    """
    Wrapper around _solve_frontier that tries with type caps first,
    then without if infeasible.  Does NOT do the full interleaved
    relaxation ladder — the frontier walk handles diversity differently.

    Returns (weights | None, dict_info).
    """
    import numpy as np

    fund_types_all = df["Fund Type"].unique()
    type_caps = {ft: max_per_type for ft in fund_types_all}

    # Try with type caps
    w, ok = _solve_frontier(
        df, min_return, max_std_dev, max_dd, max_per_fund, type_caps,
        min_per_fund=min_per_fund, min_risk_floor=min_risk_floor,
        fund_mask=fund_mask, max_per_amc=max_per_amc,
    )
    if w is not None:
        return w, {"type_caps_active": True}

    # Try without type caps
    w, ok = _solve_frontier(
        df, min_return, max_std_dev, max_dd, max_per_fund, {},
        min_per_fund=min_per_fund, min_risk_floor=min_risk_floor,
        fund_mask=fund_mask, max_per_amc=max_per_amc,
    )
    if w is not None:
        return w, {"type_caps_active": False}

    return None, {}


def run_frontier_walk(
    chunks:         list,   # list[AllocationChunk] or duck-typed objects
    universe,               # list[FundEntry] or pd.DataFrame
    n_portfolios:   int  = 10,
    risk_step:      float = 0.0005,  # ε increment in risk floor per step
    progress_cb           = None,
    risk_ref:       str   = "portfolio",  # "portfolio" or "pct75"
) -> list:
    """
    Pass 1 (Frontier Walk): Generate ``n_portfolios`` candidate portfolios
    per chunk by progressively raising the risk floor.

    For each chunk:
        P0: minimise(std + |dd|) s.t. return ≥ target  → risk₀
        Pk: minimise(std + |dd|) s.t. return ≥ target,
                                      (std + |dd|) ≥ risk_{k-1} + ε

    Each solution is guaranteed to be different because the previous optimum
    is infeasible under the new floor constraint.

    After P0, a per-fund risk cap is computed to prevent volatile outliers
    from entering subsequent portfolios:
        "portfolio" → cap = 2.5 × P0 portfolio weighted-avg std / |dd|
        "pct75"     → cap = 1.5 × 75th percentile of P0 selected fund stds / |dd|

    Parameters
    ----------
    chunks       : list of chunk objects with attributes:
                   min_return, max_std_dev, max_dd, max_per_fund, min_per_fund,
                   max_per_type, year_from, year_to
    universe     : list[FundEntry] or pd.DataFrame
    n_portfolios : target candidates per chunk (default 10)
    risk_step    : minimum risk increment between portfolios (default 0.05%)
    progress_cb  : optional callback
    risk_ref     : risk reference method — "portfolio" or "pct75"

    Returns
    -------
    Same format as run_aim_pass_multi:
    list of lists: candidates[chunk_idx] = [{weights, type_ratios, ...}, ...]
    """
    import numpy as np

    def _emit(msg: str):
        print(msg, flush=True)
        if progress_cb:
            progress_cb(msg)

    df_universe = (universe if isinstance(universe, pd.DataFrame)
                   else _fund_list_to_df(universe))
    n = len(df_universe)
    if n == 0:
        _emit("  Frontier-Walk: empty universe — nothing to optimise.")
        return [[] for _ in chunks]

    names       = list(df_universe["Fund Name"].values)
    ret_vals    = df_universe["adj_ret"].values
    std_vals    = df_universe["adj_std"].values
    dd_abs_vals = np.abs(df_universe["adj_dd"].values)
    fund_types  = df_universe["Fund Type"].values

    all_candidates = []

    for chunk_idx, chunk in enumerate(chunks):
        _emit(f"\n  [Frontier-Walk] Chunk {chunk_idx + 1} "
              f"(Yr {chunk.year_from}–{chunk.year_to})  "
              f"— generating up to {n_portfolios} candidates")

        r_target = getattr(chunk, "min_return",   0.0685)
        s_limit  = getattr(chunk, "max_std_dev",  0.0097)
        d_limit  = getattr(chunk, "max_dd",       0.0075)
        mf_max   = getattr(chunk, "max_per_fund", 0.08)
        mf_min   = getattr(chunk, "min_per_fund", 0.02)
        mt_max   = getattr(chunk, "max_per_type", 0.24)
        ma_max   = getattr(chunk, "max_per_amc",  0.16)

        candidates = []
        risk_floor = 0.0         # P0: no floor
        prev_risk  = 0.0

        # ── Iterative P0 refinement ──────────────────────────────────────
        # Solve P0, compute risk caps, check if P0 itself violates them.
        # If so, build a fund mask excluding violators and re-solve P0.
        # Repeat until P0 is clean or infeasible.
        fund_mask_for_pk = None   # None = no filter
        std_cap = None
        dd_cap  = None
        MAX_P0_ITERS = 10
        p0_w = None     # will hold the clean P0 weights

        for p0_iter in range(MAX_P0_ITERS):
            w_try, info = _solve_frontier_with_relaxation(
                df           = df_universe,
                min_return   = r_target,
                max_std_dev  = s_limit,
                max_dd       = d_limit,
                max_per_fund = mf_max,
                max_per_type = mt_max,
                min_per_fund = mf_min,
                min_risk_floor = 0.0,
                fund_mask    = fund_mask_for_pk,
                max_per_amc  = ma_max,
            )

            if w_try is None and p0_iter == 0:
                # Unconstrained P0 failed — relax ceilings
                _emit(f"    P0 infeasible at ret≥{r_target*100:.2f}% "
                      f"std≤{s_limit*100:.3f}% dd≤{d_limit*100:.3f}%")
                s_limit_try = s_limit
                d_limit_try = d_limit
                found = False
                for relax in range(1, 20):
                    s_limit_try += 0.0005
                    d_limit_try += 0.0005
                    w_try, info = _solve_frontier_with_relaxation(
                        df_universe, r_target,
                        s_limit_try, d_limit_try,
                        mf_max, mt_max, mf_min, 0.0,
                        max_per_amc=ma_max,
                    )
                    if w_try is not None:
                        s_limit = s_limit_try
                        d_limit = d_limit_try
                        _emit(f"    P0 relaxed to std≤{s_limit*100:.3f}% "
                              f"dd≤{d_limit*100:.3f}%")
                        found = True
                        break
                if not found:
                    for relax in range(1, 10):
                        r_try = r_target - relax * 0.001
                        w_try, info = _solve_frontier_with_relaxation(
                            df_universe, r_try,
                            s_limit_try, d_limit_try,
                            mf_max, mt_max, mf_min, 0.0,
                            max_per_amc=ma_max,
                        )
                        if w_try is not None:
                            r_target = r_try
                            s_limit = s_limit_try
                            d_limit = d_limit_try
                            _emit(f"    P0 relaxed to ret≥{r_target*100:.2f}% "
                                  f"std≤{s_limit*100:.3f}% dd≤{d_limit*100:.3f}%")
                            found = True
                            break
                if not found:
                    _emit(f"    P0 completely infeasible — skipping chunk")
                    break

            if w_try is None and p0_iter > 0:
                # Constrained P0 infeasible — fall back to previous clean P0
                _emit(f"    P0 iter {p0_iter}: infeasible with tighter mask — "
                      f"keeping previous P0")
                break

            if w_try is None:
                break

            # Compute caps from this P0
            wtd_std_p0 = float(np.dot(w_try, std_vals))
            wtd_dd_p0  = float(np.dot(w_try, dd_abs_vals))

            sel_mask = w_try > 1e-5
            sel_stds = std_vals[sel_mask]
            sel_dds  = dd_abs_vals[sel_mask]

            if risk_ref == "pct75":
                p75_std = float(np.percentile(sel_stds, 75)) if sel_stds.size > 0 else 0.0
                p75_dd  = float(np.percentile(sel_dds,  75)) if sel_dds.size  > 0 else 0.0
                std_cap = 2.00 * p75_std
                dd_cap  = 10.00 * p75_dd if p75_dd > 1e-9 else 1e9
                ref_label = (f"pct75: P75 std={p75_std*100:.3f}% → "
                             f"cap={std_cap*100:.3f}%, "
                             f"P75 |dd|={p75_dd*100:.3f}% → "
                             f"cap={dd_cap*100:.3f}%")
            else:
                std_cap = 3.00 * wtd_std_p0
                dd_cap  = 10.00 * wtd_dd_p0 if wtd_dd_p0 > 1e-9 else 1e9
                ref_label = (f"portfolio: wtd_std={wtd_std_p0*100:.3f}% → "
                             f"cap={std_cap*100:.3f}%, "
                             f"wtd_|dd|={wtd_dd_p0*100:.3f}% → "
                             f"cap={dd_cap*100:.3f}%")

            # Check: does P0 itself have violators?
            p0_violators = sel_mask & (
                (std_vals > std_cap + 1e-6) |
                (dd_abs_vals > dd_cap + 1e-6)
            )
            n_violators = int(p0_violators.sum())

            if n_violators == 0:
                # P0 is clean
                p0_w = w_try
                _emit(f"    P0 risk ref ({ref_label})  [iter {p0_iter}]")
                break
            else:
                # P0 has violators — tighten mask and re-solve
                violator_names = [str(names[i]) for i in range(n)
                                  if p0_violators[i]]
                _emit(f"    P0 iter {p0_iter}: excluding "
                      f"{', '.join(violator_names)}")
                new_mask = (
                    (std_vals <= std_cap + 1e-6) &
                    (dd_abs_vals <= dd_cap + 1e-6)
                )
                if fund_mask_for_pk is not None:
                    new_mask &= fund_mask_for_pk  # only tighten
                fund_mask_for_pk = new_mask
                p0_w = w_try   # keep as fallback
                # loop back to re-solve

        if p0_w is None:
            _emit(f"    → 0 candidate(s) for Chunk {chunk_idx+1}")
            all_candidates.append([])
            continue

        # Finalise fund mask for P1+ and fine_tune
        fund_mask_for_pk = (
            (std_vals <= std_cap + 1e-6) &
            (dd_abs_vals <= dd_cap + 1e-6)
        )
        n_excluded = int((~fund_mask_for_pk).sum())
        if n_excluded > 0:
            _emit(f"    {n_excluded} fund(s) excluded from P1+ by risk cap")

        # ── Strip violating funds from P0 fallback ────────────────────────
        # If the iterative P0 fell back (constrained re-solve was infeasible),
        # p0_w may still contain funds that exceed the caps.  Instead of just
        # zeroing and renormalizing (which breaks per-fund / per-type caps),
        # do a proper MILP re-solve on the masked universe, stepping down
        # the return target in 0.05% increments until feasible.
        p0_sel = p0_w > 1e-5
        p0_violating = p0_sel & ~fund_mask_for_pk
        if p0_violating.any():
            stripped_names = [str(names[i]) for i in range(n) if p0_violating[i]]
            _emit(f"    P0 contains capped fund(s): {', '.join(stripped_names)}")
            _emit(f"    Re-solving with mask, stepping down return target...")

            r_try = r_target
            RETURN_STEP = 0.0005     # 0.05% per step
            MIN_RETURN_FLOOR = 0.04  # absolute floor: 4%
            p0_resolved = False

            while r_try >= MIN_RETURN_FLOOR:
                p0_w_retry, _ = _solve_frontier_with_relaxation(
                    df           = df_universe,
                    min_return   = r_try,
                    max_std_dev  = s_limit,
                    max_dd       = d_limit,
                    max_per_fund = mf_max,
                    max_per_type = mt_max,
                    min_per_fund = mf_min,
                    min_risk_floor = 0.0,
                    fund_mask    = fund_mask_for_pk,
                    max_per_amc  = ma_max,
                )
                if p0_w_retry is not None:
                    p0_w = p0_w_retry
                    achieved_ret = float(np.dot(p0_w, ret_vals))
                    if abs(r_try - r_target) > 1e-6:
                        _emit(f"    P0 re-solved at ret≥{r_try*100:.2f}% "
                              f"(achieved {achieved_ret*100:.3f}%, "
                              f"target was {r_target*100:.2f}%)")
                    else:
                        _emit(f"    P0 re-solved at original target "
                              f"(achieved {achieved_ret*100:.3f}%)")
                    p0_resolved = True
                    break
                r_try -= RETURN_STEP

            if not p0_resolved:
                _emit(f"    P0 infeasible even at {MIN_RETURN_FLOOR*100:.1f}% "
                      f"floor — skipping chunk")
                all_candidates.append([])
                continue

        # Store caps on chunk proxy for fine_tune
        chunk.p0_max_fund_std = std_cap
        chunk.p0_max_fund_dd  = dd_cap if dd_cap < 1e8 else None

        # ── Add P0 as first candidate ─────────────────────────────────────
        w = p0_w
        wtd_ret  = float(np.dot(w, ret_vals))
        wtd_std  = float(np.dot(w, std_vals))
        wtd_dd   = float(np.dot(w, dd_abs_vals))
        wtd_risk = wtd_std + wtd_dd
        calmar   = (wtd_ret / wtd_dd) if wtd_dd > 1e-9 else float("nan")

        weights_dict = {
            names[i]: float(w[i]) for i in range(n) if w[i] > 1e-5
        }
        type_ratios = {"debt": 0.0, "equity": 0.0, "other": 0.0}
        for i in range(n):
            if w[i] > 1e-5:
                ft = str(fund_types[i]).lower()
                if ft in type_ratios:
                    type_ratios[ft] += float(w[i])

        candidates.append({
            "weights":     weights_dict,
            "type_ratios": type_ratios,
            "alpha":       -1.0,
            "wtd_ret":     wtd_ret,
            "wtd_std":     wtd_std,
            "wtd_dd":      wtd_dd,
            "calmar":      calmar,
            "risk_floor":  0.0,
        })
        _emit(
            f"    P0: "
            f"{len(weights_dict)} funds | "
            f"ret={wtd_ret*100:.3f}% std={wtd_std*100:.3f}% "
            f"|dd|={wtd_dd*100:.3f}% risk={wtd_risk*100:.4f}% "
            f"calmar={calmar:.2f}"
        )
        prev_risk = wtd_risk
        risk_floor = prev_risk + risk_step

        # ── P1+ : frontier walk with fund mask ────────────────────────────
        consecutive_rejects = 0
        MAX_CONSECUTIVE_REJECTS = 3

        for step in range(1, n_portfolios * 3):
            if len(candidates) >= n_portfolios:
                break
            if consecutive_rejects >= MAX_CONSECUTIVE_REJECTS:
                _emit(f"    {MAX_CONSECUTIVE_REJECTS} consecutive infeasible "
                      f"— frontier exhausted for this chunk")
                break

            w, info = _solve_frontier_with_relaxation(
                df           = df_universe,
                min_return   = r_target,
                max_std_dev  = s_limit,
                max_dd       = d_limit,
                max_per_fund = mf_max,
                max_per_type = mt_max,
                min_per_fund = mf_min,
                min_risk_floor = risk_floor,
                fund_mask    = fund_mask_for_pk,
                max_per_amc  = ma_max,
            )

            if w is None:
                consecutive_rejects += 1
                risk_floor += risk_step
                continue

            consecutive_rejects = 0

            # Compute metrics
            wtd_ret  = float(np.dot(w, ret_vals))
            wtd_std  = float(np.dot(w, std_vals))
            wtd_dd   = float(np.dot(w, dd_abs_vals))
            wtd_risk = wtd_std + wtd_dd
            calmar   = (wtd_ret / wtd_dd) if wtd_dd > 1e-9 else float("nan")

            weights_dict = {
                names[i]: float(w[i]) for i in range(n) if w[i] > 1e-5
            }
            type_ratios = {"debt": 0.0, "equity": 0.0, "other": 0.0}
            for i in range(n):
                if w[i] > 1e-5:
                    ft = str(fund_types[i]).lower()
                    if ft in type_ratios:
                        type_ratios[ft] += float(w[i])

            candidates.append({
                "weights":     weights_dict,
                "type_ratios": type_ratios,
                "alpha":       -1.0,
                "wtd_ret":     wtd_ret,
                "wtd_std":     wtd_std,
                "wtd_dd":      wtd_dd,
                "calmar":      calmar,
                "risk_floor":  risk_floor,
            })

            _emit(
                f"    P{len(candidates)-1}: "
                f"{len(weights_dict)} funds | "
                f"ret={wtd_ret*100:.3f}% std={wtd_std*100:.3f}% "
                f"|dd|={wtd_dd*100:.3f}% risk={wtd_risk*100:.4f}% "
                f"calmar={calmar:.2f}"
            )

            prev_risk = wtd_risk
            risk_floor = prev_risk + risk_step

        _emit(f"    → {len(candidates)} candidate(s) for Chunk {chunk_idx+1}")
        all_candidates.append(candidates)

    return all_candidates


def score_combinations(
    chunks:         list,   # list[AllocationChunk]
    all_candidates: list,   # output from run_aim_pass_multi
    total_money:    float,
    progress_cb           = None,
    fund_quality:   dict  = None,   # fund_name → combined_ratio (adj_quality)
) -> list:
    """
    Score all cross-chunk portfolio combinations for rebalancing efficiency,
    weighted by fund quality.

    For each combination (one portfolio per chunk), the score is:

        Σ over all funds:
            allocation_across_chunks × (chunks_present / total_chunks)
                                     × chunk_duration_weight
                                     × quality_factor

    where:
        allocation_across_chunks = sum of (weight × total_money) for every
            chunk where the fund appears in the selected portfolio
        chunks_present / total_chunks = fraction of chunks containing this fund
        chunk_duration_weight = (year_to - year_from + 1) for each chunk
        quality_factor = fund's combined_ratio / max_combined_ratio
            (normalised to [0, 1]; floor of 0.05 so even the worst fund gets
             some overlap credit rather than zero)

    Higher score = more capital in high-quality, overlapping funds across
    chunk transitions = less rebalancing cost with better fund selection.

    Parameters
    ----------
    chunks         : list[AllocationChunk]  (for year ranges)
    all_candidates : output from run_aim_pass_multi
    total_money    : corpus in Rs lakhs
    progress_cb    : optional
    fund_quality   : dict mapping fund_name → adj_quality (combined_ratio).
                     If None, all funds are treated equally (backward compat).

    Returns
    -------
    list of dicts, sorted descending by score:
        {
            "combo_indices": tuple[int],   # which candidate index per chunk
            "score":         float,
            "avg_calmar":    float,        # duration-weighted average Calmar
            "fund_scores":   dict,         # fund_name → contribution to score
        }
    """
    import itertools
    import math

    def _emit(msg: str):
        print(msg, flush=True)
        if progress_cb:
            progress_cb(msg)

    n_chunks = len(chunks)
    if n_chunks == 0 or not all_candidates:
        return []

    # If any chunk has 0 candidates, we can't form any combination
    for ci, cands in enumerate(all_candidates):
        if not cands:
            _emit(f"  Combination scoring: Chunk {ci+1} has no candidates — "
                  f"cannot form combinations.")
            return []

    # ── Build quality factor lookup ───────────────────────────────────────
    # Normalise to [0, 1] with a floor of 0.05 so even the worst fund
    # contributes something (pure overlap credit doesn't vanish entirely).
    quality_factor = {}
    if fund_quality:
        max_q = max(fund_quality.values()) if fund_quality else 1.0
        if max_q > 1e-9:
            for fn, q in fund_quality.items():
                quality_factor[fn] = max(0.05, q / max_q)
        _emit(f"  Quality weighting: {len(fund_quality)} funds, "
              f"max combined_ratio={max_q:.3f}")

    # Chunk durations (years)
    durations = [
        (c.year_to - c.year_from + 1) for c in chunks
    ]

    # Build all combinations (Cartesian product of candidate indices)
    ranges = [range(len(cands)) for cands in all_candidates]
    n_combos = 1
    for r in ranges:
        n_combos *= len(r)

    _emit(f"\n  Scoring {n_combos:,} cross-chunk combination(s) "
          f"({' × '.join(str(len(c)) for c in all_candidates)})...")

    results = []

    for combo in itertools.product(*ranges):
        # combo is a tuple of indices, one per chunk
        # e.g. (2, 0, 1) means candidate 2 from chunk 1,
        #       candidate 0 from chunk 2, candidate 1 from chunk 3

        # Gather the selected portfolio for each chunk
        selected = [all_candidates[ci][pi] for ci, pi in enumerate(combo)]

        # ── Score: allocation × presence × duration × quality ─────────────
        # Step 1: For each fund, find which chunks it appears in and its
        #         allocation in each.
        fund_info: dict = {}   # fund_name → list of (chunk_idx, allocation, duration)
        for ci, portfolio in enumerate(selected):
            dur = durations[ci]
            for fund_name, weight in portfolio["weights"].items():
                alloc = weight * total_money
                if fund_name not in fund_info:
                    fund_info[fund_name] = []
                fund_info[fund_name].append((ci, alloc, dur))

        # Step 2: Compute per-fund score and total combination score
        fund_scores = {}
        total_score = 0.0
        for fund_name, appearances in fund_info.items():
            presence_ratio = len(appearances) / n_chunks
            qf = quality_factor.get(fund_name, 1.0)  # default 1.0 if no quality data
            fund_score = 0.0
            for ci, alloc, dur in appearances:
                fund_score += alloc * presence_ratio * dur * qf
            fund_scores[fund_name] = fund_score
            total_score += fund_score

        # ── Duration-weighted average Calmar ─────────────────────────────
        total_dur = sum(durations)
        avg_calmar = 0.0
        for ci, portfolio in enumerate(selected):
            cal = portfolio.get("calmar", 0.0)
            if math.isfinite(cal):
                avg_calmar += cal * durations[ci] / total_dur

        results.append({
            "combo_indices": combo,
            "score":         total_score,
            "avg_calmar":    avg_calmar,
            "fund_scores":   fund_scores,
        })

    # Sort: primary = score (descending), tiebreak = avg_calmar (descending)
    results.sort(key=lambda r: (r["score"], r["avg_calmar"]), reverse=True)

    # Log top results
    n_show = min(5, len(results))
    _emit(f"\n  Top {n_show} combination(s):")
    for i in range(n_show):
        r = results[i]
        _emit(f"    #{i+1}  indices={r['combo_indices']}  "
              f"score={r['score']:.2f}  "
              f"calmar={r['avg_calmar']:.3f}  "
              f"funds={len(r['fund_scores'])}")

    return results


def select_best_combination(
    chunks:         list,   # list[AllocationChunk]  — modified in place
    all_candidates: list,   # output from run_aim_pass_multi
    scored:         list,   # output from score_combinations
    progress_cb           = None,
) -> None:
    """
    Select the best combination and write target_weights + _type_ratios
    into each chunk.

    Selection rule:
    1. Take all combinations with the highest score.
    2. Among those, pick the one with the highest duration-weighted Calmar.

    The winning portfolio's weights and type_ratios are written into each
    chunk's target_weights and _type_ratios attributes.
    """
    def _emit(msg: str):
        print(msg, flush=True)
        if progress_cb:
            progress_cb(msg)

    if not scored:
        _emit("  select_best_combination: no scored combinations — aborting.")
        return

    # The scored list is already sorted by (score, calmar) descending
    best = scored[0]
    combo = best["combo_indices"]

    _emit(f"\n  ✓ Best combination: indices={combo}  "
          f"score={best['score']:.2f}  "
          f"calmar={best['avg_calmar']:.3f}")

    for ci, pi in enumerate(combo):
        portfolio = all_candidates[ci][pi]
        chunks[ci].target_weights = dict(portfolio["weights"])
        chunks[ci]._type_ratios   = dict(portfolio["type_ratios"])
        chunks[ci].constraint_slack_used = {
            "return": 0.0, "std_dev": 0.0, "max_dd": 0.0
        }

        _emit(
            f"    Chunk {ci+1} (Yr {chunks[ci].year_from}–{chunks[ci].year_to}): "
            f"α={portfolio['alpha']:.3f}  "
            f"{len(portfolio['weights'])} funds  "
            f"ret={portfolio['wtd_ret']*100:.3f}% "
            f"std={portfolio['wtd_std']*100:.3f}% "
            f"|dd|={portfolio['wtd_dd']*100:.3f}%  "
            f"D={portfolio['type_ratios']['debt']*100:.0f}% "
            f"E={portfolio['type_ratios']['equity']*100:.0f}% "
            f"O={portfolio['type_ratios']['other']*100:.0f}%"
        )

    # Log fund commonality summary
    all_fund_names = set()
    for ci, pi in enumerate(combo):
        all_fund_names.update(all_candidates[ci][pi]["weights"].keys())
    n_chunks = len(chunks)
    common_all = set()
    for fname in all_fund_names:
        present_in = sum(
            1 for ci, pi in enumerate(combo)
            if fname in all_candidates[ci][pi]["weights"]
        )
        if present_in == n_chunks:
            common_all.add(fname)

    _emit(f"\n  Fund commonality: "
          f"{len(common_all)} fund(s) common to ALL {n_chunks} chunks, "
          f"{len(all_fund_names)} unique fund(s) total")
    if common_all:
        for fn in sorted(common_all):
            allocs = []
            for ci, pi in enumerate(combo):
                w = all_candidates[ci][pi]["weights"].get(fn, 0)
                allocs.append(f"C{ci+1}:{w*100:.1f}%")
            _emit(f"      {fn[:55]:<55}  {' '.join(allocs)}")


# ═══════════════════════════════════════════════════════════════════════════════
# PULP COMMONALITY-OPTIMISING CANDIDATE GENERATOR
#
# Inspired by the Gemini portfolio_allocator approach.  Key ideas:
#
#   1. Uses PuLP's CBC MILP solver to MINIMISE linear weighted-avg std_dev
#      subject to a return floor, per-fund / per-type caps, and a
#      per-fund std_dev ratio constraint (fund_std ≤ multiplier × port_std).
#
#   2. Generates multiple portfolios per chunk by iteratively raising the
#      portfolio std_dev floor (forcing exploration of higher-risk regions).
#      Each successive portfolio has strictly higher std_dev, giving diversity.
#      A quadratic (RMS) std_dev check is used as a stopping condition.
#
#   3. The combination scorer targets a DISTRIBUTION of fund commonality
#      across N chunks (generalised from the 3-chunk Gemini version):
#        - Target ~60% of unique funds present in ALL chunks
#        - Target ≥20% present in exactly (N-1) chunks
#      With sum-of-std_dev as tiebreaker.
#
#   This approach produces portfolios with very high cross-chunk commonality
#   (minimising rebalancing) while also inherently minimising max_dd (strongly
#   correlated with std_dev due to the minimisation objective).
# ═══════════════════════════════════════════════════════════════════════════════

def run_pulp_commonality_walk(
    chunks:           list,       # list of duck-typed chunk objects
    universe,                     # pd.DataFrame (already load_and_filter'd)
    n_portfolios:     int  = 25,
    max_alloc_fund:   float = None,   # override per-fund cap (fraction)
    max_alloc_sub:    float = None,   # override per-type cap (fraction)
    min_alloc_fund:   float = None,   # override per-fund floor (fraction)
    max_overall_std:  float = 0.025,  # stop when quadratic std > this (fraction)
    progress_cb             = None,
) -> list:
    """
    PuLP-based candidate generator that minimises portfolio std_dev
    subject to a return floor, then iterates with increasing std_dev
    floors to explore the efficient frontier.

    Parameters
    ----------
    chunks         : list of chunk objects with .min_return, .max_per_fund, etc.
    universe       : pd.DataFrame with columns: Fund Name, Fund Type,
                     adj_ret, adj_std, adj_dd, adj_quality
    n_portfolios   : max candidate portfolios per chunk (default 25)
    max_alloc_fund : per-fund weight cap; None = use chunk.max_per_fund
    max_alloc_sub  : per-type weight cap; None = use chunk.max_per_type
    min_alloc_fund : per-fund weight floor; None = use chunk.min_per_fund
    max_overall_std: stop iterating when RMS std_dev exceeds this (default 2.5%)
    progress_cb    : optional callback for progress messages

    Returns
    -------
    list of lists, same format as run_aim_pass_multi / run_frontier_walk:
    candidates[chunk_idx] = [
        {"weights": dict, "type_ratios": dict, "alpha": float,
         "wtd_ret": float, "wtd_std": float, "wtd_dd": float,
         "calmar": float, "metrics": dict},
        ...
    ]
    """
    import pulp

    def _emit(msg: str):
        print(msg, flush=True)
        if progress_cb:
            progress_cb(msg)

    df = universe if isinstance(universe, pd.DataFrame) else _fund_list_to_df(universe)
    n = len(df)
    if n == 0:
        _emit("  PuLP-Commonality: empty universe.")
        return [[] for _ in chunks]

    # ── Pre-compute Gemini-style metrics ──────────────────────────────────
    # Expected_Return: minimum of available CAGRs (most conservative)
    # std_avg:         mean of available Std_Dev periods (not worst-case)
    # max_dd_avg:      mean of available Max_DD periods (not worst-case)
    # sortino_avg:     mean of available Sortino ratios
    #
    # This matches the Gemini/Canvas algo exactly, rather than using the
    # worst-case adj_std / adj_dd from load_and_filter.
    funds = df.index.tolist()
    _emit(f"  PuLP universe: {len(funds)} funds")  # ADD THIS

    # Expected Return = min of available CAGRs (same as Worst_Exp_Ret_%)
    expected_returns = df["adj_ret"].to_dict()

    # std_avg = mean of Std_Dev_10Y, Std_Dev_5Y, Std_Dev_3Y
    _std_cols = [c for c in ["Std_Dev_10Y", "Std_Dev_5Y", "Std_Dev_3Y"]
                 if c in df.columns]
    if _std_cols:
        _std_df = df[_std_cols].apply(pd.to_numeric, errors="coerce")
        df["_pulp_std_avg"] = _std_df.max(axis=1)  # worst-case, matches portfolio_allocator.py
    else:
        df["_pulp_std_avg"] = df["adj_std"]  # fallback
    std_avgs = df["_pulp_std_avg"].to_dict()

    # max_dd_avg = mean of Max_DD_10Y, Max_DD_5Y, Max_DD_3Y (absolute values)
    _dd_cols = [c for c in ["Max_DD_10Y", "Max_DD_5Y", "Max_DD_3Y"]
                if c in df.columns]
    if _dd_cols:
        _dd_df = df[_dd_cols].apply(pd.to_numeric, errors="coerce")
        df["_pulp_dd_avg"] = _dd_df.min(axis=1).abs().fillna(0)  # worst-case (most negative), matches portfolio_allocator.py
    else:
        df["_pulp_dd_avg"] = df["adj_dd"].abs()  # fallback
    dd_abs_avgs = df["_pulp_dd_avg"].to_dict()

    # sortino_avg = mean of available Sortino ratios (for post-solve reporting)
    _sort_cols = [c for c in ["Sortino_10Y", "Sortino_5Y", "Sortino_3Y"]
                  if c in df.columns]
    if _sort_cols:
        _sort_df = df[_sort_cols].apply(pd.to_numeric, errors="coerce")
        df["_pulp_sortino_avg"] = _sort_df.mean(axis=1).fillna(0)
    else:
        df["_pulp_sortino_avg"] = pd.Series(0.0, index=df.index)
    sortino_avgs = df["_pulp_sortino_avg"].to_dict()

    # Group indices by Fund Type for per-type constraints
    fund_type_groups = {}
    for i in funds:
        ft = str(df.loc[i, "Fund Type"])
        fund_type_groups.setdefault(ft, []).append(i)

    # Group indices by AMC for per-AMC constraints
    fund_amc_groups = {}
    if "AMC" in df.columns:
        for i in funds:
            amc = str(df.loc[i, "AMC"])
            fund_amc_groups.setdefault(amc, []).append(i)
    else:
        # Derive on the fly from Fund Name (first word, title-cased)
        for i in funds:
            name = str(df.loc[i, "Fund Name"])
            amc  = name.split()[0].title() if name.strip() else "Unknown"
            fund_amc_groups.setdefault(amc, []).append(i)

    all_candidates = []

    for chunk_idx, chunk in enumerate(chunks):
        r_target = getattr(chunk, "min_return",   0.0685)
        mf_max   = max_alloc_fund if max_alloc_fund is not None else getattr(chunk, "max_per_fund", 0.08)
        mt_max   = max_alloc_sub  if max_alloc_sub  is not None else getattr(chunk, "max_per_type", 0.24)
        mf_min   = min_alloc_fund if min_alloc_fund is not None else getattr(chunk, "min_per_fund", 0.02)
        ma_max   = getattr(chunk, "max_per_amc", 0.16)

        _emit(f"\n  [PuLP-CW] Chunk {chunk_idx + 1} "
              f"(Yr {chunk.year_from}–{chunk.year_to})  "
              f"ret≥{r_target*100:.2f}% | "
              f"fund: [{mf_min*100:.0f}%, {mf_max*100:.0f}%] | "
              f"type≤{mt_max*100:.0f}% | "
              f"amc≤{ma_max*100:.0f}% | "
              f"max_rms_std≤{max_overall_std*100:.1f}%")

        candidates = []
        previous_port_std = None
        p_idx = 1
        attempts = 0
        max_attempts = n_portfolios * 2  # safeguard

        while p_idx <= n_portfolios and attempts < max_attempts:
            attempts += 1
            portfolio_found = False
            is_dup = False

            # Try progressively relaxing the per-fund std_dev ratio constraint
            for std_multiplier in [3, 4, 5]:
                prob_name = (f"PuLP_C{chunk_idx+1}_P{p_idx}_m{std_multiplier}_a{attempts}")

                floor_str = (f", floor≥{previous_port_std*100:.4f}%"
                             if previous_port_std is not None else "")
                print(f"    P{p_idx}/{n_portfolios} [m={std_multiplier}{floor_str}] "
                      f"solving ... ", end="", flush=True)

                prob = pulp.LpProblem(prob_name, pulp.LpMinimize)

                # Decision variables
                w = pulp.LpVariable.dicts("w", funds, lowBound=0, upBound=mf_max)
                z = pulp.LpVariable.dicts("z", funds, cat="Binary")

                # Portfolio std_dev (linear proxy for objective)
                port_std = pulp.LpVariable("port_std", lowBound=0)
                prob += port_std == pulp.lpSum(
                    [w[i] * std_avgs[i] for i in funds]
                ), "Def_Port_Std"

                # OBJECTIVE: minimise portfolio std_dev
                prob += port_std, "Minimize_Volatility"

                # C1: full investment
                prob += pulp.lpSum([w[i] for i in funds]) == 1.0, "Total_Alloc"

                # C2: return floor
                prob += pulp.lpSum(
                    [w[i] * expected_returns[i] for i in funds]
                ) >= r_target, "Min_Return"

                # C3+C4: semi-continuous linking (if selected, weight in [min, max])
                for i in funds:
                    prob += w[i] >= mf_min * z[i], f"Min_Alloc_{i}"
                    prob += w[i] <= mf_max * z[i], f"Max_Alloc_{i}"

                # C5: per-type caps
                for ft, ft_funds in fund_type_groups.items():
                    if len(ft_funds) > 0:
                        prob += pulp.lpSum(
                            [w[i] for i in ft_funds]
                        ) <= mt_max, f"Type_Cap_{ft}"

                # C8: per-AMC concentration caps
                # Only add when the cap is binding (< 100%) and the AMC has
                # at least 2 funds in the universe (otherwise the per-fund
                # cap already enforces it).
                if ma_max < 1.0 - 1e-6:
                    for amc, amc_funds in fund_amc_groups.items():
                        if len(amc_funds) >= 2:
                            # Sanitise AMC name for PuLP constraint ID
                            amc_id = "".join(c if c.isalnum() else "_"
                                             for c in amc)
                            prob += pulp.lpSum(
                                [w[i] for i in amc_funds]
                            ) <= ma_max, f"AMC_Cap_{amc_id}"

                # C6: per-fund std ratio constraint
                # Each selected fund's std ≤ multiplier × portfolio std
                for i in funds:
                    prob += (
                        std_avgs[i] * z[i] <= std_multiplier * port_std
                    ), f"Std_Ratio_{i}"

                # C7: iterative floor — force higher std than previous
                if previous_port_std is not None:
                    prob += port_std >= previous_port_std + 0.0001, "Iter_Std_Floor"

                # SOLVE
                try:
                    prob.solve(pulp.PULP_CBC_CMD(
                        msg=0, gapRel=0.000001, timeLimit=120, threads=1
                    ))
                except Exception as e:
                    print(f"solver error: {e}", flush=True)
                    continue

                if pulp.LpStatus[prob.status] != "Optimal":
                    print(f"infeasible", flush=True)
                    continue

                # Extract solution
                sel_funds = [i for i in funds
                             if w[i].varValue is not None and w[i].varValue > 1e-4]
                if not sel_funds:
                    print(f"empty solution", flush=True)
                    continue

                p_weights = np.array([w[i].varValue for i in sel_funds])
                p_rets    = np.array([expected_returns[i] for i in sel_funds])
                p_stds    = np.array([std_avgs[i] for i in sel_funds])
                p_dds     = np.array([dd_abs_avgs[i] for i in sel_funds])
                p_sorts   = np.array([sortino_avgs[i] for i in sel_funds])

                calc_return  = float(np.sum(p_weights * p_rets))
                # Quadratic (RMS) std_dev assuming zero correlation — matches Gemini
                calc_std_rms = float(np.sqrt(np.sum(p_weights**2 * p_stds**2)))
                calc_std_lin = float(np.sum(p_weights * p_stds))
                calc_dd      = float(np.sum(p_weights * p_dds))
                calc_sharpe  = (calc_return / calc_std_rms) if calc_std_rms > 0 else 0
                calc_sortino = float(np.sum(p_weights * p_sorts))
                calc_calmar  = (calc_return / calc_dd) if calc_dd > 1e-9 else 0

                print(f"ret={calc_return*100:.2f}% "
                      f"std={calc_std_lin*100:.3f}% "
                      f"|dd|={calc_dd*100:.3f}% "
                      f"({len(sel_funds)} funds)", flush=True)

                # Build result for de-duplication check
                weights_dict = {}
                for i in sel_funds:
                    wt = float(w[i].varValue)
                    if wt > 1e-4:
                        weights_dict[str(df.loc[i, "Fund Name"])] = wt

                # Check for duplicates vs existing candidates
                for existing in candidates:
                    ex_names = set(existing["weights"].keys())
                    new_names = set(weights_dict.keys())
                    if ex_names == new_names:
                        max_diff = max(
                            abs(weights_dict.get(fn, 0) - existing["weights"].get(fn, 0))
                            for fn in ex_names | new_names
                        )
                        if max_diff < 0.005:
                            is_dup = True
                            break

                if is_dup:
                    # Bump floor to escape the duplicate
                    _emit(f"    ⚠ P{p_idx}: duplicate portfolio detected — "
                          f"bumping std floor to force alternatives")
                    previous_port_std = (port_std.varValue or 0) + 0.0005
                    break  # break inner multiplier loop, continue outer while

                portfolio_found = True
                break  # found for this p_idx, skip other multipliers

            # Handle iteration state
            if is_dup:
                continue  # retry with bumped constraint

            if portfolio_found:
                # Derive type ratios (map AMFI subcategory to broad type)
                type_ratios = {"debt": 0.0, "equity": 0.0, "other": 0.0}
                for fname, wt in weights_dict.items():
                    ft_row = df[df["Fund Name"] == fname]
                    if len(ft_row) > 0:
                        ft_raw = str(ft_row.iloc[0]["Fund Type"]).lower()
                        if "debt" in ft_raw:
                            type_ratios["debt"] += wt
                        elif "equity" in ft_raw:
                            type_ratios["equity"] += wt
                        elif "hybrid" in ft_raw:
                            # Hybrid: split attribution — conservative/arb → debt,
                            # aggressive → equity, others → other
                            if "arbitrage" in ft_raw or "conservative" in ft_raw:
                                type_ratios["debt"] += wt
                            elif "aggressive" in ft_raw:
                                type_ratios["equity"] += wt
                            else:
                                type_ratios["other"] += wt
                        else:
                            type_ratios["other"] += wt

                metrics = {
                    "return":   calc_return,
                    "std_dev":  calc_std_rms,   # quadratic (Gemini-style primary)
                    "std_lin":  calc_std_lin,    # linear (for MILP floor tracking)
                    "max_dd":   calc_dd,
                    "sharpe":   calc_sharpe,
                    "sortino":  calc_sortino,
                    "calmar":   calc_calmar,
                }

                candidates.append({
                    "weights":     weights_dict,
                    "type_ratios": type_ratios,
                    "alpha":       -1.0,  # not applicable for this method
                    "wtd_ret":     calc_return,
                    "wtd_std":     calc_std_lin,
                    "wtd_dd":      calc_dd,
                    "calmar":      calc_calmar,
                    "metrics":     metrics,
                })

                _emit(
                    f"    P{p_idx}: {len(weights_dict)} funds | "
                    f"ret={calc_return*100:.2f}% "
                    f"rms_std={calc_std_rms*100:.4f}% "
                    f"lin_std={calc_std_lin*100:.3f}% "
                    f"|dd|={calc_dd*100:.3f}% "
                    f"calmar={calc_calmar:.2f} | "
                    f"D={type_ratios['debt']*100:.0f}% "
                    f"E={type_ratios['equity']*100:.0f}% "
                    f"O={type_ratios['other']*100:.0f}%"
                )

                previous_port_std = port_std.varValue
                p_idx += 1

                # Stop if RMS std_dev exceeds ceiling
                if calc_std_rms > max_overall_std:
                    _emit(f"    ⚠ RMS std_dev {calc_std_rms*100:.2f}% > "
                          f"{max_overall_std*100:.1f}% ceiling — stopping.")
                    break
            else:
                _emit(f"    ✗ P{p_idx}: no feasible solution at any multiplier — stopping.")
                break

        _emit(f"    → {len(candidates)} candidate(s) for Chunk {chunk_idx+1}")
        all_candidates.append(candidates)

    return all_candidates


def find_best_commonality_combination(
    chunks:           list,       # list of chunk objects
    all_candidates:   list,       # output from run_pulp_commonality_walk
    progress_cb             = None,
) -> tuple:
    """
    Evaluate all cross-chunk portfolio combinations using a
    Target Distribution Fitness Algorithm, generalised to N chunks.

    Targets:
      - ~60% of unique funds present in ALL N chunks
      - ≥20% of unique funds present in exactly (N-1) chunks
    Tiebreaker: minimise sum of RMS std_dev across chunks.

    Parameters
    ----------
    chunks         : list of chunk objects (for metadata/logging)
    all_candidates : output from run_pulp_commonality_walk
    progress_cb    : optional callback

    Returns
    -------
    (best_combo_indices, best_combo_portfolios, penalty, sum_std, stats)
    where:
      best_combo_indices = tuple of ints (one per chunk)
      best_combo_portfolios = list of candidate dicts (one per chunk)
      penalty = distribution fitness penalty (lower = better)
      sum_std = sum of RMS std_dev across chunks
      stats = dict with commonality breakdown
    Returns (None, None, inf, inf, {}) if no valid combination exists.
    """
    import itertools

    def _emit(msg: str):
        print(msg, flush=True)
        if progress_cb:
            progress_cb(msg)

    n_chunks = len(chunks)
    if n_chunks == 0 or not all_candidates:
        return None, None, float("inf"), float("inf"), {}

    # Check all chunks have candidates
    for ci, cands in enumerate(all_candidates):
        if not cands:
            _emit(f"  Commonality scoring: Chunk {ci+1} has no candidates.")
            return None, None, float("inf"), float("inf"), {}

    # Build all combinations (Cartesian product)
    ranges = [range(len(cands)) for cands in all_candidates]
    n_combos = 1
    for r in ranges:
        n_combos *= len(r)

    _emit(f"\n  Evaluating {n_combos:,} cross-chunk combinations for "
          f"Target Distribution Match...")
    _emit(f"  Target: ~60% funds in all {n_chunks} chunks | "
          f"≥20% in exactly {max(1, n_chunks-1)} chunks")

    best_combo = None
    best_indices = None
    min_penalty = float("inf")
    min_sum_std = float("inf")
    best_stats = {}

    # Progress reporting interval — ~20 log lines for large runs,
    # suppress for small counts (< 500 combos)
    report_interval = max(500, n_combos // 20)
    evaluated = 0

    for combo in itertools.product(*ranges):
        evaluated += 1
        if evaluated % report_interval == 0 or evaluated == n_combos:
            pct = evaluated / n_combos * 100
            best_str = (f"penalty={min_penalty:.4f} Σstd={min_sum_std*100:.3f}%"
                        if best_combo else "none yet")
            print(f"    [{evaluated:,}/{n_combos:,} = {pct:.0f}%]  "
                  f"best so far: {best_str}", flush=True)

        selected = [all_candidates[ci][pi] for ci, pi in enumerate(combo)]

        # Collect fund presence counts across chunks
        fund_presence = {}
        for ci, portfolio in enumerate(selected):
            for fname in portfolio["weights"].keys():
                fund_presence[fname] = fund_presence.get(fname, 0) + 1

        total_unique = len(fund_presence)
        if total_unique == 0:
            continue

        count_all = sum(1 for c in fund_presence.values() if c == n_chunks)
        count_n_minus_1 = sum(1 for c in fund_presence.values()
                              if c == max(1, n_chunks - 1))

        ratio_all = count_all / total_unique
        ratio_n_minus_1 = count_n_minus_1 / total_unique

        # Target Distribution Fitness
        penalty_all = abs(ratio_all - 0.60)
        penalty_n_minus_1 = max(0, 0.20 - ratio_n_minus_1)
        total_penalty = penalty_all + penalty_n_minus_1

        # Tiebreaker: sum of RMS std_dev
        sum_std = sum(
            p.get("metrics", {}).get("std_dev", p.get("wtd_std", 0))
            for p in selected
        )

        if (round(total_penalty, 4) < round(min_penalty, 4) or
            (round(total_penalty, 4) == round(min_penalty, 4) and
             sum_std < min_sum_std)):
            min_penalty = total_penalty
            min_sum_std = sum_std
            best_combo = selected
            best_indices = combo
            best_stats = {
                "total_unique":     total_unique,
                "count_all":        count_all,
                "count_n_minus_1":  count_n_minus_1,
                "count_1_only":     sum(1 for c in fund_presence.values() if c == 1),
                "ratio_all":        ratio_all,
                "ratio_n_minus_1":  ratio_n_minus_1,
                "fund_presence":    dict(fund_presence),
            }

    if best_combo is None:
        _emit("  ✗ No valid combination found.")
        return None, None, float("inf"), float("inf"), {}

    _emit(f"  ✓ Best Combination found!")
    _emit(f"    Distribution Penalty: {min_penalty:.4f}")
    _emit(f"    Sum of RMS Std Devs: {min_sum_std*100:.4f}%")
    _emit(f"    Unique funds: {best_stats['total_unique']}")
    _emit(f"    In ALL {n_chunks} chunks: {best_stats['count_all']} "
          f"({best_stats['ratio_all']*100:.1f}%)")
    _emit(f"    In {max(1, n_chunks-1)} chunks: {best_stats['count_n_minus_1']} "
          f"({best_stats['ratio_n_minus_1']*100:.1f}%)")
    _emit(f"    In 1 chunk only: {best_stats['count_1_only']} "
          f"({best_stats['count_1_only']/best_stats['total_unique']*100:.1f}%)")

    # Print per-chunk summary
    for ci, portfolio in enumerate(best_combo):
        m = portfolio.get("metrics", {})
        _emit(
            f"    Chunk {ci+1} (Yr {chunks[ci].year_from}–{chunks[ci].year_to}): "
            f"{len(portfolio['weights'])} funds | "
            f"ret={portfolio['wtd_ret']*100:.2f}% "
            f"rms_std={m.get('std_dev', 0)*100:.4f}% "
            f"|dd|={portfolio['wtd_dd']*100:.3f}% "
            f"calmar={portfolio.get('calmar', 0):.2f}"
        )

    return best_indices, best_combo, min_penalty, min_sum_std, best_stats


def _apply_pulp_commonality_result(
    chunks:         list,       # list of _ChunkProxy objects — modified in place
    all_candidates: list,
    best_indices:   tuple,
    best_combo:     list,
    best_stats:     dict,
    progress_cb           = None,
) -> None:
    """
    Apply the winning PuLP commonality combination to chunk proxies,
    writing target_weights and _type_ratios, compatible with the
    downstream report() / viz pipeline.
    """
    def _emit(msg: str):
        print(msg, flush=True)
        if progress_cb:
            progress_cb(msg)

    n_chunks = len(chunks)

    for ci, portfolio in enumerate(best_combo):
        chunks[ci].target_weights = dict(portfolio["weights"])
        chunks[ci]._type_ratios   = dict(portfolio["type_ratios"])
        chunks[ci].constraint_slack_used = {
            "return": 0.0, "std_dev": 0.0, "max_dd": 0.0
        }

    # Log commonality detail
    fund_presence = best_stats.get("fund_presence", {})
    common_all = sorted(fn for fn, c in fund_presence.items() if c == n_chunks)

    _emit(f"\n  Fund commonality: "
          f"{len(common_all)} fund(s) common to ALL {n_chunks} chunks, "
          f"{len(fund_presence)} unique fund(s) total")

    if common_all:
        for fn in common_all:
            allocs = []
            for ci, portfolio in enumerate(best_combo):
                w = portfolio["weights"].get(fn, 0)
                allocs.append(f"C{ci+1}:{w*100:.1f}%")
            _emit(f"      {fn[:55]:<55}  {' '.join(allocs)}")


def _merge_chunks_strict(chunks: list, total_money: float) -> "AllocationChunk":
    """
    Merge all chunks into one virtual chunk using the strictest constraints
    across all chunks.  Used by Mode A (singular allocation).
    """
    from models import AllocationChunk
    # Union of all funds
    funds_seen = {}
    for c in chunks:
        for f in c.funds:
            k = f.name.strip().lower()
            if k not in funds_seen:
                funds_seen[k] = f
    # Strictest constraints
    min_return  = max((getattr(c, 'min_return',  0.0685) for c in chunks), default=0.0685)
    max_std_dev = min((getattr(c, 'max_std_dev', 0.0099) for c in chunks), default=0.0099)
    max_dd      = min((getattr(c, 'max_dd',      0.0075) for c in chunks), default=0.0075)
    max_per_fund= min((getattr(c, 'max_per_fund', 0.10)  for c in chunks), default=0.10)
    min_per_fund= max((getattr(c, 'min_per_fund', 0.01)  for c in chunks), default=0.01)
    # Clamp: if strictest-min exceeds strictest-max, solver would be infeasible
    min_per_fund = min(min_per_fund, max_per_fund)

    merged = AllocationChunk(year_from=1, year_to=30, funds=list(funds_seen.values()))
    merged.min_return   = min_return
    merged.max_std_dev  = max_std_dev
    merged.max_dd       = max_dd
    merged.max_per_fund = max_per_fund
    merged.min_per_fund = min_per_fund
    return merged


def optimize_sticky_portfolio(
    state,              # AppState
    all_funds: list,    # list[FundEntry] — full scored database (from get_funds_data)
    progress_cb = None,
) -> "GlidePath":
    """
    Top-level orchestrator.  Reads state.allocation_mode and dispatches to
    the appropriate strategy.

    Mode A ("singular"):
        • Build merged chunk with strictest constraints across all chunks.
        • Run baseline pass on merged chunk.
        • Apply the same target_weights to every chunk.
        • Return a flat GlidePath (same weights every year).

    Mode B ("chunked_sticky"):
        • Build expanded universe (seed + up to 50 similar funds).
        • Run baseline pass on all chunks independently.
        • Run backward-induction pass to minimise turnover.
        • Return a GlidePath built from chunk.target_weights with glide-path
          interpolation around chunk boundaries.

    In both cases the returned GlidePath is also stored in state.glide_path.
    """
    from models import AppState
    # Import glide_path builder lazily so this module doesn't hard-require
    # glide_path.py at import time (the module is in the same package).
    try:
        from glide_path import build_glide_path, build_flat_glide_path
    except ImportError:
        # glide_path.py not yet present — provide stubs so the rest works
        def build_glide_path(chunks, spread):
            from models import GlidePath
            sched = {}
            for c in chunks:
                for y in range(c.year_from, c.year_to + 1):
                    sched[y] = dict(c.target_weights)
            return GlidePath(sched)

        def build_flat_glide_path(weights):
            from models import GlidePath
            return GlidePath({y: dict(weights) for y in range(1, 31)})

    def _emit(msg: str):
        print(msg, flush=True)
        if progress_cb:
            progress_cb(msg)

    chunks = state.allocation_chunks
    if not chunks:
        _emit("optimize_sticky_portfolio: no allocation chunks defined.")
        from models import GlidePath
        state.glide_path = GlidePath({})
        return state.glide_path

    # ── Mode A ────────────────────────────────────────────────────────────────
    if state.allocation_mode == "singular":
        _emit("\n═══ Mode A: Singular Lifetime Allocation ═══")
        total = state.total_allocation()
        merged = _merge_chunks_strict(chunks, total)
        universe = build_expanded_universe([merged], all_funds)
        _emit(f"  Universe: {len(universe)} funds "
              f"(merged from {len(chunks)} chunk(s))")
        # Mode A: single-chunk, single pass — use Aim-pass only (no Track needed)
        _aim_mode = (state.allocation_params or {}).get("mode", "fine")
        run_aim_pass([merged], universe, progress_cb=progress_cb, mode=_aim_mode)

        for c in chunks:
            c.target_weights = dict(merged.target_weights)

        gp = build_flat_glide_path(merged.target_weights)
        state.glide_path = gp
        _emit("\n  Mode A complete.")
        return gp

    # ── Mode B ────────────────────────────────────────────────────────────────
    _emit("\n═══ Mode B: Chunk-by-Chunk (Multi-Portfolio Aim & Combination Scoring) ═══")

    universe = build_expanded_universe(chunks, all_funds)
    _emit(f"  Universe: {len(universe)} funds "
          f"({len(chunks)} chunk seed + up to 50 similar)")

    # ── Derive risk caps from the coarse allocation's actual achieved metrics ──
    # AllocationChunk may carry stale defaults (0.97%/0.75%) from the config.
    # Replace with actuals computed from each chunk's seed target_weights.
    df_univ = (_fund_list_to_df(universe) if not isinstance(universe, pd.DataFrame)
               else universe)
    for chunk in chunks:
        if not chunk.target_weights:
            continue
        wtd_std = wtd_dd = wtd_ret = total_w = 0.0
        for fname, w in chunk.target_weights.items():
            match = df_univ[df_univ["Fund Name"] == fname]
            if len(match) == 0:
                continue
            row = match.iloc[0]
            wtd_std += w * float(row["adj_std"])
            wtd_dd  += w * abs(float(row["adj_dd"]))
            wtd_ret += w * float(row["adj_ret"])
            total_w += w
        if total_w > 0.5:
            chunk.max_std_dev = wtd_std
            chunk.max_dd      = wtd_dd
            _emit(f"  Yr {chunk.year_from}–{chunk.year_to}: coarse actuals → "
                  f"std={wtd_std*100:.3f}% |dd|={wtd_dd*100:.3f}% "
                  f"ret={wtd_ret*100:.2f}%")

    _aim_mode = (state.allocation_params or {}).get("mode", "fine")
    n_portfolios = (state.allocation_params or {}).get("n_portfolios", 10)
    alpha_step   = (state.allocation_params or {}).get("alpha_step", 0.025)
    use_frontier = (state.allocation_params or {}).get("frontier_walk", False)

    if len(chunks) == 1:
        # Single chunk: no combination scoring needed, just use best single solve
        _emit("\n─── Single chunk: running standard Aim pass ───")
        run_aim_pass(chunks, universe, progress_cb=progress_cb, mode=_aim_mode)
    else:
        # Multi-chunk: generate candidates, score combinations, select best
        if use_frontier:
            _emit(f"\n─── Pass 1: Frontier Walk "
                  f"(up to {n_portfolios} candidates/chunk) ───")
            _emit("    Each chunk solved at progressively higher risk floors.")

            all_candidates = run_frontier_walk(
                chunks, universe,
                n_portfolios=n_portfolios,
                risk_step=0.0005,
                progress_cb=progress_cb,
                risk_ref=(state.allocation_params or {}).get("risk_ref", "portfolio"),
            )
        else:
            _emit(f"\n─── Pass 1: Multi-Portfolio Aim "
                  f"(up to {n_portfolios} candidates/chunk, "
                  f"α step={alpha_step}) ───")
            _emit("    Each chunk solved at multiple risk/return tradeoff points.")

            all_candidates = run_aim_pass_multi(
                chunks, universe,
                n_portfolios=n_portfolios,
                alpha_step=alpha_step,
                progress_cb=progress_cb,
                mode=_aim_mode,
            )

        _emit("\n─── Pass 2: Cross-Chunk Combination Scoring ───")
        _emit("    Evaluating all combinations for rebalancing efficiency.")

        total_money = state.total_allocation()

        # Build fund quality lookup from universe for quality-weighted scoring
        _df_univ = (_fund_list_to_df(universe) if not isinstance(universe, pd.DataFrame)
                    else universe)
        _fq = {str(row["Fund Name"]): float(row["adj_quality"])
               for _, row in _df_univ.iterrows()
               if pd.notna(row.get("adj_quality")) and float(row["adj_quality"]) > 0}

        scored = score_combinations(
            chunks, all_candidates, total_money,
            progress_cb=progress_cb,
            fund_quality=_fq,
        )

        if scored:
            _emit("\n─── Pass 3: Best Combination Selection ───")
            select_best_combination(
                chunks, all_candidates, scored,
                progress_cb=progress_cb,
            )
        else:
            _emit("\n  ⚠ Combination scoring returned no results — "
                  "falling back to single-mode Aim pass.")
            run_aim_pass(chunks, universe, progress_cb=progress_cb, mode=_aim_mode)

    gp = build_glide_path(chunks, state.rebalance_spread_years)
    state.glide_path = gp

    n_trans = len(gp.transition_years())
    _emit(f"\n  Mode B complete. "
          f"GlidePath: {n_trans} transition year(s) across "
          f"{len(chunks)} chunk(s).")
    return gp


def _prompt(text: str, default, cast=str):
    while True:
        raw = input(f"  {text} [{default}]: ").strip()
        if raw == "":
            return default
        try:
            return cast(raw)
        except (ValueError, TypeError):
            print("    Invalid value, please try again.")


def interactive_inputs() -> dict:
    print("\n" + "═" * 62)
    print("  FUND ALLOCATION OPTIMISER  –  Input Parameters")
    print("═" * 62)
    print("  Press Enter to accept the default (shown in brackets).\n")

    total    = _prompt("1) Total money to allocate (Rs lakhs)", 100.0, float)
    min_ret  = _prompt("2) Min avg expected return (%)",         6.85,  float)
    max_std  = _prompt("3) Max avg std deviation (%)",           0.99,  float)
    max_dd   = _prompt("4) Max avg max-drawdown (%)",            0.75,  float)
    min_hist = _prompt("5) Min fund history (years)",            5,     int)
    max_fund = _prompt("6) Max allocation to any one fund (%)",  7.0,   float)
    max_type = _prompt("7) Max allocation to any fund type (%)", 20.0,  float)
    min_fund = _prompt("8) Min allocation to any selected fund (%, 0=no min)", 1.0, float)
    max_fstd = _prompt("9) Max std_dev per individual fund (%, 0=no limit)", 1.5, float)
    max_fdd  = _prompt("10) Max |max_dd| per individual fund (%, 0=no limit)", 1.5, float)
    max_amc  = _prompt("11) Max allocation to any one AMC (%, 0=no limit)", 16.0, float)

    return dict(
        total_money  = total,
        min_return   = min_ret  / 100.0,
        max_std_dev  = max_std  / 100.0,
        max_dd       = max_dd   / 100.0,
        min_history  = min_hist,
        max_per_fund = max_fund / 100.0,
        max_per_type = max_type / 100.0,
        min_per_fund = min_fund / 100.0,
        max_fund_std = max_fstd / 100.0,
        max_fund_dd  = max_fdd  / 100.0,
        max_per_amc  = max_amc  / 100.0,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Mutual fund portfolio allocator – single or multi-chunk mode"
    )
    # ── Shared ────────────────────────────────────────────────────────────────
    parser.add_argument("--input",             default=DEFAULT_INPUT)
    parser.add_argument("--total",             type=float, default=None)

    # ── Multi-chunk mode ──────────────────────────────────────────────────────
    parser.add_argument("--chunks-file",       default=None,
                        help="JSON file with list of chunk dicts")
    parser.add_argument("--output-dir",        default=None,
                        help="Directory for per-chunk CSVs + summary")
    parser.add_argument("--commonality-bonus", type=float, default=0.002,
                        help="adj_ret bonus for already-selected funds (fraction, e.g. 0.002)")
    parser.add_argument("--mode",              default="coarse",
                        help="Optimization mode: coarse (minimise risk) or fine (maximise return)")
    parser.add_argument("--frontier-walk",     action="store_true", default=False,
                        help="Use frontier-walk candidate generation instead of α-blending")
    parser.add_argument("--pulp-commonality",  action="store_true", default=False,
                        help="Use PuLP commonality-optimising walk (minimise std_dev, "
                             "maximise cross-chunk fund overlap)")
    parser.add_argument("--pulp-max-portfolios", type=int, default=0,
                        help="Max candidate portfolios per chunk for PuLP walk "
                             "(default: 0 = auto from number of chunks)")
    parser.add_argument("--pulp-max-std",      type=float, default=2.5,
                        help="Stop PuLP walk when RMS std_dev exceeds this %% (default: 2.5)")
    parser.add_argument("--risk-ref",           default="portfolio",
                        choices=["portfolio", "pct75"],
                        help="Risk reference method for frontier walk: "
                             "'portfolio' = 2.5×P0 weighted-avg, "
                             "'pct75' = 1.5×75th percentile (default: portfolio)")

    # ── Legacy single-chunk args ──────────────────────────────────────────────
    parser.add_argument("--output",       default=DEFAULT_OUTPUT)
    parser.add_argument("--min-return",   type=float, default=None)
    parser.add_argument("--max-std-dev",  type=float, default=None)
    parser.add_argument("--max-dd",       type=float, default=None)
    parser.add_argument("--min-history",  type=int,   default=None)
    parser.add_argument("--max-per-fund", type=float, default=None)
    parser.add_argument("--max-per-type", type=float, default=None)
    parser.add_argument("--min-per-fund", type=float, default=None)
    parser.add_argument("--max-fund-std", type=float, default=None)
    parser.add_argument("--max-fund-dd",  type=float, default=None)
    parser.add_argument("--max-per-amc",  type=float, default=None,
                        help="Max total allocation to any one AMC (%%). Default 16%%.")
    args = parser.parse_args()

    if not Path(args.input).exists():
        print(f"\n  ERROR: Input file not found: {args.input}")
        sys.exit(1)

    # ═════════════════════════════════════════════════════════════════════════
    # MULTI-CHUNK MODE
    # ═════════════════════════════════════════════════════════════════════════
    if args.chunks_file:
        if not Path(args.chunks_file).exists():
            print(f"\n  ERROR: Chunks file not found: {args.chunks_file}")
            sys.exit(1)
        with open(args.chunks_file, "r", encoding="utf-8") as f:
            chunks = json.load(f)
        if not chunks:
            print("\n  ERROR: Chunks file is empty.")
            sys.exit(1)

        output_dir = args.output_dir or str(Path(args.input).parent)
        total      = args.total if args.total is not None else 100.0

        print(f"\n  Multi-chunk allocation: {len(chunks)} chunk(s)")
        print(f"  Total corpus : ₹{total:,.2f} L")
        print(f"  Output dir   : {output_dir}")
        print(f"  Commonality bonus: {args.commonality_bonus*100:.2f}%")

        results = allocate_chunks(
            input_csv         = args.input,
            chunks            = chunks,
            total_money       = total,
            output_dir        = output_dir,
            commonality_bonus = args.commonality_bonus,
            mode              = args.mode,
            frontier_walk     = args.frontier_walk,
            risk_ref          = args.risk_ref,
            pulp_commonality  = args.pulp_commonality,
            pulp_max_portfolios = args.pulp_max_portfolios,
            pulp_max_std      = args.pulp_max_std,
        )

        n_ok   = sum(1 for r in results if r.get("success"))
        n_fail = len(results) - n_ok
        print(f"\n  Done: {n_ok} chunk(s) succeeded, {n_fail} failed.")
        sys.exit(1 if n_fail else 0)

    # ═════════════════════════════════════════════════════════════════════════
    # LEGACY SINGLE-CHUNK MODE  (backward compatible)
    # ═════════════════════════════════════════════════════════════════════════
    cli_given = any(v is not None for v in [
        args.total, args.min_return, args.max_std_dev, args.max_dd,
        args.min_history, args.max_per_fund, args.max_per_type,
        args.min_per_fund, args.max_fund_std, args.max_fund_dd,
        args.max_per_amc,
    ])

    if cli_given:
        params = dict(
            total_money  = args.total        if args.total        is not None else 100.0,
            min_return   = (args.min_return  if args.min_return   is not None else 6.85) / 100,
            max_std_dev  = (args.max_std_dev if args.max_std_dev  is not None else 0.99) / 100,
            max_dd       = (args.max_dd      if args.max_dd       is not None else 0.75) / 100,
            min_history  = args.min_history  if args.min_history  is not None else 5,
            max_per_fund = (args.max_per_fund if args.max_per_fund is not None else 7.0)  / 100,
            max_per_type = (args.max_per_type if args.max_per_type is not None else 20.0) / 100,
            min_per_fund = (args.min_per_fund if args.min_per_fund is not None else 1.0)  / 100,
            max_fund_std = (args.max_fund_std if args.max_fund_std is not None else 1.5)  / 100,
            max_fund_dd  = (args.max_fund_dd  if args.max_fund_dd  is not None else 1.5)  / 100,
            max_per_amc  = (args.max_per_amc  if args.max_per_amc  is not None else 16.0) / 100,
        )
    else:
        params = interactive_inputs()

    min_hist_months = params["min_history"] * 12
    print(f"\n  Input file  : {args.input}")
    print(f"  Output file : {args.output}")
    print(f"  History filter: >= {params['min_history']} years ({min_hist_months} months)")
    df = load_and_filter(args.input, min_hist_months)

    if len(df) == 0:
        print("  ERROR: No eligible funds. Try reducing --min-history.")
        sys.exit(1)

    print(f"\n  Optimisation parameters:")
    print(f"    Corpus        : ₹{params['total_money']:,.2f} L")
    print(f"    Min return    : {params['min_return']*100:.2f}%")
    print(f"    Max std dev   : {params['max_std_dev']*100:.2f}%")
    print(f"    Max drawdown  : {params['max_dd']*100:.2f}%")
    print(f"    Min history   : {params['min_history']} yrs")
    print(f"    Max per-fund  : {params['max_per_fund']*100:.1f}%")
    print(f"    Max per-type  : {params['max_per_type']*100:.1f}%")
    print(f"    Min per-fund  : {params['min_per_fund']*100:.1f}%"
          + (" (no minimum)" if params['min_per_fund'] < 1e-6 else ""))
    fstd_s = f"{params['max_fund_std']*100:.2f}%" if params['max_fund_std'] > 1e-6 else "no limit"
    fdd_s  = f"{params['max_fund_dd']*100:.2f}%"  if params['max_fund_dd']  > 1e-6 else "no limit"
    amc_s  = f"{params['max_per_amc']*100:.1f}%"  if params['max_per_amc']  < 1.0  else "no limit"
    print(f"    Max fund std  : {fstd_s}  (per-fund eligibility filter)")
    print(f"    Max fund |dd| : {fdd_s}  (per-fund eligibility filter)")
    print(f"    Max per-AMC   : {amc_s}  (AMC concentration limit)")
    print(f"    Objective     : {'minimise risk (coarse)' if args.mode == 'coarse' else 'maximise return (fine)'}")

    original_params = {
        "min_return":   params["min_return"]   * 100,
        "max_std_dev":  params["max_std_dev"]  * 100,
        "max_dd":       params["max_dd"]       * 100,
        "max_per_fund": params["max_per_fund"] * 100,
        "max_per_type": params["max_per_type"] * 100,
        "min_per_fund": params["min_per_fund"] * 100,
        "max_fund_std": params["max_fund_std"] * 100 if params["max_fund_std"] > 1e-6 else None,
        "max_fund_dd":  params["max_fund_dd"]  * 100 if params["max_fund_dd"]  > 1e-6 else None,
        "max_per_amc":  params["max_per_amc"]  * 100 if params["max_per_amc"]  < 1.0  else None,
    }

    weights, info = optimise_with_relaxation(
        df           = df,
        min_return   = params["min_return"],
        max_std_dev  = params["max_std_dev"],
        max_dd       = params["max_dd"],
        max_per_fund = params["max_per_fund"],
        max_per_type = params["max_per_type"],
        min_per_fund = params["min_per_fund"],
        max_fund_std = params["max_fund_std"],
        max_fund_dd  = params["max_fund_dd"],
        mode         = args.mode,
        max_per_amc  = params["max_per_amc"],
    )

    if weights is None:
        print("\n  ✗  Could not find a feasible allocation even after full relaxation.")
        sys.exit(1)

    eff_max_per_type = info["final"].get("max_per_type") or 0.0
    weights, _, ft_summary = fine_tune(
        df           = df,
        weights      = weights,
        min_return   = params["min_return"],
        max_std_dev  = params["max_std_dev"],
        max_dd       = params["max_dd"],
        max_per_fund = params["max_per_fund"],
        min_per_fund = params["min_per_fund"],
        max_per_type = eff_max_per_type,
        mode         = getattr(args, "mode", "coarse"),
    )

    report(
        df              = df,
        weights         = weights,
        total_money     = params["total_money"],
        original_params = original_params,
        info            = info,
        output_path     = args.output,
        fine_tune_info  = ft_summary,
    )


if __name__ == "__main__":
    main()