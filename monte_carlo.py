# Copyright (c) 2025 Uddipan Bagchi. All rights reserved.
# See LICENSE in the project root for license information.package com.costheta.cortexa.action

"""
monte_carlo.py – Sequence-of-Returns Risk via Historical Block Bootstrap.

METHOD
------
Two simulation modes are available:

1.  HISTORICAL BLOCK BOOTSTRAP (default, ``use_bootstrap=True``)
    ─────────────────────────────────────────────────────────────
    Instead of generating synthetic random returns from a log-normal
    distribution, we resample ACTUAL historical annual return sequences from:
      * Nifty 50 equity proxy   (via Nifty 50 Index Fund NAV history)
      * Nifty Composite Debt    (embedded in get_funds_data.py)

    Why block bootstrap rather than simple i.i.d. resampling?
    Indian equity markets exhibit:
      (a) Fat tails  — crashes are more severe than log-normal predicts.
      (b) Volatility clustering — a bad year is often followed by more bad
          years (e.g., 2008 was followed by a volatile 2009).
    Block bootstrap preserves this: we draw contiguous blocks of
    ``block_length`` consecutive years so that multi-year bear/bull runs
    are kept intact as a unit.

    Data acquisition:
      * Equity: mfapi.in public API → Nifty 50 Index Fund (UTI/HDFC/Nippon)
        NAV history.  Same API used by get_funds_data.py; works wherever
        get_funds_data.py works.  Tries 6 scheme codes in order of data
        length (UTI Regular has data from ~2000).
        NAV discontinuities (splits/consolidations) are corrected using the
        same _NAV_BASE_FIXES table and auto-detect logic from get_funds_data.py.
        Falls back to AMFI portal DownloadNAVHistoryReport (same domain as
        NAVAll.txt) if mfapi.in fails for all codes.
        Fetched series saved to <script_dir>/mc_nifty50_nav.csv so subsequent
        runs skip the download (refreshed if file is >7 days old).
      * Debt: Nifty Composite Debt Index extracted directly from the embedded
        _NIFTY_DEBT_DATA table in get_funds_data.py — NO network call needed.
        Pre-2016 months stitched with synthetic 5Y bond model (same as
        get_funds_data.get_debt_benchmark_monthly).
        Fetched series saved to <script_dir>/mc_debt_index.csv for audit.

    WHY BOTH EQUITY AND DEBT SERIES?
    The portfolio is almost always a MIX of equity and debt funds.  Using only
    one historical series would misstate the simulated volatility:
      • 80% debt / 20% equity  →  ~4% annual std, not equity's ~22%.
      • 80% equity / 20% debt  →  ~18% annual std, not debt's ~2%.
    The blended portfolio return for each simulated year is:
        r_portfolio = (w_equity + w_other) * r_equity_centred
                    +  w_debt              * r_debt_centred
    where equity and debt blocks share the same block-start index, preserving
    the historically observed correlation between crashes in both markets.
    "Other" (Gold ETFs, hybrid, international) is treated as equity for
    volatility purposes — a conservative / higher-vol assumption.

    Anchoring:
    Raw historical returns are *centred* per-FY to each chunk's expected return:
        r_centred = r_historical - mean(r_history) + mu_det[fy]
    This preserves historical volatility / tail shape while keeping the
    simulation mean aligned to each chunk's return assumption.
    Per-FY return floors = mu_det[fy] − N × sigma[fy] (default N=3).

2.  LOG-NORMAL (``use_bootstrap=False``, or automatic fallback)
    ─────────────────────────────────────────────────────────────
    Parameters from return_chunks (mean) + linear allocation-weighted sigma
    (perfect-correlation upper bound = sum(w_i * sigma_i), matching the
    'Std:X.XX%' shown in View Fund Selection & Allocation).

OUTPUT (MCResults)
──────────────────
All fields from the original version are preserved for backward compatibility.
New metadata fields: method_used, n_equity_years, n_debt_years, block_length.
"""

from __future__ import annotations
import math
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import numpy as np

from models import AppState
from dataclasses import dataclass


# ─────────────────────────────────────────────────────────────────────────────
# MCResults data structure (backward-compatible: new fields have defaults)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MCResults:
    n_sims: int
    fy_labels: List[int]        # [1, 2, ..., 30]

    # Shape (30,) – percentile corpus (personal + HUF combined)
    corpus_p5:  np.ndarray
    corpus_p25: np.ndarray
    corpus_p50: np.ndarray
    corpus_p75: np.ndarray
    corpus_p95: np.ndarray
    corpus_det: np.ndarray      # deterministic base-case corpus (30,)

    # Shape (30,) – percentile net cash
    cash_p5:  np.ndarray
    cash_p25: np.ndarray
    cash_p50: np.ndarray
    cash_p75: np.ndarray
    cash_p95: np.ndarray
    cash_det: np.ndarray        # deterministic base-case net cash (30,)

    # Shape (30,) – fraction of sims ruined BY this FY
    ruin_by_fy: np.ndarray

    # Raw arrays (n_sims, 30) – kept for fan chart + worst/best paths (float32)
    corpus_raw: np.ndarray
    cash_raw:   np.ndarray

    # Summary scalars
    ruin_probability: float
    median_final_corpus: float
    p5_final_corpus: float

    # Floor and sigma arrays used in this run (30,)
    floors:   np.ndarray      # per-FY floor array
    sigmas:   np.ndarray      # per-FY sigma array

    # Legacy compatibility — derived from arrays
    @property
    def floor_fy1_10(self) -> float:
        return float(self.floors[0]) if len(self.floors) > 0 else 0.0

    @property
    def floor_fy11_30(self) -> float:
        return float(self.floors[10]) if len(self.floors) > 10 else 0.0

    @property
    def sigma_used(self) -> float:
        return float(self.sigmas.mean()) if hasattr(self.sigmas, 'mean') else 0.0

    # Marginal ruin path (best-performing sim that still went bankrupt)
    marginal_ruin_idx:     Optional[int]
    marginal_ruin_corpus:  Optional[np.ndarray]
    marginal_ruin_returns: Optional[np.ndarray]
    marginal_ruin_fy:      Optional[int]

    # Method metadata (new — backward-compatible via defaults)
    method_used:    str = "log_normal"   # "block_bootstrap" or "log_normal"
    n_equity_years: int = 0              # Nifty 50 years in historical pool
    n_debt_years:   int = 0              # Debt Index years in historical pool
    block_length:   int = 3              # bootstrap block length in years


# ─────────────────────────────────────────────────────────────────────────────
# Cache file paths  (same directory as this script)
# ─────────────────────────────────────────────────────────────────────────────

_SCRIPT_DIR       = Path(__file__).resolve().parent
_EQUITY_CACHE_CSV = _SCRIPT_DIR / "mc_nifty50_nav.csv"
_DEBT_CACHE_CSV   = _SCRIPT_DIR / "mc_debt_index.csv"
_CACHE_MAX_AGE_DAYS = 7          # re-download if cached file is older than this


def _cache_is_fresh(path: Path) -> bool:
    """Return True if the CSV cache exists and is < _CACHE_MAX_AGE_DAYS old."""
    if not path.exists():
        return False
    age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    return age < timedelta(days=_CACHE_MAX_AGE_DAYS)


# ─────────────────────────────────────────────────────────────────────────────
# NAV split / consolidation correction
# Mirrors the _NAV_BASE_FIXES table + auto-detect logic in get_funds_data.py.
# ─────────────────────────────────────────────────────────────────────────────

# Hard-coded known discontinuities for Nifty 50 index-related funds.
# Format: scheme_code_str → (cutoff_date_str, multiplier_for_navs_BEFORE_cutoff)
# Multiplier > 1  → pre-split NAVs scaled UP   (consolidation: low → high NAV)
# Multiplier < 1  → pre-split NAVs scaled DOWN  (split: high → low NAV)
_NAV_BASE_FIXES: dict[str, tuple[str, float]] = {
    # ── Nifty 50 ETF face-value splits (high → low NAV) ──────────────────
    "112351": ("2017-07-28", 1 / 10),    # Kotak Nifty 50 ETF
    "135320": ("2023-09-26", 1 / 10),    # UTI Nifty 50 ETF
    "135853": ("2021-02-22", 1 / 10),    # HDFC Nifty 50 ETF
    "115512": ("2021-11-29", 1 / 10),    # ABSL Nifty 50 ETF
    # ── Overnight / Liquid fund consolidations (low → high NAV) ──────────
    "120785": ("2018-05-03", 100),       # UTI Overnight Fund (wrong code guard)
    "140196": ("2017-07-02", 100),       # Edelweiss Liquid Fund
    "145536": ("2022-08-17",  10),       # ICICI Prudential Overnight Fund
}


def _apply_nav_fixes(df: "pd.DataFrame", code_str: str) -> "pd.DataFrame":
    """
    Apply NAV split/consolidation corrections to a DataFrame with columns
    ['date', 'nav'] sorted ascending by date.

    Step 1: Apply hard-coded fix from _NAV_BASE_FIXES if the code is known.
    Step 2: If no hard-coded fix, run auto-detect: look for a single-day NAV
            change > 60% in either direction (same logic as get_funds_data.py).
    Returns the corrected DataFrame (copy, original untouched).
    """
    import pandas as pd

    df = df.copy()

    if code_str in _NAV_BASE_FIXES:
        cutoff_str, multiplier = _NAV_BASE_FIXES[code_str]
        cutoff = pd.Timestamp(cutoff_str)
        mask = df["date"] < cutoff
        df.loc[mask, "nav"] = df.loc[mask, "nav"] * multiplier
        return df

    # Auto-detect: scan for a single large NAV jump (one fix per fund)
    navs  = df["nav"].values
    dates = df["date"].values
    for i in range(1, len(navs)):
        if navs[i - 1] <= 0:
            continue
        ratio = navs[i] / navs[i - 1]
        if ratio < 0.40:                        # Split: NAV fell > 60%
            if ratio < 0.02:
                multiplier = 1 / 100
            elif ratio < 0.15:
                multiplier = 1 / 10
            else:
                multiplier = ratio
            mask = df["date"] < dates[i]
            df.loc[mask, "nav"] = df.loc[mask, "nav"] * multiplier
            break
        elif ratio > 2.5:                       # Consolidation: NAV jumped
            if ratio > 50:
                multiplier = 100
            elif ratio > 5:
                multiplier = 10
            else:
                multiplier = ratio
            mask = df["date"] < dates[i]
            df.loc[mask, "nav"] = df.loc[mask, "nav"] * multiplier
            break

    return df


# ─────────────────────────────────────────────────────────────────────────────
# Nifty 50 equity proxy  –  mfapi.in → AMFI portal fallback, with CSV cache
# ─────────────────────────────────────────────────────────────────────────────

# Scheme codes tried in order; UTI Regular (101035) has data from ~2000 and
# is the longest available Nifty 50 index fund series.
_NIFTY50_SCHEME_CODES = [
    ("101035", "UTI Nifty 50 Index Fund Regular Growth"),        # from ~2000
    ("100644", "HDFC Index Fund Nifty 50 Plan Regular Growth"),  # from ~2002
    ("120716", "UTI Nifty 50 Index Fund Direct Growth"),         # from 2013
    ("120468", "HDFC Index Fund Nifty 50 Plan Direct Growth"),   # from 2013
    ("120594", "Nippon India Index Fund Nifty 50 Direct"),       # from 2013
    ("122639", "SBI Nifty 50 Index Fund Direct Growth"),         # from 2013
]
_MIN_EQUITY_FY = 15   # minimum complete April–March FYs required


def _nav_df_from_mfapi(scheme_code: str) -> "Optional[pd.DataFrame]":
    """
    Fetch daily NAV history from mfapi.in.
    Returns DataFrame ['date', 'nav'] sorted ascending, or None on failure.
    """
    try:
        import requests
        import pandas as pd
        resp = requests.get(
            f"https://api.mfapi.in/mf/{scheme_code}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
        )
        resp.raise_for_status()
        records = resp.json().get("data", [])
        if not records:
            return None
        df = pd.DataFrame(records)
        df["date"] = pd.to_datetime(df["date"], dayfirst=True, errors="coerce")
        df["nav"]  = pd.to_numeric(df["nav"], errors="coerce")
        df = df.dropna(subset=["date", "nav"]).sort_values("date").reset_index(drop=True)
        return df if not df.empty else None
    except Exception:
        return None


def _nav_df_from_amfi_portal(scheme_code: str) -> "Optional[pd.DataFrame]":
    """
    Fetch daily NAV history from the AMFI portal DownloadNAVHistoryReport.
    Same domain as NAVAll.txt (portal.amfiindia.com) — proven reachable.
    Returns DataFrame ['date', 'nav'] sorted ascending, or None on failure.
    """
    try:
        import requests
        import pandas as pd
        url = (
            "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"
            f"?frmdt=01-Apr-2000&todt=31-Mar-2030&Sch={scheme_code}"
        )
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=30,
        )
        resp.raise_for_status()
        rows = []
        for line in resp.text.splitlines():
            parts = line.split(";")
            if len(parts) < 6:
                continue
            try:
                nav_val  = float(parts[2].strip())
                date_val = pd.to_datetime(parts[5].strip(), dayfirst=True,
                                          errors="coerce")
                if date_val is not pd.NaT and nav_val > 0:
                    rows.append({"date": date_val, "nav": nav_val})
            except (ValueError, IndexError):
                continue
        if not rows:
            return None
        df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
        return df if not df.empty else None
    except Exception:
        return None


def _nav_df_to_fy_returns(df: "pd.DataFrame") -> "Optional[np.ndarray]":
    """
    Convert a daily NAV DataFrame (['date', 'nav'], sorted ascending) to
    April-to-March (Indian FY) annual return fractions.
    Returns None if fewer than _MIN_EQUITY_FY complete FYs are found.
    """
    import pandas as pd
    eom = df.set_index("date")["nav"].resample("ME").last().dropna()
    annual = []
    fy_starts = sorted(set(
        d.year if d.month >= 4 else d.year - 1 for d in eom.index))
    for fy in fy_starts:
        apr = eom[(eom.index.year == fy)     & (eom.index.month == 4)]
        mar = eom[(eom.index.year == fy + 1) & (eom.index.month == 3)]
        if apr.empty or mar.empty:
            continue
        r = float(mar.iloc[-1] / apr.iloc[0]) - 1.0
        if -0.90 <= r <= 4.0:   # sanity bounds
            annual.append(r)
    return np.array(annual) if len(annual) >= _MIN_EQUITY_FY else None


def _load_equity_cache() -> "Optional[pd.DataFrame]":
    """Load the cached NAV DataFrame from CSV (mc_nifty50_nav.csv).
    Returns None if file is missing or stale (> _CACHE_MAX_AGE_DAYS old)."""
    if not _cache_is_fresh(_EQUITY_CACHE_CSV):
        return None
    try:
        import pandas as pd
        df = pd.read_csv(_EQUITY_CACHE_CSV, parse_dates=["date"])
        df["nav"] = pd.to_numeric(df["nav"], errors="coerce")
        df = df.dropna(subset=["date", "nav"]).sort_values("date").reset_index(drop=True)
        return df if not df.empty else None
    except Exception:
        return None


def _save_equity_cache(df: "pd.DataFrame") -> None:
    """Save equity NAV DataFrame to mc_nifty50_nav.csv.
    Columns saved: date (YYYY-MM-DD), nav.  Failure is non-fatal."""
    try:
        df[["date", "nav"]].to_csv(
            _EQUITY_CACHE_CSV, index=False, date_format="%Y-%m-%d")
    except Exception:
        pass


def _fetch_nifty50_annual_returns() -> "Optional[np.ndarray]":
    """
    Return Nifty 50 April-to-March annual returns as a float array.

    Sources tried in order:
      1. Local CSV cache (mc_nifty50_nav.csv) — skips download for 7 days.
      2. mfapi.in API (same as get_funds_data.py) for each of 6 scheme codes.
      3. AMFI portal DownloadNAVHistoryReport (same domain as NAVAll.txt).
    In steps 2-3, NAV split/consolidation corrections are applied before
    computing annual returns.  The best result (most FY years) is cached.

    Returns None only if all sources fail for all scheme codes.
    """
    import pandas as pd

    # ── 1. Try local cache ────────────────────────────────────────────────────
    cached_df = _load_equity_cache()
    if cached_df is not None:
        result = _nav_df_to_fy_returns(cached_df)
        if result is not None:
            return result

    # ── 2 & 3. Try each scheme code via mfapi then AMFI portal ───────────────
    best_df: Optional[pd.DataFrame] = None
    best_n_years = 0

    for code, _name in _NIFTY50_SCHEME_CODES:
        for fetch_fn in (_nav_df_from_mfapi, _nav_df_from_amfi_portal):
            df = fetch_fn(code)
            if df is None:
                continue
            df = _apply_nav_fixes(df, code)
            result = _nav_df_to_fy_returns(df)
            if result is not None and len(result) > best_n_years:
                best_n_years = len(result)
                best_df = df
                if best_n_years >= 20:   # 20+ years is plenty; stop searching
                    break
        if best_n_years >= 20:
            break

    if best_df is not None:
        _save_equity_cache(best_df)          # write mc_nifty50_nav.csv
        return _nav_df_to_fy_returns(best_df)

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Nifty Composite Debt Index — extracted from get_funds_data.py (zero network)
# ─────────────────────────────────────────────────────────────────────────────

def _load_debt_cache() -> "Optional[np.ndarray]":
    """Load cached debt annual returns from mc_debt_index.csv.
    Returns None if missing or stale."""
    if not _cache_is_fresh(_DEBT_CACHE_CSV):
        return None
    try:
        import pandas as pd
        df = pd.read_csv(_DEBT_CACHE_CSV)
        col = "annual_return_fraction"
        if col not in df.columns:
            return None
        arr = pd.to_numeric(df[col], errors="coerce").dropna().values
        return arr if len(arr) >= 10 else None
    except Exception:
        return None


def _save_debt_cache(annual_returns: np.ndarray,
                     fy_labels: "list[str]") -> None:
    """Save debt annual returns to mc_debt_index.csv.
    Columns: fy (e.g. 'FY2000-2001'), annual_return_fraction."""
    try:
        import pandas as pd
        pd.DataFrame({
            "fy": fy_labels,
            "annual_return_fraction": annual_returns,
        }).to_csv(_DEBT_CACHE_CSV, index=False)
    except Exception:
        pass


def _fetch_debt_index_annual_returns() -> "Optional[np.ndarray]":
    """
    Return Nifty Composite Debt Index April-to-March annual returns.

    Source: embedded _NIFTY_DEBT_DATA in get_funds_data.py (weekly Nifty
    Composite Debt Index prices, Apr-2016 → present) stitched with a
    synthetic 5Y bond index for pre-2016 months — the same series used
    for debt fund Alpha/Beta calculations.

    This requires NO network call.  get_funds_data.py is imported from the
    same directory as this script.
    Results are saved to mc_debt_index.csv for transparency.
    Returns None if get_funds_data.py is not importable or data is too short.
    """
    # ── 1. Try local cache ────────────────────────────────────────────────────
    cached = _load_debt_cache()
    if cached is not None:
        return cached

    # ── 2. Import get_funds_data and call get_debt_benchmark_monthly ──────────
    try:
        import sys
        import importlib
        import pandas as pd

        _here = str(_SCRIPT_DIR)
        if _here not in sys.path:
            sys.path.insert(0, _here)

        gfd = importlib.import_module("get_funds_data")

        # Returns monthly return fractions from 2000-01 onward
        monthly: pd.Series = gfd.get_debt_benchmark_monthly(
            pd.Timestamp("2000-01-01"))
        if monthly is None or len(monthly) < 12:
            return None

        # Reconstruct cumulative price index, then compute April–March FY returns
        monthly.index = pd.DatetimeIndex(monthly.index)
        prices = (1.0 + monthly).cumprod()

        annual_rets: list[float] = []
        fy_labels:   list[str]   = []
        fy_starts = sorted(set(
            d.year if d.month >= 4 else d.year - 1 for d in prices.index))
        for fy in fy_starts:
            apr = prices[(prices.index.year == fy)     & (prices.index.month == 4)]
            mar = prices[(prices.index.year == fy + 1) & (prices.index.month == 3)]
            if apr.empty or mar.empty:
                continue
            r = float(mar.iloc[-1] / apr.iloc[0]) - 1.0
            if -0.50 <= r <= 0.50:   # sanity bounds for debt
                annual_rets.append(r)
                fy_labels.append(f"FY{fy}-{fy + 1}")

        if len(annual_rets) < 10:
            return None

        arr = np.array(annual_rets)
        _save_debt_cache(arr, fy_labels)     # write mc_debt_index.csv
        return arr

    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Block bootstrap return generator
# ─────────────────────────────────────────────────────────────────────────────

def _block_bootstrap_returns(
    eq_hist:       np.ndarray,   # equity annual returns, shape (n_e,)
    dbt_hist:      np.ndarray,   # debt annual returns,   shape (n_d,)
    w_equity:      float,        # equity weight (0–1)
    w_debt:        float,        # debt weight   (0–1)
    w_other:       float,        # other weight  (0–1) — treated as equity proxy
    mu_det:        np.ndarray,   # per-FY deterministic expected returns (30,)
    n_sims:        int,
    block_length:  int,
    floors:        np.ndarray,   # per-FY floor array (30,)
    rng:           np.random.Generator,
) -> np.ndarray:
    """
    Generate (n_sims, 30) portfolio returns via FULLY VECTORISED block bootstrap.

    No Python loop over simulations — all n_sims paths built in two NumPy
    fancy-index gather operations.

    Algorithm:
      1. Draw ALL (n_sims × n_blocks) block-start indices at once.
         Equity and debt share the SAME indices → preserves observed crisis
         correlation (both asset classes crash together).
      2. Build integer index arrays eq_idx / dbt_idx of shape (n_sims, 30)
         in a tiny inner loop over n_blocks (≤ 10 iterations regardless of
         n_sims), clamping each start to avoid overrunning either history array.
      3. Gather all returns in two vectorised fancy-index ops:
           raw_eq  = eq_hist[eq_idx]    # (n_sims, 30) — C-level loop
           raw_dbt = dbt_hist[dbt_idx]
      4. Centre per-FY (using mu_det), blend, and floor — all element-wise array ops.

    ~13× faster than the equivalent Python loop (e.g. 22 ms vs 340 ms for
    10 000 simulations).
    """
    N_YEARS  = 30
    n_e, n_d = len(eq_hist), len(dbt_hist)

    # Clamp block_length to the shortest available history so that
    # np.random.integers never receives a non-positive upper bound.
    effective_block = min(block_length, n_e, n_d)
    if effective_block < 1:
        effective_block = 1

    n_blocks = math.ceil(N_YEARS / effective_block)
    eq_mean  = float(eq_hist.mean())
    dbt_mean = float(dbt_hist.mean())

    # Shared upper bound so the same block-start index fits both series
    max_start = min(max(0, n_e - effective_block), max(0, n_d - effective_block))

    # ── Step 1: draw all block starts at once — shape (n_sims, n_blocks) ─────
    starts = rng.integers(0, max_start + 1, size=(n_sims, n_blocks))

    # ── Step 2: build per-position index arrays ───────────────────────────────
    # eq_idx[sim, year] = index into eq_hist for that sim at that year position
    eq_idx  = np.empty((n_sims, N_YEARS), dtype=np.intp)
    dbt_idx = np.empty((n_sims, N_YEARS), dtype=np.intp)

    pos = 0
    for b in range(n_blocks):
        if pos >= N_YEARS:
            break
        take = min(effective_block, N_YEARS - pos)
        # Clamp so slice never overruns either history array
        se = np.minimum(starts[:, b], n_e - take)   # shape (n_sims,)
        sd = np.minimum(starts[:, b], n_d - take)
        for off in range(take):
            eq_idx [:, pos + off] = se + off
            dbt_idx[:, pos + off] = sd + off
        pos += take

    # ── Step 3: gather all returns in two vectorised ops ─────────────────────
    raw_eq  = eq_hist [eq_idx]    # (n_sims, 30) — one C-level gather
    raw_dbt = dbt_hist[dbt_idx]   # (n_sims, 30)

    # ── Step 4: centre per-FY, blend, apply per-FY floors ────────────────────
    # Centre each bootstrapped year around its chunk-specific mu_det rather
    # than a single global mean.  mu_det shape (30,) broadcasts over sims.
    port = ((w_equity + w_other) * (raw_eq  - eq_mean  + mu_det[np.newaxis, :]) +
             w_debt              * (raw_dbt  - dbt_mean + mu_det[np.newaxis, :]))
    port = np.maximum(port, floors[np.newaxis, :])

    return port


# ─────────────────────────────────────────────────────────────────────────────
# Log-normal helpers (retained as fallback)
# ─────────────────────────────────────────────────────────────────────────────

def _portfolio_sigma(state: AppState) -> float:
    """
    Allocation-weighted annual portfolio sigma from fund std_devs.

    Uses the linear allocation-weighted average (perfect-correlation upper bound):
        sigma = sum(w_i * sigma_i)

    This is the HIGHEST valid portfolio std dev estimate — it assumes all funds
    move in perfect lockstep.  It matches the 'Std:X.XX%' value shown in the
    View Fund Selection & Allocation dialog.

    Formula ordering for reference:
        rms_correct = sqrt(sum(w_i² * sigma_i²))   ← zero-correlation lower bound
        lin         = sum(w_i * sigma_i)            ← perfect-correlation upper bound  [used here]
        rms_old     = sqrt(sum(w_i * sigma_i²))     ← NOT a valid portfolio formula;
                                                       by Jensen: sqrt(E[σ²]) ≥ E[σ],
                                                       so rms_old ≥ lin (overestimates)
    """
    active = [f for f in state.funds if f.allocation > 0]
    total  = sum(f.allocation for f in active)
    if total == 0 or not active:
        return 0.0097
    return sum((f.allocation / total) * (f.std_dev / 100.0) for f in active)


def _per_chunk_sigmas(state: AppState) -> np.ndarray:
    """
    Return a (30,) array of annualised portfolio sigma, one value per FY.

    Each sigma is the linear allocation-weighted average of fund std_devs
    (perfect-correlation upper bound), matching the 'Std:X.XX%' shown in
    the View Fund Selection & Allocation dialog.

    If allocation_chunks are defined, each chunk's funds produce a separate
    sigma for the years that chunk covers.  Uses optimized_sigma() which
    respects target_weights (post-optimizer) when available.
    Falls back to the flat portfolio sigma when no chunks exist.
    """
    flat_sigma = _portfolio_sigma(state)
    sigmas = np.full(30, flat_sigma)

    if not state.allocation_chunks:
        return sigmas

    for ac in state.allocation_chunks:
        chunk_sigma = ac.optimized_sigma()
        yr_from = max(1, ac.year_from)
        yr_to   = min(30, ac.year_to)
        for fy in range(yr_from, yr_to + 1):
            sigmas[fy - 1] = chunk_sigma

    return sigmas


def _lognormal_returns(
    mu_det:        np.ndarray,
    sigma:         np.ndarray,     # (30,) per-FY sigma
    n_sims:        int,
    floors:        np.ndarray,     # (30,) per-FY floor
    rng:           np.random.Generator,
) -> np.ndarray:
    mu_ln = np.log(1.0 + mu_det) - 0.5 * sigma ** 2
    z     = rng.standard_normal((n_sims, 30))
    r     = np.exp(mu_ln[np.newaxis, :] + sigma[np.newaxis, :] * z) - 1.0
    r     = np.maximum(r, floors[np.newaxis, :])
    return r


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_monte_carlo(
    state: AppState,
    n_sims:         int   = 2000,
    sigma_override: Optional[float] = None,
    floor_multiplier: float = 3.0,
    seed:           int   = 42,
    use_bootstrap:  bool  = True,
    block_length:   int   = 3,
) -> MCResults:
    """
    Run Monte Carlo sequence-of-returns simulation.

    Parameters
    ----------
    floor_multiplier : Number of sigmas below the mean to set the per-FY
        return floor.  Default 3.0 → floor = mu − 3σ per chunk.
    use_bootstrap : If True (default), uses Historical Block Bootstrap with
        real Nifty 50 equity and Nifty Composite Debt Index history.
        Falls back to log-normal if data is unavailable.
    block_length  : Consecutive years per bootstrap block (default 3).
        Larger values = stronger volatility clustering.
    """
    rng    = np.random.default_rng(seed)
    mu_det = np.array([state.get_return_rate(fy) for fy in range(1, 31)])

    # ── Per-chunk sigma and floors ────────────────────────────────────────────
    if sigma_override is not None:
        sigmas = np.full(30, sigma_override)
    else:
        sigmas = _per_chunk_sigmas(state)

    floors = mu_det - floor_multiplier * sigmas

    # ── Select simulation method ──────────────────────────────────────────────
    method_used  = "log_normal"
    n_eq_yrs = n_dbt_yrs = 0

    if use_bootstrap:
        eq_hist  = _fetch_nifty50_annual_returns()
        dbt_hist = _fetch_debt_index_annual_returns()

        if eq_hist is not None and dbt_hist is not None:
            n_eq_yrs  = len(eq_hist)
            n_dbt_yrs = len(dbt_hist)

            total = state.total_allocation()
            if total > 0:
                w_eq  = state.total_equity_allocation() / total
                w_dbt = state.total_debt_allocation()   / total
                w_oth = state.total_other_allocation()  / total
            else:
                w_eq, w_dbt, w_oth = 0.2, 0.8, 0.0

            r_sim       = _block_bootstrap_returns(
                eq_hist=eq_hist, dbt_hist=dbt_hist,
                w_equity=w_eq, w_debt=w_dbt, w_other=w_oth,
                mu_det=mu_det, n_sims=n_sims, block_length=block_length,
                floors=floors, rng=rng,
            )
            method_used = "block_bootstrap"

        else:
            if eq_hist is None and dbt_hist is None:
                _what = ("both Nifty 50 equity NAV (mfapi.in / AMFI portal) "
                         "and Debt Index (get_funds_data) unavailable")
            elif eq_hist is None:
                _what = ("Nifty 50 equity NAV unavailable — tried mfapi.in "
                         "and AMFI portal for all scheme codes")
            else:
                _what = ("Debt Index unavailable — get_funds_data.py not "
                         "importable or data too short")
            warnings.warn(
                f"Block bootstrap: {_what}. Falling back to log-normal.",
                RuntimeWarning, stacklevel=2,
            )
            r_sim = _lognormal_returns(mu_det, sigmas, n_sims, floors, rng)
    else:
        r_sim = _lognormal_returns(mu_det, sigmas, n_sims, floors, rng)

    # ── Deterministic base from engine ───────────────────────────────────────
    from engine import Engine
    _, det_y, _, _ = Engine(state).run()
    det_corpus   = np.array([r.corpus_debt_personal + r.corpus_equity_personal +
                              r.corpus_other_personal for r in det_y])
    det_huf_corp = np.array([r.corpus_debt_huf + r.corpus_equity_huf +
                              r.corpus_other_huf       for r in det_y])
    det_tax_pers = np.array([r.tax_personal   for r in det_y])
    det_tax_sav  = np.array([r.tax_saved      for r in det_y])
    det_net_cash = np.array([r.net_cash_total for r in det_y])
    det_req      = np.array([state.get_requirement(fy) for fy in range(1, 31)])

    safe_corp    = np.where(det_corpus > 0, det_corpus, 1.0)
    tax_sav_frac = np.where(det_corpus > 0, det_tax_sav  / safe_corp, 0.0)
    tax_per_frac = np.where(det_corpus > 0, det_tax_pers / safe_corp, 0.0)
    other_cash   = det_net_cash - (det_req - det_tax_sav - det_tax_pers)

    # ── Simulate ─────────────────────────────────────────────────────────────
    # Use float32 throughout to halve memory (240 MB vs 480 MB at 1 M sims).
    # At 1 M sims the float64 approach causes Windows to page-fault heavily
    # (corpus_all + cash_all = 480 MB float64 → swap), making the run take
    # 10–20× longer than necessary.  float32 precision (7 significant digits)
    # is more than sufficient for corpus values in lakhs.
    init_p = float(state.total_debt_allocation() + state.total_equity_allocation() +
                   state.total_other_allocation())

    r32          = r_sim.astype(np.float32)    # bootstrap already fast; cast here
    det_req32    = det_req   .astype(np.float32)
    tsav_frac32  = tax_sav_frac.astype(np.float32)
    tper_frac32  = tax_per_frac.astype(np.float32)
    other_cash32 = other_cash  .astype(np.float32)

    corpus_all = np.empty((n_sims, 30), dtype=np.float32)  # 120 MB at 1 M sims
    cash_all   = np.empty((n_sims, 30), dtype=np.float32)  # 120 MB at 1 M sims
    corp_p     = np.full(n_sims, np.float32(init_p), dtype=np.float32)
    corp_h     = np.zeros(n_sims, dtype=np.float32)
    ever_ruined = np.zeros(n_sims, dtype=bool)
    ruin_by_fy  = np.zeros(30, dtype=np.float64)

    for fi in range(30):
        r         = r32[:, fi]
        corp_p    = corp_p * (np.float32(1.0) + r)
        actual_wd = np.minimum(det_req32[fi], corp_p)
        tax_sav   = tsav_frac32[fi] * corp_p
        tax_per   = tper_frac32[fi] * corp_p
        corp_p    = np.maximum(np.float32(0.0), corp_p - actual_wd)
        corp_h    = corp_h * (np.float32(1.0) + r) + tax_sav

        corpus_all[:, fi] = corp_p + corp_h
        cash_all  [:, fi] = actual_wd + other_cash32[fi] - tax_sav - tax_per

        # Track ruin inline — avoids second pass over (n_sims,30) matrix later
        ever_ruined |= (corpus_all[:, fi] <= np.float32(0.01))
        ruin_by_fy[fi] = ever_ruined.mean()

    # ── Marginal ruin path ───────────────────────────────────────────────────
    mri = mrc = mrr = mrfy = None
    if ever_ruined.any():
        ri       = np.where(ever_ruined)[0]
        geom_ret = np.exp(np.log1p(r_sim[ri].astype(np.float64)).mean(axis=1)) - 1.0
        best     = int(np.argmax(geom_ret))
        mri      = int(ri[best])
        mrc      = corpus_all[mri].astype(np.float64)
        mrr      = r_sim[mri]
        fr       = np.where(corpus_all[mri] <= np.float32(0.01))[0]
        mrfy     = int(fr[0] + 1) if len(fr) else 30

    # ── Percentiles — np.quantile with interpolation='lower' ─────────────────
    # np.quantile uses the introselect partial-sort algorithm: O(N) per
    # percentile vs O(N log N) for a full sort.  With axis=0 NumPy processes
    # all 30 columns in one C-level pass, keeping the working set in cache.
    # At 1 M sims this is ~4–6× faster than the 60-column np.sort approach.
    PCTS   = [0.05, 0.25, 0.50, 0.75, 0.95]
    c_pcts = np.quantile(corpus_all, PCTS, axis=0).astype(np.float64)  # (5, 30)
    n_pcts = np.quantile(cash_all,   PCTS, axis=0).astype(np.float64)  # (5, 30)

    c5, c25, c50, c75, c95 = c_pcts
    n5, n25, n50, n75, n95 = n_pcts

    return MCResults(
        n_sims=n_sims,
        fy_labels=list(range(1, 31)),
        corpus_p5=c5, corpus_p25=c25, corpus_p50=c50, corpus_p75=c75, corpus_p95=c95,
        corpus_det=det_corpus + det_huf_corp,
        cash_p5=n5, cash_p25=n25, cash_p50=n50, cash_p75=n75, cash_p95=n95,
        cash_det=det_net_cash,
        ruin_by_fy=ruin_by_fy,
        corpus_raw=corpus_all,   # float32 — avoids 240 MB copy; callers that
        cash_raw=cash_all,       # need float64 can cast locally
        ruin_probability=float(ever_ruined.mean()),
        median_final_corpus=float(c50[-1]),
        p5_final_corpus=float(c5[-1]),
        floors=floors, sigmas=sigmas,
        marginal_ruin_idx=mri, marginal_ruin_corpus=mrc,
        marginal_ruin_returns=mrr, marginal_ruin_fy=mrfy,
        method_used=method_used,
        n_equity_years=n_eq_yrs, n_debt_years=n_dbt_yrs,
        block_length=block_length,
    )