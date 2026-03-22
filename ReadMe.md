# 📊 SWP Financial Planner

> A desktop application for Indian retirees planning a **Systematic Withdrawal Plan (SWP)** from a portfolio of mutual funds — optimising fund selection, computing taxes across Individual and HUF entities, and stress-testing corpus survival with Monte Carlo simulation over a 30-year horizon.

---

## ✨ What it does

The core thesis: a well-structured portfolio of **debt + arbitrage mutual funds** split across an **Individual + HUF** entity pair can significantly outperform fixed deposits on an after-tax basis over 30 years.

| Capability | Detail |
|---|---|
| 📥 **Data Acquisition** | Downloads live AMFI NAV data, computes Sharpe / Sortino / Calmar / Alpha / Max DD across 3Y / 5Y / 10Y windows |
| 🧮 **Portfolio Optimisation** | Mixed-Integer Linear Program (HiGHS + PuLP/CBC) with constraints on return, volatility, drawdown, per-fund, per-type, and per-AMC concentration |
| 📅 **Multi-Chunk Planning** | Separate optimal portfolios for different life phases (e.g. years 1–10, 11–20, 21–30) with minimised turnover between phases |
| 🔀 **Glide Path** | Linear weight interpolation across chunk boundaries — monthly withdrawals do the rebalancing, avoiding large CGT events |
| 💰 **Full Tax Engine** | Month-by-month simulation: progressive slabs, LTCG, STCG, 87A marginal relief, cess, exit loads — Individual and HUF in parallel |
| 🎲 **Monte Carlo** | Historical Block Bootstrap using real Nifty 50 + Nifty Composite Debt index data; log-normal fallback |
| 📋 **4-Scenario Comparison** | Run up to 4 independent allocation strategies side-by-side |

---

## 🗺️ System Architecture

```mermaid
graph TB
    subgraph DATA["📥 Data Acquisition Layer"]
        A1[get_amfi_fund_schemes_names.py<br/>AMFI NAVAll.txt feed] --> A3
        A2[fetch_amfi_aum.py<br/>AMFI Performance API] --> A3
        A3[mutual_funds.csv<br/>+ AUM filter] --> A4
        A4[get_funds_data.py<br/>NAV history → metrics] --> A5
        A4b[reclassify_legacy_funds.py<br/>Groww scraper] -.->|re-classifies pre-SEBI funds| A4
        A5[(Fund_Metrics_Output.csv<br/>807 funds · 33 columns)]
    end

    subgraph OPT["🧮 Optimisation Layer"]
        B1[allocate_funds.py]
        B2[MILP: _solve<br/>HiGHS · scipy.milp]
        B3[MILP: _solve_frontier<br/>Frontier Walk · HiGHS]
        B4[MILP: run_pulp_*<br/>Commonality Walk · CBC]
        B1 --> B2 & B3 & B4
    end

    subgraph GP["🔀 Glide Path Layer"]
        C1[glide_path.py<br/>build_glide_path]
        C2[GlidePath<br/>year 1–30 weight schedule]
        C1 --> C2
    end

    subgraph SIM["⚙️ Simulation Layer"]
        D1[engine.py · Engine.run]
        D2[Per-fund FIFO lot tracker]
        D3[Bounded Smart Withdrawal]
        D4[HIFO micro-rebalancing]
        D5[FY tax computation]
        D1 --> D2 & D3 & D4 & D5
    end

    subgraph MC["🎲 Monte Carlo Layer"]
        E1[monte_carlo.py]
        E2[Historical Block Bootstrap<br/>Nifty 50 + Debt Index]
        E3[Log-normal fallback]
        E1 --> E2 & E3
    end

    subgraph UI["🖥️ UI Layer · PySide6"]
        F1[main.py · MainWindow]
        F2[fund_dialog.py]
        F3[tax_dialog.py]
        F4[chart_dialog.py]
        F5[optimization_report.py]
        F6[dialogs.py]
        F7[chunk_editor.py]
    end

    A5 --> B1
    B1 --> C1
    C2 --> D1
    D1 --> E1
    D1 & E1 --> F1
    F1 --> F2 & F3 & F4 & F5 & F6

    style DATA fill:#1a1a2e,stroke:#4a90d9,color:#e0e0e0
    style OPT  fill:#16213e,stroke:#e94560,color:#e0e0e0
    style GP   fill:#0f3460,stroke:#53d8fb,color:#e0e0e0
    style SIM  fill:#1a1a2e,stroke:#4ade80,color:#e0e0e0
    style MC   fill:#16213e,stroke:#f59e0b,color:#e0e0e0
    style UI   fill:#0f3460,stroke:#a78bfa,color:#e0e0e0
```

---

## 🔄 End-to-End Data Flow

```mermaid
flowchart LR
    classDef file    fill:#1e3a5f,stroke:#4a90d9,color:#e0e0e0,rx:6
    classDef process fill:#2d1b4e,stroke:#a78bfa,color:#e0e0e0,rx:6
    classDef output  fill:#1a3a2a,stroke:#4ade80,color:#e0e0e0,rx:6
    classDef user    fill:#3a1a1a,stroke:#f87171,color:#e0e0e0,rx:6

    U1([👤 User\nmarks funds of interest]):::user
    P1[get_amfi_fund_schemes_names]:::process
    P2[fetch_amfi_aum]:::process
    F1[(mutual_funds.csv)]:::file
    F2[(amfi_aum.csv)]:::file
    F3[(Fund_Details.csv\nwith Allocation_L)]:::file
    P3[get_funds_data.py\nNAV download + metrics]:::process
    F4[(Fund_Metrics_Output.csv)]:::file
    P4[allocate_funds.py\nMILP optimisation]:::process
    F5[(allocation_chunk_N.csv\nper-chunk weights)]:::file
    P5[glide_path.py\nbuild schedule]:::process
    F6[(GlidePath\nyear → weights)]:::file
    P6[engine.py\n360-month simulation]:::process
    O1[(MonthlyRow × 360\nYearSummary × 30)]:::output
    P7[monte_carlo.py\nblock bootstrap]:::process
    O2[(MCResults\nfan charts + ruin %)]:::output

    P1 --> F1
    P2 --> F2
    F1 & F2 --> U1 --> F3
    F3 --> P3 --> F4
    F4 --> P4 --> F5
    F5 --> P5 --> F6
    F6 --> P6 --> O1
    O1 --> P7 --> O2
```

---

## 📐 Data Model

```mermaid
classDiagram
    class AppState {
        +List~FundEntry~ funds
        +List~AllocationChunk~ allocation_chunks
        +Dict annual_requirements
        +OtherIncome personal_income
        +OtherIncome huf_income
        +List~WindfallEntry~ windfalls
        +GlidePath glide_path
        +String allocation_mode
        +int rebalance_spread_years
        +to_dict() dict
        +from_dict(d) AppState
    }

    class AllocationChunk {
        +int year_from
        +int year_to
        +List~FundEntry~ funds
        +Dict target_weights
        +Dict constraint_slack_used
        +portfolio_yield() float
        +optimized_sigma() float
        +category_yield(type) float
    }

    class FundEntry {
        +String name
        +String fund_type
        +float allocation
        +float std_dev
        +float sharpe
        +float sortino
        +float calmar
        +float max_dd
        +float combined_ratio
        +float cagr_1/3/5/10
        +float worst_exp_ret
        +String amfi_fund_type
    }

    class GlidePath {
        +Dict schedule
        +weights_for_year(y) Dict
        +transition_years() List
        +is_flat() bool
    }

    class TaxChunk {
        +int year_from
        +int year_to
        +float exempt_limit
        +List~TaxSlab~ slabs
    }

    class EquityTaxChunk {
        +int year_from
        +int year_to
        +float tax_rate
        +float exempt_limit
    }

    AppState "1" --> "0..*" AllocationChunk
    AppState "1" --> "0..*" FundEntry
    AppState "1" --> "0..1" GlidePath
    AppState "1" --> "0..*" TaxChunk
    AppState "1" --> "0..*" EquityTaxChunk
    AllocationChunk "1" --> "1..*" FundEntry
```

---

## 🧮 Optimisation Pipeline

```mermaid
flowchart TD
    classDef decision fill:#3a2000,stroke:#f59e0b,color:#e0e0e0
    classDef solver   fill:#1a0030,stroke:#a78bfa,color:#e0e0e0
    classDef step     fill:#001a30,stroke:#4a90d9,color:#e0e0e0
    classDef output   fill:#001a20,stroke:#4ade80,color:#e0e0e0

    START([Fund_Metrics_Output.csv\n807 funds · 33 columns]):::output

    START --> FILTER[load_and_filter\nHistory ≥ N years\nDerive AMC column]:::step

    FILTER --> MODE{Allocation\nMode?}:::decision

    MODE -->|Coarse| COARSE[Minimise std+dd\nsubject to return floor]:::step
    MODE -->|Fine| FINE[Maximise return+quality\nsubject to risk ceilings]:::step
    MODE -->|Blended α| BLEND[Sweep α 1.0→0.0\ngenerate frontier]:::step

    COARSE & FINE & BLEND --> SOLVE

    subgraph SOLVE["MILP Formulation  ·  _solve()  +  _solve_frontier()  +  run_pulp_*()"]
        S1["C1: Σwᵢ = 1                     (full investment)"]
        S2["C2: Σwᵢ·retᵢ ≥ min_return      (return floor)"]
        S3["C3: Σwᵢ·stdᵢ ≤ max_std_dev     (volatility ceiling)"]
        S4["C4: Σwᵢ·|ddᵢ| ≤ max_dd         (drawdown ceiling)"]
        S5["C5+: Σwᵢ[type=T] ≤ max_per_type  (SEBI sub-type caps)"]
        S6["C6: wᵢ ≤ max_per_fund × yᵢ     (semi-continuous upper)"]
        S7["C7: wᵢ ≥ min_per_fund × yᵢ     (semi-continuous lower)"]
        S8["C8+: Σwᵢ[AMC=A] ≤ max_per_amc  (AMC concentration cap)"]
    end

    SOLVE --> RELAX{Feasible?}:::decision
    RELAX -->|No| LOOSEN[Relaxation ladder\nper-type → max_dd →\nmax_std → min_return]:::step
    LOOSEN --> SOLVE
    RELAX -->|Yes| CANDS[N candidate portfolios\nper chunk]:::output

    CANDS --> SCORE[score_combinations\nmaximise fund overlap\nacross all chunks]:::step
    SCORE --> SELECT[select_best_combination\nbest cross-chunk combo]:::step
    SELECT --> FINETUNE[fine_tune\nquality-aware weight\nrebalance on selected funds]:::step
    FINETUNE --> SUBADV[_substitution_advisor\nidentify outliers\nsuggest swaps]:::step
    SUBADV --> WEIGHTS[(target_weights\nper AllocationChunk)]:::output
```

---

## 🔀 Two-Pass Aim-and-Track

*Used by **Optimize Sticky Portfolio** to minimise fund turnover between time chunks.*

```mermaid
sequenceDiagram
    participant U as User
    participant M as main.py
    participant AIM as Pass 1 · Aim
    participant TRK as Pass 2 · Track
    participant GP as glide_path.py

    U->>M: Optimize Sticky Portfolio
    M->>AIM: run_aim_pass(chunks, universe)

    loop For each chunk (forward)
        AIM->>AIM: Full MILP · zero turnover penalty
        AIM->>AIM: Record optimal D/E/O ratios → _type_ratios
        AIM-->>M: chunk.target_weights (unconstrained optimal)
    end

    note over AIM,TRK: Asset-class ratios LOCKED after Pass 1.<br/>Pass 2 can only choose WHICH funds fill each bucket.

    M->>TRK: run_track_pass(chunks, universe)

    loop For each chunk (BACKWARD from last)
        TRK->>TRK: anchor = next chunk's target_weights
        TRK->>TRK: minimise Σ soft-L1(w − anchor) + absence penalty
        TRK->>TRK: subject to: D/E/O ratios == _type_ratios
        TRK-->>M: chunk.target_weights (turnover-minimised)
    end

    M->>GP: build_glide_path(chunks, spread_years)
    GP-->>M: GlidePath (year → weights, linearly interpolated)
    M-->>U: Optimization Report dialog
```

---

## ⚙️ Engine Simulation Loop

```mermaid
flowchart TD
    classDef monthly fill:#001a30,stroke:#4a90d9,color:#e0e0e0
    classDef yearly  fill:#001a20,stroke:#4ade80,color:#e0e0e0
    classDef tax     fill:#2d0a0a,stroke:#f87171,color:#e0e0e0
    classDef rebal   fill:#1a1a00,stroke:#f59e0b,color:#e0e0e0
    classDef decision fill:#2a1a00,stroke:#fb923c,color:#e0e0e0

    INIT([Invest corpus\nMonth 0 · FIFO lots created\nper-fund @ NAV=1.0]):::yearly

    INIT --> MLOOP

    subgraph MLOOP["Monthly Loop  (months 1–360)"]
        M1[Grow each fund's NAV\nby its own CAGR ÷ 12]:::monthly
        M2{Month ≥ SWP_START\n& corpus > 0?}:::decision
        M3[Compute target weights\nfrom GlidePath for this FY]:::monthly
        M4[Bounded Smart Withdrawal\ncheck weight drift + return drift\nsell over-weighted funds first]:::monthly
        M5[Redeem FIFO lots\nto meet withdrawal target]:::monthly
        M6[Record MonthlyRow\nprincipal · gain · corpus]:::monthly
        M1 --> M2
        M2 -->|No| M1
        M2 -->|Yes| M3 --> M4 --> M5 --> M6 --> M1
    end

    MLOOP --> FYCHECK{April?\nFY boundary}:::decision

    subgraph FYTAX["Annual Tax Block  (each April)"]
        T1[Pool all FY gains\ndebt · equity · other]:::tax
        T2[Individual:\nSlab tax + 87A rebate\non debt income]:::tax
        T3[Individual:\n12.5% LTCG on equity\nabove annual exemption]:::tax
        T4[HUF:\nSlab tax, no 87A\nbasic exemption absorbs LTCG]:::tax
        T5[Apply 4% cess\nto all tax]:::tax
        T6[Record YearSummary\ntax_personal · tax_huf\ntax_saved vs FD benchmark]:::tax
        T1 --> T2 & T3 & T4 --> T5 --> T6
    end

    FYCHECK -->|Yes| FYTAX

    FYCHECK -->|Yes,\nif transition year| REBAL

    subgraph REBAL["Glide-Path Micro-Rebalancing"]
        R1{Total drift\n< 0.5%?}:::decision
        R2[Skip — no-trade region\nGarleanu & Pedersen]:::rebal
        R3[Lookahead: estimate\ntotal transition CGT]:::rebal
        R4[Annual tax budget =\ntotal_CGT ÷ spread_years]:::rebal
        R5[HIFO lot selection\nlowest-gain lots first\n→ 30–50% CGT saving vs FIFO]:::rebal
        R6[SWP-assisted sell:\nraise withdrawal cash from\nover-weighted funds first]:::rebal
        R7[Buy under-weighted funds\nself-funded: tax deducted\nfrom portfolio cash]:::rebal
        R1 -->|Yes| R2
        R1 -->|No| R3 --> R4 --> R5 --> R6 --> R7
    end

    FYTAX --> NEXT[Next month]
    REBAL --> NEXT
    NEXT --> MLOOP

    MLOOP -->|360 months done| DONE[(MonthlyRow 360\nYearSummary 30)]:::yearly
```

---

## 🎲 Monte Carlo Simulation

```mermaid
flowchart LR
    classDef data    fill:#1e3a5f,stroke:#4a90d9,color:#e0e0e0
    classDef process fill:#2d1b4e,stroke:#a78bfa,color:#e0e0e0
    classDef output  fill:#1a3a2a,stroke:#4ade80,color:#e0e0e0
    classDef formula fill:#3a2000,stroke:#f59e0b,color:#e0e0e0

    EQ[(Nifty 50\nNAV history\n~25 FYs)]:::data
    DT[(Nifty Composite\nDebt Index\n~25 FYs)]:::data

    EQ & DT --> BLOCK[Block Bootstrap\nDraw contiguous 3-year blocks\nequity + debt share same start\n→ preserves correlation]:::process

    BLOCK --> BLEND["Blend per FY\nr = (w_eq + w_oth) × r_eq_centred\n   + w_debt × r_debt_centred"]:::formula

    BLEND --> CENTRE["Centre each year:\nr_centred = r_hist − μ_hist + μ_det[fy]\n→ preserves fat tails & clustering\n→ mean aligned to plan return"]:::formula

    CENTRE --> FLOOR["Apply floor:\nr = max(r, μ_det − 3σ)\nσ = Σ(wᵢ × σᵢ)  [linear, perfect-corr]"]:::formula

    FLOOR --> SIM["Simulate 2,000 paths\ncorpus × (1+r) − withdrawal\ntrack ruin when corpus ≤ 0"]:::process

    SIM --> PCTS["Percentiles per FY\nP5 · P25 · P50 · P75 · P95\ncorpus + net cash"]:::output

    SIM --> RUIN["Ruin probability\n+ marginal ruin path\n(best sim that went bankrupt)"]:::output

    SIM --> RAW["Raw arrays float32\n(2000 × 30)\nfor fan chart rendering"]:::output
```

---

## 🖥️ Application UI Flow

```mermaid
stateDiagram-v2
    [*] --> Startup: python run.py
    Startup --> MainWindow: Enter user name\ncreate output directory

    state MainWindow {
        [*] --> Scenario1
        Scenario1 --> Scenario2 : Option 2 tab
        Scenario2 --> Scenario3 : Option 3 tab
        Scenario3 --> Scenario4 : Option 4 tab

        note right of Scenario1
            4 independent scenarios
            Shared: tax rules, income,
                    requirements
            Per-scenario: funds,
                    return rates, allocation
        end note
    }

    state "Data Menu" as DM {
        FetchNames : Fetch Scheme Names\nNAVAll.txt → mutual_funds.csv
        FetchMetrics : Fetch Fund Metrics\nNAV history → Fund_Metrics_Output.csv
        AllocateCapital : Allocate Capital\nMILP → allocation_chunk_N.csv
        OptimizeSticky : Optimize Sticky Portfolio\nTwo-Pass Aim-and-Track → GlidePath
    }

    state "Configuration Menu" as CM {
        TaxRules : Tax Rules\n(Individual + HUF)
        Requirements : Annual Withdrawal Requirements
        FundView : View Fund Selection & Allocation
        ReturnRate : Portfolio Return Rate Chunks
        GlideParams : Glide-Path Parameters
    }

    state "Analysis Menu" as AM {
        Sensitivity : Sensitivity Analysis\n(return rate sweep)
        MonteCarlo : Monte Carlo Simulation\n(2000 paths, fan chart)
    }

    MainWindow --> DM
    MainWindow --> CM
    MainWindow --> AM

    DM --> RunCalc : Run Calculations\n(engine.py · 360-month sim)
    RunCalc --> Results : YearSummary table\ncharts · tax breakdown
    Results --> AM
```

---

## 📁 Module Map

```
RetirementTaxPlanning/
│
├── run.py                      ← Launch: user name dialog → MainWindow
├── main.py                     ← MainWindow · 4-scenario tabs · all menus
│
├── ── Data Pipeline ──
├── get_amfi_fund_schemes_names.py   AMFI NAVAll.txt → fund list CSV
├── fetch_amfi_aum.py                AMFI Performance API → AUM CSV
├── get_funds_data.py                NAV history → Fund_Metrics_Output.csv
├── reclassify_legacy_funds.py       Groww scraper → reclassify old fund types
│
├── ── Optimisation ──
├── allocate_funds.py                MILP portfolio optimiser (HiGHS + PuLP)
├── glide_path.py                    Chunk weights → year-by-year GlidePath
│
├── ── Simulation ──
├── engine.py                        360-month SWP simulator + tax engine
├── monte_carlo.py                   Historical block bootstrap MC
│
├── ── Data Model ──
├── models.py                        Dataclasses: AppState, FundEntry, chunks...
├── configuration.py                 Singleton config reader
│
├── ── UI Dialogs ──
├── fund_dialog.py                   Fund selection & allocation viewer/editor
├── tax_dialog.py                    Tax rules editor (slabs, LTCG rates)
├── chart_dialog.py                  Matplotlib chart pop-ups (non-modal)
├── optimization_report.py           Post-optimisation 4-tab report
├── dialogs.py                       Income, requirements, HUF, FD rate, MC
├── chunk_editor.py                  Reusable year-range chunk table widget
│
├── RetirementTaxPlanning.configuration    All tunable constants
└── .gitignore
```

---

## 🔑 Key Algorithms

### MILP Portfolio Optimisation

The optimizer uses **Mixed-Integer Linear Programming** (semi-continuous variables) to handle the "if selected, allocate at least X%" constraint natively — no iterative pruning needed.

```
Objective (Fine mode):
  maximise  Σ wᵢ × (adj_retᵢ + λ × quality_normᵢ)
  where  quality_norm = Combined_Ratio / max(Combined_Ratio)
         λ = 10% of return spread  →  quality as tiebreaker, not driver

Constraints:
  C1:  Σwᵢ = 1                       full investment
  C2:  Σwᵢ·retᵢ ≥ min_return        return floor
  C3:  Σwᵢ·stdᵢ ≤ max_std_dev       volatility ceiling (lin weighted)
  C4:  Σwᵢ·|ddᵢ| ≤ max_dd          drawdown ceiling
  C5+: Σwᵢ[type=T] ≤ max_per_type   per-SEBI-subcategory cap
  C6:  wᵢ ≤ max_per_fund × yᵢ       semi-continuous upper link
  C7:  wᵢ ≥ min_per_fund × yᵢ       semi-continuous lower link
  C8+: Σwᵢ[AMC=A] ≤ max_per_amc    per-AMC concentration cap
```

AMC is derived from the first word of the fund name (e.g. `"ICICI Prudential Short Term Fund"` → AMC `"Icici"`).

### Sigma Convention

All portfolio volatility is the **linear allocation-weighted average**:

```
σ_portfolio = Σ(wᵢ × σᵢ)     ← perfect correlation, upper bound
```

This is the same value shown in the "View Fund Selection" dialog (`Std:X.XX%`) and used by Monte Carlo. It is the most conservative valid estimate — the correct ordering is:

```
σ_rms = sqrt(Σ wᵢ² × σᵢ²)  ≤  σ_lin = Σwᵢσᵢ  ≤  sqrt(Σwᵢσᵢ²)
 zero correlation                 ↑ used           Jensen's ineq — not valid
  (lower bound)               upper bound           (overestimates)
```

### HIFO Tax-Alpha

During rebalancing, lots are sorted by **ascending unrealised gain** (Highest cost-basis In, First Out). Selling the cheapest-to-realise lots reduces CGT by 30–50% vs FIFO. Normal monthly SWP withdrawals use FIFO (SEBI retail requirement).

### Backward Induction Eliminates Conservative Drag

A naïve penalised optimiser suffers the *Binary Cliff*: too-high turnover penalty → solver holds safe assets early to avoid future sells. Two-Pass fixes this by locking D/E/O ratios in Pass 1. Pass 2 can only choose *which funds* fill each bucket — it cannot reduce equity allocation to avoid a future sell.

---

## ⚙️ Configuration

All tunable constants in `RetirementTaxPlanning.configuration`:

| Key | Default | Meaning |
|---|---|---|
| `cess_rate` | `0.04` | 4% health & education cess on all tax |
| `stcg_holding_months` | `12` | Months before LTCG treatment applies |
| `exit_load_fraction` | `0.01` | 1% exit load within STCG period |
| `drift_cap_personal` | `0.0015` | 0.15% return drift cap (personal portfolio) |
| `drift_cap_huf` | `0.0050` | 0.50% return drift cap (HUF portfolio) |
| `weight_drift_threshold` | `0.015` | 1.5% per-fund weight deviation trigger |
| `rebalance_no_trade_band` | `0.005` | 0.5% total portfolio drift no-trade zone |
| `amfi_sleep_between_calls` | `0.4` | Seconds between AMFI API calls |
| `allocator_default_input` | `Fund_Metrics_Output.csv` | Default input for the allocator |
| `default_cagr_fallback` | `7.0` | Fallback CAGR (%) when no data available |

---

## 🚀 Installation

```bash
# Clone
git clone https://github.com/<your-username>/RetirementTaxPlanning.git
cd RetirementTaxPlanning

# Virtual environment
python3 -m venv .venv
source .venv/bin/activate        # Linux / macOS
# .venv\Scripts\activate         # Windows

# Dependencies
pip install -r requirements.txt
```

**Core dependencies:** `PySide6` · `pandas` · `numpy` · `scipy` · `matplotlib` · `requests` · `pulp` · `tqdm`

> **Linux:** `sudo apt-get install libxcb-cursor0 libxcb-xinerama0` if you see Qt platform errors.

---

## 📖 Usage

### Step 1 — Launch

```bash
python run.py
```
Enter your name. All outputs are written to `<project_root>/<your_name>/`.

### Step 2 — Build the fund universe

```
Data → Fetch Scheme Names      downloads NAVAll.txt → mutual_funds.csv
Data → Fetch Fund Metrics      NAV history → Fund_Metrics_Output.csv  (~10–30 min)
```

### Step 3 — Allocate capital

```
Data → Allocate Capital

  Mode:  Coarse (minimise risk)  or  Fine (maximise return)

  Per time-chunk parameters:
    Min Ret%    minimum expected return  (e.g. 7.25%)
    Max/Fund%   max weight per fund      (e.g. 8%)
    Min/Fund%   min weight if selected   (e.g. 2%)
    Max/Type%   max per SEBI sub-type    (e.g. 24%)
    Max/AMC%    max per AMC house        (e.g. 16%)
    Min Hist Y  minimum fund age         (e.g. 12 years)

  ▶ Run Allocation  →  ⟳ Apply Substitutions
```

### Step 4 — Review and edit

```
Configuration → View Fund Selection & Allocation
  Header: live Yield · Std (lin) · |DD| · D%/E%/O%
  Edit allocation column directly; header updates instantly.
```

### Step 5 — Optimise the glide path *(optional)*

```
Data → Optimize Sticky Portfolio
  Two-Pass Aim-and-Track minimises turnover across chunks.
  Opens 4-tab Optimization Report.
```

### Step 6 — Run the simulation

```
[Run Calculations button]        360-month simulation → annual table + charts

Analysis → Run Monte Carlo       2,000 bootstrap paths → fan chart + ruin %
```

---

## 📤 File Outputs

All written to `<project_root>/<user_name>/`:

| File | Source | Contents |
|---|---|---|
| `Fund_Metrics_Output.csv` | `get_funds_data.py` | 33-column risk metrics for all analysed funds |
| `allocation_chunk_N_yrX-Y.csv` | `allocate_funds.py` | Fund weights + metrics for chunk N |
| `allocation_summary.csv` | `allocate_funds.py` | All chunks combined with portfolio totals |
| `portfolio_viz.html` | `allocate_funds.py` | Standalone interactive risk-return visualisation |
| `allocation_params.json` | `main.py` | Persisted allocation dialog settings |
| `*.swp_project` | `main.py` | Full project save (AppState as JSON) |
| `mc_nifty50_nav.csv` | `monte_carlo.py` | Cached Nifty 50 NAV history (weekly refresh) |
| `mc_debt_index.csv` | `monte_carlo.py` | Cached Nifty Composite Debt Index |
| `Schemes_and_Funds/mutual_funds.csv` | `get_amfi_fund_schemes_names.py` | Full AMFI fund universe |
| `Schemes_and_Funds/amfi_aum.csv` | `fetch_amfi_aum.py` | Daily AUM per fund |

---

## 📜 Licence

Private repository — all rights reserved.