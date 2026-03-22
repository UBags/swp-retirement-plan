# Copyright (c) 2025 Uddipan Bagchi. All rights reserved.
# See LICENSE in the project root for license information.

"""
Data models for SWP Financial Planner
"""
from dataclasses import dataclass, field
from typing import List, Optional, Dict
import json
from datetime import date


def _first_available(*vals, default=7.0):
    """Return first non-None value, or default.

    Replaces the ``v1 or v2 or v3 or fallback`` pattern which
    incorrectly treats ``0.0`` as falsy, skipping legitimate zero
    values (e.g. a fund with 0% CAGR would cascade to the fallback).
    """
    for v in vals:
        if v is not None:
            return v
    return default

# ── Default fund data from the Excel sheet ──────────────────────────────────
DEFAULT_FUNDS = [
    # name, type (debt/equity), alloc, std_dev, sharpe, sortino, calmar, alpha, treynor, max_dd, beta, cagr_1, cagr_3, cagr_5, cagr_10
    {"name": "Kotak Floating Rate Fund",                  "type": "debt",   "allocation": 40,  "std_dev": 0.68, "sharpe": 2.25, "sortino": 4.45, "calmar": 8.38, "alpha": 0.55, "treynor": 2.15, "max_dd": -0.01, "beta": 0.32, "cagr_1": 8.38, "cagr_3": 8.37, "cagr_5": 7.00, "cagr_10": None},
    {"name": "Nippon India Floater Fund",                 "type": "debt",   "allocation": 40,  "std_dev": 0.70, "sharpe": 2.20, "sortino": 4.35, "calmar": 7.95, "alpha": 0.50, "treynor": 2.10, "max_dd": -0.0105, "beta": 0.34, "cagr_1": 8.35, "cagr_3": 8.32, "cagr_5": 6.95, "cagr_10": None},
    {"name": "HDFC Floating Rate Debt Fund",              "type": "debt",   "allocation": 30,  "std_dev": 0.72, "sharpe": 2.15, "sortino": 4.25, "calmar": 7.55, "alpha": 0.45, "treynor": 2.05, "max_dd": -0.011, "beta": 0.35, "cagr_1": 8.30, "cagr_3": 8.28, "cagr_5": 6.92, "cagr_10": None},
    {"name": "ICICI Prudential Floating Interest Fund",   "type": "debt",   "allocation": 30,  "std_dev": 0.75, "sharpe": 2.10, "sortino": 4.15, "calmar": 6.88, "alpha": 0.40, "treynor": 2.00, "max_dd": -0.012, "beta": 0.38, "cagr_1": 8.25, "cagr_3": 8.22, "cagr_5": 6.88, "cagr_10": None},
    {"name": "Aditya Birla Sun Life Savings Fund",        "type": "debt",   "allocation": 25,  "std_dev": 0.85, "sharpe": 2.05, "sortino": 4.05, "calmar": 6.27, "alpha": 0.35, "treynor": 1.95, "max_dd": -0.013, "beta": 0.42, "cagr_1": 8.15, "cagr_3": 8.12, "cagr_5": 6.85, "cagr_10": None},
    {"name": "ICICI Prudential Corporate Bond Fund",      "type": "debt",   "allocation": 20,  "std_dev": 1.15, "sharpe": 1.95, "sortino": 3.85, "calmar": 4.24, "alpha": 0.75, "treynor": 1.85, "max_dd": -0.019, "beta": 0.85, "cagr_1": 8.05, "cagr_3": 8.02, "cagr_5": 6.85, "cagr_10": 7.92},
    {"name": "ICICI Prudential Short Term Fund",          "type": "debt",   "allocation": 20,  "std_dev": 0.83, "sharpe": 1.15, "sortino": 2.85, "calmar": 5.47, "alpha": 0.55, "treynor": 1.45, "max_dd": -0.015, "beta": 0.95, "cagr_1": 8.20, "cagr_3": 8.15, "cagr_5": 7.00, "cagr_10": 8.20},
    {"name": "Aditya Birla Sun Life Short Term Fund",     "type": "debt",   "allocation": 30,  "std_dev": 1.02, "sharpe": 1.32, "sortino": 3.10, "calmar": 4.67, "alpha": 0.62, "treynor": 1.52, "max_dd": -0.0175, "beta": 1.00, "cagr_1": 8.18, "cagr_3": 8.15, "cagr_5": 6.98, "cagr_10": 8.06},
    {"name": "Nippon India Corporate Bond Fund",          "type": "debt",   "allocation": 20,  "std_dev": 1.25, "sharpe": 1.85, "sortino": 3.65, "calmar": 3.81, "alpha": 0.65, "treynor": 1.75, "max_dd": -0.021, "beta": 0.92, "cagr_1": 8.00, "cagr_3": 7.98, "cagr_5": 6.80, "cagr_10": 7.85},
    {"name": "Axis Short Duration Fund",                  "type": "debt",   "allocation": 10,  "std_dev": 1.04, "sharpe": 1.29, "sortino": 3.00, "calmar": 4.59, "alpha": 0.60, "treynor": 1.50, "max_dd": -0.018, "beta": 1.02, "cagr_1": 8.26, "cagr_3": 8.18, "cagr_5": 6.80, "cagr_10": 7.85},
    {"name": "ICICI Prudential Medium Term Bond Fund",    "type": "debt",   "allocation": 10,  "std_dev": 1.65, "sharpe": 1.45, "sortino": 3.25, "calmar": 2.89, "alpha": 0.85, "treynor": 1.65, "max_dd": -0.028, "beta": 1.15, "cagr_1": 8.10, "cagr_3": 8.05, "cagr_5": 6.95, "cagr_10": 7.60},
    {"name": "Kotak Medium Term Fund",                   "type": "debt",   "allocation": 10,  "std_dev": 1.72, "sharpe": 1.38, "sortino": 3.10, "calmar": 2.68, "alpha": 0.75, "treynor": 1.55, "max_dd": -0.030, "beta": 1.18, "cagr_1": 8.05, "cagr_3": 8.00, "cagr_5": 6.85, "cagr_10": 7.55},
    # Equity/Arbitrage
    {"name": "Axis Income Plus Arbitrage Omni FoF",       "type": "equity", "allocation": 25,  "std_dev": 0.98, "sharpe": 2.95, "sortino": 5.35, "calmar": 5.05, "alpha": 2.05, "treynor": 2.55, "max_dd": -0.016, "beta": 0.42, "cagr_1": 8.08, "cagr_3": 8.09, "cagr_5": 6.82, "cagr_10": None},
    {"name": "HDFC Income Plus Arbitrage Active FoF",     "type": "equity", "allocation": 15,  "std_dev": 1.05, "sharpe": 2.85, "sortino": 5.12, "calmar": 4.44, "alpha": 1.85, "treynor": 2.45, "max_dd": -0.018, "beta": 0.45, "cagr_1": 7.95, "cagr_3": 7.98, "cagr_5": 6.78, "cagr_10": None},
    {"name": "DSP Income Plus Arbitrage Omni FoF",        "type": "equity", "allocation": 15,  "std_dev": 1.08, "sharpe": 2.72, "sortino": 4.95, "calmar": 4.08, "alpha": 1.72, "treynor": 2.40, "max_dd": -0.019, "beta": 0.48, "cagr_1": 7.85, "cagr_3": 7.92, "cagr_5": 6.75, "cagr_10": None},
    {"name": "ICICI Prudential Income Plus Arb Omni FoF", "type": "equity", "allocation": 10,  "std_dev": 1.12, "sharpe": 2.65, "sortino": 4.85, "calmar": 3.71, "alpha": 1.65, "treynor": 2.35, "max_dd": -0.021, "beta": 0.52, "cagr_1": 7.80, "cagr_3": 7.85, "cagr_5": 6.70, "cagr_10": None},
    # Zero-allocation funds (for reference/selection)
    {"name": "ICICI Prudential Banking & PSU Debt Fund",  "type": "debt",   "allocation": 0,   "std_dev": 0.94, "sharpe": 1.54, "sortino": 3.25, "calmar": 5.61, "alpha": 0.65, "treynor": 1.85, "max_dd": -0.014, "beta": 1.09, "cagr_1": 7.85, "cagr_3": 7.82, "cagr_5": 6.55, "cagr_10": 7.45},
    {"name": "Kotak Banking and PSU Debt Fund",           "type": "debt",   "allocation": 0,   "std_dev": 1.05, "sharpe": 1.48, "sortino": 3.10, "calmar": 4.88, "alpha": 0.55, "treynor": 1.75, "max_dd": -0.016, "beta": 1.12, "cagr_1": 7.80, "cagr_3": 7.78, "cagr_5": 6.50, "cagr_10": 7.40},
    {"name": "HDFC Short Term Debt Fund",                 "type": "debt",   "allocation": 0,   "std_dev": 0.95, "sharpe": 1.35, "sortino": 3.05, "calmar": 4.79, "alpha": 0.65, "treynor": 1.55, "max_dd": -0.017, "beta": 1.05, "cagr_1": 8.15, "cagr_3": 8.10, "cagr_5": 6.80, "cagr_10": 7.85},
    {"name": "UTI Short Duration Fund",                   "type": "debt",   "allocation": 0,   "std_dev": 1.05, "sharpe": 1.25, "sortino": 2.95, "calmar": 4.33, "alpha": 0.45, "treynor": 1.35, "max_dd": -0.019, "beta": 1.08, "cagr_1": 8.20, "cagr_3": 8.12, "cagr_5": 6.75, "cagr_10": 7.80},
    {"name": "Nippon India Short Duration Fund",          "type": "debt",   "allocation": 0,   "std_dev": 1.08, "sharpe": 1.22, "sortino": 2.85, "calmar": 3.95, "alpha": 0.50, "treynor": 1.40, "max_dd": -0.0195,"beta": 1.10, "cagr_1": 8.15, "cagr_3": 8.08, "cagr_5": 6.70, "cagr_10": 7.75},
    {"name": "Axis Strategic Bond Fund",                  "type": "debt",   "allocation": 0,   "std_dev": 1.55, "sharpe": 1.52, "sortino": 3.40, "calmar": 3.06, "alpha": 0.95, "treynor": 1.75, "max_dd": -0.026, "beta": 1.12, "cagr_1": 7.95, "cagr_3": 8.15, "cagr_5": 6.75, "cagr_10": 7.65},
    {"name": "ICICI Prudential Credit Risk Fund",         "type": "debt",   "allocation": 0,   "std_dev": 3.45, "sharpe": 1.85, "sortino": 3.65, "calmar": 1.46, "alpha": 1.25, "treynor": 2.15, "max_dd": -0.065, "beta": 1.45, "cagr_1": 9.50, "cagr_3": 10.80,"cagr_5": 7.50, "cagr_10": 9.50},
    {"name": "ICICI Prudential Gilt Fund",                "type": "debt",   "allocation": 0,   "std_dev": 1.92, "sharpe": 0.84, "sortino": 1.95, "calmar": 2.27, "alpha": 0.25, "treynor": 1.25, "max_dd": -0.032, "beta": 0.62, "cagr_1": 7.25, "cagr_3": 8.10, "cagr_5": 6.65, "cagr_10": 7.80},
    {"name": "ICICI Prudential Constant Maturity Gilt",   "type": "debt",   "allocation": 0,   "std_dev": 2.85, "sharpe": 0.65, "sortino": 1.45, "calmar": 1.43, "alpha": -0.35,"treynor": 0.95, "max_dd": -0.048, "beta": 0.95, "cagr_1": 6.80, "cagr_3": 7.85, "cagr_5": 6.50, "cagr_10": None},
    {"name": "SBI Constant Maturity 10-Year Gilt Fund",   "type": "debt",   "allocation": 0,   "std_dev": 2.95, "sharpe": 0.62, "sortino": 1.40, "calmar": 1.31, "alpha": -0.45,"treynor": 0.90, "max_dd": -0.051, "beta": 0.98, "cagr_1": 6.70, "cagr_3": 7.75, "cagr_5": 6.40, "cagr_10": None},
]

SCORE_COLUMNS = [
    ("combined_ratio", "Combined Ratio", "higher"),  # sqrt(sortino x calmar) — default
    ("std_dev",  "Std Dev",    "lower"),
    ("sharpe",   "Sharpe",     "higher"),
    ("sortino",  "Sortino",    "higher"),
    ("calmar",   "Calmar",     "higher"),
    ("alpha",    "Alpha",      "higher"),
    ("treynor",  "Treynor",    "higher"),
    ("max_dd",   "Max DD",     "higher"),  # less negative = higher = better
    ("beta",     "Beta",       "lower"),
    ("cagr_1",   "1Y CAGR%",   "higher"),
    ("cagr_3",   "3Y CAGR%",   "higher"),
    ("cagr_5",   "5Y CAGR%",   "higher"),
    ("cagr_10",  "10Y CAGR%",  "higher"),
]

@dataclass
class TaxSlab:
    lower: float   # lower bound in lakhs
    upper: float   # upper bound in lakhs (1e9 represents infinity)
    rate: float    # 0..1

@dataclass
class TaxChunk:
    year_from: int
    year_to: int
    exempt_limit: float          # Sec 87A / zero-tax limit (lakhs) — for individual
    slabs: List[TaxSlab] = field(default_factory=list)

@dataclass
class EquityTaxChunk:
    year_from: int
    year_to: int
    tax_rate: float              # e.g. 0.125 for 12.5%
    exempt_limit: float          # annual LTCG exempt (lakhs)

@dataclass
class OtherTaxChunk:
    """Tax parameters for 'other' funds (Gold ETFs, International ETFs, etc.)
    From FY 2025-26: LTCG @12.5% for listed (>12m), no exemption, no indexation.
    """
    year_from: int
    year_to: int
    tax_rate: float              # e.g. 0.125 for 12.5%

@dataclass
class ReturnChunk:
    year_from: int
    year_to: int
    annual_return: float         # e.g. 0.07 for 7%

@dataclass
class SplitChunk:
    year_from: int
    year_to: int
    debt_ratio: float            # 0..1  (equity ratio = 1 - debt_ratio)

@dataclass
class FDRateChunk:
    year_from: int
    year_to: int
    fd_rate: float               # e.g. 0.07 for 7%

@dataclass
class AllocationChunk:
    """Per-period fund allocation produced by the multi-chunk allocator."""
    year_from: int
    year_to:   int
    funds:     List["FundEntry"] = field(default_factory=list)

    # Normalised weights (sum=1) written by the sticky-portfolio optimizer.
    # Keys are fund names (matching FundEntry.name).  Empty until the
    # optimizer has run.  These are the *target* weights before any
    # glide-path interpolation.
    target_weights: Dict[str, float] = field(default_factory=dict)

    # Records which of the three soft tolerances were consumed during
    # backward induction.  Keys: "return", "std_dev", "max_dd";
    # values: the actual slack used as a positive fraction (0 = not consumed).
    constraint_slack_used: Dict[str, float] = field(default_factory=dict)

    def portfolio_yield(self) -> float:
        """Weighted-average 5Y CAGR across allocated funds in this chunk."""
        total = weight = 0.0
        for f in self.funds:
            if f.allocation > 0:
                cagr = _first_available(f.cagr_5, f.cagr_3, f.cagr_1, default=7.0)
                total  += cagr * f.allocation
                weight += f.allocation
        return (total / weight / 100) if weight > 0 else 0.07

    def optimized_yield(self) -> float:
        """Weighted-average 5Y CAGR using target_weights (post-optimizer).

        Falls back to portfolio_yield() if target_weights is empty.
        """
        if not self.target_weights:
            return self.portfolio_yield()
        fund_map = {f.name: f for f in self.funds}
        total = weight = 0.0
        for fname, w in self.target_weights.items():
            f = fund_map.get(fname)
            if f and w > 0:
                cagr = _first_available(f.cagr_5, f.cagr_3, f.cagr_1, default=7.0)
                total  += cagr * w
                weight += w
        return (total / weight / 100) if weight > 0 else self.portfolio_yield()

    def optimized_sigma(self) -> float:
        """
        Allocation-weighted annualised sigma using target_weights (post-optimizer).

        Uses the linear allocation-weighted average (perfect-correlation upper bound):
            sigma = sum(w_i * sigma_i)

        This is the HIGHEST valid portfolio std dev estimate — it assumes all funds
        move in perfect lockstep, which is the most conservative assumption for
        Monte Carlo purposes.  It matches the 'Std:X.XX%' value shown in the
        View Fund Selection & Allocation dialog.

        Formula ordering for reference:
            rms_correct = sqrt(sum(w_i² * sigma_i²))   ← zero-correlation lower bound
            lin         = sum(w_i * sigma_i)            ← perfect-correlation upper bound  [used here]
            rms_old     = sqrt(sum(w_i * sigma_i²))     ← NOT a valid portfolio formula;
                                                           by Jensen: sqrt(E[σ²]) ≥ E[σ],
                                                           so rms_old ≥ lin (overestimates)

        Falls back to fund-allocation-weighted sigma if target_weights is empty.
        Returns fraction (e.g. 0.0147 for 1.47%).
        """
        if self.target_weights:
            fund_map = {f.name: f for f in self.funds}
            total_w = 0.0
            wtd_std = 0.0
            for fname, w in self.target_weights.items():
                f = fund_map.get(fname)
                if f and w > 0:
                    wtd_std += w * (f.std_dev / 100.0)
                    total_w += w
            if total_w > 0:
                return wtd_std / total_w
        # Fallback: use fund allocations (mirrors fund_dialog formula exactly)
        active = [f for f in self.funds if f.allocation > 0]
        total = sum(f.allocation for f in active)
        if total <= 0 or not active:
            return 0.0097
        return sum((f.allocation / total) * (f.std_dev / 100.0) for f in active)

    def category_yield(self, fund_type: str) -> Optional[float]:
        """Weighted-average CAGR for one fund_type ('debt'/'equity'/'other').
        Returns None if no funds of that type are allocated."""
        total = weight = 0.0
        for f in self.funds:
            if f.allocation > 0 and f.fund_type == fund_type:
                cagr = _first_available(f.cagr_5, f.cagr_3, f.cagr_1, default=7.0)
                total  += cagr * f.allocation
                weight += f.allocation
        return (total / weight / 100) if weight > 0 else None

    def debt_ratio(self) -> float:
        total = sum(f.allocation for f in self.funds)
        if total == 0:
            return 0.8
        debt = sum(f.allocation for f in self.funds if f.fund_type == "debt")
        return debt / total


@dataclass
class FundEntry:
    name: str
    fund_type: str               # "debt", "equity", or "other"
    allocation: float            # Rs lakhs
    # scores
    std_dev: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    alpha: float = 0.0
    treynor: float = 0.0
    max_dd: float = 0.0
    beta: float = 0.0
    combined_ratio: float = 0.0   # sqrt(sortino x calmar)
    cagr_1: Optional[float] = None
    cagr_3: Optional[float] = None
    cagr_5: Optional[float] = None
    cagr_10: Optional[float] = None
    # Conservative-mode return: Worst_Exp_Ret_% from fund analyser
    # = min historical rolling CAGR minus STT adjustment (annualised %).
    # None when fund metrics haven't been imported yet; engine falls back
    # to min(cagr_1, cagr_3, cagr_5, cagr_10) in that case.
    worst_exp_ret: Optional[float] = None
    # AMFI sub-category (e.g. "Debt Scheme - Floater Fund",
    # "Equity Scheme - ELSS").  Carried through from Fund_Metrics_Output.csv
    # for display and per-type allocation constraints.
    # fund_type is the TAX category ("debt"/"equity"/"other");
    # amfi_fund_type is the AMFI regulatory sub-category.
    amfi_fund_type: Optional[str] = None

@dataclass
class OtherIncome:
    salary: float = 0.0
    taxable_interest: float = 0.0
    tax_free_interest: float = 0.0
    pension: float = 0.0
    rental: float = 0.0
    other_taxable: float = 0.0
    other_non_taxable: float = 0.0

@dataclass
class WindfallEntry:
    year: int
    amount: float                # Rs lakhs
    target: str                  # "personal" or "huf"

@dataclass
class HUFWithdrawalChunk:
    year_from: int
    year_to: int
    annual_withdrawal: float     # Rs lakhs

@dataclass
class RebalanceCost:
    """Capital-gains tax and exit-load costs incurred during a single rebalancing year."""
    year:            int
    taxes_paid:      float   # Rs lakhs — sum of STCG + LTCG taxes triggered
    exit_loads_paid: float   # Rs lakhs — exit loads on units redeemed < 1 yr

    @property
    def total(self) -> float:
        return self.taxes_paid + self.exit_loads_paid


@dataclass
class GlidePath:
    """
    Year-by-year portfolio weight schedule for a 30-year simulation.

    ``schedule`` maps plan year (1–30) → {fund_name: weight_fraction}.
    Weights in each year sum to 1.0.

    Built by glide_path.build_glide_path() after the sticky-portfolio
    optimizer has written target_weights into every AllocationChunk.
    """
    schedule: Dict[int, Dict[str, float]] = field(default_factory=dict)

    def weights_for_year(self, year: int) -> Dict[str, float]:
        """Return the target weight dict for *year* (1-30).
        Falls back to the nearest year that exists if *year* is missing."""
        if year in self.schedule:
            return self.schedule[year]
        # Fallback: search backwards then forwards
        for y in range(year - 1, 0, -1):
            if y in self.schedule:
                return self.schedule[y]
        for y in range(year + 1, 31):
            if y in self.schedule:
                return self.schedule[y]
        return {}

    def transition_years(self) -> List[int]:
        """Return years where weights differ from the prior year (rebalance needed)."""
        result = []
        for year in sorted(self.schedule.keys()):
            if year == 1:
                continue
            if self.schedule.get(year) != self.schedule.get(year - 1):
                result.append(year)
        return result

    def is_flat(self) -> bool:
        """True when all years share identical weights (Mode A or single chunk)."""
        if not self.schedule:
            return True
        first = next(iter(self.schedule.values()))
        return all(v == first for v in self.schedule.values())


@dataclass
class AppState:
    # ── SCENARIO SYNC NOTE ────────────────────────────────────────────────────
    # main.py supports 4 independent scenarios that share certain fields.
    # The shared-field list is maintained in MainWindow._SHARED_FIELDS.
    # *** When adding a new field to AppState, decide whether it should be
    # shared across scenarios (tax rules, requirements, income, etc.) or
    # scenario-specific (funds, allocation_chunks, return_chunks, etc.).
    # If shared, add it to _SHARED_FIELDS in main.py or the scenarios will
    # silently diverge. ***
    # ──────────────────────────────────────────────────────────────────────────

    # Investment start  [SHARED across scenarios]
    investment_date: date = field(default_factory=lambda: date(date.today().year, 3, 1))

    # Funds (flat list — used when no allocation_chunks defined)
    funds: List[FundEntry] = field(default_factory=list)

    # Per-period fund allocations from the multi-chunk allocator.
    # When present, these take precedence over the flat funds list.
    allocation_chunks: List[AllocationChunk] = field(default_factory=list)

    # Individual tax rules
    individual_debt_chunks: List[TaxChunk] = field(default_factory=list)
    individual_equity_chunks: List[EquityTaxChunk] = field(default_factory=list)
    individual_other_chunks: List[OtherTaxChunk] = field(default_factory=list)

    # HUF tax rules
    huf_debt_chunks: List[TaxChunk] = field(default_factory=list)
    huf_equity_chunks: List[EquityTaxChunk] = field(default_factory=list)
    huf_other_chunks: List[OtherTaxChunk] = field(default_factory=list)

    # Return rate (applies to both debt and equity within each bucket separately
    # but we keep one series for now and allow per-bucket if needed)
    return_chunks: List[ReturnChunk] = field(default_factory=list)

    # Withdrawal split
    split_chunks: List[SplitChunk] = field(default_factory=list)

    # Annual fund requirements (SWP withdrawal target) — dict year->amount
    annual_requirements: Dict[int, float] = field(default_factory=dict)

    # Other income
    personal_income: OtherIncome = field(default_factory=OtherIncome)
    huf_income: OtherIncome = field(default_factory=OtherIncome)

    # Windfalls
    windfalls: List[WindfallEntry] = field(default_factory=list)

    # HUF withdrawals
    huf_withdrawal_chunks: List[HUFWithdrawalChunk] = field(default_factory=list)

    # Per-FY HUF withdrawal overrides (set by the tax-optimal split optimizer).
    # When a FY key is present, it overrides huf_withdrawal_chunks for that year.
    huf_annual_requirements: Dict[int, float] = field(default_factory=dict)

    # FD rate chunks (user-specified FD interest rate per time period)
    fd_rate_chunks: List[FDRateChunk] = field(default_factory=list)

    # Legacy scalar — kept for backward compat during deserialization only
    fd_rate: float = 0.07

    # ── Sticky-portfolio / rebalancing settings ──────────────────────────────
    # "singular"       – Mode A: one portfolio held for all 30 years, no
    #                    rebalancing (buy-and-hold).
    # "chunked_sticky" – Mode B: separate allocation per chunk, backward-
    #                    induction minimises turnover between adjacent chunks.
    allocation_mode: str = "chunked_sticky"

    # Number of years centred on each chunk boundary over which the glide-path
    # interpolation spreads the rebalance.  Only used in Mode B.
    rebalance_spread_years: int = 4

    # Index into the robustness-ranking table for Mode A / chunk selection.
    # 0 = best-ranked (default); the UI lets the user pick any rank.
    selected_robustness_rank: int = 0

    # Backward-induction soft tolerances (set by GlidePathParametersDialog)
    # None → use run_backward_induction defaults (return=0.0025, std=0.0025, dd=0.005)
    bi_tolerances: Optional[dict] = field(default=None, compare=False)

    # Capital-allocation dialog input parameters (total_money, bonus, chunks).
    # Persisted so the user's allocation choices survive save/load.
    allocation_params: Optional[dict] = field(default=None, compare=False)

    # The computed glide-path schedule (set after optimize_sticky_portfolio
    # runs).  None until then.  Not persisted to JSON (recomputed on load).
    glide_path: Optional["GlidePath"] = field(default=None, compare=False)

    def to_dict(self):
        import dataclasses
        # Fields that should NOT be persisted to JSON
        _skip = {"glide_path"}
        def convert(obj):
            if isinstance(obj, date):
                return obj.isoformat()
            if dataclasses.is_dataclass(obj):
                return {k: convert(v) for k, v in dataclasses.asdict(obj).items()
                        if k not in _skip}
            if isinstance(obj, list):
                return [convert(i) for i in obj]
            if isinstance(obj, dict):
                return {k: convert(v) for k, v in obj.items()}
            return obj
        d = convert(self)
        # Belt-and-suspenders: also remove at the top level
        for k in _skip:
            d.pop(k, None)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "AppState":
        from datetime import date as date_cls
        def make_slabs(lst):
            return [TaxSlab(**s) for s in lst]
        def make_tax_chunks(lst):
            return [TaxChunk(c["year_from"], c["year_to"], c["exempt_limit"],
                             make_slabs(c["slabs"])) for c in lst]
        def make_eq_chunks(lst):
            return [EquityTaxChunk(**c) for c in lst]
        def make_other_chunks(lst):
            return [OtherTaxChunk(**c) for c in lst]
        def make_ret_chunks(lst):
            return [ReturnChunk(**c) for c in lst]
        def make_split_chunks(lst):
            return [SplitChunk(**c) for c in lst]
        def make_funds(lst):
            out = []
            for f in lst:
                f.setdefault("worst_exp_ret", None)
                f.setdefault("amfi_fund_type", None)
                out.append(FundEntry(**f))
            return out
        def make_income(d2):
            return OtherIncome(**d2) if d2 else OtherIncome()
        def make_windfalls(lst):
            return [WindfallEntry(**w) for w in lst]
        def make_huf_wd(lst):
            return [HUFWithdrawalChunk(**c) for c in lst]

        def make_fd_rate_chunks(lst):
            return [FDRateChunk(**c) for c in lst]

        def make_alloc_chunks(lst):
            result = []
            for c in lst:
                funds = []
                for f in c.get("funds", []):
                    f.setdefault("worst_exp_ret", None)
                    f.setdefault("amfi_fund_type", None)
                    funds.append(FundEntry(**f))
                result.append(AllocationChunk(
                    year_from=c["year_from"],
                    year_to=c["year_to"],
                    funds=funds,
                    target_weights=dict(c.get("target_weights", {})),
                    constraint_slack_used=c.get("constraint_slack_used", {}),
                ))
            # Post-pass: if all chunks have identical non-empty target_weights,
            # they are stale (optimizer wrote chunk 1 weights to all chunks).
            # Discard all of them so main.py seeds fresh weights from fund allocs.
            if len(result) > 1:
                non_empty = [c for c in result if c.target_weights]
                if non_empty and all(c.target_weights == non_empty[0].target_weights
                                     for c in non_empty):
                    for c in result:
                        c.target_weights = {}
                        c.constraint_slack_used = {}
            return result

        # Ignore any keys not valid in AppState (e.g. output_dir from old saves)
        known_keys = {
            "investment_date", "funds", "allocation_chunks",
            "individual_debt_chunks", "individual_equity_chunks",
            "individual_other_chunks",
            "huf_debt_chunks", "huf_equity_chunks",
            "huf_other_chunks",
            "return_chunks", "split_chunks", "annual_requirements",
            "personal_income", "huf_income", "windfalls",
            "huf_withdrawal_chunks", "fd_rate", "fd_rate_chunks",
            "huf_annual_requirements",
            "allocation_mode", "rebalance_spread_years",
            "selected_robustness_rank", "bi_tolerances",
            "allocation_params",
        }
        d = {k: v for k, v in d.items() if k in known_keys}

        inv_date = date_cls.fromisoformat(d.get("investment_date", date_cls(date_cls.today().year,3,1).isoformat()))
        result = cls(
            investment_date=inv_date,
            funds=make_funds(d.get("funds", [])),
            allocation_chunks=make_alloc_chunks(d.get("allocation_chunks", [])),
            individual_debt_chunks=make_tax_chunks(d.get("individual_debt_chunks", [])),
            individual_equity_chunks=make_eq_chunks(d.get("individual_equity_chunks", [])),
            individual_other_chunks=make_other_chunks(d.get("individual_other_chunks", [])),
            huf_debt_chunks=make_tax_chunks(d.get("huf_debt_chunks", [])),
            huf_equity_chunks=make_eq_chunks(d.get("huf_equity_chunks", [])),
            huf_other_chunks=make_other_chunks(d.get("huf_other_chunks", [])),
            return_chunks=make_ret_chunks(d.get("return_chunks", [])),
            split_chunks=make_split_chunks(d.get("split_chunks", [])),
            annual_requirements={int(k): v for k, v in d.get("annual_requirements", {}).items()},
            personal_income=make_income(d.get("personal_income")),
            huf_income=make_income(d.get("huf_income")),
            windfalls=make_windfalls(d.get("windfalls", [])),
            huf_withdrawal_chunks=make_huf_wd(d.get("huf_withdrawal_chunks", [])),
            huf_annual_requirements={int(k): v for k, v in d.get("huf_annual_requirements", {}).items()},
            fd_rate_chunks=make_fd_rate_chunks(d.get("fd_rate_chunks", [])),
            fd_rate=d.get("fd_rate", 0.07),
            allocation_mode=d.get("allocation_mode", "chunked_sticky"),
            rebalance_spread_years=int(d.get("rebalance_spread_years", 4)),
            selected_robustness_rank=int(d.get("selected_robustness_rank", 0)),
            bi_tolerances=d.get("bi_tolerances", None),
            allocation_params=d.get("allocation_params", None),
        )
        # ── Migration: populate missing chunks from older save files ─────
        # FD rate chunks (legacy scalar → chunk migration)
        if not result.fd_rate_chunks and result.fd_rate > 0:
            result.fd_rate_chunks = [FDRateChunk(1, 30, result.fd_rate)]
        # 'Other' tax chunks (added in v4 for Gold/Intl ETFs)
        if not result.individual_other_chunks:
            result.individual_other_chunks = [OtherTaxChunk(1, 30, 0.125)]
        if not result.huf_other_chunks:
            result.huf_other_chunks = [OtherTaxChunk(1, 30, 0.125)]
        return result

    def get_requirement(self, year: int) -> float:
        """Return annual withdrawal requirement for given year (1-30)."""
        req = 0.0
        for y in sorted(self.annual_requirements.keys()):
            if y <= year:
                req = self.annual_requirements[y]
        return req

    def get_debt_tax_chunk(self, year: int, entity: str) -> Optional[TaxChunk]:
        chunks = self.individual_debt_chunks if entity == "individual" else self.huf_debt_chunks
        for c in chunks:
            if c.year_from <= year <= c.year_to:
                return c
        return None

    def get_equity_tax_chunk(self, year: int, entity: str) -> Optional[EquityTaxChunk]:
        chunks = self.individual_equity_chunks if entity == "individual" else self.huf_equity_chunks
        for c in chunks:
            if c.year_from <= year <= c.year_to:
                return c
        return None

    def get_other_tax_chunk(self, year: int, entity: str) -> Optional[OtherTaxChunk]:
        chunks = self.individual_other_chunks if entity == "individual" else self.huf_other_chunks
        for c in chunks:
            if c.year_from <= year <= c.year_to:
                return c
        return None

    def get_return_rate(self, year: int) -> float:
        for c in self.return_chunks:
            if c.year_from <= year <= c.year_to:
                return c.annual_return
        return 0.07

    def get_allocation_chunk(self, year: int) -> Optional["AllocationChunk"]:
        """Return the AllocationChunk covering this year, or None."""
        for c in self.allocation_chunks:
            if c.year_from <= year <= c.year_to:
                return c
        return None

    def get_funds_for_year(self, year: int) -> List[FundEntry]:
        """
        Return the fund list appropriate for this plan year.
        Uses allocation_chunks if defined, otherwise falls back to state.funds.
        """
        ac = self.get_allocation_chunk(year)
        if ac is not None:
            return [f for f in ac.funds if f.allocation > 0]
        return [f for f in self.funds if f.allocation > 0]

    def get_fd_rate(self, year: int) -> float:
        """Return FD interest rate for a given year from fd_rate_chunks."""
        for c in self.fd_rate_chunks:
            if c.year_from <= year <= c.year_to:
                return c.fd_rate
        # Fallback to legacy scalar
        return self.fd_rate

    def get_split(self, year: int) -> float:
        """Return debt ratio for given year."""
        # 1. Explicit split_chunks override (user-configured)
        for c in self.split_chunks:
            if c.year_from <= year <= c.year_to:
                return c.debt_ratio
        # 2. Derive from allocation_chunks if available
        ac = self.get_allocation_chunk(year)
        if ac is not None:
            active = [f for f in ac.funds if f.allocation > 0]
            total = sum(f.allocation for f in active)
            if total > 0:
                debt = sum(f.allocation for f in active if f.fund_type == "debt")
                return debt / total
        # 3. Default from flat fund allocation
        total = sum(f.allocation for f in self.funds)
        if total == 0:
            return 0.8
        debt = sum(f.allocation for f in self.funds if f.fund_type == "debt")
        return debt / total

    def get_split_3way(self, year: int) -> tuple:
        """Return (debt_ratio, equity_ratio, other_ratio) for a given year.
        Sums to 1.0.  Used for HUF investments so 'other' is not ignored."""
        ac = self.get_allocation_chunk(year)
        if ac is not None:
            active = [f for f in ac.funds if f.allocation > 0]
        else:
            active = [f for f in self.funds if f.allocation > 0]
        total = sum(f.allocation for f in active)
        if total == 0:
            return (0.8, 0.2, 0.0)
        d = sum(f.allocation for f in active if f.fund_type == "debt") / total
        e = sum(f.allocation for f in active if f.fund_type == "equity") / total
        o = sum(f.allocation for f in active if f.fund_type == "other") / total
        return (d, e, o)

    def _init_funds(self) -> List["FundEntry"]:
        """Return the fund list used for initial corpus sizing (year-1 funds)."""
        return self.get_funds_for_year(1)

    def total_debt_allocation(self) -> float:
        return sum(f.allocation for f in self._init_funds() if f.fund_type == "debt")

    def total_equity_allocation(self) -> float:
        return sum(f.allocation for f in self._init_funds() if f.fund_type == "equity")

    def total_other_allocation(self) -> float:
        return sum(f.allocation for f in self._init_funds() if f.fund_type == "other")

    def total_allocation(self) -> float:
        return self.total_debt_allocation() + self.total_equity_allocation() + self.total_other_allocation()

    def chunk_boundary_years(self) -> List[int]:
        """Return the first FY of each allocation chunk after the first one.
        These are the years where rebalancing should occur."""
        if len(self.allocation_chunks) <= 1:
            return []
        return [ac.year_from for ac in self.allocation_chunks[1:]]

    def import_fund_metrics(self, csv_path: str) -> int:
        """
        Import fund score metrics from Fund_Metrics_Output.csv into all
        FundEntry objects (both in self.funds and in allocation_chunks).
        Matches by fund name (case-insensitive).
        Returns the number of funds updated.
        """
        import csv as _csv
        # Read CSV into a name->row dict
        metrics_map: dict = {}
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = _csv.DictReader(f)
            for row in reader:
                name = (row.get("Fund Name") or "").strip()
                if name:
                    metrics_map[name.lower()] = row

        def _update_fund(fund: "FundEntry") -> bool:
            row = metrics_map.get(fund.name.lower())
            if not row:
                return False
            def _f(key, default=None):
                v = row.get(key, "")
                try:
                    return float(v) if v else default
                except ValueError:
                    return default
            # Use 10Y scores if available, fallback to 5Y, then 3Y
            # std_dev in CSV is a fraction (e.g. 0.0159); store as % (1.59)
            fund.std_dev = _first_available(_f("Std_Dev_10Y"), _f("Std_Dev_5Y"), _f("Std_Dev_3Y"), default=0.0) * 100
            fund.sharpe = _first_available(_f("Sharpe_10Y"), _f("Sharpe_5Y"), _f("Sharpe_3Y"), default=0.0)
            fund.sortino = _first_available(_f("Sortino_10Y"), _f("Sortino_5Y"), _f("Sortino_3Y"), default=0.0)
            fund.calmar = _first_available(_f("Calmar_10Y"), _f("Calmar_5Y"), _f("Calmar_3Y"), default=0.0)
            # max_dd in CSV is already a fraction (e.g. -0.004); keep as-is
            fund.max_dd = _first_available(_f("Max_DD_10Y"), _f("Max_DD_5Y"), _f("Max_DD_3Y"), default=0.0)
            fund.alpha = (_f("Alpha_10Y") or 0.0) * 100     # fraction -> %
            fund.beta = _f("Beta_10Y") or 0.0
            fund.treynor = (_f("Treynor_10Y") or 0.0) * 100  # fraction -> %
            fund.combined_ratio = _first_available(_f("Combined_Ratio_10Y"), _f("Combined_Ratio_5Y"), _f("Combined_Ratio_3Y"), default=0.0)
            # CAGRs in CSV are fractions (0.0808 = 8.08%); store as % (8.08)
            c1 = _f("1Y_CAGR");  fund.cagr_1 = (c1 * 100) if c1 is not None else None
            c3 = _f("3Y_CAGR");  fund.cagr_3 = (c3 * 100) if c3 is not None else None
            c5 = _f("5Y_CAGR");  fund.cagr_5 = (c5 * 100) if c5 is not None else None
            c10 = _f("10Y_CAGR"); fund.cagr_10 = (c10 * 100) if c10 is not None else None
            # Worst_Exp_Ret_% in CSV is a fraction (e.g. 0.0612 = 6.12%); store as %
            wer = _f("Worst_Exp_Ret_%")
            fund.worst_exp_ret = (wer * 100) if wer is not None else None
            return True

        updated_names: set = set()
        for f in self.funds:
            if _update_fund(f):
                updated_names.add(f.name)
        for ac in self.allocation_chunks:
            for f in ac.funds:
                if _update_fund(f):
                    updated_names.add(f.name)
        return len(updated_names)

    def evaluate_chunk_scores(self) -> List[dict]:
        """
        Evaluate each allocation chunk using the composite score formula:
          score = portfolio_return × portfolio_combined_ratio × sqrt(portfolio_sharpe / portfolio_std_dev)

        portfolio_std_dev = sqrt(sum(w_i × σ_i²))  where w_i = allocation fraction, σ_i in %
        portfolio_return  = weighted avg CAGR (5Y preferred, fallback 3Y/1Y/7%)
        portfolio_sharpe  = weighted avg Sharpe ratio
        portfolio_combined_ratio = weighted avg combined_ratio

        Returns list of dicts with chunk info and scores, sorted best-first.
        """
        import math
        results = []
        chunks = self.allocation_chunks if self.allocation_chunks else []
        if not chunks:
            # Single flat fund list — treat as one chunk
            chunks_to_eval = [("Years 1-30", self.funds)]
        else:
            chunks_to_eval = [
                (f"Chunk {i+1} (Yr {ac.year_from}-{ac.year_to})", ac.funds)
                for i, ac in enumerate(chunks)
            ]

        for label, funds in chunks_to_eval:
            active = [f for f in funds if f.allocation > 0]
            total_alloc = sum(f.allocation for f in active)
            if total_alloc == 0:
                continue

            w_return = w_sharpe = w_combined = w_sortino = 0.0
            w_std = 0.0
            debt_alloc = eq_alloc = 0.0
            for f in active:
                w = f.allocation / total_alloc
                cagr = _first_available(f.cagr_5, f.cagr_3, f.cagr_1, default=7.0)  # percentages
                w_return   += w * cagr
                w_sharpe   += w * f.sharpe
                w_combined += w * f.combined_ratio
                w_sortino  += w * f.sortino
                # std_dev is in % (e.g. 1.59 means 1.59%)
                # Use weighted-average std (same as allocator's C3 constraint:
                # dot(w, std) <= max_std_dev).  This assumes perfect correlation
                # (conservative) and matches the metric the optimizer constrains.
                w_std      += w * f.std_dev
                if f.fund_type == "debt":
                    debt_alloc += f.allocation
                else:
                    eq_alloc += f.allocation

            port_std = w_std if w_std > 0 else 0.01
            port_return = w_return
            port_sharpe = w_sharpe
            port_combined = w_combined

            # Composite score
            if port_std > 0 and port_sharpe > 0:
                score = port_return * port_combined * math.sqrt(port_sharpe / port_std)
            else:
                score = 0.0

            results.append({
                "label": label,
                "funds": active,
                "total_alloc": total_alloc,
                "debt_alloc": debt_alloc,
                "equity_alloc": eq_alloc,
                "port_return": port_return,
                "port_std": port_std,
                "port_sharpe": port_sharpe,
                "port_sortino": w_sortino,
                "port_combined": port_combined,
                "score": score,
            })

        results.sort(key=lambda x: x["score"], reverse=True)
        return results

    def apply_best_chunk(self) -> Optional[dict]:
        """
        Evaluate all allocation chunks, pick the best-ranked one (rank index 0
        by default, or self.selected_robustness_rank if set), and apply it as
        a single chunk spanning years 1-30 (Mode A — no rebalancing).

        Returns the winning chunk info dict, or None if no chunks.
        Use apply_ranked_chunk() if you want to specify the rank explicitly.
        """
        return self.apply_ranked_chunk(self.selected_robustness_rank)

    def apply_ranked_chunk(self, rank: int = 0) -> Optional[dict]:
        """
        Evaluate all allocation chunks, pick the chunk at *rank* position in
        the robustness-score table (0 = best), and apply it as a single chunk
        spanning years 1-30.

        If rank is out-of-range, the best (rank 0) is used with a warning.
        Returns the winning chunk info dict, or None if no chunks.
        """
        import logging
        _log = logging.getLogger("models.debug")

        scores = self.evaluate_chunk_scores()
        if not scores:
            return None

        safe_rank = max(0, min(rank, len(scores) - 1))
        if safe_rank != rank:
            _log.warning(
                f"apply_ranked_chunk: rank {rank} out of range "
                f"({len(scores)} options); using rank {safe_rank}."
            )
        best = scores[safe_rank]

        _log.debug(f"\napply_ranked_chunk(rank={safe_rank}): "
                   f"selected = {best['label']}")
        _log.debug(f"  funds ({len(best['funds'])}):")
        for f in best["funds"]:
            _log.debug(
                f"    {f.name[:45]:<45s} type={f.fund_type:<6s} "
                f"alloc={f.allocation:.2f}")

        import copy as _copy
        best_funds = [_copy.deepcopy(f) for f in best["funds"]]
        self.allocation_chunks = [
            AllocationChunk(year_from=1, year_to=30, funds=best_funds)
        ]
        self.funds = [_copy.deepcopy(f) for f in best_funds]

        d = sum(f.allocation for f in self.allocation_chunks[0].funds
                if f.fund_type == 'debt')
        e = sum(f.allocation for f in self.allocation_chunks[0].funds
                if f.fund_type == 'equity')
        o = sum(f.allocation for f in self.allocation_chunks[0].funds
                if f.fund_type == 'other')
        _log.debug(f"  chunk[0] yr1-30: D={d:.2f} E={e:.2f} O={o:.2f}")

        return best

    def portfolio_yield(self) -> float:
        """Weighted average of 5Y CAGR across all selected (allocation>0) funds."""
        total = 0.0
        weight = 0.0
        for f in self.funds:
            if f.allocation > 0:
                cagr = _first_available(f.cagr_5, f.cagr_3, f.cagr_1, default=7.0)
                total += cagr * f.allocation
                weight += f.allocation
        return (total / weight / 100) if weight > 0 else 0.07

    def category_yield(self, fund_type: str) -> Optional[float]:
        """Weighted-average CAGR for one fund_type ('debt'/'equity'/'other')
        from the flat funds list.  Returns None if no funds of that type."""
        total = weight = 0.0
        for f in self.funds:
            if f.allocation > 0 and f.fund_type == fund_type:
                cagr = _first_available(f.cagr_5, f.cagr_3, f.cagr_1, default=7.0)
                total += cagr * f.allocation
                weight += f.allocation
        return (total / weight / 100) if weight > 0 else None

    def get_category_return(self, year: int, fund_type: str) -> Optional[float]:
        """Return per-category yield for a given year.
        Reads from allocation_chunks if available, else from flat funds list.
        Returns None if no funds of that type are allocated."""
        ac = self.get_allocation_chunk(year)
        if ac is not None:
            return ac.category_yield(fund_type)
        return self.category_yield(fund_type)


def default_state() -> AppState:
    """Create a default AppState with sample data pre-loaded."""
    funds = [FundEntry(
        name=f["name"], fund_type=f["type"], allocation=f["allocation"],
        std_dev=f["std_dev"], sharpe=f["sharpe"], sortino=f["sortino"],
        calmar=f["calmar"], alpha=f["alpha"], treynor=f["treynor"],
        max_dd=f["max_dd"], beta=f["beta"],
        cagr_1=f["cagr_1"], cagr_3=f["cagr_3"], cagr_5=f["cagr_5"], cagr_10=f["cagr_10"]
    ) for f in DEFAULT_FUNDS]

    # Default individual tax chunks (5-year blocks, projected)
    ind_debt = [
        TaxChunk(1, 5, 12.0, [TaxSlab(0,4,0), TaxSlab(4,8,0.05), TaxSlab(8,12,0.10),
                               TaxSlab(12,16,0.15), TaxSlab(16,20,0.20), TaxSlab(20,24,0.25), TaxSlab(24,1e9,0.30)]),
        TaxChunk(6,10, 14.0, [TaxSlab(0,4,0), TaxSlab(4,9,0.05), TaxSlab(9,14,0.10),
                               TaxSlab(14,18,0.15), TaxSlab(18,22,0.20), TaxSlab(22,26,0.25), TaxSlab(26,1e9,0.30)]),
        TaxChunk(11,15,16.0, [TaxSlab(0,5,0), TaxSlab(5,10,0.05), TaxSlab(10,15,0.10),
                               TaxSlab(15,20,0.15), TaxSlab(20,24,0.20), TaxSlab(24,28,0.25), TaxSlab(28,1e9,0.30)]),
        TaxChunk(16,20,18.0, [TaxSlab(0,6,0), TaxSlab(6,12,0.05), TaxSlab(12,16,0.10),
                               TaxSlab(16,20,0.15), TaxSlab(20,24,0.20), TaxSlab(24,28,0.25), TaxSlab(28,1e9,0.30)]),
        TaxChunk(21,25,20.0, [TaxSlab(0,7,0), TaxSlab(7,14,0.05), TaxSlab(14,18,0.10),
                               TaxSlab(18,22,0.15), TaxSlab(22,26,0.20), TaxSlab(26,30,0.25), TaxSlab(30,1e9,0.30)]),
        TaxChunk(26,30,20.0, [TaxSlab(0,7,0), TaxSlab(7,15,0.05), TaxSlab(15,20,0.10),
                               TaxSlab(20,24,0.15), TaxSlab(24,28,0.20), TaxSlab(28,32,0.25), TaxSlab(32,1e9,0.30)]),
    ]
    ind_eq = [
        EquityTaxChunk(1, 5,  0.125, 1.25),
        EquityTaxChunk(6, 10, 0.125, 1.50),
        EquityTaxChunk(11,15, 0.125, 1.75),
        EquityTaxChunk(16,20, 0.125, 2.00),
        EquityTaxChunk(21,25, 0.125, 2.25),
        EquityTaxChunk(26,30, 0.125, 2.50),
    ]
    # HUF — no 87A rebate, but same slabs; exempt_limit set very low (4L basic)
    huf_debt = [
        TaxChunk(1, 5,  4.0, [TaxSlab(0,4,0), TaxSlab(4,8,0.05), TaxSlab(8,12,0.10),
                               TaxSlab(12,16,0.15), TaxSlab(16,20,0.20), TaxSlab(20,24,0.25), TaxSlab(24,1e9,0.30)]),
        TaxChunk(6, 10, 4.0, [TaxSlab(0,4,0), TaxSlab(4,9,0.05), TaxSlab(9,14,0.10),
                               TaxSlab(14,18,0.15), TaxSlab(18,22,0.20), TaxSlab(22,26,0.25), TaxSlab(26,1e9,0.30)]),
        TaxChunk(11,15, 5.0, [TaxSlab(0,5,0), TaxSlab(5,10,0.05), TaxSlab(10,15,0.10),
                               TaxSlab(15,20,0.15), TaxSlab(20,24,0.20), TaxSlab(24,28,0.25), TaxSlab(28,1e9,0.30)]),
        TaxChunk(16,20, 6.0, [TaxSlab(0,6,0), TaxSlab(6,12,0.05), TaxSlab(12,16,0.10),
                               TaxSlab(16,20,0.15), TaxSlab(20,24,0.20), TaxSlab(24,28,0.25), TaxSlab(28,1e9,0.30)]),
        TaxChunk(21,25, 7.0, [TaxSlab(0,7,0), TaxSlab(7,14,0.05), TaxSlab(14,18,0.10),
                               TaxSlab(18,22,0.15), TaxSlab(22,26,0.20), TaxSlab(26,30,0.25), TaxSlab(30,1e9,0.30)]),
        TaxChunk(26,30, 7.0, [TaxSlab(0,7,0), TaxSlab(7,15,0.05), TaxSlab(15,20,0.10),
                               TaxSlab(20,24,0.15), TaxSlab(24,28,0.20), TaxSlab(28,32,0.25), TaxSlab(32,1e9,0.30)]),
    ]
    huf_eq = [EquityTaxChunk(c.year_from, c.year_to, c.tax_rate, c.exempt_limit) for c in ind_eq]

    # 'Other' funds (Gold ETFs, International, etc.): 12.5% LTCG, no exemption
    ind_other = [OtherTaxChunk(1, 30, 0.125)]
    huf_other = [OtherTaxChunk(1, 30, 0.125)]

    state = AppState(
        funds=funds,
        individual_debt_chunks=ind_debt,
        individual_equity_chunks=ind_eq,
        individual_other_chunks=ind_other,
        huf_debt_chunks=huf_debt,
        huf_equity_chunks=huf_eq,
        huf_other_chunks=huf_other,
        return_chunks=[ReturnChunk(1, 30, 0.07)],
        split_chunks=[],   # will be auto-derived from fund allocation
        annual_requirements={1: 20.0},
        huf_withdrawal_chunks=[
            HUFWithdrawalChunk(1, 10, 0.0),
            HUFWithdrawalChunk(11, 20, 5.0),
            HUFWithdrawalChunk(21, 30, 10.0),
        ],
        fd_rate_chunks=[FDRateChunk(1, 30, 0.07)],
        fd_rate=0.07,
    )
    return state
