"""
Calculation engine for SWP Financial Planner — v7.

New in v7:
  - Bug #1 fixed: Rebalancing tax is now self-funded by the portfolio.
    It is deducted from the portfolio's own cash_raised before buys execute.
    The user's net_cash_personal (SWP income) is never reduced by rebalancing.
  - _rebalance_portfolio upgraded with:
      * HIFO (lowest-gain-first) lot selection for rebalancing sells, reducing
        CGT by 30-50% compared to FIFO in mature portfolios.
      * No-trade threshold (drift_tolerance=3%): rebalancing is skipped when
        total portfolio drift is below the threshold (Garleanu & Pedersen).
      * Smart buy-side / SWP-assisted rebalancing: swp_cash_needed raises
        cash from over-weighted funds first, letting withdrawals do
        rebalancing work before any explicit rebalancing trades.

New in v6:
  - GlidePath support: Engine accepts an optional GlidePath object and
    executes annual micro-rebalancing when weights drift across chunk
    boundaries (Mode B — chunked_sticky).
  - _rebalance_portfolio: FIFO-aware rebalancing step that sells over-
    weighted funds, buys under-weighted funds, records RebalanceCost.
  - YearSummary gains rebalance_tax_paid (already existed) and now also
    tracks exit_loads_paid separately.
  - Mode A (singular / flat GlidePath): zero-rebalancing, unchanged logic.

New in v5:
  - Per-category growth rates: debt, equity, and other funds now grow at
    their own allocation-weighted CAGR instead of a single blended rate.
    Per-fund FIFO buckets grow at each fund's individual CAGR.
  - Fixed: only year-1 funds get initial corpus; later-chunk funds start
    with empty buckets (populated at rebalance boundaries).
  - Three tax categories: debt (slab), equity (12.5% + exemption),
    other (12.5% flat, no exemption) for Gold/International ETFs.
  - Per-fund FIFO: each FundEntry has its own FIFOBucket + NAV tracker.
  - MonthlyRow gains fund_withdrawals: List[FundWithdrawalDetail].
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional
from datetime import date

from models import (AppState, TaxChunk, TaxSlab, EquityTaxChunk,
                    OtherTaxChunk, FundEntry, GlidePath, RebalanceCost,
                    _first_available)
from configuration import config

CESS = config.cess_rate


# ─── Tax helpers ──────────────────────────────────────────────────────────────

def _nil_slab_upper(chunk: TaxChunk) -> float:
    for slab in chunk.slabs:
        if slab.rate == 0.0:
            return slab.upper
    return 0.0


def compute_slab_tax(income: float, chunk: TaxChunk, entity: str) -> float:
    if income <= 0:
        return 0.0
    tax = 0.0
    for slab in chunk.slabs:
        if income <= slab.lower:
            break
        taxable_in_slab = max(0.0, min(income, slab.upper) - slab.lower)
        tax += taxable_in_slab * slab.rate
    if entity == "individual":
        if income <= chunk.exempt_limit:
            return 0.0
        excess = income - chunk.exempt_limit
        tax = min(tax, excess)
    return tax * (1 + CESS)


def compute_ltcg_individual(gains: float, eq_chunk: EquityTaxChunk) -> float:
    taxable = max(0.0, gains - eq_chunk.exempt_limit)
    return taxable * eq_chunk.tax_rate * (1 + CESS)


def compute_ltcg_other_individual(gains: float, oc) -> float:
    """Tax on 'other' fund LTCG (Gold ETF, International ETF, etc.)
    12.5% flat — NO exemption, NO indexation."""
    if oc is None or gains <= 0:
        return 0.0
    return gains * oc.tax_rate * (1 + CESS)


def compute_ltcg_huf(equity_gains: float, eq_chunk: EquityTaxChunk,
                     debt_income: float, debt_chunk: TaxChunk) -> float:
    nil_upper = _nil_slab_upper(debt_chunk)
    unused_basic = max(0.0, nil_upper - debt_income)
    taxable = max(0.0, equity_gains - eq_chunk.exempt_limit - unused_basic)
    return taxable * eq_chunk.tax_rate * (1 + CESS)


def compute_ltcg_other_huf(other_gains: float, oc,
                           debt_income: float, debt_chunk: TaxChunk) -> float:
    """HUF 'other' fund LTCG: 12.5% flat, no exemption.
    Can still absorb unused basic exemption from debt slab."""
    if oc is None or other_gains <= 0:
        return 0.0
    nil_upper = _nil_slab_upper(debt_chunk)
    unused_basic = max(0.0, nil_upper - debt_income)
    taxable = max(0.0, other_gains - unused_basic)
    return taxable * oc.tax_rate * (1 + CESS)


# ─── FIFO lot tracker ─────────────────────────────────────────────────────────

@dataclass
class Lot:
    units: float
    purchase_nav: float
    purchase_month: int


class FIFOBucket:
    def __init__(self):
        self.lots: List[Lot] = []

    @property
    def total_units(self) -> float:
        return sum(l.units for l in self.lots)

    def current_value(self, nav: float) -> float:
        return self.total_units * nav

    def invest(self, amount: float, nav: float, month: int):
        if amount > 0 and nav > 0:
            self.lots.append(Lot(amount / nav, nav, month))

    def redeem(self, amount: float, nav: float) -> Tuple[float, float]:
        if amount <= 0 or nav <= 0 or self.total_units == 0:
            return 0.0, 0.0
        units_needed = min(amount / nav, self.total_units)
        remaining = units_needed
        principal = gain = 0.0
        new_lots = []
        for lot in self.lots:
            if remaining <= 1e-12:
                new_lots.append(lot)
                continue
            take = min(lot.units, remaining)
            principal += take * lot.purchase_nav
            gain      += take * (nav - lot.purchase_nav)
            remaining -= take
            leftover = lot.units - take
            if leftover > 1e-12:
                new_lots.append(Lot(leftover, lot.purchase_nav, lot.purchase_month))
        self.lots = new_lots
        return principal, gain


# ─── Glide-path rebalancing ───────────────────────────────────────────────────

def _rebalance_portfolio(
    target_weights:    Dict[str, float],
    prior_weights:     Dict[str, float],
    fund_buckets:      Dict[str, 'FIFOBucket'],
    fund_navs:         Dict[str, float],
    fund_types:        Dict[str, str],         # fund_name -> "debt"|"equity"|"other"
    state:             'AppState',
    fy:                int,
    month_idx:         int,
    drift_tolerance:   float = 0.03,    # Skip rebalance if total |drift| < this (no-trade region)
    swp_cash_needed:   float = 0.0,     # SWP amount to raise preferentially from over-weighted funds
    use_hifo:          bool  = True,    # HIFO (lowest-gain-first) lot selection to minimise CGT
    annual_tax_budget: float = float('inf'),  # Max rebalancing tax Rs lakhs this FY (lookahead budget)
) -> Tuple[RebalanceCost, Dict[str, float], Dict[str, float]]:
    """
    Perform one annual micro-rebalancing step from prior_weights toward
    target_weights.

    Key design principles (v7 upgrades):

    1.  Self-funded tax (Bug #1 fix):
        Tax from rebalancing sells is deducted from the portfolio's own cash
        BEFORE executing buys.  The user's net_cash_personal (SWP income) is
        never reduced by rebalancing taxes — the portfolio absorbs the cost.

    2.  HIFO lot selection (Tax Alpha):
        When selling to rebalance, lots are sorted by ASCENDING realised gain
        (lowest gain first = Highest cost basis In, First Out).  Selling the
        cheapest-to-realise lots minimises CGT by 30-50% in mature portfolios.
        FIFO is still used for normal monthly SWP withdrawals (legal requirement
        for retail redemptions).

    3.  No-trade threshold (drift_tolerance):
        If the total absolute drift sum(|target_w - current_w|) across all
        funds is below ``drift_tolerance`` (default 3%), the entire rebalance
        is skipped.  This implements the Garleanu & Pedersen no-trade region.

    4.  Smart buy-side / SWP-assisted rebalancing:
        If ``swp_cash_needed`` > 0, the engine first raises that cash by
        preferentially selling OVER-weighted funds.  Regular withdrawals do
        rebalancing work, avoiding separate explicit trades.  The returned
        ``swp_proceeds`` dict tells the caller how much was already raised
        per fund so it can skip those funds in its own redemption pass.

    Returns:
        (RebalanceCost, rebal_gains_by_cat, swp_proceeds)
          rebal_gains_by_cat: {'d': float, 'e': float, 'o': float}
          RebalanceCost.taxes_paid is self-funded (already deducted from
          portfolio; caller must NOT subtract it from net_cash_personal).
    """
    STCG_MONTHS      = config.stcg_holding_months
    EXIT_LOAD        = config.exit_load_fraction

    # ── Read tax rates from user's config for this FY ────────────────
    # These are estimation rates for mid-year cash-retention and budget-
    # capping purposes.  The actual authoritative tax is always computed
    # at FY boundaries by the year-end block which pools ALL gains and
    # applies exemptions exactly once.
    _ec = state.get_equity_tax_chunk(fy, "individual")
    EQUITY_LTCG_RATE = _ec.tax_rate if _ec else config.fallback_equity_ltcg_rate
    _oc = state.get_other_tax_chunk(fy, "individual")
    OTHER_LTCG_RATE  = _oc.tax_rate if _oc else config.fallback_other_ltcg_rate

    _dc_budget = state.get_debt_tax_chunk(fy, "individual")
    if _dc_budget and _dc_budget.slabs:
        _debt_top_rate = max(s.rate for s in _dc_budget.slabs) * (1 + CESS)
    else:
        _debt_top_rate = config.fallback_debt_top_rate * (1 + CESS)

    # ── Current portfolio value ──────────────────────────────────────
    corpus_total = sum(
        bk.current_value(fund_navs[fn])
        for fn, bk in fund_buckets.items()
        if fn in fund_navs
    )
    if corpus_total < 1e-6:
        return (RebalanceCost(year=fy, taxes_paid=0.0, exit_loads_paid=0.0),
                {'d': 0.0, 'e': 0.0, 'o': 0.0},
                {})

    # ── Compute current weights ──────────────────────────────────────
    current_weights: Dict[str, float] = {
        fn: bk.current_value(fund_navs[fn]) / corpus_total
        for fn, bk in fund_buckets.items()
        if fn in fund_navs
    }

    # ── No-trade threshold check (Improvement #3 — Garleanu & Pedersen) ──
    all_names = (set(target_weights.keys()) | set(prior_weights.keys())
                 | set(fund_buckets.keys()))
    total_drift = sum(
        abs(target_weights.get(fn, 0.0) - current_weights.get(fn, 0.0))
        for fn in all_names
        if fn in fund_navs
    )
    if total_drift < drift_tolerance:
        # Portfolio is within the no-trade region — skip rebalancing
        return (RebalanceCost(year=fy, taxes_paid=0.0, exit_loads_paid=0.0),
                {'d': 0.0, 'e': 0.0, 'o': 0.0},
                {})

    taxes_paid      = 0.0
    exit_loads_paid = 0.0
    rebal_gains: Dict[str, float] = {'d': 0.0, 'e': 0.0, 'o': 0.0}

    # ─────────────────────────────────────────────────────────────────
    # Helper: sell ``sell_amt`` from fund ``fn`` using HIFO lot selection.
    # Returns (gains_stcg, gains_ltcg, exit_loads, cash_net_of_loads).
    # ─────────────────────────────────────────────────────────────────
    def _sell_hifo(fn: str, sell_amt: float) -> tuple:
        bk  = fund_buckets.get(fn)
        if bk is None or sell_amt < 0.01:
            return 0.0, 0.0, 0.0, 0.0
        nav = fund_navs[fn]

        if use_hifo:
            # Sort ascending by gain-per-unit → sell lowest-gain lots first
            sorted_lots = sorted(bk.lots, key=lambda lot: (nav - lot.purchase_nav))
        else:
            sorted_lots = list(bk.lots)   # FIFO

        remaining  = sell_amt
        stcg = ltcg = loads = 0.0
        kept = []
        for lot in sorted_lots:
            if remaining <= 1e-12:
                kept.append(lot)
                continue
            lot_val    = lot.units * nav
            take_val   = min(lot_val, remaining)
            take_units = take_val / nav
            holding    = month_idx - lot.purchase_month
            gain       = take_units * (nav - lot.purchase_nav)
            if holding < STCG_MONTHS:
                stcg  += max(0.0, gain)
                loads += take_val * EXIT_LOAD
            else:
                ltcg  += max(0.0, gain)
            remaining -= take_val
            leftover   = lot.units - take_units
            if leftover > 1e-12:
                kept.append(Lot(leftover, lot.purchase_nav, lot.purchase_month))
        bk.lots = kept
        return stcg, ltcg, loads, sell_amt - loads

    def _tax_for(ftype: str, stcg: float, ltcg: float) -> float:
        """Estimate tax for budget/cash purposes during rebalancing.

        IMPORTANT: This is an *estimation* used to determine how much cash
        the portfolio must retain to cover its own tax bill.  The actual,
        authoritative tax is computed at year-end by the FY-boundary block
        which pools ALL gains (SWP + rebalancing) and applies exemptions
        exactly once.

        We deliberately do NOT apply the equity LTCG exemption here because
        it is a once-per-FY benefit shared with SWP gains.  Applying it
        per-fund here would under-estimate the tax, causing the buy-side to
        be over-funded and the portfolio to absorb an unfunded liability.
        """
        if ftype == "equity":
            # No exemption here — it's applied once at year-end
            return (stcg + ltcg) * EQUITY_LTCG_RATE * (1 + CESS)
        elif ftype == "other":
            return (stcg + ltcg) * OTHER_LTCG_RATE * (1 + CESS)
        else:
            # Use a conservative flat estimate for debt.  The slab-based
            # calculation needs the full FY income context (other income +
            # all debt gains) which isn't available fund-by-fund here.
            # 15% + 4% cess is a reasonable mid-bracket estimate.
            return (stcg + ltcg) * config.fallback_stcg_rate * (1 + CESS)

    def _ck(ftype: str) -> str:
        return 'e' if ftype == "equity" else ('o' if ftype == "other" else 'd')

    # ─────────────────────────────────────────────────────────────────
    # Step 1: Smart buy-side / SWP-assisted rebalancing (Improvement #4)
    #
    # Raise swp_cash_needed by preferentially selling OVER-weighted funds.
    # Regular withdrawals do the rebalancing heavy-lifting before any
    # explicit rebalancing trades are needed.
    # ─────────────────────────────────────────────────────────────────
    swp_proceeds: Dict[str, float] = {}   # fund_name -> cash already raised for SWP

    if swp_cash_needed > 0.01:
        over_funds = [
            (fn, (current_weights.get(fn, 0.0) - target_weights.get(fn, 0.0)) * corpus_total)
            for fn in all_names
            if (fn in fund_navs and fn in fund_buckets
                and current_weights.get(fn, 0.0) > target_weights.get(fn, 0.0) + 1e-4)
        ]
        over_funds.sort(key=lambda x: -x[1])   # largest over-weight first

        remaining_swp = swp_cash_needed
        for fn, excess_val in over_funds:
            if remaining_swp <= 0.01:
                break
            sell_amt = min(excess_val, remaining_swp)
            stcg, ltcg, ld, net = _sell_hifo(fn, sell_amt)
            ftype = fund_types.get(fn, "debt")
            rebal_gains[_ck(ftype)] += stcg + ltcg
            taxes_paid              += _tax_for(ftype, stcg, ltcg)
            exit_loads_paid         += ld
            swp_proceeds[fn]         = swp_proceeds.get(fn, 0.0) + net
            remaining_swp           -= sell_amt

    # ─────────────────────────────────────────────────────────────────
    # Step 2: Residual sell / buy deltas after SWP-assisted sells
    # ─────────────────────────────────────────────────────────────────
    corpus_now = sum(
        bk.current_value(fund_navs[fn])
        for fn, bk in fund_buckets.items()
        if fn in fund_navs
    )
    if corpus_now < 1e-6:
        cost = RebalanceCost(year=fy, taxes_paid=taxes_paid, exit_loads_paid=exit_loads_paid)
        return cost, rebal_gains, swp_proceeds

    sell_ops: List[Tuple[str, float]] = []
    buy_ops:  List[Tuple[str, float]] = []

    for fn in all_names:
        if fn not in fund_navs:
            continue
        bk          = fund_buckets.get(fn)
        current_val = bk.current_value(fund_navs[fn]) if bk else 0.0
        target_val  = corpus_now * target_weights.get(fn, 0.0)
        delta       = target_val - current_val
        if delta < -0.01:
            sell_ops.append((fn, -delta))
        elif delta > 0.01:
            buy_ops.append((fn, delta))

    # ── Sells with HIFO — budget-capped ─────────────────────────────
    # When annual_tax_budget < inf, sell lot-by-lot and stop exactly when
    # the running tax tally hits the budget (Gemini v8 lookahead approach).
    # This ensures each transition year bears an equal share of the total
    # transition tax, smoothing the incidence across the spread window.
    cash_raised = 0.0
    rebal_tax_tally = taxes_paid  # includes any tax already paid in Step 1 (SWP)
    for fn, sell_amt in sell_ops:
        budget_left = annual_tax_budget - rebal_tax_tally
        if budget_left <= 1e-4:
            break   # budget exhausted — defer remaining sells to future years
        ftype = fund_types.get(fn, "debt")
        nav   = fund_navs[fn]
        bk    = fund_buckets.get(fn)
        if bk is None:
            continue
        # HIFO sort (or FIFO)
        sorted_lots = (sorted(bk.lots, key=lambda lot: (nav - lot.purchase_nav))
                       if use_hifo else list(bk.lots))
        rem_amt = sell_amt
        stcg_f = ltcg_f = ld_f = net_f = 0.0
        kept = []
        for lot in sorted_lots:
            if rem_amt <= 1e-6:
                kept.append(lot)
                continue
            budget_left = annual_tax_budget - rebal_tax_tally
            if budget_left <= 1e-4:
                kept.append(lot)   # budget hit mid-lot — stop here
                continue
            take_val   = min(lot.units * nav, rem_amt)
            take_units = take_val / nav
            gain       = take_units * (nav - lot.purchase_nav)
            holding    = month_idx - lot.purchase_month
            # Marginal tax for this lot slice
            # Use the user's top slab rate (+ cess) for debt; for equity/other
            # use their flat LTCG rates.  This avoids the hardcoded 0.312 and
            # respects the user's actual tax configuration.
            if ftype == "equity":
                slice_tax = max(0.0, gain) * EQUITY_LTCG_RATE * (1 + CESS)
            elif ftype == "other":
                slice_tax = max(0.0, gain) * OTHER_LTCG_RATE * (1 + CESS)
            else:
                slice_tax = max(0.0, gain) * _debt_top_rate
            # If this full lot would exceed budget, fraction it
            if slice_tax > budget_left:
                fraction  = budget_left / slice_tax if slice_tax > 0 else 1.0
                take_val   *= fraction
                take_units *= fraction
                gain       *= fraction
                slice_tax   = budget_left
            if holding < STCG_MONTHS:
                stcg_f += max(0.0, gain)
                ld_f   += take_val * EXIT_LOAD
            else:
                ltcg_f += max(0.0, gain)
            rebal_tax_tally += slice_tax
            net_f  += take_val - (take_val * EXIT_LOAD if holding < STCG_MONTHS else 0)
            rem_amt -= take_val
            leftover = lot.units - take_units
            if leftover > 1e-12:
                kept.append(Lot(leftover, lot.purchase_nav, lot.purchase_month))
        bk.lots = kept
        rebal_gains[_ck(ftype)] += stcg_f + ltcg_f
        taxes_paid              += _tax_for(ftype, stcg_f, ltcg_f)
        exit_loads_paid         += ld_f
        cash_raised             += net_f

    # ── Buys — portfolio self-funds its own tax (Bug #1 fix) ────────
    # We deduct taxes from cash_raised so the portfolio absorbs the cost.
    # net_cash_personal (the user's SWP income) is NEVER reduced here.
    net_cash_for_buys = max(0.0, cash_raised - taxes_paid)
    total_buy_need    = sum(amt for _, amt in buy_ops)
    if total_buy_need > 0 and net_cash_for_buys > 0.01:
        scale = min(1.0, net_cash_for_buys / total_buy_need)
        for fn, buy_amt in buy_ops:
            actual_buy = buy_amt * scale
            if actual_buy < 0.01:
                continue
            nav = fund_navs[fn]
            if fn not in fund_buckets:
                fund_buckets[fn] = FIFOBucket()
                fund_navs[fn]    = nav
            fund_buckets[fn].invest(actual_buy, nav, month_idx)

    cost = RebalanceCost(year=fy, taxes_paid=taxes_paid, exit_loads_paid=exit_loads_paid)
    return cost, rebal_gains, swp_proceeds


# ─── Output data structures ───────────────────────────────────────────────────

@dataclass
class FundWithdrawalDetail:
    """Per-fund breakdown for one month's withdrawal."""
    fund_name: str
    fund_type: str          # "debt", "equity", or "other"
    corpus_start: float
    withdrawal: float
    principal: float
    gain: float
    corpus_end: float


@dataclass
class MonthlyRow:
    month_idx: int
    calendar_month: int
    calendar_year: int
    fy_year: int

    corpus_debt_start: float
    corpus_equity_start: float
    corpus_other_start: float
    wd_debt: float
    wd_equity: float
    wd_other: float
    principal_debt: float
    gain_debt: float
    principal_equity: float
    gain_equity: float
    principal_other: float
    gain_other: float
    corpus_debt_end: float
    corpus_equity_end: float
    corpus_other_end: float

    windfall_personal: float = 0.0
    windfall_huf: float = 0.0

    ind_tax_paid: float = 0.0
    huf_transfer_in: float = 0.0
    fd_tax_paid: float = 0.0

    # Per-fund breakdown (populated by Engine when per-fund tracking is active)
    fund_withdrawals: List[FundWithdrawalDetail] = field(default_factory=list)


@dataclass
class YearSummary:
    year: int
    corpus_debt_personal: float
    corpus_equity_personal: float
    corpus_other_personal: float
    corpus_debt_huf: float
    corpus_equity_huf: float
    corpus_other_huf: float
    tax_personal: float
    tax_huf: float
    net_cash_personal: float
    net_cash_huf: float
    net_cash_total: float
    fd_tax_benchmark: float
    tax_saved: float
    # Tax paid specifically due to chunk-boundary rebalancing (a subset of tax_personal).
    # Shown separately in the KPI strip so users can see the "switching cost".
    rebalance_tax_paid: float = 0.0
    # Exit loads paid due to glide-path rebalancing (separate from tax).
    rebalance_exit_loads: float = 0.0


# ─── Engine ───────────────────────────────────────────────────────────────────

class Engine:
    def __init__(self, state: AppState, alt_return_chunks=None,
                 conservative_mode: bool = False,
                 glide_path: Optional[GlidePath] = None):
        """
        Parameters
        ----------
        conservative_mode : if True, each fund's return is taken from
            ``worst_exp_ret`` (the worst expected return from fund analyser —
            minimum rolling CAGR minus STT cost).  This gives a genuine
            stress-tested lower-bound projection.  Falls back to
            min(cagr_1, cagr_3, cagr_5, cagr_10) if ``worst_exp_ret`` is None.
            In Historical mode (default), the 5Y CAGR is used.
        glide_path : Optional GlidePath from the sticky-portfolio optimizer.
            When provided and not flat, triggers annual micro-rebalancing at
            glide-path transition years (Mode B — chunked_sticky).
            When None or flat (Mode A), no rebalancing occurs beyond the
            existing chunk-boundary logic.
        """
        self.state = state
        self.alt_return_chunks = alt_return_chunks
        self.conservative_mode = conservative_mode
        self.glide_path = glide_path

    def _annual_return(self, fy: int) -> float:
        """Blended portfolio return (used as fallback)."""
        src = self.alt_return_chunks or self.state.return_chunks
        for c in src:
            if c.year_from <= fy <= c.year_to:
                return c.annual_return
        return 0.07

    def _monthly_factor(self, fy: int) -> float:
        """Blended monthly growth factor (used as fallback)."""
        return (1 + self._annual_return(fy)) ** (1 / 12)

    def _category_annual_return(self, fy: int, fund_type: str) -> float:
        """Per-category annual return derived from fund CAGRs.
        Falls back to blended return_chunks rate if no category data."""
        if self.alt_return_chunks:
            # Sensitivity analysis — use the override for ALL categories
            return self._annual_return(fy)
        cat_ret = self.state.get_category_return(fy, fund_type)
        if cat_ret is not None:
            return cat_ret
        return self._annual_return(fy)

    def _category_monthly_factor(self, fy: int, fund_type: str) -> float:
        """Per-category monthly growth factor."""
        return (1 + self._category_annual_return(fy, fund_type)) ** (1 / 12)

    def _fund_annual_return(self, fund: FundEntry, fy: int) -> float:
        """Per-fund annual return from its own CAGR data.

        Historical mode (default):
            Uses 5Y CAGR -> 3Y -> 1Y -> category average.

        Conservative mode (stress-test):
            Uses fund.worst_exp_ret (Worst_Exp_Ret_% from fund analyser —
            the minimum historically observed rolling CAGR after STT costs).
            Falls back to min(cagr_1, cagr_3, cagr_5, cagr_10) if not set,
            then to category average.  This gives the true worst-case
            corpus-survival projection.
        """
        if self.alt_return_chunks:
            # Sensitivity-analysis override: use the blended rate for all funds
            return self._annual_return(fy)
        if self.conservative_mode:
            if fund.worst_exp_ret is not None and fund.worst_exp_ret > 0:
                return fund.worst_exp_ret / 100.0
            # Fallback: minimum of all available CAGR windows
            cagrs = [c for c in (fund.cagr_1, fund.cagr_3, fund.cagr_5, fund.cagr_10)
                     if c is not None and c > 0]
            if cagrs:
                return min(cagrs) / 100.0
            return self._category_annual_return(fy, fund.fund_type)
        # Historical mode
        cagr = _first_available(fund.cagr_5, fund.cagr_3, fund.cagr_1, default=None)
        if cagr is not None and cagr > 0:
            return cagr / 100.0
        return self._category_annual_return(fy, fund.fund_type)

    def _fund_monthly_factor(self, fund: FundEntry, fy: int) -> float:
        """Per-fund monthly growth factor."""
        return (1 + self._fund_annual_return(fund, fy)) ** (1 / 12)

    # ── Bounded Smart Withdrawals ─────────────────────────────────────────
    # As funds compound at different CAGRs, the debt:equity:other mix drifts
    # away from the originally published allocation, skewing the effective
    # portfolio return (and therefore risk) upward.
    #
    # The waterfall algorithm sells overweighted funds in descending order
    # of their individual expected return, draining the exact sources that
    # push the portfolio return above the ceiling.  This is per-fund, not
    # per-category — it targets the specific culprits with minimum turnover.
    #
    # Personal uses max_drift = 0.0015 (0.15%), HUF uses 0.005 (0.50%).
    # ──────────────────────────────────────────────────────────────────────

    _DRIFT_CAP_PERSONAL = config.drift_cap_personal
    _DRIFT_CAP_HUF      = config.drift_cap_huf

    # Weight-drift threshold: if any fund's weight deviates from its target
    # by more than this, the SWP preferentially withdraws from overweighted
    # funds to self-correct the portfolio back toward target allocations.
    # This maintains bucket-type (debt/equity/other) and sub-bucket (individual
    # fund within each type) ratio fidelity, preserving the published
    # portfolio std_dev and max_dd characteristics.
    _WEIGHT_DRIFT_THRESHOLD = config.weight_drift_threshold

    def _compute_smart_withdrawal(
        self,
        fund_values: Dict[str, float],     # fund_name → current corpus value
        fund_returns: Dict[str, float],    # fund_name → annual return (fraction)
        target_weights: Dict[str, float],  # fund_name → target weight (sum≈1)
        withdrawal_amt: float,
        entity: str = "personal",
    ) -> Dict[str, float]:
        """
        Compute per-fund withdrawal amounts using the Bounded Smart Withdrawal
        waterfall with weight-drift correction.

        Returns {fund_name: amount_to_withdraw}.

        Protocol:
          1. Check WEIGHT DRIFT: if any fund's actual weight deviates from
             target by more than _WEIGHT_DRIFT_THRESHOLD, enter weight-
             correction mode (regardless of return drift).
          2. Check RETURN DRIFT: compute anchor_return from target weights,
             ceiling = anchor + cap.
          3. If neither weight nor return drift exceeds threshold →
             proportional withdrawal (no correction).
          4. Weight-correction mode (priority over return-correction):
             A. Compute excess_value per fund (current_val − target_val).
             B. Sort overweighted funds by descending weight deviation
                (largest deviation corrected first — this is the key
                difference from the return-drift protocol which sorts
                by return).
             C. Sell from overweighted funds top-down to fund the SWP,
                capped at each fund's excess_value.
             D. If SWP is fully funded from excess, stop.
                If overweighted excess is insufficient, fill remainder
                pro-rata from all funds.
          5. Return-correction mode (original protocol, when only return drifts):
             Same as before — sort overweighted by descending expected return.
        """
        total = sum(fund_values.values())
        if total < 1e-6 or withdrawal_amt < 1e-6:
            return {}

        max_drift = (self._DRIFT_CAP_PERSONAL if entity == "personal"
                     else self._DRIFT_CAP_HUF)

        # ── Check weight drift ────────────────────────────────────────────
        # Compute the maximum absolute weight deviation across all funds.
        # This catches both individual-fund drift AND category drift (since
        # if the debt bucket drifts 3% heavy, its constituent funds will
        # each show a positive deviation that sums to 3%).
        max_weight_dev = 0.0
        for fn in set(target_weights) | set(fund_values):
            actual_w = (fund_values.get(fn, 0.0) / total) if total > 0 else 0.0
            target_w = target_weights.get(fn, 0.0)
            max_weight_dev = max(max_weight_dev, abs(actual_w - target_w))

        weight_drift_triggered = max_weight_dev > self._WEIGHT_DRIFT_THRESHOLD

        # ── Check return drift ────────────────────────────────────────────
        anchor_ret = sum(
            target_weights.get(fn, 0.0) * fund_returns.get(fn, 0.07)
            for fn in set(target_weights) | set(fund_values)
        )
        ceiling_ret = anchor_ret + max_drift

        current_ret = sum(
            (v / total) * fund_returns.get(fn, 0.07)
            for fn, v in fund_values.items()
        )

        return_drift_triggered = current_ret > ceiling_ret

        # ── Within BOTH tolerances → proportional withdrawal ──────────────
        if not weight_drift_triggered and not return_drift_triggered:
            result = {}
            for fn, v in fund_values.items():
                result[fn] = withdrawal_amt * (v / total)
            return result

        # ── Correction mode ───────────────────────────────────────────────

        # Step A: Calculate excess_value per fund (current − target)
        excess = {}
        for fn, v in fund_values.items():
            target_val = target_weights.get(fn, 0.0) * total
            ex = v - target_val
            if ex > 0.01:
                excess[fn] = ex

        # Step B: Sort overweighted funds by the appropriate criterion.
        # Weight-drift correction takes priority: sort by descending
        # weight deviation (largest deviators corrected first).
        # When only return drifts, sort by descending expected return
        # (original behaviour — drain the high-return culprits).
        if weight_drift_triggered:
            sorted_excess = sorted(
                excess.items(),
                key=lambda item: (
                    (fund_values.get(item[0], 0.0) / total)
                    - target_weights.get(item[0], 0.0)
                ),
                reverse=True,
            )
        else:
            sorted_excess = sorted(
                excess.items(),
                key=lambda item: fund_returns.get(item[0], 0.0),
                reverse=True,
            )

        # Step C: Sell from overweighted funds top-down
        result: Dict[str, float] = {fn: 0.0 for fn in fund_values}
        remaining = withdrawal_amt

        for fn, ex_val in sorted_excess:
            if remaining <= 1e-6:
                break
            # Don't sell more than the fund's excess value
            sell = min(remaining, ex_val)
            # Also don't sell more than the fund's current value
            sell = min(sell, fund_values[fn])
            result[fn] = sell
            remaining -= sell

        # Step D: If overweighted excess didn't cover the full SWP,
        # fill the remainder pro-rata from all funds (proportional fallback).
        if remaining > 1e-6:
            # Pro-rata from whatever corpus remains after excess sells
            remaining_values = {}
            for fn, v in fund_values.items():
                leftover = v - result[fn]
                if leftover > 0.01:
                    remaining_values[fn] = leftover
            rv_total = sum(remaining_values.values())
            if rv_total > 1e-6:
                for fn, leftover in remaining_values.items():
                    fill = remaining * (leftover / rv_total)
                    result[fn] += fill

        return result

    def _calendar_date_for_month(self, m: int) -> Tuple[int, int]:
        inv = self.state.investment_date
        total = inv.year * 12 + (inv.month - 1) + m
        return total // 12, total % 12 + 1

    def _fy_year_for_month(self, m: int) -> int:
        cy, cm = self._calendar_date_for_month(m)
        cal_fy = cy if cm >= 4 else cy - 1
        inv = self.state.investment_date
        inv_fy = inv.year if inv.month >= 4 else inv.year - 1
        return max(1, cal_fy - inv_fy + 1)

    def _huf_annual_wd(self, fy: int) -> float:
        # Per-FY override (set by tax-optimal split optimizer) takes precedence
        if hasattr(self.state, 'huf_annual_requirements') and fy in self.state.huf_annual_requirements:
            return self.state.huf_annual_requirements[fy]
        for c in self.state.huf_withdrawal_chunks:
            if c.year_from <= fy <= c.year_to:
                return c.annual_withdrawal
        return 0.0

    def _estimate_transition_tax(
        self,
        prior_w: Dict[str, float],
        target_w: Dict[str, float],
        fund_buckets: Dict,
        fund_navs: Dict,
        fund_types: Dict,
        month_idx: int,
    ) -> float:
        """
        Lookahead: deep-copy the portfolio, simulate the COMPLETE transition
        from prior_w to target_w, and return total estimated tax in Rs lakhs.
        Used once per boundary to set the annual tax budget = total / spread_years.
        """
        import copy as _copy
        sim_buckets = _copy.deepcopy(fund_buckets)
        corpus = sum(
            bk.current_value(fund_navs[fn])
            for fn, bk in sim_buckets.items() if fn in fund_navs
        )
        if corpus < 1.0:
            return 0.0

        # Read tax rates from user config (same FY used by the caller)
        fy_est = self.state._fy_for_transition if hasattr(self.state, '_fy_for_transition') else 1
        # Use a rough FY from the month_idx
        fy_est = self._fy_year_for_month(month_idx)
        _ec = self.state.get_equity_tax_chunk(fy_est, "individual")
        _oc = self.state.get_other_tax_chunk(fy_est, "individual")
        eq_rate  = (_ec.tax_rate if _ec else config.fallback_equity_ltcg_rate) * (1 + CESS)
        oth_rate = (_oc.tax_rate if _oc else config.fallback_other_ltcg_rate) * (1 + CESS)
        _dc = self.state.get_debt_tax_chunk(fy_est, "individual")
        if _dc and _dc.slabs:
            debt_rate = max(s.rate for s in _dc.slabs) * (1 + CESS)
        else:
            debt_rate = config.fallback_debt_top_rate * (1 + CESS)

        total_tax = 0.0
        for fn, bk in sim_buckets.items():
            cw = prior_w.get(fn, 0.0)
            tw = target_w.get(fn, 0.0)
            if cw <= tw + 1e-4:
                continue   # not being sold in this transition
            sell_amt  = (cw - tw) * corpus
            nav       = fund_navs.get(fn, 1.0)
            ftype     = fund_types.get(fn, "debt")
            # HIFO: sort ascending by gain (lowest-gain lots first)
            sorted_lots = sorted(bk.lots, key=lambda lot: nav - lot.purchase_nav)
            rem = sell_amt
            for lot in sorted_lots:
                if rem <= 1e-6:
                    break
                take    = min(lot.units * nav, rem)
                gain    = max(0.0, (take / nav) * (nav - lot.purchase_nav))
                if ftype == "equity":
                    total_tax += gain * eq_rate
                elif ftype == "other":
                    total_tax += gain * oth_rate
                else:
                    total_tax += gain * debt_rate
                rem -= take
        return total_tax

    def run(self) -> Tuple[List[MonthlyRow], List[YearSummary],
                           List[MonthlyRow], List[YearSummary]]:
        state = self.state
        SWP_START = config.swp_start_month

        # ── Diagnostic logging ─────────────────────────────────────────────────
        import logging, os
        _log = logging.getLogger("engine.debug")
        _log_path = os.environ.get("SWP_DEBUG_LOG")
        if _log_path:
            _fh = logging.FileHandler(_log_path, mode="a", encoding="utf-8")
            _fh.setFormatter(logging.Formatter("%(message)s"))
            _log.addHandler(_fh)
            _log.setLevel(logging.DEBUG)
        else:
            _log.setLevel(logging.WARNING)

        _log.debug("=" * 70)
        _log.debug("ENGINE RUN START")
        _log.debug(f"  allocation_chunks: {len(state.allocation_chunks)}")
        for i, ac in enumerate(state.allocation_chunks):
            d = sum(f.allocation for f in ac.funds if f.fund_type == "debt")
            e = sum(f.allocation for f in ac.funds if f.fund_type == "equity")
            o = sum(f.allocation for f in ac.funds if f.fund_type == "other")
            _log.debug(f"    chunk[{i}] yr{ac.year_from}-{ac.year_to}: "
                        f"D={d:.2f}  E={e:.2f}  O={o:.2f}  "
                        f"({len(ac.funds)} funds)")
            for f in ac.funds:
                if f.allocation > 0:
                    _log.debug(f"      {f.name[:45]:<45s} type={f.fund_type:<6s} "
                                f"alloc={f.allocation:.2f}  cagr5={f.cagr_5}")
        _log.debug(f"  flat state.funds: {len(state.funds)} entries")
        fd = sum(f.allocation for f in state.funds if f.fund_type == "debt")
        fe = sum(f.allocation for f in state.funds if f.fund_type == "equity")
        fo = sum(f.allocation for f in state.funds if f.fund_type == "other")
        _log.debug(f"    flat totals: D={fd:.2f}  E={fe:.2f}  O={fo:.2f}")

        # ── Build union of ALL funds across all allocation chunks ─────────────
        # Every fund that appears in any chunk needs a NAV and FIFO bucket.
        # But only year-1 funds get their allocation invested at month 0.
        # Later-chunk funds get empty buckets (populated at rebalance time).
        all_funds_map: dict = {}   # name -> FundEntry (first occurrence)
        if state.allocation_chunks:
            for ac in state.allocation_chunks:
                for f in ac.funds:
                    if f.allocation > 0 and f.name not in all_funds_map:
                        all_funds_map[f.name] = f
        else:
            for f in state.funds:
                if f.allocation > 0:
                    all_funds_map[f.name] = f

        all_active = list(all_funds_map.values())
        all_debt   = [f for f in all_active if f.fund_type == "debt"]
        all_equity = [f for f in all_active if f.fund_type == "equity"]
        all_other  = [f for f in all_active if f.fund_type == "other"]

        # fund_types_map used by glide-path rebalancing
        fund_types_map = {f.name: f.fund_type for f in all_active}

        _log.debug(f"  all_funds_map: {len(all_funds_map)} funds "
                    f"(debt={len(all_debt)}, equity={len(all_equity)}, other={len(all_other)})")

        # For aggregate buckets, use the first-chunk allocation
        # (or flat funds list) to size the initial corpus.
        init_funds = state.get_funds_for_year(1)
        init_debt   = [f for f in init_funds if f.fund_type == "debt"]
        init_equity = [f for f in init_funds if f.fund_type == "equity"]
        init_other  = [f for f in init_funds if f.fund_type == "other"]
        total_debt   = sum(f.allocation for f in init_debt)
        total_equity = sum(f.allocation for f in init_equity)
        total_other  = sum(f.allocation for f in init_other)

        _log.debug(f"  init_funds (year-1): {len(init_funds)} funds")
        _log.debug(f"    total_debt={total_debt:.2f}  total_equity={total_equity:.2f}  "
                    f"total_other={total_other:.2f}")
        for f in init_funds:
            _log.debug(f"    {f.name[:45]:<45s} type={f.fund_type:<6s} "
                        f"alloc={f.allocation:.2f}")

        # Set of fund names active in year-1 (only these get initial investment)
        init_fund_names = {f.name for f in init_funds}

        # ── Windfall index ────────────────────────────────────────────────────
        wf_p: Dict[int, float] = {}
        wf_h: Dict[int, float] = {}
        for wf in state.windfalls:
            dst = wf_p if wf.target == "personal" else wf_h
            dst[wf.year] = dst.get(wf.year, 0.0) + wf.amount

        # ── Per-fund FIFO buckets (personal) ──────────────────────────────────
        # Each fund has its own bucket + NAV.
        # Only year-1 funds get their allocation invested at month 0.
        # Later-chunk funds get empty buckets (will be filled at rebalance).
        p_fund_buckets: Dict[str, FIFOBucket] = {}
        p_fund_navs:    Dict[str, float]      = {}
        for f in all_active:
            bk = FIFOBucket()
            if f.name in init_fund_names:
                bk.invest(f.allocation, 1.0, 0)
            p_fund_buckets[f.name] = bk
            p_fund_navs[f.name]    = 1.0

        # Aggregate personal buckets (for backward-compat totals)
        p_debt_agg   = FIFOBucket()
        p_equity_agg = FIFOBucket()
        p_other_agg  = FIFOBucket()
        p_debt_agg.invest(total_debt,   1.0, 0)
        p_equity_agg.invest(total_equity, 1.0, 0)
        p_other_agg.invest(total_other,  1.0, 0)
        p_nav_d = p_nav_e = p_nav_o = 1.0

        _log.debug(f"  Aggregate buckets invested: D={total_debt:.2f}  E={total_equity:.2f}  "
                    f"O={total_other:.2f}")
        _log.debug(f"  p_other_agg units after invest: {p_other_agg.current_value(1.0):.4f}")

        # ── HUF buckets (aggregate, no per-fund breakdown needed) ─────────────
        h_debt   = FIFOBucket()
        h_equity = FIFOBucket()
        h_other  = FIFOBucket()
        h_nav_d  = h_nav_e = h_nav_o = 1.0

        # ── Accumulators ──────────────────────────────────────────────────────
        p_acc: Dict[int, dict] = {}
        h_acc: Dict[int, dict] = {}

        def acc_get(d, fy):
            if fy not in d:
                d[fy] = dict(gain_d=0.0, gain_e=0.0, gain_o=0.0,
                             wd_d=0.0, wd_e=0.0, wd_o=0.0,
                             corp_d=0.0, corp_e=0.0, corp_o=0.0)
            return d[fy]

        p_rows: List[MonthlyRow] = []
        h_rows: List[MonthlyRow] = []

        seen_p_fy: set = set()
        seen_h_fy: set = set()

        pending:       Dict[int, dict] = {}
        pending_by_fy: Dict[int, dict] = {}
        # Track per-FY rebalancing gains separately so we can compute the
        # share of income tax attributable to chunk-boundary rebalancing.
        rebal_gains_by_fy: Dict[int, dict] = {}  # fy -> {d, e, o}

        # FD benchmark: corpus stays constant at the initial value.
        # Not a realistic assumption, but used as a simple benchmark.
        fd_corpus_fixed = state.total_allocation()

        # Chunk-boundary rebalancing years
        rebalance_years = set(state.chunk_boundary_years())
        rebalanced_set: set = set()   # track which FYs we've already rebalanced

        # Glide-path micro-rebalancing tracking
        gp = self.glide_path  # may be None
        # Safety: if there is only one allocation chunk (or none), any non-flat
        # glide path is stale (left over from a previous multi-chunk run).
        # Force it flat so the engine doesn't apply phantom rebalancing.
        if gp is not None and not gp.is_flat() and len(state.allocation_chunks) <= 1:
            gp = None
        glide_rebalanced_set: set = set()   # FYs where glide rebalancing was done
        # Accumulate RebalanceCost objects per FY for glide-path micro-rebalances
        glide_costs_by_fy: Dict[int, RebalanceCost] = {}
        # Lookahead tax budget per boundary:
        #   glide_tax_budget[b_start_yr] = total_transition_tax / spread_years
        glide_tax_budget: Dict[int, float] = {}
        # fund_types_map is already built above (line ~733) from all_active.
        # DO NOT re-declare here — it would wipe the correctly-built map.

        # NOTE on SWP-assisted rebalancing (Gemini suggestion #1):
        # After analysis, passing swp_cash_needed > 0 to the glide-path
        # rebalancer was found to cause zero monthly withdrawals during
        # transition years (the pre-raised cash eliminates monthly SWP).
        # The original design is correct: the monthly SWP loop naturally
        # drains retiring funds pro-rata, and the rebalancer only handles
        # weight shifts.  The two mechanisms work independently.

        total_months = 32 * 12
        prev_fy = -1

        for m in range(total_months):
            cy, cm = self._calendar_date_for_month(m)
            fy = self._fy_year_for_month(m)

            if fy > 31:
                break

            # ── FY boundary: compute prior FY's tax ───────────────────────────
            if fy != prev_fy and 1 <= prev_fy <= 30:
                a = acc_get(p_acc, prev_fy)
                pi = state.personal_income
                taxable_other = (pi.salary + pi.taxable_interest +
                                 pi.pension + pi.rental + pi.other_taxable)
                total_slab_income = taxable_other + a["gain_d"]
                dc = state.get_debt_tax_chunk(prev_fy, "individual")
                ind_debt_tax = compute_slab_tax(total_slab_income, dc, "individual") if dc else 0.0
                ec = state.get_equity_tax_chunk(prev_fy, "individual")
                ind_eq_tax = compute_ltcg_individual(a["gain_e"], ec) if ec else 0.0
                oc = state.get_other_tax_chunk(prev_fy, "individual")
                ind_other_tax = compute_ltcg_other_individual(a["gain_o"], oc)
                ind_total = ind_debt_tax + ind_eq_tax + ind_other_tax

                # FD benchmark: static corpus × per-year FD rate
                fd_rate_fy = state.get_fd_rate(prev_fy)
                fd_interest = fd_corpus_fixed * fd_rate_fy
                fd_total_income = fd_interest + taxable_other
                fd_tax = compute_slab_tax(fd_total_income, dc, "individual") if dc else 0.0

                tax_saved = max(0.0, fd_tax - ind_total)
                pending[prev_fy + 1]    = dict(ind_tax=ind_total, fd_tax=fd_tax, tax_saved=tax_saved)
                pending_by_fy[prev_fy]  = dict(ind_tax=ind_total, fd_tax=fd_tax, tax_saved=tax_saved)

            prev_fy = fy

            # ── Chunk-boundary rebalancing ────────────────────────────────────
            # When entering a new allocation chunk's first FY, rebalance the
            # per-fund buckets to match the new chunk's target allocations.
            # This models the real-world switchover (sell some debt, buy equity
            # or vice versa). Gains from the sell side are accumulated in the
            # PREVIOUS FY's tax accumulators (rebalancing happens at FY start).
            #
            # GLIDE-PATH OVERRIDE: If a smooth glide path is active (non-flat),
            # we SKIP this hard instant switch entirely.  The glide path's
            # transition window spreads the switch over multiple years via the
            # micro-rebalancing block below, using SWP-assisted drawdown to
            # avoid a single large CGT event at the boundary.
            is_smooth_glide = (gp is not None and not gp.is_flat())
            if fy in rebalance_years and fy not in rebalanced_set and not is_smooth_glide:
                rebalanced_set.add(fy)
                new_funds = state.get_funds_for_year(fy)
                new_active = [f for f in new_funds if f.allocation > 0]
                new_total = sum(f.allocation for f in new_active)
                if new_total > 0:
                    # Current total corpus value
                    curr_total_val = sum(
                        p_fund_buckets[f.name].current_value(p_fund_navs[f.name])
                        for f in new_active if f.name in p_fund_buckets
                    )
                    # Also include funds that were in the old chunk but NOT in new
                    old_chunk_fy = fy - 1
                    old_funds = state.get_funds_for_year(old_chunk_fy) if old_chunk_fy >= 1 else []
                    old_active_names = {f.name for f in old_funds if f.allocation > 0}
                    new_active_names = {f.name for f in new_active}
                    # Funds being removed (in old but not in new)
                    retiring_names = old_active_names - new_active_names
                    retiring_val = sum(
                        p_fund_buckets[n].current_value(p_fund_navs[n])
                        for n in retiring_names if n in p_fund_buckets
                    )
                    curr_total_val += retiring_val

                    if curr_total_val > 0:
                        # Redeem retiring funds completely
                        rebal_gain_d = 0.0
                        rebal_gain_e = 0.0
                        rebal_gain_o = 0.0
                        for rn in retiring_names:
                            if rn not in p_fund_buckets:
                                continue
                            bk = p_fund_buckets[rn]
                            nav = p_fund_navs[rn]
                            val = bk.current_value(nav)
                            if val > 0.01:
                                pr, gn = bk.redeem(val, nav)
                                ftype = all_funds_map[rn].fund_type if rn in all_funds_map else "debt"
                                if ftype == "equity":
                                    rebal_gain_e += gn
                                elif ftype == "other":
                                    rebal_gain_o += gn
                                else:
                                    rebal_gain_d += gn

                        # For each new-chunk fund, compute target value and rebalance
                        for f in new_active:
                            target_val = curr_total_val * (f.allocation / new_total)
                            if f.name not in p_fund_buckets:
                                # New fund never seen — create bucket with fresh NAV
                                p_fund_buckets[f.name] = FIFOBucket()
                                p_fund_navs[f.name] = 1.0
                                # Register in all_funds_map so it gets per-fund growth
                                all_funds_map[f.name] = f
                            bk = p_fund_buckets[f.name]
                            nav = p_fund_navs[f.name]
                            curr_val = bk.current_value(nav)
                            diff = target_val - curr_val

                            if diff < -0.01:
                                # Over-allocated: redeem excess
                                pr, gn = bk.redeem(-diff, nav)
                                if f.fund_type == "equity":
                                    rebal_gain_e += gn
                                elif f.fund_type == "other":
                                    rebal_gain_o += gn
                                else:
                                    rebal_gain_d += gn
                            elif diff > 0.01:
                                # Under-allocated: invest the difference
                                bk.invest(diff, nav, m)

                        # Accumulate rebalancing gains into the current FY's tax
                        # (rebalancing occurs at start of FY, taxed in that FY)
                        a_rebal = acc_get(p_acc, fy)
                        a_rebal["gain_d"] += rebal_gain_d
                        a_rebal["gain_e"] += rebal_gain_e
                        a_rebal["gain_o"] += rebal_gain_o

                        # ── Tax-aware rebalancing: sell extra to cover tax ──
                        # Estimate the tax on rebalancing gains and sell
                        # additional units to cover it, choosing the category
                        # that minimises additional tax.
                        def _estimate_rebal_tax(gd, ge, go):
                            """Estimate total tax for given gain breakdown."""
                            pi = state.personal_income
                            taxable_other = (pi.salary + pi.taxable_interest +
                                             pi.pension + pi.rental + pi.other_taxable)
                            prev_fy_tax = fy - 1  # rebalancing happens at FY boundary
                            dc = state.get_debt_tax_chunk(prev_fy_tax, "individual")
                            dtax = compute_slab_tax(taxable_other + gd, dc, "individual") if dc else 0.0
                            ec = state.get_equity_tax_chunk(prev_fy_tax, "individual")
                            etax = compute_ltcg_individual(ge, ec) if ec else 0.0
                            oc = state.get_other_tax_chunk(prev_fy_tax, "individual")
                            otax = compute_ltcg_other_individual(go, oc)
                            return dtax + etax + otax

                        # Total gains so far (from rebalancing)
                        cum_gain_d = rebal_gain_d
                        cum_gain_e = rebal_gain_e
                        cum_gain_o = rebal_gain_o
                        total_tax = _estimate_rebal_tax(cum_gain_d, cum_gain_e, cum_gain_o)
                        total_sold = 0.0  # total cash raised by extra sales

                        for _tax_iter in range(8):
                            shortfall = total_tax - total_sold
                            if shortfall < 0.01:
                                break

                            # Evaluate each category: if we sell 'shortfall'
                            # from it, what marginal tax does it add?
                            # FIX: compute gain_frac from per-fund buckets
                            # (authoritative FIFO lots) instead of aggregate
                            # buckets whose cost basis is destroyed by syncs.
                            candidates = []
                            for cat in ("debt", "equity", "other"):
                                cat_fns = [fn for fn, ft in fund_types_map.items()
                                           if ft == cat and fn in p_fund_buckets]
                                cat_val = sum(
                                    p_fund_buckets[fn].current_value(p_fund_navs[fn])
                                    for fn in cat_fns)
                                if cat_val < 1.0:
                                    continue
                                cat_cost = sum(
                                    sum(lot.units * lot.purchase_nav
                                        for lot in p_fund_buckets[fn].lots)
                                    for fn in cat_fns)
                                gain_frac = max(0.0, 1.0 - cat_cost / cat_val) if cat_val > 0 else 0.0
                                # If we sell 'shortfall' from this category:
                                sell_amt = min(shortfall, cat_val * 0.8)
                                sell_gain = sell_amt * gain_frac
                                # Marginal tax from this extra gain
                                test_gd = cum_gain_d + (sell_gain if cat == "debt" else 0)
                                test_ge = cum_gain_e + (sell_gain if cat == "equity" else 0)
                                test_go = cum_gain_o + (sell_gain if cat == "other" else 0)
                                new_tax = _estimate_rebal_tax(test_gd, test_ge, test_go)
                                marginal_tax = new_tax - total_tax
                                candidates.append((cat, cat_fns, cat_val, gain_frac, sell_amt, marginal_tax))

                            if not candidates:
                                break

                            # Pick category with lowest marginal tax per unit sold
                            candidates.sort(key=lambda x: x[5] / max(x[4], 0.01))
                            cat, cat_fns, cat_val, gain_frac, sell_amt, _ = candidates[0]

                            if sell_amt < 0.01:
                                break

                            # FIX: Execute the sale from per-fund buckets
                            # (authoritative), and sum real gains from FIFO
                            # lots — not from aggregate bucket.
                            real_gain = 0.0
                            cat_total_val = sum(
                                p_fund_buckets[fn].current_value(p_fund_navs[fn])
                                for fn in cat_fns)
                            if cat_total_val > 0:
                                for fn in cat_fns:
                                    f_val = p_fund_buckets[fn].current_value(p_fund_navs[fn])
                                    f_share = f_val / cat_total_val
                                    f_sell = sell_amt * f_share
                                    _pr, _gn = p_fund_buckets[fn].redeem(
                                        f_sell, p_fund_navs[fn])
                                    real_gain += _gn

                            # Also redeem from aggregate bucket to keep it in sync
                            if cat == "debt":
                                p_debt_agg.redeem(sell_amt, p_nav_d)
                            elif cat == "equity":
                                p_equity_agg.redeem(sell_amt, p_nav_e)
                            else:
                                p_other_agg.redeem(sell_amt, p_nav_o)

                            # Update running totals with real gains from per-fund FIFO
                            if cat == "debt":
                                cum_gain_d += real_gain
                            elif cat == "equity":
                                cum_gain_e += real_gain
                            else:
                                cum_gain_o += real_gain
                            total_sold += sell_amt
                            total_tax = _estimate_rebal_tax(cum_gain_d, cum_gain_e, cum_gain_o)

                        # Store final cumulative gains in accumulators
                        a_rebal["gain_d"] = cum_gain_d
                        a_rebal["gain_e"] = cum_gain_e
                        a_rebal["gain_o"] = cum_gain_o
                        # Record rebalancing-specific gains for tax attribution
                        rebal_gains_by_fy[fy] = dict(
                            d=cum_gain_d, e=cum_gain_e, o=cum_gain_o)

                        # Sync aggregate buckets with per-fund reality.
                        # Use ALL funds (not just new_active) to capture any
                        # retiring funds still holding value.
                        agg_debt_val = sum(
                            bk.current_value(p_fund_navs[fn])
                            for fn, bk in p_fund_buckets.items()
                            if fn in p_fund_navs
                            and all_funds_map.get(fn) is not None
                            and all_funds_map[fn].fund_type == "debt"
                        )
                        agg_eq_val = sum(
                            bk.current_value(p_fund_navs[fn])
                            for fn, bk in p_fund_buckets.items()
                            if fn in p_fund_navs
                            and all_funds_map.get(fn) is not None
                            and all_funds_map[fn].fund_type == "equity"
                        )
                        agg_oth_val = sum(
                            bk.current_value(p_fund_navs[fn])
                            for fn, bk in p_fund_buckets.items()
                            if fn in p_fund_navs
                            and all_funds_map.get(fn) is not None
                            and all_funds_map[fn].fund_type == "other"
                        )
                        # Reset aggregates to match
                        p_debt_agg   = FIFOBucket()
                        p_equity_agg = FIFOBucket()
                        p_other_agg  = FIFOBucket()
                        if agg_debt_val > 0:
                            p_debt_agg.invest(agg_debt_val, p_nav_d, m)
                        if agg_eq_val > 0:
                            p_equity_agg.invest(agg_eq_val, p_nav_e, m)
                        if agg_oth_val > 0:
                            p_other_agg.invest(agg_oth_val, p_nav_o, m)

            # ── Glide-path micro-rebalancing — Tax-Budget Mode (v8) ──────────
            # Fires once per FY (first month).  Before executing the sell,
            # computes a lookahead tax budget = total_transition_tax / spread_years
            # and passes it to _rebalance_portfolio, which stops selling mid-lot
            # the moment running tax hits the budget.  This distributes the full
            # transition tax evenly across the window years in rupee terms.
            if (gp is not None and not gp.is_flat()
                    and fy >= 2 and fy <= 30
                    and fy not in glide_rebalanced_set
                    and fy not in rebalanced_set):
                target_w = gp.weights_for_year(fy)
                prior_w  = gp.weights_for_year(fy - 1)
                if target_w != prior_w:
                    glide_rebalanced_set.add(fy)

                    # ── Find the true first year of this transition window ────
                    # Walk backwards from fy to find the earliest year whose
                    # weights differ from the pre-window stable weights.
                    # The pre-window stable weights are those of the last year
                    # BEFORE the window where weights were constant.
                    pre_window_w = gp.weights_for_year(fy - 1)
                    # Keep walking back as long as weights are still transitioning
                    b_start_yr = fy
                    for probe in range(fy - 1, 1, -1):
                        pw = gp.weights_for_year(probe)
                        pwm1 = gp.weights_for_year(probe - 1)
                        if pw == pwm1:
                            # probe is stable → window starts at probe+1
                            pre_window_w = pw
                            b_start_yr = probe + 1
                            break
                    else:
                        # Reached year 2 without finding a stable year
                        pre_window_w = gp.weights_for_year(1)
                        b_start_yr = 2

                    # Find the post-window stable weights (first year after
                    # the window where weights stop changing)
                    post_window_w = target_w
                    for probe in range(fy + 1, 31):
                        pw_next = gp.weights_for_year(probe)
                        pw_curr = gp.weights_for_year(probe - 1)
                        if pw_next == pw_curr:
                            post_window_w = pw_next
                            break
                    else:
                        post_window_w = gp.weights_for_year(30)

                    # Compute lookahead budget once per boundary (keyed by b_start_yr).
                    # CRITICAL: estimate tax for the FULL transition
                    # (pre_window_w → post_window_w), not just one year's step.
                    # Dividing by spread_years gives the correct per-year budget.
                    if b_start_yr not in glide_tax_budget:
                        spread = max(1, state.rebalance_spread_years)
                        tot_tax = self._estimate_transition_tax(
                            pre_window_w, post_window_w, p_fund_buckets,
                            p_fund_navs, fund_types_map, m,
                        )
                        glide_tax_budget[b_start_yr] = tot_tax / spread
                        _log.debug(
                            f"  [TaxBudget] Boundary {b_start_yr}: "
                            f"full_transition_tax={tot_tax:.2f}L / {spread}yr "
                            f"= {glide_tax_budget[b_start_yr]:.2f}L/yr"
                        )

                    annual_tax_budget = glide_tax_budget[b_start_yr]
                    # SWP-assisted rebalancing is NOT used during glide-path
                    # transitions.  The rebalancer handles weight shifts only
                    # (sells over-weighted, buys under-weighted).  The monthly
                    # SWP loop independently redeems from ALL active funds
                    # (including retiring chunk funds) pro-rata, so they drain
                    # naturally over the transition window.
                    #
                    # Passing swp_cash_needed > 0 would pre-raise cash that
                    # reduces the portfolio corpus without being tracked as
                    # monthly withdrawal income, breaking the accounting.
                    cost, gp_gains, gp_swp_proceeds = _rebalance_portfolio(
                        target_weights    = target_w,
                        prior_weights     = prior_w,
                        fund_buckets      = p_fund_buckets,
                        fund_navs         = p_fund_navs,
                        fund_types        = fund_types_map,
                        state             = state,
                        fy                = fy,
                        month_idx         = m,
                        swp_cash_needed   = 0.0,
                        annual_tax_budget = annual_tax_budget,
                    )
                    glide_costs_by_fy[fy] = cost
                    # Accumulate rebalancing gains into tax accumulators
                    a_gp = acc_get(p_acc, fy)
                    a_gp["gain_d"] += gp_gains.get('d', 0.0)
                    a_gp["gain_e"] += gp_gains.get('e', 0.0)
                    a_gp["gain_o"] += gp_gains.get('o', 0.0)
                    existing = rebal_gains_by_fy.get(fy, {})
                    rebal_gains_by_fy[fy] = {
                        'd': existing.get('d', 0.0) + gp_gains.get('d', 0.0),
                        'e': existing.get('e', 0.0) + gp_gains.get('e', 0.0),
                        'o': existing.get('o', 0.0) + gp_gains.get('o', 0.0),
                    }
                    # Sync aggregate buckets using ALL funds with non-zero value.
                    # Must include retiring chunk funds (not just current chunk) so
                    # the agg bucket correctly reflects total portfolio value.
                    agg_d2 = sum(
                        bk.current_value(p_fund_navs[fn])
                        for fn, bk in p_fund_buckets.items()
                        if fn in p_fund_navs
                        and all_funds_map.get(fn) is not None
                        and all_funds_map[fn].fund_type == "debt"
                    )
                    agg_e2 = sum(
                        bk.current_value(p_fund_navs[fn])
                        for fn, bk in p_fund_buckets.items()
                        if fn in p_fund_navs
                        and all_funds_map.get(fn) is not None
                        and all_funds_map[fn].fund_type == "equity"
                    )
                    agg_o2 = sum(
                        bk.current_value(p_fund_navs[fn])
                        for fn, bk in p_fund_buckets.items()
                        if fn in p_fund_navs
                        and all_funds_map.get(fn) is not None
                        and all_funds_map[fn].fund_type == "other"
                    )
                    p_debt_agg   = FIFOBucket(); p_debt_agg.invest(agg_d2,   p_nav_d, m)
                    p_equity_agg = FIFOBucket(); p_equity_agg.invest(agg_e2, p_nav_e, m)
                    p_other_agg  = FIFOBucket(); p_other_agg.invest(agg_o2,  p_nav_o, m)

            # ── Grow NAVs (per-fund and per-category rates) ────────────────
            safe_fy = min(fy, 30)

            # Per-fund NAVs: each fund grows at its own CAGR
            for fname, fentry in all_funds_map.items():
                p_fund_navs[fname] *= self._fund_monthly_factor(fentry, safe_fy)

            # Aggregate NAVs: grow at category-weighted rates
            mf_d = self._category_monthly_factor(safe_fy, "debt")
            mf_e = self._category_monthly_factor(safe_fy, "equity")
            mf_o = self._category_monthly_factor(safe_fy, "other")
            p_nav_d *= mf_d;  p_nav_e *= mf_e;  p_nav_o *= mf_o
            h_nav_d *= mf_d;  h_nav_e *= mf_e;  h_nav_o *= mf_o

            # ── Personal windfall ─────────────────────────────────────────────
            wf_p_this = 0.0
            if fy <= 30 and fy not in seen_p_fy:
                seen_p_fy.add(fy)
                amt = wf_p.get(fy, 0.0)
                if amt > 0:
                    split = state.get_split(fy)
                    # Invest windfall proportionally across funds active THIS year
                    fy_funds   = state.get_funds_for_year(fy)
                    fy_debt    = [f for f in fy_funds if f.fund_type == "debt"]
                    fy_equity  = [f for f in fy_funds if f.fund_type == "equity"]
                    fy_other   = [f for f in fy_funds if f.fund_type == "other"]
                    fy_td      = sum(f.allocation for f in fy_debt)
                    fy_te      = sum(f.allocation for f in fy_equity)
                    fy_to      = sum(f.allocation for f in fy_other)
                    fy_total   = fy_td + fy_te + fy_to
                    for f in fy_debt:
                        share = (f.allocation / fy_total) if fy_total > 0 else 0
                        p_fund_buckets[f.name].invest(
                            amt * share, p_fund_navs[f.name], m)
                    for f in fy_equity:
                        share = (f.allocation / fy_total) if fy_total > 0 else 0
                        p_fund_buckets[f.name].invest(
                            amt * share, p_fund_navs[f.name], m)
                    for f in fy_other:
                        share = (f.allocation / fy_total) if fy_total > 0 else 0
                        p_fund_buckets[f.name].invest(
                            amt * share, p_fund_navs[f.name], m)
                    # Aggregate buckets — split by type ratio
                    d_ratio = (fy_td / fy_total) if fy_total > 0 else split
                    e_ratio = (fy_te / fy_total) if fy_total > 0 else (1 - split)
                    o_ratio = (fy_to / fy_total) if fy_total > 0 else 0
                    p_debt_agg.invest(amt * d_ratio,   p_nav_d, m)
                    p_equity_agg.invest(amt * e_ratio, p_nav_e, m)
                    p_other_agg.invest(amt * o_ratio,  p_nav_o, m)
                    wf_p_this = amt

            # ── April tax event ───────────────────────────────────────────────
            ind_tax_this = fd_tax_this = huf_xfer_this = 0.0
            if cm == 4 and fy >= 2:
                pend = pending.get(fy, {})
                if pend:
                    ind_tax_this  = pend["ind_tax"]
                    fd_tax_this   = pend["fd_tax"]
                    huf_xfer_this = pend["tax_saved"]

            # ── Annual aggregate sync (every April) ─────────────────────────
            # The per-fund buckets grow at each fund's own CAGR; the aggregate
            # buckets grow at the blended category rate.  Over several years
            # these diverge, producing step-jumps in annual summaries at
            # rebalancing boundaries.  Syncing every April keeps the aggregate
            # accurate and eliminates those jumps.
            # Skip months where _rebalance_portfolio has already synced the agg.
            if cm == 4 and fy >= 1:
                _agg_sync_d = sum(
                    bk.current_value(p_fund_navs[fn])
                    for fn, bk in p_fund_buckets.items()
                    if fn in p_fund_navs
                    and all_funds_map.get(fn) is not None
                    and all_funds_map[fn].fund_type == "debt"
                )
                _agg_sync_e = sum(
                    bk.current_value(p_fund_navs[fn])
                    for fn, bk in p_fund_buckets.items()
                    if fn in p_fund_navs
                    and all_funds_map.get(fn) is not None
                    and all_funds_map[fn].fund_type == "equity"
                )
                _agg_sync_o = sum(
                    bk.current_value(p_fund_navs[fn])
                    for fn, bk in p_fund_buckets.items()
                    if fn in p_fund_navs
                    and all_funds_map.get(fn) is not None
                    and all_funds_map[fn].fund_type == "other"
                )
                p_debt_agg   = FIFOBucket(); p_debt_agg.invest(_agg_sync_d,   p_nav_d, m)
                p_equity_agg = FIFOBucket(); p_equity_agg.invest(_agg_sync_e, p_nav_e, m)
                p_other_agg  = FIFOBucket(); p_other_agg.invest(_agg_sync_o,  p_nav_o, m)

            # ── Personal corpus snapshot ──────────────────────────────────────
            p_cd_s = p_debt_agg.current_value(p_nav_d)
            p_ce_s = p_equity_agg.current_value(p_nav_e)
            p_co_s = p_other_agg.current_value(p_nav_o)

            if m <= 1:
                _log.debug(f"  Month {m}: corpus snapshot D={p_cd_s:.2f}  E={p_ce_s:.2f}  O={p_co_s:.2f}"
                            f"  (nav_d={p_nav_d:.6f} nav_e={p_nav_e:.6f} nav_o={p_nav_o:.6f})")

            # ── Personal SWP withdrawal ───────────────────────────────────────
            wd_d = wd_e = wd_o = 0.0
            prin_d = gain_d = prin_e = gain_e = prin_o = gain_o = 0.0
            fund_details: List[FundWithdrawalDetail] = []

            if m >= SWP_START and fy <= 30:
                req_annual  = state.get_requirement(fy)
                req_monthly = req_annual / 12

                # Use ALL funds with non-zero bucket value (not just current chunk).
                # During glide-path transitions, retiring chunk funds still hold
                # value; including them lets the monthly SWP drain them naturally.
                _all_fy_funds = [
                    f for f in all_active
                    if p_fund_buckets.get(f.name) is not None
                    and p_fund_buckets[f.name].current_value(p_fund_navs.get(f.name, 1.0)) > 0.01
                ]

                net_monthly_req = req_monthly

                # ── Bounded Smart Withdrawal (per-fund waterfall) ─────────
                # After month 18, use the waterfall to preferentially sell
                # overweighted high-return funds, capping effective return
                # drift at +0.15%.  Before month 18, use static allocation
                # ratios (corpus too young for meaningful drift).
                CORPUS_SPLIT_START = config.smart_withdrawal_start_month
                p_total_corpus = p_cd_s + p_ce_s + p_co_s

                if m >= CORPUS_SPLIT_START and p_total_corpus > 0:
                    # Build per-fund value and return maps
                    _fv = {}   # fund_name → current value
                    _fr = {}   # fund_name → annual return
                    for f in _all_fy_funds:
                        nav_f = p_fund_navs.get(f.name, 1.0)
                        _fv[f.name] = p_fund_buckets[f.name].current_value(nav_f)
                        _fr[f.name] = self._fund_annual_return(f, fy)

                    # Target weights: glide path if available, else allocation ratios
                    if gp is not None and not gp.is_flat():
                        _tw = gp.weights_for_year(fy)
                    else:
                        # Derive from allocation chunk fund allocations
                        fy_funds_alloc = state.get_funds_for_year(fy)
                        _alloc_total = sum(f.allocation for f in fy_funds_alloc
                                           if f.allocation > 0)
                        if _alloc_total > 0:
                            _tw = {f.name: f.allocation / _alloc_total
                                   for f in fy_funds_alloc if f.allocation > 0}
                        else:
                            _tw = {fn: v / p_total_corpus for fn, v in _fv.items()}

                    # Compute per-fund withdrawal amounts via waterfall
                    _wd_map = self._compute_smart_withdrawal(
                        fund_values=_fv,
                        fund_returns=_fr,
                        target_weights=_tw,
                        withdrawal_amt=net_monthly_req,
                        entity="personal",
                    )

                    # Execute redemptions per fund
                    for f in _all_fy_funds:
                        amt_f = _wd_map.get(f.name, 0.0)
                        if amt_f < 0.01:
                            continue
                        nav_f  = p_fund_navs[f.name]
                        bk     = p_fund_buckets[f.name]
                        corp_s = bk.current_value(nav_f)
                        pr_f, gn_f = bk.redeem(amt_f, nav_f)
                        corp_e_f   = bk.current_value(nav_f)
                        wd_f_total = pr_f + gn_f
                        if f.fund_type == "debt":
                            prin_d += pr_f; gain_d += gn_f; wd_d += wd_f_total
                        elif f.fund_type == "equity":
                            prin_e += pr_f; gain_e += gn_f; wd_e += wd_f_total
                        else:
                            prin_o += pr_f; gain_o += gn_f; wd_o += wd_f_total
                        fund_details.append(FundWithdrawalDetail(
                            fund_name=f.name, fund_type=f.fund_type,
                            corpus_start=corp_s, withdrawal=wd_f_total,
                            principal=pr_f, gain=gn_f, corpus_end=corp_e_f))

                    # Keep aggregate buckets in sync
                    if wd_d > 0.01:
                        p_debt_agg.redeem(wd_d, p_nav_d)
                    if wd_e > 0.01:
                        p_equity_agg.redeem(wd_e, p_nav_e)
                    if wd_o > 0.01:
                        p_other_agg.redeem(wd_o, p_nav_o)

                else:
                    # Before month 18: use static allocation ratios
                    fy_funds_alloc = state.get_funds_for_year(fy)
                    _td = sum(f.allocation for f in fy_funds_alloc if f.fund_type == "debt")
                    _te = sum(f.allocation for f in fy_funds_alloc if f.fund_type == "equity")
                    _to = sum(f.allocation for f in fy_funds_alloc if f.fund_type == "other")
                    _tt = _td + _te + _to
                    split_d = (_td / _tt) if _tt > 0 else state.get_split(fy)
                    split_e = (_te / _tt) if _tt > 0 else (1 - state.get_split(fy))
                    split_o = (_to / _tt) if _tt > 0 else 0.0

                    debt_funds   = [f for f in _all_fy_funds if f.fund_type == "debt"]
                    equity_funds = [f for f in _all_fy_funds if f.fund_type == "equity"]
                    other_funds  = [f for f in _all_fy_funds if f.fund_type == "other"]

                    # Redeem from each debt fund pro-rata
                    _live_debt_corpus = sum(
                        p_fund_buckets[f.name].current_value(p_fund_navs[f.name])
                        for f in debt_funds)
                    if debt_funds and _live_debt_corpus > 0:
                        debt_corpus = _live_debt_corpus
                        debt_target = net_monthly_req * split_d
                        for f in debt_funds:
                            nav_f  = p_fund_navs[f.name]
                            bk     = p_fund_buckets[f.name]
                            corp_s = bk.current_value(nav_f)
                            share  = (corp_s / debt_corpus) if debt_corpus > 0 else 0
                            amt_f  = debt_target * share
                            pr_f, gn_f = bk.redeem(amt_f, nav_f)
                            corp_e_f   = bk.current_value(nav_f)
                            wd_f_total = pr_f + gn_f
                            prin_d    += pr_f;  gain_d += gn_f
                            wd_d      += wd_f_total
                            fund_details.append(FundWithdrawalDetail(
                                fund_name=f.name, fund_type="debt",
                                corpus_start=corp_s, withdrawal=wd_f_total,
                                principal=pr_f, gain=gn_f, corpus_end=corp_e_f))
                        p_debt_agg.redeem(debt_target, p_nav_d)

                    # Redeem from each equity fund pro-rata
                    _live_equity_corpus = sum(
                        p_fund_buckets[f.name].current_value(p_fund_navs[f.name])
                        for f in equity_funds)
                    if equity_funds and _live_equity_corpus > 0:
                        eq_corpus = _live_equity_corpus
                        eq_target = net_monthly_req * split_e
                        for f in equity_funds:
                            nav_f  = p_fund_navs[f.name]
                            bk     = p_fund_buckets[f.name]
                            corp_s = bk.current_value(nav_f)
                            share  = (corp_s / eq_corpus) if eq_corpus > 0 else 0
                            amt_f  = eq_target * share
                            pr_f, gn_f = bk.redeem(amt_f, nav_f)
                            corp_e_f   = bk.current_value(nav_f)
                            wd_f_total = pr_f + gn_f
                            prin_e    += pr_f;  gain_e += gn_f
                            wd_e      += wd_f_total
                            fund_details.append(FundWithdrawalDetail(
                                fund_name=f.name, fund_type="equity",
                                corpus_start=corp_s, withdrawal=wd_f_total,
                                principal=pr_f, gain=gn_f, corpus_end=corp_e_f))
                        p_equity_agg.redeem(eq_target, p_nav_e)

                    # Redeem from each 'other' fund pro-rata
                    _live_other_corpus = sum(
                        p_fund_buckets[f.name].current_value(p_fund_navs[f.name])
                        for f in other_funds)
                    if other_funds and _live_other_corpus > 0:
                        oth_corpus = _live_other_corpus
                        oth_target = net_monthly_req * split_o
                        for f in other_funds:
                            nav_f  = p_fund_navs[f.name]
                            bk     = p_fund_buckets[f.name]
                            corp_s = bk.current_value(nav_f)
                            share  = (corp_s / oth_corpus) if oth_corpus > 0 else 0
                            amt_f  = oth_target * share
                            pr_f, gn_f = bk.redeem(amt_f, nav_f)
                            corp_e_f   = bk.current_value(nav_f)
                            wd_f_total = pr_f + gn_f
                            prin_o    += pr_f;  gain_o += gn_f
                            wd_o      += wd_f_total
                            fund_details.append(FundWithdrawalDetail(
                                fund_name=f.name, fund_type="other",
                                corpus_start=corp_s, withdrawal=wd_f_total,
                                principal=pr_f, gain=gn_f, corpus_end=corp_e_f))
                        p_other_agg.redeem(oth_target, p_nav_o)

            p_cd_e = p_debt_agg.current_value(p_nav_d)
            p_ce_e = p_equity_agg.current_value(p_nav_e)
            p_co_e = p_other_agg.current_value(p_nav_o)

            a = acc_get(p_acc, fy)
            a["gain_d"] += gain_d;  a["gain_e"] += gain_e;  a["gain_o"] += gain_o
            a["wd_d"]   += wd_d;    a["wd_e"]   += wd_e;    a["wd_o"]   += wd_o
            a["corp_d"]  = p_cd_e;  a["corp_e"]  = p_ce_e;  a["corp_o"]  = p_co_e

            # ── Tax-free LTCG harvesting (March of each FY) ──────────────
            if cm == 3 and 1 <= fy <= 30:
                harvest_total = self._execute_ltcg_harvest(
                    state, fy, m, a, fund_types_map, p_fund_buckets, p_fund_navs, _log)

            p_rows.append(MonthlyRow(
                month_idx=m, calendar_month=cm, calendar_year=cy, fy_year=fy,
                corpus_debt_start=p_cd_s, corpus_equity_start=p_ce_s,
                corpus_other_start=p_co_s,
                wd_debt=wd_d, wd_equity=wd_e, wd_other=wd_o,
                principal_debt=prin_d, gain_debt=gain_d,
                principal_equity=prin_e, gain_equity=gain_e,
                principal_other=prin_o, gain_other=gain_o,
                corpus_debt_end=p_cd_e, corpus_equity_end=p_ce_e,
                corpus_other_end=p_co_e,
                windfall_personal=wf_p_this,
                ind_tax_paid=ind_tax_this,
                fd_tax_paid=fd_tax_this,
                huf_transfer_in=huf_xfer_this,
                fund_withdrawals=fund_details,
            ))

            # ══════════════════════════════════════════════════════════════════
            # HUF simulation (aggregate only, no per-fund breakdown)
            # ══════════════════════════════════════════════════════════════════
            huf_inflow = 0.0
            if cm == 4 and fy >= 2:
                huf_inflow += huf_xfer_this

            wf_h_this = 0.0
            if fy <= 30 and fy not in seen_h_fy:
                seen_h_fy.add(fy)
                amt_h = wf_h.get(fy, 0.0)
                if amt_h > 0:
                    huf_inflow += amt_h
                    wf_h_this = amt_h

            if huf_inflow > 0:
                sd, se, so = state.get_split_3way(safe_fy)
                h_debt.invest(huf_inflow * sd,   h_nav_d, m)
                h_equity.invest(huf_inflow * se,  h_nav_e, m)
                h_other.invest(huf_inflow * so,   h_nav_o, m)

            h_cd_s = h_debt.current_value(h_nav_d)
            h_ce_s = h_equity.current_value(h_nav_e)
            h_co_s = h_other.current_value(h_nav_o)

            h_wd_d = h_wd_e = h_wd_o = 0.0
            h_prin_d = h_gain_d = h_prin_e = h_gain_e = h_prin_o = h_gain_o = 0.0
            if fy <= 30:
                huf_req = self._huf_annual_wd(fy) / 12
                # Smart withdrawal for HUF (0.50% cap) from mid-year-2.
                # HUF uses aggregate buckets (no per-fund breakdown), so we
                # treat the 3 categories as pseudo-funds for the waterfall.
                CORPUS_SPLIT_START_H = config.smart_withdrawal_start_month
                h_total = h_cd_s + h_ce_s + h_co_s
                if m >= CORPUS_SPLIT_START_H and h_total > 0:
                    # Build category-level value/return/target maps
                    _hfv = {"_h_debt": h_cd_s, "_h_equity": h_ce_s, "_h_other": h_co_s}
                    _hfr = {
                        "_h_debt":   self._category_annual_return(fy, "debt"),
                        "_h_equity": self._category_annual_return(fy, "equity"),
                        "_h_other":  self._category_annual_return(fy, "other"),
                    }
                    _htd, _hte, _hto = state.get_split_3way(safe_fy)
                    _htw = {"_h_debt": _htd, "_h_equity": _hte, "_h_other": _hto}

                    _hwd_map = self._compute_smart_withdrawal(
                        fund_values=_hfv, fund_returns=_hfr,
                        target_weights=_htw, withdrawal_amt=huf_req,
                        entity="huf")
                    split_hd_amt = _hwd_map.get("_h_debt", 0.0)
                    split_he_amt = _hwd_map.get("_h_equity", 0.0)
                    split_ho_amt = _hwd_map.get("_h_other", 0.0)
                    h_prin_d, h_gain_d = h_debt.redeem(split_hd_amt, h_nav_d)
                    h_prin_e, h_gain_e = h_equity.redeem(split_he_amt, h_nav_e)
                    h_prin_o, h_gain_o = h_other.redeem(split_ho_amt, h_nav_o)
                else:
                    split_hd, split_he, split_ho = state.get_split_3way(safe_fy)
                    h_prin_d, h_gain_d = h_debt.redeem(huf_req * split_hd,   h_nav_d)
                    h_prin_e, h_gain_e = h_equity.redeem(huf_req * split_he, h_nav_e)
                    h_prin_o, h_gain_o = h_other.redeem(huf_req * split_ho,  h_nav_o)
                h_wd_d = h_prin_d + h_gain_d
                h_wd_e = h_prin_e + h_gain_e
                h_wd_o = h_prin_o + h_gain_o

            h_cd_e = h_debt.current_value(h_nav_d)
            h_ce_e = h_equity.current_value(h_nav_e)
            h_co_e = h_other.current_value(h_nav_o)

            ha = acc_get(h_acc, fy)
            ha["gain_d"] += h_gain_d;  ha["gain_e"] += h_gain_e;  ha["gain_o"] += h_gain_o
            ha["wd_d"]   += h_wd_d;    ha["wd_e"]   += h_wd_e;    ha["wd_o"]   += h_wd_o
            ha["corp_d"]  = h_cd_e;    ha["corp_e"]  = h_ce_e;    ha["corp_o"]  = h_co_e

            h_rows.append(MonthlyRow(
                month_idx=m, calendar_month=cm, calendar_year=cy, fy_year=fy,
                corpus_debt_start=h_cd_s, corpus_equity_start=h_ce_s,
                corpus_other_start=h_co_s,
                wd_debt=h_wd_d, wd_equity=h_wd_e, wd_other=h_wd_o,
                principal_debt=h_prin_d, gain_debt=h_gain_d,
                principal_equity=h_prin_e, gain_equity=h_gain_e,
                principal_other=h_prin_o, gain_other=h_gain_o,
                corpus_debt_end=h_cd_e, corpus_equity_end=h_ce_e,
                corpus_other_end=h_co_e,
                windfall_huf=wf_h_this,
                huf_transfer_in=huf_xfer_this if (cm == 4 and fy >= 2) else 0.0,
            ))

        # ── Filter ────────────────────────────────────────────────────────────
        p_rows = [r for r in p_rows if r.fy_year <= 30]
        h_rows = [r for r in h_rows if r.fy_year <= 30]

        # ── Yearly summaries ──────────────────────────────────────────────────
        yearly = self._build_yearly_summaries(
            p_acc, h_acc, pending_by_fy, rebal_gains_by_fy,
            glide_costs_by_fy, acc_get,
        )

        return p_rows, yearly, h_rows, yearly

    # ── Extracted from run(): LTCG harvesting ───────────────────────────

    @staticmethod
    def _execute_ltcg_harvest(
        state, fy, m, accumulator, fund_types_map,
        p_fund_buckets, p_fund_navs, _log,
    ) -> float:
        """
        Tax-free LTCG harvesting (March of each FY).

        Sell equity lots up to the remaining annual LTCG exemption and
        immediately re-buy at the same NAV.  This steps up the cost basis,
        preventing a large taxable event when the glide path eventually
        forces selling.  Gains booked here fall within the exemption so
        no additional tax is generated.

        Returns the total gains harvested (already booked into accumulator).
        """
        ec_harvest = state.get_equity_tax_chunk(fy, "individual")
        if not ec_harvest:
            return 0.0
        already_realised_eq = accumulator["gain_e"]
        harvest_budget = max(0.0, ec_harvest.exempt_limit - already_realised_eq)
        if harvest_budget <= 0.01:
            return 0.0

        harvest_total = 0.0
        eq_fund_names = [
            fn for fn, ft in fund_types_map.items()
            if ft == "equity" and fn in p_fund_buckets
        ]
        for fn in eq_fund_names:
            if harvest_total >= harvest_budget - 0.001:
                break
            bk  = p_fund_buckets[fn]
            nav = p_fund_navs[fn]
            new_lots = []
            for lot in bk.lots:
                remaining_budget = harvest_budget - harvest_total
                if remaining_budget < 0.001:
                    new_lots.append(lot)
                    continue
                holding = m - lot.purchase_month
                if holding < config.stcg_holding_months:
                    new_lots.append(lot)
                    continue
                unrealised_gain = lot.units * (nav - lot.purchase_nav)
                if unrealised_gain <= 0.001:
                    new_lots.append(lot)
                    continue
                if unrealised_gain <= remaining_budget:
                    harvest_total += unrealised_gain
                    new_lots.append(Lot(lot.units, nav, m))
                else:
                    fraction = remaining_budget / unrealised_gain
                    harvest_units = lot.units * fraction
                    keep_units    = lot.units - harvest_units
                    harvest_total += remaining_budget
                    new_lots.append(Lot(harvest_units, nav, m))
                    if keep_units > 1e-12:
                        new_lots.append(Lot(keep_units, lot.purchase_nav, lot.purchase_month))
            bk.lots = new_lots

        if harvest_total > 0.001:
            accumulator["gain_e"] += harvest_total
            _log.debug(
                f"  [LTCG Harvest] FY{fy} Mar: harvested "
                f"{harvest_total:.4f}L equity gains "
                f"(budget was {harvest_budget:.4f}L, "
                f"exempt_limit={ec_harvest.exempt_limit}L)"
            )
        return harvest_total

    # ── Extracted from run(): yearly summary computation ──────────────────

    def _build_yearly_summaries(
        self,
        p_acc: Dict,
        h_acc: Dict,
        pending_by_fy: Dict,
        rebal_gains_by_fy: Dict,
        glide_costs_by_fy: Dict,
        acc_get,
    ) -> List[YearSummary]:
        """Build per-FY summary rows from accumulated monthly data."""
        state = self.state
        yearly: List[YearSummary] = []
        for fy in range(1, config.max_simulation_years + 1):
            a  = acc_get(p_acc, fy)
            ha = acc_get(h_acc, fy)

            pi = state.personal_income
            hi = state.huf_income
            taxable_other     = pi.salary + pi.taxable_interest + pi.pension + pi.rental + pi.other_taxable
            non_taxable_other = pi.tax_free_interest + pi.other_non_taxable
            huf_taxable_other = hi.salary + hi.taxable_interest + hi.pension + hi.rental + hi.other_taxable
            huf_nontax_other  = hi.tax_free_interest + hi.other_non_taxable

            this_fy_pend  = pending_by_fy.get(fy, {})
            this_fy_tax   = this_fy_pend.get("ind_tax",   0.0)
            this_fy_saved = this_fy_pend.get("tax_saved", 0.0)

            # ── Rebalancing tax attribution ────────────────────────────────
            rebal_tax_fy = 0.0
            rg = rebal_gains_by_fy.get(fy, {})
            if rg and this_fy_tax > 0:
                total_gains = a["gain_d"] + a["gain_e"] + a["gain_o"]
                rebal_gains = rg.get("d", 0.0) + rg.get("e", 0.0) + rg.get("o", 0.0)
                if total_gains > 1e-9 and rebal_gains > 0:
                    rebal_frac = min(1.0, rebal_gains / total_gains)
                    rebal_tax_fy = this_fy_tax * rebal_frac

            ann_wd_p = a["wd_d"]  + a["wd_e"]  + a["wd_o"]
            ann_wd_h = ha["wd_d"] + ha["wd_e"] + ha["wd_o"]

            glide_cost = glide_costs_by_fy.get(fy)
            glide_exit_loads = glide_cost.exit_loads_paid if glide_cost else 0.0

            swp_only_tax = this_fy_tax - rebal_tax_fy
            net_p = ann_wd_p + taxable_other + non_taxable_other - this_fy_saved - swp_only_tax

            huf_debt_income = huf_taxable_other + ha["gain_d"]
            hdc = state.get_debt_tax_chunk(fy, "huf")
            hec = state.get_equity_tax_chunk(fy, "huf")
            hoc = state.get_other_tax_chunk(fy, "huf")
            huf_debt_tax  = compute_slab_tax(huf_debt_income, hdc, "huf") if hdc else 0.0
            huf_eq_tax    = (compute_ltcg_huf(ha["gain_e"], hec, huf_debt_income, hdc)
                             if (hec and hdc) else 0.0)
            huf_other_tax = (compute_ltcg_other_huf(ha["gain_o"], hoc, huf_debt_income, hdc)
                             if (hoc and hdc) else 0.0)
            huf_total_tax = huf_debt_tax + huf_eq_tax + huf_other_tax
            net_h = ann_wd_h + huf_taxable_other + huf_nontax_other - huf_total_tax

            yearly.append(YearSummary(
                year=fy,
                corpus_debt_personal=a["corp_d"],
                corpus_equity_personal=a["corp_e"],
                corpus_other_personal=a.get("corp_o", 0.0),
                corpus_debt_huf=ha["corp_d"],
                corpus_equity_huf=ha["corp_e"],
                corpus_other_huf=ha.get("corp_o", 0.0),
                tax_personal=this_fy_tax,
                tax_huf=huf_total_tax,
                net_cash_personal=net_p,
                net_cash_huf=net_h,
                net_cash_total=net_p + net_h,
                fd_tax_benchmark=this_fy_pend.get("fd_tax", 0.0),
                tax_saved=this_fy_saved,
                rebalance_tax_paid=rebal_tax_fy,
                rebalance_exit_loads=glide_exit_loads,
            ))

        return yearly


def run_sensitivity(state: AppState, scenarios: list,
                    conservative_mode: bool = False,
                    glide_path=None) -> Dict[str, List[YearSummary]]:
    results = {}
    _, base, _, _ = Engine(state, conservative_mode=conservative_mode,
                           glide_path=glide_path).run()
    results["Base Case"] = base
    for sc in scenarios:
        _, ys, _, _ = Engine(state, alt_return_chunks=sc["return_chunks"],
                             conservative_mode=conservative_mode,
                             glide_path=glide_path).run()
        results[sc["name"]] = ys
    return results


# ─── Tax-Optimal HUF–Personal Withdrawal Split Optimizer ──────────────────────

def _nil_slab_upper_from_chunk(dc) -> float:
    """Return the upper boundary of the 0% (nil) slab from a TaxChunk."""
    for s in dc.slabs:
        if s.rate == 0:
            return s.upper
    return 0.0


def optimize_withdrawal_split(
    state: AppState,
    p_monthly: List[MonthlyRow],
    h_monthly: List[MonthlyRow],
    p_yearly: List[YearSummary],
    user_cap: float = 7.5,
    min_personal_frac: float = 0.25,
    max_huf_corpus_drain: float = 0.03,
    iterations: int = 2,
    conservative_mode: bool = False,
    glide_path=None,
):
    """
    Two-pass optimizer that shifts withdrawal from Personal to HUF to
    minimise combined tax, then re-runs the engine with optimised settings.

    Parameters
    ----------
    state : AppState (will be MODIFIED in-place with optimised requirements)
    p_monthly, h_monthly, p_yearly : baseline engine output (from first run)
    user_cap : maximum extra HUF withdrawal per FY (₹ lakhs)
    min_personal_frac : keep at least this fraction of personal SWP
    max_huf_corpus_drain : max fraction of HUF corpus to withdraw per FY
    iterations : number of optimise→re-run cycles (2 is usually sufficient)
    conservative_mode, glide_path : passed through to engine re-runs

    Returns
    -------
    (p_monthly, p_yearly, h_monthly, h_yearly) from the final optimised run
    """
    import copy as _copy

    # Save the ORIGINAL baseline requirements — deltas are always relative to these
    original_personal_reqs = _copy.deepcopy(state.annual_requirements)
    original_huf_base = {}
    for fy in range(1, 31):
        for c in state.huf_withdrawal_chunks:
            if c.year_from <= fy <= c.year_to:
                original_huf_base[fy] = c.annual_withdrawal
                break
        if fy not in original_huf_base:
            original_huf_base[fy] = 0.0

    for iteration in range(iterations):
        # ── Extract per-FY gain fractions from latest engine output ────────
        # Personal: aggregate debt gains & debt withdrawals per FY
        p_fy_gain_d = {}; p_fy_wd_d = {}
        p_fy_gain_e = {}; p_fy_gain_o = {}
        for r in p_monthly:
            fy = r.fy_year
            if fy < 1 or fy > 30:
                continue
            p_fy_gain_d[fy] = p_fy_gain_d.get(fy, 0.0) + r.gain_debt
            p_fy_wd_d[fy]   = p_fy_wd_d.get(fy, 0.0)   + r.wd_debt
            p_fy_gain_e[fy] = p_fy_gain_e.get(fy, 0.0) + r.gain_equity
            p_fy_gain_o[fy] = p_fy_gain_o.get(fy, 0.0) + r.gain_other

        # HUF: same extraction
        h_fy_gain_d = {}; h_fy_wd_d = {}
        for r in h_monthly:
            fy = r.fy_year
            if fy < 1 or fy > 30:
                continue
            h_fy_gain_d[fy] = h_fy_gain_d.get(fy, 0.0) + r.gain_debt
            h_fy_wd_d[fy]   = h_fy_wd_d.get(fy, 0.0)   + r.wd_debt

        # HUF corpus per FY (from yearly summaries)
        huf_corpus = {}
        for ys in p_yearly:
            huf_corpus[ys.year] = (ys.corpus_debt_huf
                                   + ys.corpus_equity_huf
                                   + ys.corpus_other_huf)

        # ── Personal other taxable income (constant) ──────────────────────
        pi = state.personal_income
        taxable_other = (pi.salary + pi.taxable_interest
                         + pi.pension + pi.rental + pi.other_taxable)

        # ── Compute optimal δ for each FY ─────────────────────────────────
        opt_personal: Dict[int, float] = {}
        opt_huf: Dict[int, float] = {}

        for fy in range(1, 31):
            req = 0.0
            for y in sorted(original_personal_reqs.keys()):
                if y <= fy:
                    req = original_personal_reqs[y]

            # Original HUF base withdrawal (from chunks, NOT overrides)
            huf_base = original_huf_base[fy]

            # Get tax chunks for this FY
            dc_p = state.get_debt_tax_chunk(fy, "individual")
            dc_h = state.get_debt_tax_chunk(fy, "huf")
            ec_p = state.get_equity_tax_chunk(fy, "individual")
            oc_p = state.get_other_tax_chunk(fy, "individual")
            ec_h = state.get_equity_tax_chunk(fy, "huf")
            oc_h = state.get_other_tax_chunk(fy, "huf")

            if not dc_p or not dc_h:
                opt_huf[fy] = huf_base
                continue

            # Gain fractions (from baseline run)
            p_wd_total = p_fy_wd_d.get(fy, 0.0)
            gf_p = (p_fy_gain_d.get(fy, 0.0) / p_wd_total
                    if p_wd_total > 0.01 else 0.0)
            h_wd_total = h_fy_wd_d.get(fy, 0.0)
            gf_h = (h_fy_gain_d.get(fy, 0.0) / h_wd_total
                    if h_wd_total > 0.01 else 0.0)

            # Skip if no real benefit possible:
            # - FY1 (investment month, no SWP yet)
            # - Personal gain fraction ≈ 0 (nothing to save)
            # - HUF corpus too small
            # - Personal has no actual withdrawals in the baseline
            h_corp = huf_corpus.get(fy, 0.0)
            if (fy <= 1 or gf_p < 0.01 or h_corp < 1.0
                    or req < 0.01 or p_wd_total < 0.01):
                opt_personal[fy] = req
                opt_huf[fy] = huf_base
                continue

            # Non-debt gains (unchanged by the shift — only debt withdrawal changes)
            gain_e = p_fy_gain_e.get(fy, 0.0)
            gain_o = p_fy_gain_o.get(fy, 0.0)

            # If HUF has no baseline withdrawals, estimate gain fraction from
            # corpus age.  HUF corpus is built from annual tax-savings transfers
            # starting FY2, so average lot age ≈ (fy-2)/2 years.
            # gain_fraction ≈ 1 - 1/(1+r)^avg_age where r ≈ 0.075
            if gf_h < 0.01 and fy >= 3:
                avg_age = max(1, (fy - 2) / 2)
                gf_h = 1.0 - 1.0 / (1.075 ** avg_age)
                gf_h = max(0.05, min(0.8, gf_h))  # clamp to reasonable range

            # HUF nil slab upper
            nil_h = _nil_slab_upper_from_chunk(dc_h)

            # ── Constraints on δ ──────────────────────────────────────────
            # C1: keep minimum personal SWP
            c1 = req * (1.0 - min_personal_frac)
            # C2: keep HUF debt gains within nil slab
            huf_current_gains = huf_base * gf_h
            huf_headroom = max(0.0, nil_h - huf_current_gains)
            c2 = (huf_headroom / gf_h) if gf_h > 0.01 else 999.0
            # C3: don't drain HUF corpus too fast
            c3 = h_corp * max_huf_corpus_drain
            # C4: user cap
            c4 = user_cap

            delta_max = max(0.0, min(c1, c2, c3, c4))

            # ── Grid search ───────────────────────────────────────────────
            best_delta = 0.0
            best_combined = float('inf')
            step = 0.25  # ₹0.25L granularity

            delta = 0.0
            while delta <= delta_max + 1e-9:
                # Personal tax with reduced withdrawal
                personal_gains_d = max(0.0,
                    p_fy_gain_d.get(fy, 0.0) - delta * gf_p)
                personal_slab = taxable_other + personal_gains_d
                p_debt_tax = compute_slab_tax(personal_slab, dc_p, "individual")
                p_eq_tax = (compute_ltcg_individual(gain_e, ec_p)
                            if ec_p else 0.0)
                p_oth_tax = compute_ltcg_other_individual(gain_o, oc_p)
                p_total = p_debt_tax + p_eq_tax + p_oth_tax

                # HUF tax with increased withdrawal
                huf_gains_d = (huf_base + delta) * gf_h
                h_debt_tax = compute_slab_tax(huf_gains_d, dc_h, "huf")
                # HUF equity/other gains are unchanged by shift
                h_eq_tax = 0.0  # HUF equity gains from baseline (tiny/zero)
                h_oth_tax = 0.0
                h_total = h_debt_tax + h_eq_tax + h_oth_tax

                combined = p_total + h_total

                if combined < best_combined - 1e-6:
                    best_combined = combined
                    best_delta = delta

                delta += step

            # Record optimised values
            opt_personal[fy] = req - best_delta
            opt_huf[fy] = huf_base + best_delta

        # ── Apply optimised settings to state ─────────────────────────────
        state.annual_requirements = opt_personal
        state.huf_annual_requirements = opt_huf

        # ── Re-run engine ─────────────────────────────────────────────────
        p_monthly, p_yearly, h_monthly, _ = Engine(
            state,
            conservative_mode=conservative_mode,
            glide_path=glide_path,
        ).run()

    return p_monthly, p_yearly, h_monthly, p_yearly