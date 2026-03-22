# SWP Financial Planner

A desktop application for Indian retirees planning a **Systematic Withdrawal Plan (SWP)** from a portfolio of mutual funds. It models 30 years of withdrawals, optimises fund selection and allocation across multiple time chunks, computes taxes (Individual + HUF split), benchmarks against FD returns, and runs Monte Carlo stress tests.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Data Pipeline](#data-pipeline)
- [Module Reference](#module-reference)
- [Key Algorithms](#key-algorithms)
- [Configuration](#configuration)
- [Installation](#installation)
- [Usage](#usage)
- [File Outputs](#file-outputs)

---

## Overview

The core thesis is that a well-structured portfolio of **debt + arbitrage mutual funds** split across an **Individual + HUF** entity pair can significantly outperform fixed deposits on an after-tax basis over a 30-year retirement horizon.

The planner:

1. Downloads live AMFI NAV data and computes fund quality metrics (Sharpe, Sortino, Calmar, Alpha, Max DD, Combined Ratio) across 3Y / 5Y / 10Y windows.
2. Uses a **Mixed-Integer Linear Program** (HiGHS via SciPy, or PuLP/CBC) to allocate capital across the best funds, subject to constraints on return, volatility, drawdown, and per-fund / per-type / per-AMC concentration.
3. Simulates month-by-month withdrawals for 30 years using a full tax engine (progressive slabs, LTCG, STCG, 87A rebate, cess, exit loads).
4. Optionally optimises fund selection across multiple time chunks with a **Two-Pass Aim-and-Track** backward-induction algorithm that minimises portfolio turnover between chunks.
5. Runs a **Historical Block Bootstrap** Monte Carlo using real Nifty 50 and Nifty Composite Debt index data to stress-test corpus survival probability.

---

## Architecture

### High-Level Component Map

```
┌─────────────────────────────────────────────────────────────────────┐
│                        DATA ACQUISITION LAYER                       │
│                                                                     │
│  get_amfi_fund_schemes_names.py      fetch_amfi_aum.py              │
│        │ NAVAll.txt                        │ Fund-Performance API   │
│        ▼                                  ▼                         │
│  mutual_funds.csv                  amfi_aum.csv                     │
│        │                                  │                         │
│        └──────────────────┬───────────────┘                         │
│                           ▼                                         │
│                 get_funds_data.py                                   │
│            (NAV history → risk metrics)                             │
│                           │                                         │
│                           ▼                                         │
│                Fund_Metrics_Output.csv                              │
│                   (807 funds, 33 columns)                           │
└─────────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      OPTIMISATION LAYER                             │
│                                                                     │
│                    allocate_funds.py                                │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  MILP Solvers                                                │  │
│  │  _solve()            HiGHS via scipy.milp  (Coarse/Fine/α)  │  │
│  │  _solve_frontier()   Frontier Walk          (HiGHS)          │  │
│  │  run_pulp_*()        PuLP / CBC             (Commonality)    │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  Multi-Chunk Orchestration                                   │  │
│  │  run_aim_pass_multi()     λ-blending candidate generation    │  │
│  │  run_frontier_walk()      risk-floor candidate generation    │  │
│  │  run_pulp_commonality_walk()  PuLP commonality walk          │  │
│  │  score_combinations()     cross-chunk combination scoring    │  │
│  │  run_aim_pass()           Two-Pass Aim   (Pass 1)            │  │
│  │  run_track_pass()         Two-Pass Track (Pass 2, backward)  │  │
│  │  optimize_sticky_portfolio()  top-level Mode A / B driver    │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  Post-Allocation                                             │  │
│  │  fine_tune()              quality-aware weight rebalance     │  │
│  │  _substitution_advisor()  risk-reducing fund swap advisor    │  │
│  └──────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       GLIDE PATH LAYER                              │
│                                                                     │
│                       glide_path.py                                 │
│                                                                     │
│  build_glide_path()      year-by-year weight schedule (years 1–30) │
│  build_flat_glide_path() Mode A (buy-and-hold, constant weights)   │
│                                                                     │
│  Linear interpolation over a transition window centred on each     │
│  chunk boundary spreads rebalancing over N years to minimise CGT.  │
└─────────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      SIMULATION LAYER                               │
│                                                                     │
│                        engine.py                                    │
│                                                                     │
│  Month-by-month for up to 360 months:                               │
│  • Per-fund FIFO lot tracking + per-fund NAV growth                 │
│  • Bounded Smart Withdrawal waterfall (weight-drift correction)     │
│  • Annual micro-rebalancing (HIFO lot selection, no-trade band)     │
│  • Full tax computation at FY boundaries                            │
│    (progressive slab + LTCG + STCG + 87A + cess + exit loads)      │
│  • HUF parallel portfolio                                           │
│  • FD benchmark in parallel                                         │
│                                                                     │
│  Outputs: List[MonthlyRow], List[YearSummary]  (personal + HUF)    │
└─────────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     MONTE CARLO LAYER                               │
│                                                                     │
│                     monte_carlo.py                                  │
│                                                                     │
│  Mode 1: Historical Block Bootstrap                                 │
│    • Nifty 50 NAV history (mfapi.in → AMFI portal fallback)        │
│    • Nifty Composite Debt Index (embedded)                          │
│    • Contiguous blocks preserve volatility clustering               │
│  Mode 2: Log-normal fallback                                        │
│                                                                     │
│  σ per FY = Σ(w_i × σ_i)  [linear, perfect-correlation upper bound]│
│  Floor = mu − N × sigma  (default N = 3)                           │
│                                                                     │
│  Outputs: MCResults (P5/P25/P50/P75/P95 corpus & cash, ruin prob)  │
└─────────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       UI LAYER  (PySide6)                           │
│                                                                     │
│  run.py                  launch, per-user output directory          │
│  main.py                 MainWindow, 4-scenario tabs, menus         │
│  dialogs.py              income, requirements, windfalls, HUF, FD   │
│  fund_dialog.py          fund selection & allocation viewer/editor  │
│  tax_dialog.py           tax rules editor (slabs, LTCG rates)       │
│  chart_dialog.py         Matplotlib chart pop-ups (non-modal)       │
│  optimization_report.py  post-optimisation 4-tab summary            │
│  chunk_editor.py         reusable year-range chunk table widget     │
└─────────────────────────────────────────────────────────────────────┘
```

### Data Model

```
AppState
├── funds: List[FundEntry]                  ← flat fund list (Mode A fallback)
├── allocation_chunks: List[AllocationChunk]
│   ├── year_from, year_to
│   ├── funds: List[FundEntry]
│   │   └── name, fund_type (debt/equity/other), allocation (₹L)
│   │       std_dev, sharpe, sortino, calmar, alpha, treynor
│   │       max_dd, beta, combined_ratio
│   │       cagr_1/3/5/10, worst_exp_ret, amfi_fund_type
│   ├── target_weights: Dict[fund_name, weight]   ← set by optimizer
│   └── constraint_slack_used: Dict               ← set by track pass
│
├── individual_debt_chunks:   List[TaxChunk]       (progressive slabs + 87A)
├── individual_equity_chunks: List[EquityTaxChunk] (12.5% LTCG + exemption)
├── individual_other_chunks:  List[OtherTaxChunk]  (12.5% flat, Gold/Intl)
├── huf_debt/equity/other_chunks                   (same structure, no 87A)
│
├── annual_requirements: Dict[year, amount_L]
├── huf_withdrawal_chunks / huf_annual_requirements
├── return_chunks / fd_rate_chunks / split_chunks
├── windfalls / personal_income / huf_income
│
├── allocation_mode: "singular" | "chunked_sticky"
├── rebalance_spread_years: int             (glide path transition width)
└── glide_path: Optional[GlidePath]
        └── schedule: Dict[year(1–30), Dict[fund_name, weight]]
```

---

## Data Pipeline

```
AMFI NAVAll.txt ─────────────────────────────────────────────┐
AMFI Fund-Performance API                                    │
         │                                                   │
         ▼                                                   │
 get_amfi_fund_schemes_names.py                              │
   Downloads NAVAll.txt → (Fund Type, Fund Name) CSV         │
   Optional AUM filter via fetch_amfi_aum.py                 │
   Output: mutual_funds.csv / mutual_funds_minNcr.csv        │
         │                                                   │
         ▼                                                   │
   [User adds Allocation_L column marking funds of interest] │
         │                                                   │
         ▼                                                   │
 get_funds_data.py                                           │
   AMFI code resolution (exact → fuzzy → overrides)          │
   NAV download: mfapi.in primary, AMFI portal fallback      │
   NAV split/consolidation auto-correction                   │
   Risk-free rate: overnight fund NAV (RBI repo fallback)    │
   Benchmark: Nifty Composite Debt Index                     │
   Metrics per 3Y / 5Y / 10Y window:                         │
     Std_Dev, Sharpe, Sortino, Max_DD, Calmar                │
     Combined_Ratio = sqrt(Sortino × Calmar)                 │
   Alpha / Beta / Treynor (10Y regression vs benchmark)      │
   Worst_Exp_Ret_% = min(CAGR windows) − 0.40% STT (arb)    │
   Output: Fund_Metrics_Output.csv                           │
         │                                                   │
         │  reclassify_legacy_funds.py                       │
         │    Re-classifies pre-SEBI "Income"/"Growth" funds │
         │    by scraping Groww.in for current category      │
         │                                                   │
         ▼                                                   │
 Fund_Metrics_Output.csv ◄───────────────────────────────────┘
   (807 funds, 33 metric columns)
         │
         ▼
 allocate_funds.py → AllocationChunk.target_weights
```

---

## Module Reference

### `configuration.py`
Singleton that reads `RetirementTaxPlanning.configuration` once at startup. All modules access it via `from configuration import config`. Values auto-cast to int / float / bool.

---

### `models.py`
Pure data layer — dataclasses only, no UI or computation.

| Class | Purpose |
|---|---|
| `FundEntry` | Single fund: allocation, all risk metrics, CAGRs, amfi_fund_type |
| `AllocationChunk` | Time-period bucket: funds + target_weights + constraint_slack_used |
| `GlidePath` | Year-by-year weight schedule (1–30) for glide-path rebalancing |
| `AppState` | Master state — serialised to/from JSON for project save/load |
| `TaxChunk` / `TaxSlab` | Progressive slab tax per time period (debt) |
| `EquityTaxChunk` | Flat LTCG rate + annual exemption per time period |
| `OtherTaxChunk` | 12.5% LTCG flat (Gold ETFs / International ETFs) |
| `ReturnChunk` | Expected annual return per time period |
| `FDRateChunk` | FD benchmark interest rate per time period |
| `HUFWithdrawalChunk` | Annual HUF withdrawal target per time period |
| `WindfallEntry` | One-off corpus addition in a given plan year |
| `RebalanceCost` | Tax + exit loads incurred in a single rebalancing year |

---

### `get_amfi_fund_schemes_names.py`
Downloads the AMFI `NAVAll.txt` master feed and produces a clean CSV of `(Fund Type, Fund Name)` pairs. Optionally cross-references `fetch_amfi_aum.py` to filter by minimum AUM (e.g. ≥ ₹1,000 Cr).

```
python get_amfi_fund_schemes_names.py \
    --output-dir ./Schemes_and_Funds \
    [--min-aum 1000] \
    [--date DD-Mon-YYYY]
```

---

### `fetch_amfi_aum.py`
POSTs to the AMFI fund-performance API across all 39 open-ended SEBI subcategories to collect daily AUM per fund. Rate-limited at 0.4 s between calls. Produces `amfi_aum.csv`.

```
python fetch_amfi_aum.py \
    --output-dir ./Schemes_and_Funds \
    [--date DD-Mon-YYYY] \
    [--min-aum 0]
```

---

### `get_funds_data.py`
The fund metrics engine. For each fund in the input CSV:

1. **AMFI code resolution** — exact name match → fuzzy match → `KNOWN_CODE_OVERRIDES` dict
2. **NAV download** — mfapi.in primary, AMFI portal fallback; NAV split/consolidation auto-correction
3. **Risk-free rate** — dynamic, derived from overnight fund NAV history; RBI repo rate fallback pre-2019
4. **Benchmark** — Nifty Composite Debt Index (embedded) for Alpha / Beta / Treynor
5. **Metrics** — per 3Y / 5Y / 10Y window: Std_Dev, Sharpe, Sortino, Max_DD, Calmar; `Combined_Ratio = sqrt(Sortino × Calmar)`
6. **Worst_Exp_Ret_%** — `min(1Y, 3Y, 5Y, 10Y CAGR) − 0.40%` STT hit for arbitrage/tax-efficient funds

```
python get_funds_data.py \
    --input Fund_Details.csv \
    [--output Fund_Metrics_Output.csv] \
    [--workers 8]
```

---

### `reclassify_legacy_funds.py`
Handles pre-SEBI-categorisation funds labelled as generic "Income" or "Growth" types. Scrapes Groww.in to determine whether each fund is still active and classifies it as Equity or Debt. Produces `reclassified_funds.csv`, `closed_funds.csv`, and `unresolved_funds.csv`.

```
python reclassify_legacy_funds.py \
    --input mutual_funds_unclassified.csv \
    [--output-dir ./output] \
    [--delay 1.5]
```

---

### `allocate_funds.py`
The portfolio optimisation engine. Reads `Fund_Metrics_Output.csv` and solves a Mixed-Integer Linear Program to allocate capital.

#### MILP Formulation

```
Variables:   w_i ∈ [0, max_per_fund]    (continuous weight for fund i)
             y_i ∈ {0, 1}               (binary inclusion indicator)

Objective:   maximise  Σ w_i × (adj_ret_i + λ × quality_norm_i)
             where  quality_norm = Combined_Ratio / max(Combined_Ratio)
                    λ = 10% of return spread   (quality as tiebreaker only)

Constraints:
  C1:  Σ w_i = 1                              (full investment)
  C2:  Σ w_i × ret_i  ≥ min_return            (return floor)
  C3:  Σ w_i × std_i  ≤ max_std_dev           (volatility ceiling)
  C4:  Σ w_i × |dd_i| ≤ max_dd               (drawdown ceiling)
  C5+: Σ w_i [type=T] ≤ max_per_type          (per-SEBI-subcategory caps)
  C6:  w_i ≤ max_per_fund × y_i               (semi-continuous upper link)
  C7:  w_i ≥ min_per_fund × y_i               (semi-continuous lower link)
  C8+: Σ w_i [AMC=A]  ≤ max_per_amc           (per-AMC concentration cap)
```

The AMC for each fund is derived from the first word of the fund name (e.g. "ICICI", "HDFC", "Kotak"), which groups all funds from the same house under a single cap.

#### Three Solver Modes

| Mode | Objective | Use |
|---|---|---|
| **Coarse** | Minimise `std + \|dd\|` subject to return floor | Initial allocation — finds lowest-risk feasible portfolio |
| **Fine** | Maximise `return + λ·quality` subject to risk ceilings | Maximises return within explicit risk constraints |
| **Blended (α)** | `α × risk_obj + (1−α) × (−return_obj)` | Sweeps α from 1.0 → 0.0 to generate a frontier of candidates |

#### Multi-Chunk Candidate Generation

Three methods, selectable in the UI:

```
Method 1: λ-Blending  (run_aim_pass_multi)
  α values: 1.0, 0.975, 0.95, ...  → up to N candidate portfolios per chunk
  Each candidate must have a distinct fund set (de-duplicated by frozenset).

Method 2: Frontier Walk  (run_frontier_walk)
  P0: minimise(std + |dd|)  s.t. return ≥ target           → risk₀
  Pk: minimise(std + |dd|)  s.t. return ≥ target,
                                 (std + |dd|) ≥ risk_{k-1} + ε
  Forces strictly increasing portfolio risk each step → diverse candidates.
  Per-fund risk cap computed from P0 to prevent volatile outliers.

Method 3: PuLP Commonality Walk  (run_pulp_commonality_walk)
  CBC MILP, iterates with increasing std_dev floors.
  Combination scorer targets:
    ~60% of unique funds common to ALL N chunks
    ≥20% of unique funds common to (N-1) chunks
  → maximises fund overlap to minimise rebalancing cost.
```

#### Two-Pass Aim-and-Track (Mode B)

```
Pass 1 — AIM  (run_aim_pass):
  Solve each chunk independently, zero turnover penalty.
  Records the optimal D/E/O ratios → chunk._type_ratios.
  These ratios are LOCKED for Pass 2.

Pass 2 — TRACK  (run_track_pass, backward induction):
  Working backwards from the last chunk:
  For chunk k, anchor = target_weights of chunk k+1 (already solved).
  Minimise:
      Σ sqrt((w_i − anchor_i)² + ε)     [soft-L1 total turnover]
    + Σ P(i) × max(0, anchor_i − w_i)   [absence penalty for new funds]
  Subject to:
      D/E/O type ratios == chunk._type_ratios  [locked from Pass 1]
      All original return / risk constraints (with soft tolerances)
  
  This completely eliminates "Conservative Drag": the solver cannot
  reduce equity allocation in early chunks to avoid a future sell,
  because the asset-class ratios for each chunk are fixed.
```

#### Substitution Advisor

After allocation, identifies "outlier" funds (individual std_dev > 2× portfolio weighted std), searches the full universe for lower-risk replacements, and suggests swaps. A swap is dropped if it causes > 0.1% portfolio return loss. Dropped outliers get weight shifted to successfully swapped-in candidates where possible.

---

### `glide_path.py`

Builds the `GlidePath` — a `Dict[year(1–30), Dict[fund_name, weight]]` — from per-chunk `target_weights`.

#### Transition Window Formula

```
Chunk boundary at year B, transition width = spread_years (default 4):

  left_start = B − (spread // 2) + 1
  right_end  = left_start + spread − 1
  (clipped so year 1 is a clean buy, year 30 holds to end-of-life)

  For year y in [left_start, right_end]:
      t = (y − left_start + 1) / (right_end − left_start + 1)
      weight(fund) = (1 − t) × w_left(fund) + t × w_right(fund)
```

Linear interpolation means the engine's monthly withdrawals preferentially sell over-weighted funds, doing rebalancing work each month rather than in one large event. This avoids large CGT crystallisation at chunk boundaries.

---

### `engine.py`

The core simulation engine. Runs month-by-month for up to 360 months.

#### Per-Fund FIFO Lot Tracking

Each fund has its own `FIFOBucket` (list of `Lot` objects: units, purchase NAV, purchase month). NAV grows at each fund's own CAGR (5Y preferred, fallback 3Y → 1Y → category average). Redemptions consume lots FIFO. CGT calculation at FY boundaries uses exact lot history.

#### Bounded Smart Withdrawal

```
Each month, before redeeming to meet the withdrawal target:

1. Check WEIGHT DRIFT:
   If any fund's actual weight deviates from target by > 1.5%,
   enter weight-correction mode.

2. Check RETURN DRIFT:
   If blended portfolio return > anchor_return + cap (0.15% personal),
   enter return-correction mode.

3. If both within tolerance → proportional withdrawal (no correction).

4. Correction mode:
   A. Sort over-weighted funds descending by weight deviation (or return).
   B. Sell excess from over-weighted funds top-down.
   C. Pro-rata fallback from all funds if excess insufficient.
```

#### Annual Micro-Rebalancing (Mode B)

```
Triggered at glide-path transition years:

1. No-trade check:  if Σ|target_w − current_w| < 0.5% → skip entirely.

2. HIFO lot selection:
   Sort lots ascending by unrealised gain per unit.
   Sell lowest-gain lots first → minimises CGT 30–50% vs FIFO.
   (Monthly SWP withdrawals still use FIFO — SEBI requirement.)

3. SWP-assisted rebalancing:
   Raise the monthly withdrawal amount preferentially from over-weighted
   funds first → withdrawals do rebalancing work at no extra tax cost.

4. Tax budget cap:
   Lookahead: simulate the full transition cost on a deep-copy portfolio.
   Annual tax budget = total_transition_tax / spread_years.
   Sells stop when the running tax tally hits the budget.

5. Self-funded tax:
   Rebalancing CGT is deducted from portfolio cash (not from user's
   SWP income). Buys are scaled to net_cash_raised − taxes_paid.
```

#### Tax Computation (FY Boundary)

| Entity | Fund Type | Tax |
|---|---|---|
| Individual | Debt | Progressive slab; if total income ≤ exempt_limit → 0; else marginal relief |
| Individual | Equity / Arb | 12.5% LTCG on gains above annual exemption (default ₹1.25L, rising) |
| Individual | Other (Gold/Intl) | 12.5% flat, no exemption |
| HUF | Debt | Same slabs; no 87A; basic exemption absorbs LTCG |
| HUF | Equity / Other | 12.5%; unused basic exemption from debt slab offsets gains |
| Both | STCG (< 12 months) | Flat rate; 1% exit load also charged |
| All | — | 4% cess on all computed tax |

---

### `monte_carlo.py`

Sequence-of-returns risk simulation.

#### Mode 1 — Historical Block Bootstrap (default)

```
Data sources:
  Equity: Nifty 50 Index Fund NAV (mfapi.in → AMFI portal fallback)
          Cached to mc_nifty50_nav.csv (refreshed if > 7 days old)
  Debt:   Nifty Composite Debt Index (embedded in get_funds_data.py)
          Cached to mc_debt_index.csv

Block construction:
  Draw contiguous blocks of block_length consecutive years (default 3).
  Preserves volatility clustering (bad years follow bad years).
  Equity and debt blocks share the same block-start index,
  preserving the historical equity–debt correlation.

Blending:
  r_portfolio = (w_equity + w_other) × r_equity_centred
              +  w_debt              × r_debt_centred

Per-FY centering:
  r_centred = r_historical − mean(r_history) + mu_det[fy]
  Preserves historical shape while aligning the mean to the plan's
  expected return for that chunk. "Other" (Gold, hybrid, international)
  is treated as equity — conservative / higher-vol assumption.

Floor:
  r[fy] = max(r[fy], mu_det[fy] − floor_multiplier × sigma[fy])
  Default floor_multiplier = 3  (floor = mu − 3σ)
```

#### Mode 2 — Log-Normal Fallback

```
r ~ LogNormal(mu_ln, sigma)
where mu_ln = log(1 + mu_det) − 0.5 × sigma²
      sigma = Σ(w_i × sigma_i)   [linear, perfect-correlation upper bound]
```

#### Sigma Convention

The per-FY sigma used by Monte Carlo is the **linear allocation-weighted average** of fund std_devs:

```
sigma = Σ(w_i × sigma_i)
```

This equals `Σ(w_i × sigma_i)` — the **perfect-correlation upper bound** — and is the same number displayed in the "View Fund Selection & Allocation" dialog header (`Std:X.XX%`). It is the highest valid portfolio volatility estimate, making the Monte Carlo fan charts conservative (wider).

```
Formula ordering for reference:
  sigma_rms_correct = sqrt(Σ w_i² × sigma_i²)   ← zero correlation  (lower bound)
  sigma_lin         = Σ w_i × sigma_i            ← perfect correlation (upper bound, used)
  sigma_rms_old     = sqrt(Σ w_i × sigma_i²)     ← not a valid portfolio formula;
                                                     by Jensen's inequality ≥ sigma_lin
```

---

### `tax_dialog.py`
Four-tab Qt dialog for editing all tax rules:

| Tab | Contents |
|---|---|
| Individual – Debt | Time-chunked progressive slab editor (lower / upper / rate per slab; 87A exempt limit per chunk) |
| Individual – Equity/Arb LTCG | Flat LTCG rate + annual exemption per time period |
| HUF – Debt | Same slab structure; basic exemption instead of 87A |
| HUF – Equity/Arb LTCG | Same as Individual equity |

---

### `fund_dialog.py`
View and edit fund allocations per chunk.

- Multi-chunk colour coding: 🟢 fund present in all chunks, 🟠 some chunks, 🔴 one chunk only.
- Live portfolio Yield, Std, and |DD| header recomputed as allocations are edited.
- **Std displayed = linear allocation-weighted average** (`Σ w_i × σ_i`) — identical to the sigma used by Monte Carlo.
- Sort by any risk metric; filter by fund type (debt / equity / other / all).

---

### `optimization_report.py`
Four-tab post-optimisation summary dialog, shown after "Optimize Sticky Portfolio" runs:

| Tab | Contents |
|---|---|
| Glide Path Summary | Per-chunk D/E/O ratios, constraint slack consumed, year-by-year stacked weight chart |
| Fund Selection | Fund weights per chunk, carry-over vs new funds, turnover between consecutive chunks |
| Tax Attribution | Per-year SWP tax, rebalancing tax, exit loads; lifetime totals and percentages |
| Robustness | Constraint slack traffic-light per chunk, years where drift tolerance skipped rebalancing |

---

### `chart_dialog.py`
Non-modal Matplotlib chart windows — stay open alongside the main UI. Charts: deterministic corpus trajectory, net cash per year, Monte Carlo fan charts (P5–P95 band + worst/best paths), per-fund corpus breakdown, tax savings vs FD benchmark.

---

### `chunk_editor.py`
Generic reusable `ChunkTableWidget` (PySide6) for any year-range parameter table. Enforces continuity automatically: `year_from[row N+1]` is always set to `year_to[row N] + 1`. Used for FD rate chunks, return chunks, HUF withdrawal chunks, and the allocation parameter chunk tables in `AllocateCapitalDialog`.

---

### `dialogs.py`
All non-chart secondary dialogs: annual withdrawal requirements editor, other income (salary, rental, pension, interest), windfalls, HUF withdrawal targets, FD rate chunks, return rate chunks, and the tax-optimal Individual/HUF split optimizer.

---

### `main.py`
`MainWindow` with four independent scenario tabs (Option 1–4). Tax rules, income, and annual requirements are shared across scenarios; fund allocations and return assumptions are scenario-specific.

Key embedded dialogs:

- **`AllocateCapitalDialog`** — full multi-chunk allocation UI with Coarse / Fine mode toggle, chunk parameter table, live fund-count history-status label, and log window for the subprocess run.
- **`FetchSchemeNamesDialog`** / **`FetchFundMetricsDialog`** — threaded progress dialogs for the data pipeline steps (run in a subprocess to keep the UI responsive).

---

### `run.py`
Launch script. Prompts for a user name at startup (Qt dialog), sanitises it for use as a directory name, creates `<project_root>/<user_name>/` as the per-user output directory, and opens `MainWindow`.

---

## Key Algorithms

### Why Linear Std Dev (not RMS)?

The Monte Carlo engine and the fund dialog both use `sigma_lin = Σ(w_i × sigma_i)`. This assumes **perfect correlation** between all funds — the most conservative valid portfolio volatility estimate. Using the upper bound means the Monte Carlo fan charts are conservative (wider spread, higher ruin probability), which is appropriate for retirement planning where you want to stress-test against the worst case.

The old formula `sqrt(Σ w_i × sigma_i²)` was not a valid portfolio volatility formula at all — by Jensen's inequality it exceeds `sigma_lin`, so it was actually over-estimating volatility while being conceptually wrong.

### Backward Induction Eliminates Conservative Drag

A naïve single-pass penalised optimiser suffers the "Binary Cliff": if the turnover penalty is too high, the solver holds safe assets early to avoid future sells. Two-Pass decouples these concerns: Pass 1 locks the D/E/O ratios; Pass 2 can only choose *which* funds fill each bucket. The solver is physically prevented from changing asset-class allocations to reduce turnover.

### HIFO Tax-Alpha

During rebalancing, lots are sorted by **ascending unrealised gain** (Highest cost-basis In, First Out). Selling the cheapest-to-realise lots first reduces realised CGT by 30–50% compared to FIFO in a mature portfolio with appreciated holdings.

### SWP-Assisted Rebalancing

Monthly withdrawal cash is raised preferentially from over-weighted funds. Regular withdrawals do rebalancing work at no extra tax or transaction cost, reducing the volume of explicit rebalancing trades needed.

---

## Configuration

`RetirementTaxPlanning.configuration`:

```ini
# Tax
cess_rate                    = 0.04     # 4% health & education cess
fallback_equity_ltcg_rate    = 0.125    # 12.5% LTCG fallback
fallback_other_ltcg_rate     = 0.125    # 12.5% for Gold/Intl
fallback_debt_top_rate       = 0.30     # 30% top debt slab fallback
stcg_holding_months          = 12       # months before LTCG treatment
exit_load_fraction           = 0.01     # 1% exit load within STCG period

# Withdrawal
swp_start_month              = 3        # month withdrawals begin
smart_withdrawal_start_month = 18       # month smart withdrawal activates
drift_cap_personal           = 0.0015   # 0.15% return drift cap (personal)
drift_cap_huf                = 0.0050   # 0.50% return drift cap (HUF)
weight_drift_threshold       = 0.015    # 1.5% per-fund weight drift trigger

# Rebalancing
rebalance_no_trade_band      = 0.005    # 0.5% total drift no-trade threshold

# AMFI
amfi_sleep_between_calls     = 0.4      # seconds between API calls

# File paths (relative to project root)
allocator_default_input      = Fund_Metrics_Output.csv
allocator_default_output     = allocation_result.csv
default_cagr_fallback        = 7.0      # % fallback when no CAGR data
```

---

## Installation

```bash
# 1. Clone
git clone https://github.com/<your-username>/RetirementTaxPlanning.git
cd RetirementTaxPlanning

# 2. Create virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Linux / macOS
# .venv\Scripts\activate           # Windows

# 3. Install dependencies
pip install -r requirements.txt
```

Core dependencies: `PySide6`, `pandas`, `numpy`, `scipy`, `matplotlib`, `requests`, `pulp`, `tqdm`.

> **Linux note:** PySide6 requires Qt platform plugins.
> `sudo apt-get install libxcb-cursor0 libxcb-xinerama0` if you see platform errors at startup.

---

## Usage

### 1. Launch

```bash
python run.py
```

Enter your name at the startup prompt. All outputs go to `<project_root>/<your_name>/`.

### 2. Build the fund universe  *(Data menu)*

```
Data → Fetch Scheme Names
  Downloads NAVAll.txt, produces mutual_funds.csv.
  Optionally filter by AUM (e.g. ≥ ₹1,000 Cr).

Data → Fetch Fund Metrics
  Runs get_funds_data.py on your fund list.
  Produces Fund_Metrics_Output.csv.
  Allow 10–30 min for 500+ funds.
```

### 3. Allocate capital  *(Data menu)*

```
Data → Allocate Capital

  Mode: Coarse (minimise risk) or Fine (maximise return)

  Per-chunk parameters:
    Min Ret%    minimum expected return floor (e.g. 7.25%)
    Max/Fund%   max allocation to any single fund (e.g. 8%)
    Min/Fund%   min allocation if a fund is selected (e.g. 2%)
    Max/Type%   max allocation to any SEBI sub-category (e.g. 24%)
    Max/AMC%    max allocation to any one AMC house (e.g. 16%)
    Min Hist Y  minimum fund history in years (e.g. 12)

  Status bar: "N funds with minimum history of Y years selected out of M funds"
  updates live as you change Min Hist Y.

  Click ▶ Run Allocation.
  Click ⟳ Apply Substitutions to accept the advisor's swap recommendations.
```

### 4. View and edit allocations  *(Portfolio menu)*

```
Portfolio → View Fund Selection & Allocation
  Review the optimizer's fund choices.
  Edit Allocation (L) column directly if desired.
  Header shows live: Yield, Std (lin), |DD|, D%/E%/O% ratios.
```

### 5. Optimise the glide path  *(Portfolio menu, optional)*

```
Portfolio → Optimize Sticky Portfolio
  Runs the Two-Pass Aim-and-Track algorithm.
  Minimises fund turnover between adjacent time chunks.
  Opens the Optimization Report dialog (glide path, fund selection,
  tax attribution, robustness tabs).
```

### 6. Run the simulation  *(Calculate menu)*

```
Calculate → Run Calculations
  30-year month-by-month simulation.
  Results appear in the main table (year-by-year corpus, tax, cash).
  Open charts via the chart buttons.

Calculate → Monte Carlo
  Historical block bootstrap (default) or log-normal fallback.
  Shows corpus fan chart (P5–P95), ruin probability,
  and the marginal ruin path (best sim that still went bankrupt).
```

---

## File Outputs

All written to `<project_root>/<user_name>/`:

| File | Produced by | Contents |
|---|---|---|
| `Fund_Metrics_Output.csv` | `get_funds_data.py` | 33-column metrics for all analysed funds |
| `allocation_chunk_N_yrX-Y.csv` | `allocate_funds.py` | Fund weights and metrics for chunk N |
| `allocation_summary.csv` | `allocate_funds.py` | All chunks in one file with portfolio totals |
| `portfolio_viz.html` | `allocate_funds.py` | Standalone interactive risk-return visualisation |
| `allocation_params.json` | `main.py` | Persisted allocation dialog settings |
| `*.swp_project` | `main.py` | Full project save (AppState as JSON) |
| `mc_nifty50_nav.csv` | `monte_carlo.py` | Cached Nifty 50 NAV history (refreshed weekly) |
| `mc_debt_index.csv` | `monte_carlo.py` | Cached Nifty Composite Debt Index series |
| `Schemes_and_Funds/mutual_funds.csv` | `get_amfi_fund_schemes_names.py` | Full AMFI fund universe |
| `Schemes_and_Funds/amfi_aum.csv` | `fetch_amfi_aum.py` | Daily AUM per fund |