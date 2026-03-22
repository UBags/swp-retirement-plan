# Copyright (c) 2025 Uddipan Bagchi. All rights reserved.
# See LICENSE in the project root for license information.

"""
SWP Financial Planner – Main Window v3.

Changes vs v2:
  1. CSV export: all files written with encoding='utf-8-sig' (BOM) so Excel on
     Windows reads UTF-8 correctly; unicode chars in headers replaced with ASCII
     equivalents to avoid charmap errors on legacy Windows code-pages.
  2. "Save All CSVs" – prompts for a folder + user prefix, writes 5 files:
       {prefix}_Personal_Monthly.csv
       {prefix}_Personal_Annual_Summary.csv
       {prefix}_HUF_Monthly.csv
       {prefix}_HUF_Annual_Summary.csv
       {prefix}_Sensitivity.csv  (if sensitivity has been run)
  3. Each output table has a "Show Chart" button above it that opens a
     non-modal chart pop-up window (using matplotlib).
"""
import sys, json, csv, subprocess, traceback
from datetime import date
from pathlib import Path
from typing import List, Optional

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QTabWidget,
    QTableWidget, QTableWidgetItem, QHeaderView, QLabel, QPushButton,
    QFileDialog, QMessageBox, QStatusBar,
    QGroupBox, QDoubleSpinBox, QDateEdit, QApplication,
    QAbstractItemView, QDialog, QDialogButtonBox, QLineEdit,
    QFormLayout, QToolTip, QSpinBox, QProgressDialog, QTextEdit,
    QCheckBox, QComboBox, QGridLayout
)
from PySide6.QtCore import Qt, QDate, QPoint, QThread, Signal, QObject
from PySide6.QtGui import QColor, QFont, QAction

from models import AppState, default_state
from engine import Engine, run_sensitivity, optimize_withdrawal_split, YearSummary, MonthlyRow, FundWithdrawalDetail
from tax_dialog import TaxRulesDialog
from fund_dialog import FundAllocationDialog
try:
    from optimization_report import show_optimization_report
    _OPT_REPORT_AVAILABLE = True
except ImportError:
    _OPT_REPORT_AVAILABLE = False
from dialogs import (RequirementsDialog, ReturnRateDialog, SplitDialog,
                     IncomeDialog, WindfallDialog, HUFWithdrawalDialog,
                     SensitivityDialog, MonteCarloDialog)

# ── Colours ────────────────────────────────────────────────────────────────────
CLR_DEBT   = QColor("#ddeeff")
CLR_EQUITY = QColor("#dff0d8")
CLR_OTHER  = QColor("#fef3e2")
CLR_TAX    = QColor("#fce8e8")
CLR_CASH   = QColor("#fff3cd")
CLR_HUF    = QColor("#e8d5f5")
CLR_ALT    = QColor("#f5f5f5")
CLR_XFER   = QColor("#d5f5e3")


def _ro_item(text: str, bg: Optional[QColor] = None) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    if bg:
        item.setBackground(bg)
    return item


def _fmt(val: float, d: int = 2) -> str:
    return f"{val:,.{d}f}"


def _classify_fund_type(amfi_fund_type: str) -> str:
    """Map AMFI 'Fund Type' string to tax category: 'debt', 'equity', or 'other'.

    Tax categories (FY 2025-26 onwards):
      equity  — Equity-oriented (≥65% domestic equity): LTCG 12.5% + ₹1.25L exemption
      debt    — Specified MF (>65% debt/money-market): all gains at slab rate (Sec 50AA)
      other   — Gold ETFs, International ETFs, non-equity ETFs, hybrid (35-65%):
                LTCG 12.5% flat (>12m listed), no exemption, no indexation
    """
    t = amfi_fund_type.lower()
    # Normalise multiple spaces
    import re
    t = re.sub(r'\s+', ' ', t).strip()

    # ── Equity-oriented (>= 65% domestic equity) ─────────────────────
    if any(k in t for k in ("equity scheme", "arbitrage")):
        return "equity"
    if "index fund" in t:
        # Nifty 50, Sensex index funds are equity-oriented
        return "equity"
    if "aggressive hybrid" in t:
        return "equity"   # 65-80% equity
    # "Other Scheme - Other ETFs" = Nifty 50 ETFs, BHARAT 22, CPSE, PSU Bank ETFs
    # These are equity-oriented (>65% domestic equity)
    if "other etf" in t:
        return "equity"

    # ── Debt / Specified MF (>65% debt/money-market) ─────────────────
    if "debt scheme" in t:
        return "debt"
    if "liquid" in t or "overnight" in t or "money market" in t:
        return "debt"
    if "conservative hybrid" in t:
        return "debt"     # <35% equity → specified MF

    # ── Other (Gold, International, non-equity ETFs, mixed hybrids) ──
    if "gold" in t:
        return "other"
    if "fof overseas" in t or "fof domestic" in t:
        # FoF Domestic could be equity or debt depending on underlying;
        # conservatively treat as 'other' (12.5% no exemption)
        return "other"
    if any(k in t for k in ("dynamic asset", "balanced advantage",
                             "equity savings", "multi asset")):
        return "other"    # 35-65% equity → other category
    if "solution oriented" in t:
        return "equity"   # typically equity-heavy

    # Default: debt (safest assumption for tax purposes)
    return "debt"


# ASCII-safe column headers (no unicode arrows/rupee signs in CSV headers)
PERSONAL_MONTHLY_COLS = [
    "Month#", "Cal Month", "FY Year",
    "Corpus Debt Start", "Corpus Eq Start", "Corpus Other Start",
    "WD Debt", "WD Equity", "WD Other", "WD Total",
    "Principal Debt", "Gain Debt",
    "Principal Equity", "Gain Equity",
    "Principal Other", "Gain Other",
    "Ind Tax Paid (Apr)", "HUF Transfer (Apr)", "FD Tax Benchmark",
    "Corpus Debt End", "Corpus Eq End", "Corpus Other End",
    "Windfall Personal",
]

HUF_MONTHLY_COLS = [
    "Month#", "Cal Month", "FY Year",
    "Corpus Debt Start", "Corpus Eq Start", "Corpus Other Start",
    "WD Debt", "WD Equity", "WD Other", "WD Total",
    "Principal Debt", "Gain Debt",
    "Principal Equity", "Gain Equity",
    "Principal Other", "Gain Other",
    "Corpus Debt End", "Corpus Eq End", "Corpus Other End",
    "HUF Transfer In", "Windfall HUF",
]

YEARLY_COLS = [
    "FY Year",
    "Corpus Debt (Personal)", "Corpus Equity (Personal)", "Corpus Other (Personal)",
    "Corpus Debt (HUF)",      "Corpus Equity (HUF)",      "Corpus Other (HUF)",
    "Tax Personal (L)", "Tax HUF (L)",
    "Net Cash Personal (L)", "Net Cash HUF (L)",
    "Net Cash Total (L)",
    "FD Tax Benchmark (L)", "Tax Saved to HUF (L)",
]

SENSITIVITY_COLS_BASE = ["FY Year"]

# Tooltip text for each column in the annual summary table
YEARLY_COL_TOOLTIPS = {
    "FY Year": (
        "Financial Year number.\n"
        "FY 1 = the first April-March year starting from your investment date.\n"
        "FY 30 = the final projection year."
    ),
    "Corpus Debt (Personal)": (
        "End-of-year market value of your personal DEBT fund portfolio (Rs Lakhs).\n"
        "Grows monthly at the portfolio return rate, reduced by monthly SWP withdrawals.\n"
        "Uses strict FIFO lot tracking for gain calculations."
    ),
    "Corpus Equity (Personal)": (
        "End-of-year market value of your personal EQUITY/ARBITRAGE fund portfolio (Rs Lakhs).\n"
        "Grows monthly at the portfolio return rate, reduced by monthly SWP withdrawals.\n"
        "LTCG taxed at flat 12.5% above the annual exemption limit."
    ),
    "Corpus Other (Personal)": (
        "End-of-year market value of your personal OTHER fund portfolio (Rs Lakhs).\n"
        "Includes Gold ETFs, International ETFs, and hybrid funds (35-65% equity).\n"
        "LTCG taxed at flat 12.5% — NO ₹1.25L exemption, NO indexation."
    ),
    "Corpus Debt (HUF)": (
        "End-of-year market value of the HUF's debt fund holdings (Rs Lakhs).\n"
        "The HUF corpus is funded by:\n"
        "  * Annual tax savings (FD benchmark tax - actual SWP tax) transferred each April\n"
        "  * Any windfalls assigned to the HUF\n"
        "Invested in the same debt:equity split as the personal SWP."
    ),
    "Corpus Equity (HUF)": (
        "End-of-year market value of the HUF's equity fund holdings (Rs Lakhs).\n"
        "HUF LTCG is first offset by any unused basic exemption (nil slab) before taxation."
    ),
    "Corpus Other (HUF)": (
        "End-of-year market value of the HUF's 'other' fund holdings (Rs Lakhs).\n"
        "Gold ETFs, International ETFs, etc. LTCG @12.5% flat, no exemption."
    ),
    "Tax Personal (L)": (
        "Individual income tax liability FOR THIS FY (Rs Lakhs).\n"
        "= Slab tax on (other taxable income + debt fund gains)\n"
        "  + LTCG tax on equity gains above the annual exemption\n"
        "  + LTCG tax on 'other' fund gains (12.5% flat, no exemption)\n"
        "  + 4% health & education cess\n"
        "  - 87A rebate (if total income <= exempt limit)\n"
        "This tax is deducted from Net Cash Personal in the same year."
    ),
    "Tax HUF (L)": (
        "HUF income tax liability for this FY (Rs Lakhs).\n"
        "= Slab tax on (HUF other income + HUF debt gains)\n"
        "  + LTCG on HUF equity gains, offset by unused basic exemption\n"
        "  + 4% cess.  HUF has no 87A rebate."
    ),
    "Net Cash Personal (L)": (
        "Cash available to the individual this year (Rs Lakhs).\n\n"
        "Formula:\n"
        "  + Annual SWP withdrawal (debt + equity)\n"
        "  + Other taxable income (salary, interest, pension, rental)\n"
        "  + Non-taxable income (tax-free interest, other non-taxable)\n"
        "  - Tax saving diverted to HUF (= FD benchmark tax - SWP tax)\n"
        "  - Individual income tax for this FY\n\n"
        "Tax is deducted in the same year it accrues.\n"
        "Note: From mid-year 2 onward, monthly SWP is split between debt\n"
        "and equity in proportion to the current corpus value of each."
    ),
    "Net Cash HUF (L)": (
        "Cash available from the HUF this year (Rs Lakhs).\n\n"
        "Formula:\n"
        "  + HUF annual withdrawal (from HUF SWP corpus)\n"
        "  + HUF other taxable income + non-taxable income\n"
        "  - HUF income tax for this FY"
    ),
    "Net Cash Total (L)": (
        "Combined household cash available this year (Rs Lakhs).\n\n"
        "= Net Cash Personal + Net Cash HUF\n\n"
        "Total spendable amount across both entities after all taxes and HUF diversions."
    ),
    "FD Tax Benchmark (L)": (
        "Tax you WOULD have paid if the same corpus were in a Fixed Deposit (Rs Lakhs).\n\n"
        "FD income = Initial corpus (constant) x FD interest rate (per-year chunk)\n"
        "FD tax = Slab tax on (FD income + other taxable income)\n\n"
        "The FD corpus is held constant at the initial investment amount.\n"
        "FD interest rates are user-configurable per time chunk.\n"
        "The difference between this and Tax Personal is the annual tax saving."
    ),
    "Tax Saved to HUF (L)": (
        "Tax saving vs FD benchmark, diverted into the HUF corpus (Rs Lakhs).\n\n"
        "= max(0, FD Tax Benchmark - Tax Personal)\n\n"
        "Transferred to HUF in April of the FOLLOWING financial year,\n"
        "where it compounds. Shown in green as it is a structural benefit."
    ),
}


# ── CSV writing helper ─────────────────────────────────────────────────────────
def _write_csv(path, headers: list, rows: list):
    """
    Write CSV with utf-8-sig encoding (BOM) so Excel on Windows opens correctly.
    All values are plain strings – no commas inside values.

    Raises PermissionError with a user-friendly message if the file is locked
    (e.g. open in Excel on Windows).
    """
    try:
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for row in rows:
                writer.writerow(row)
    except PermissionError:
        raise PermissionError(
            f"Cannot write to:\n{path}\n\n"
            "The file is likely open in another application (e.g. Excel).\n"
            "Please close it and try again."
        )


# ── Row extractors ─────────────────────────────────────────────────────────────
def _personal_monthly_rows(p_monthly):
    out = []
    for r in p_monthly:
        out.append([
            r.month_idx + 1,
            f"{r.calendar_year}-{r.calendar_month:02d}",
            r.fy_year,
            _fmt(r.corpus_debt_start), _fmt(r.corpus_equity_start), _fmt(r.corpus_other_start),
            _fmt(r.wd_debt), _fmt(r.wd_equity), _fmt(r.wd_other),
            _fmt(r.wd_debt + r.wd_equity + r.wd_other),
            _fmt(r.principal_debt), _fmt(r.gain_debt),
            _fmt(r.principal_equity), _fmt(r.gain_equity),
            _fmt(r.principal_other), _fmt(r.gain_other),
            _fmt(r.ind_tax_paid), _fmt(r.huf_transfer_in), _fmt(r.fd_tax_paid),
            _fmt(r.corpus_debt_end), _fmt(r.corpus_equity_end), _fmt(r.corpus_other_end),
            _fmt(r.windfall_personal),
        ])
    return out


def _huf_monthly_rows(h_monthly):
    out = []
    for r in h_monthly:
        out.append([
            r.month_idx + 1,
            f"{r.calendar_year}-{r.calendar_month:02d}",
            r.fy_year,
            _fmt(r.corpus_debt_start), _fmt(r.corpus_equity_start), _fmt(r.corpus_other_start),
            _fmt(r.wd_debt), _fmt(r.wd_equity), _fmt(r.wd_other),
            _fmt(r.wd_debt + r.wd_equity + r.wd_other),
            _fmt(r.principal_debt), _fmt(r.gain_debt),
            _fmt(r.principal_equity), _fmt(r.gain_equity),
            _fmt(r.principal_other), _fmt(r.gain_other),
            _fmt(r.corpus_debt_end), _fmt(r.corpus_equity_end), _fmt(r.corpus_other_end),
            _fmt(r.huf_transfer_in), _fmt(r.windfall_huf),
        ])
    return out


def _yearly_rows(p_yearly):
    out = []
    for r in p_yearly:
        out.append([
            r.year,
            _fmt(r.corpus_debt_personal), _fmt(r.corpus_equity_personal),
            _fmt(r.corpus_other_personal),
            _fmt(r.corpus_debt_huf),      _fmt(r.corpus_equity_huf),
            _fmt(r.corpus_other_huf),
            _fmt(r.tax_personal), _fmt(r.tax_huf),
            _fmt(r.net_cash_personal), _fmt(r.net_cash_huf),
            _fmt(r.net_cash_total),
            _fmt(r.fd_tax_benchmark), _fmt(r.tax_saved),
        ])
    return out


def _sensitivity_rows(sens_results: dict):
    """sensitivity_results: {name: [YearSummary]}"""
    if not sens_results:
        return [], []
    names = list(sens_results.keys())
    headers = ["FY Year"] + \
              [f"{n} - Net Cash (L)" for n in names] + \
              [f"{n} - Total Corpus (L)" for n in names]
    rows = []
    for i in range(30):
        row = [i + 1]
        for name in names:
            ys = sens_results[name]
            r = ys[i] if i < len(ys) else None
            row.append(_fmt(r.net_cash_total) if r else "")
        for name in names:
            ys = sens_results[name]
            r = ys[i] if i < len(ys) else None
            corpus = (r.corpus_debt_personal + r.corpus_equity_personal +
                      r.corpus_other_personal +
                      r.corpus_debt_huf + r.corpus_equity_huf +
                      r.corpus_other_huf) if r else 0
            row.append(_fmt(corpus) if r else "")
        rows.append(row)
    return headers, rows


# ── Prefix prompt dialog ───────────────────────────────────────────────────────
class PrefixDialog(QDialog):
    def __init__(self, parent=None, default_prefix: str = "SWP"):
        super().__init__(parent)
        self.setWindowTitle("Save All CSVs")
        self.resize(380, 120)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.prefix_edit = QLineEdit(default_prefix)
        form.addRow("File prefix / user name:", self.prefix_edit)
        layout.addLayout(form)
        layout.addWidget(QLabel(
            "Files will be saved as:\n"
            "  {prefix}_Personal_Monthly.csv\n"
            "  {prefix}_Personal_Annual_Summary.csv\n"
            "  {prefix}_HUF_Monthly.csv\n"
            "  {prefix}_HUF_Annual_Summary.csv\n"
            "  {prefix}_Sensitivity.csv  (if available)"))
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def prefix(self) -> str:
        return self.prefix_edit.text().strip() or "SWP"


# ── Expandable monthly table ───────────────────────────────────────────────────
CLR_FUND_DEBT   = QColor("#eaf4fb")   # light blue for debt fund sub-rows
CLR_FUND_EQUITY = QColor("#eafaf1")   # light green for equity fund sub-rows
CLR_FUND_OTHER  = QColor("#fdf5e6")   # light gold for other fund sub-rows (Gold/Intl ETFs)
CLR_EXPAND_BTN  = QColor("#d6eaf8")   # button cell highlight

FUND_SUB_COLS = [
    "Fund Name", "Type",
    "Corpus Start", "Withdrawal", "Principal", "Gain", "Corpus End"
]

class ExpandableMonthlyTable(QTableWidget):
    """
    Monthly table where each parent row has a +/- toggle in col 0.
    Clicking expands/collapses per-fund breakdown rows beneath it.
    """

    # How many cols the fund sub-rows use (same as parent; padded with blanks)
    _PARENT_COLS_PERSONAL = PERSONAL_MONTHLY_COLS
    _PARENT_COLS_HUF      = HUF_MONTHLY_COLS

    def __init__(self, rows: List[MonthlyRow], entity: str = "personal", parent=None):
        self.entity   = entity
        self.src_rows = rows          # original MonthlyRow list
        self._expanded: set = set()   # set of parent logical indices (0-based)

        # Map: table row index -> (kind, data)
        # kind = "parent" | "child"
        # data = (logical_idx, MonthlyRow) | (logical_idx, FundWithdrawalDetail)
        self._row_map: List[tuple] = []

        cols = PERSONAL_MONTHLY_COLS if entity == "personal" else HUF_MONTHLY_COLS
        super().__init__(0, len(cols), parent)
        self.setHorizontalHeaderLabels(cols)
        self.setAlternatingRowColors(False)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.verticalHeader().setDefaultSectionSize(22)
        self.verticalHeader().hide()

        self._rebuild()

    # ── Public interface ──────────────────────────────────────────────────────

    def toggle_expand(self, logical_idx: int):
        if logical_idx in self._expanded:
            self._expanded.discard(logical_idx)
        else:
            # Only expand if there are fund details
            if self.src_rows[logical_idx].fund_withdrawals:
                self._expanded.add(logical_idx)
        self._rebuild()

    # ── Internal rebuild ──────────────────────────────────────────────────────

    def _rebuild(self):
        self.setUpdatesEnabled(False)
        self.clearContents()
        self._row_map = []

        # Count total rows needed
        total = 0
        for li, row in enumerate(self.src_rows):
            total += 1  # parent
            if li in self._expanded:
                total += len(row.fund_withdrawals)

        self.setRowCount(total)

        tr = 0  # table row index
        for li, row in enumerate(self.src_rows):
            self._fill_parent_row(tr, li, row)
            self._row_map.append(("parent", li))
            tr += 1
            if li in self._expanded:
                for fd in row.fund_withdrawals:
                    self._fill_fund_row(tr, li, fd)
                    self._row_map.append(("child", li))
                    tr += 1

        self.setUpdatesEnabled(True)

    def _fill_parent_row(self, tr: int, li: int, row: MonthlyRow):
        entity  = self.entity
        has_fd  = bool(row.fund_withdrawals)
        expanded = li in self._expanded
        is_april = (row.calendar_month == 4 and row.fy_year >= 2)
        base_bg  = CLR_ALT if li % 2 == 0 else None

        # Col 0: toggle button cell
        symbol = "−" if expanded else "+"
        toggle_text = f" {symbol}  {row.month_idx + 1}" if has_fd else f"     {row.month_idx + 1}"
        toggle_item = QTableWidgetItem(toggle_text)
        toggle_item.setFlags(toggle_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        if has_fd:
            toggle_item.setBackground(CLR_EXPAND_BTN)
            toggle_item.setForeground(QColor("#1a5276"))
            f = QFont(); f.setBold(True); toggle_item.setFont(f)
            toggle_item.setToolTip("Click to expand/collapse per-fund breakdown")
        else:
            toggle_item.setBackground(base_bg or QColor("white"))
        self.setItem(tr, 0, toggle_item)

        if entity == "personal":
            vals = [
                None,  # col 0 handled above
                f"{row.calendar_year}-{row.calendar_month:02d}",
                str(row.fy_year),
                _fmt(row.corpus_debt_start),
                _fmt(row.corpus_equity_start),
                _fmt(row.corpus_other_start),
                _fmt(row.wd_debt),
                _fmt(row.wd_equity),
                _fmt(row.wd_other),
                _fmt(row.wd_debt + row.wd_equity + row.wd_other),
                _fmt(row.principal_debt),
                _fmt(row.gain_debt),
                _fmt(row.principal_equity),
                _fmt(row.gain_equity),
                _fmt(row.principal_other),
                _fmt(row.gain_other),
                _fmt(row.ind_tax_paid),
                _fmt(row.huf_transfer_in),
                _fmt(row.fd_tax_paid),
                _fmt(row.corpus_debt_end),
                _fmt(row.corpus_equity_end),
                _fmt(row.corpus_other_end),
                _fmt(row.windfall_personal),
            ]
            for j, v in enumerate(vals):
                if j == 0:
                    continue
                if is_april and j == 16:
                    bg = CLR_TAX
                elif is_april and j == 17:
                    bg = CLR_XFER
                elif is_april and j == 18:
                    bg = CLR_TAX
                elif row.windfall_personal > 0 and j == 22:
                    bg = CLR_CASH
                else:
                    bg = base_bg
                self.setItem(tr, j, _ro_item(v, bg))
        else:
            vals = [
                None,
                f"{row.calendar_year}-{row.calendar_month:02d}",
                str(row.fy_year),
                _fmt(row.corpus_debt_start),
                _fmt(row.corpus_equity_start),
                _fmt(row.corpus_other_start),
                _fmt(row.wd_debt),
                _fmt(row.wd_equity),
                _fmt(row.wd_other),
                _fmt(row.wd_debt + row.wd_equity + row.wd_other),
                _fmt(row.principal_debt),
                _fmt(row.gain_debt),
                _fmt(row.principal_equity),
                _fmt(row.gain_equity),
                _fmt(row.principal_other),
                _fmt(row.gain_other),
                _fmt(row.corpus_debt_end),
                _fmt(row.corpus_equity_end),
                _fmt(row.corpus_other_end),
                _fmt(row.huf_transfer_in),
                _fmt(row.windfall_huf),
            ]
            for j, v in enumerate(vals):
                if j == 0:
                    continue
                if is_april and j == 19:
                    bg = CLR_XFER
                elif row.windfall_huf > 0 and j == 20:
                    bg = CLR_CASH
                else:
                    bg = base_bg
                self.setItem(tr, j, _ro_item(v, bg))

    def _fill_fund_row(self, tr: int, li: int, fd: "FundWithdrawalDetail"):
        """Fill a fund sub-row. Uses first 7 cols for fund detail, rest blank."""
        n_cols = self.columnCount()
        bg = CLR_FUND_DEBT if fd.fund_type == "debt" else (
             CLR_FUND_EQUITY if fd.fund_type == "equity" else CLR_FUND_OTHER)

        # Indent marker in col 0
        indent_item = QTableWidgetItem("    ↳")
        indent_item.setFlags(indent_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        indent_item.setBackground(bg)
        indent_item.setForeground(QColor("#555"))
        self.setItem(tr, 0, indent_item)

        # Fund name in col 1, truncated to fit
        name_item = _ro_item(fd.fund_name, bg)
        name_item.setFont(QFont("", -1))
        self.setItem(tr, 1, name_item)

        # Type in col 2
        self.setItem(tr, 2, _ro_item(fd.fund_type.capitalize(), bg))

        # Corpus start in col 3 (debt), 4 (equity), or 5 (other)
        _cs_col = {"debt": 3, "equity": 4, "other": 5}
        self.setItem(tr, _cs_col.get(fd.fund_type, 3), _ro_item(_fmt(fd.corpus_start), bg))

        # Withdrawal in col 6 (debt), 7 (equity), 8 (other)
        _wd_col = {"debt": 6, "equity": 7, "other": 8}
        self.setItem(tr, _wd_col.get(fd.fund_type, 6), _ro_item(_fmt(fd.withdrawal), bg))

        # Principal/Gain in cols 10,11 (debt) or 12,13 (equity) or 14,15 (other)
        _pg_col = {"debt": (10, 11), "equity": (12, 13), "other": (14, 15)}
        pc, gc = _pg_col.get(fd.fund_type, (10, 11))
        self.setItem(tr, pc, _ro_item(_fmt(fd.principal), bg))
        self.setItem(tr, gc, _ro_item(_fmt(fd.gain), bg))

        # Corpus end — personal: 19/20/21, HUF: 16/17/18
        if self.entity == "personal":
            _ce_col = {"debt": 19, "equity": 20, "other": 21}
        else:
            _ce_col = {"debt": 16, "equity": 17, "other": 18}
        self.setItem(tr, _ce_col.get(fd.fund_type, 19 if self.entity == "personal" else 16),
                     _ro_item(_fmt(fd.corpus_end), bg))

        # Fill remaining blanks
        for j in range(n_cols):
            if self.item(tr, j) is None:
                blank = QTableWidgetItem("")
                blank.setFlags(blank.flags() & ~Qt.ItemFlag.ItemIsEditable)
                blank.setBackground(bg)
                self.setItem(tr, j, blank)

    def mousePressEvent(self, event):
        """Intercept clicks on col 0 parent rows to toggle expansion."""
        idx = self.indexAt(event.pos())
        if idx.isValid() and idx.column() == 0:
            tr = idx.row()
            if tr < len(self._row_map):
                kind, li = self._row_map[tr]
                if kind == "parent" and self.src_rows[li].fund_withdrawals:
                    self.toggle_expand(li)
                    return
        super().mousePressEvent(event)


def build_monthly_table(rows: List[MonthlyRow], entity: str = "personal") -> ExpandableMonthlyTable:
    return ExpandableMonthlyTable(rows, entity)


def build_yearly_table(rows: List[YearSummary]) -> QTableWidget:
    t = QTableWidget(len(rows), len(YEARLY_COLS))
    t.setAlternatingRowColors(True)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    hdr = t.horizontalHeader()
    hdr.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)

    # Set header labels with ? tooltip on each column
    for col_idx, col_name in enumerate(YEARLY_COLS):
        item = QTableWidgetItem(col_name + "  ?")
        tip = YEARLY_COL_TOOLTIPS.get(col_name, "")
        if tip:
            item.setToolTip(tip)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        item.setFont(QFont("", -1, QFont.Weight.Bold))
        t.setHorizontalHeaderItem(col_idx, item)

    col_bg = {1: CLR_DEBT, 2: CLR_EQUITY, 3: CLR_OTHER,
              4: CLR_HUF, 5: CLR_HUF, 6: CLR_HUF,
              7: CLR_TAX, 8: CLR_TAX,
              9: CLR_CASH, 10: CLR_CASH, 11: CLR_CASH}

    # HUF clubbing risk threshold: flag years where HUF net income > personal net income
    # (simplified proxy — real 64(2) is about income from assets transferred to HUF)
    CLUBBING_TIP = (
        "Section 64(2) — HUF Clubbing Provision\n\n"
        "If you (the karta) transfer personal assets to the HUF without adequate\n"
        "consideration, the income from those assets is CLUBBED back into YOUR\n"
        "personal income for tax purposes — defeating the purpose of the HUF structure.\n\n"
        "This year is flagged because the HUF corpus is growing significantly from\n"
        "tax-saving transfers (which originate from YOUR personal tax savings).\n"
        "The IT department may treat these as 'indirect transfers' and club the\n"
        "resulting HUF income with your income.\n\n"
        "MITIGATION STRATEGIES:\n"
        "1. Ensure all HUF contributions are via genuine HUF income (gifts from\n"
        "   non-members, ancestral property income) — NOT from your personal savings.\n"
        "2. Obtain a formal valuation & legal opinion that the tax-saving diversion\n"
        "   is HUF income earned through the SWP structure, not a personal transfer.\n"
        "3. Maintain clean documentation: board of karta resolutions, HUF bank\n"
        "   account statements separate from personal accounts.\n"
        "4. Consult a CA specialising in HUF matters to structure contributions\n"
        "   so they qualify as HUF income rather than transferred assets.\n\n"
        "Seek professional advice — this is a flag for review, not a definitive finding."
    )

    for i, row in enumerate(rows):
        # Detect clubbing risk.  Two triggers:
        #   (A) ORIGINATION: ratio crosses 15% THIS year (was below last year).
        #       This catches pure windfalls that push HUF over the threshold
        #       even when tax_saved happens to be zero in that year.
        #   (B) ONGOING: ratio already above 15% AND tax_saved > 0 this year,
        #       meaning fresh transfers are still flowing into an already-
        #       disproportionate HUF, compounding the clubbing exposure.
        huf_total  = row.corpus_debt_huf + row.corpus_equity_huf + row.corpus_other_huf
        pers_total = (row.corpus_debt_personal + row.corpus_equity_personal +
                      row.corpus_other_personal)
        ratio_now  = (huf_total / pers_total) if pers_total > 0 else 0.0

        if i > 0:
            prev = rows[i - 1]
            prev_huf   = prev.corpus_debt_huf + prev.corpus_equity_huf + prev.corpus_other_huf
            prev_pers  = (prev.corpus_debt_personal + prev.corpus_equity_personal +
                          prev.corpus_other_personal)
            ratio_prev = (prev_huf / prev_pers) if prev_pers > 0 else 0.0
        else:
            ratio_prev = 0.0

        threshold_crossed = (ratio_now > 0.15 and ratio_prev <= 0.15)
        ongoing_transfers = (ratio_now > 0.15 and row.tax_saved > 0)
        clubbing_risk = threshold_crossed or ongoing_transfers

        vals = [
            str(row.year),
            _fmt(row.corpus_debt_personal),
            _fmt(row.corpus_equity_personal),
            _fmt(row.corpus_other_personal),
            _fmt(row.corpus_debt_huf),
            _fmt(row.corpus_equity_huf),
            _fmt(row.corpus_other_huf),
            _fmt(row.tax_personal),
            _fmt(row.tax_huf),
            _fmt(row.net_cash_personal),
            _fmt(row.net_cash_huf),
            _fmt(row.net_cash_total),
            _fmt(row.fd_tax_benchmark),
            _fmt(row.tax_saved),
        ]
        for j, v in enumerate(vals):
            item = _ro_item(v, col_bg.get(j))
            if j == 13 and row.tax_saved > 0:
                item.setFont(QFont("", -1, QFont.Weight.Bold))
                item.setForeground(QColor("#27ae60"))

            # Clubbing risk: colour FY Year cell red, add ? tooltip
            if j == 0 and clubbing_risk:
                item.setText(f"{row.year}  ?")
                item.setBackground(QColor("#fdecea"))
                item.setForeground(QColor("#c0392b"))
                item.setFont(QFont("", -1, QFont.Weight.Bold))
                item.setToolTip(CLUBBING_TIP)

            # Also highlight HUF corpus columns red when risk is flagged
            if clubbing_risk and j in (4, 5, 6):
                item.setBackground(QColor("#fdecea"))

            t.setItem(i, j, item)

    return t


def build_sensitivity_table(results: dict) -> QTableWidget:
    names = list(results.keys())
    cols = (["FY Year"] +
            [f"{n} - Net Cash (L)" for n in names] +
            [f"{n} - Total Corpus (L)" for n in names])
    t = QTableWidget(30, len(cols))
    t.setHorizontalHeaderLabels(cols)
    t.setAlternatingRowColors(True)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)

    for i in range(30):
        t.setItem(i, 0, _ro_item(str(i + 1)))
        for j, name in enumerate(names):
            ys = results[name]
            if i < len(ys):
                r = ys[i]
                net = r.net_cash_total
                corpus = (r.corpus_debt_personal + r.corpus_equity_personal +
                          r.corpus_other_personal +
                          r.corpus_debt_huf + r.corpus_equity_huf +
                          r.corpus_other_huf)
                t.setItem(i, 1 + j, _ro_item(_fmt(net), CLR_CASH))
                t.setItem(i, 1 + len(names) + j,
                          _ro_item(_fmt(corpus), CLR_DEBT if j == 0 else CLR_EQUITY))
    return t


# ── Chart button helper ────────────────────────────────────────────────────────
def _chart_btn(label: str, slot) -> QPushButton:
    """Chart button. Wraps slot to absorb Qt's 'checked' boolean argument."""
    btn = QPushButton(f"  {label}")
    btn.setStyleSheet(
        "background:#3498db;color:white;font-weight:bold;"
        "padding:4px 12px;border-radius:4px;font-size:11px;")
    btn.clicked.connect(lambda checked=False: slot())
    return btn


# ── Main window ────────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self, output_dir: Path = None, user_name: str = ""):
        super().__init__()
        # ── Per-user output directory ─────────────────────────────────────────
        # Defaults to a sibling "outputs" folder if run without run.py
        self.output_dir: Path = output_dir or Path(".") / "outputs"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.user_name: str = user_name or "SWP"

        title = f"SWP Financial Planner"
        if user_name:
            title += f"  –  {user_name}"
        self.setWindowTitle(title)
        self.resize(1400, 900)

        # ── 4-scenario state model ────────────────────────────────────────────
        import copy as _copy
        _s0 = default_state()
        self.scenarios: list = [_s0, _copy.deepcopy(_s0),
                                _copy.deepcopy(_s0), _copy.deepcopy(_s0)]
        self._active_scenario_idx: int = 0
        # Per-scenario results and bookkeeping
        self._scenario_results = [None, None, None, None]
        self._scenario_yearly  = [None, None, None, None]
        self._scenario_sensitivity = [None, None, None, None]
        self._scenario_mc = [None, None, None, None]

        # output_dir is a window-level concept, never stored on AppState
        self._chart_windows = []         # keep references so GC doesn't kill them

        # Fields that are SHARED across all 4 scenarios.  When a shared dialog
        # saves, changes propagate from the active scenario to all others.
        self._SHARED_FIELDS = [
            'investment_date',
            'individual_debt_chunks', 'individual_equity_chunks',
            'individual_other_chunks',
            'huf_debt_chunks', 'huf_equity_chunks', 'huf_other_chunks',
            'annual_requirements',
            'personal_income', 'huf_income',
            'windfalls',
            'huf_withdrawal_chunks', 'huf_annual_requirements',
            'fd_rate_chunks', 'fd_rate',
            # NOTE: return_chunks and split_chunks are deliberately NOT shared.
            # Each scenario derives its own from its portfolio allocation yields.
        ]

        self._setup_ui()
        self._setup_menus()
        self._update_status()

    # ── Active-scenario property ──────────────────────────────────────────────
    # Provides backward compatibility: existing code that uses self.state
    # continues to work, transparently hitting the active scenario's AppState.
    @property
    def state(self):
        return self.scenarios[self._active_scenario_idx]

    @state.setter
    def state(self, value):
        self.scenarios[self._active_scenario_idx] = value

    @property
    def _last_results(self):
        return self._scenario_results[self._active_scenario_idx]

    @_last_results.setter
    def _last_results(self, value):
        self._scenario_results[self._active_scenario_idx] = value

    @property
    def _last_sensitivity(self):
        return self._scenario_sensitivity[self._active_scenario_idx]

    @_last_sensitivity.setter
    def _last_sensitivity(self, value):
        self._scenario_sensitivity[self._active_scenario_idx] = value

    @property
    def _last_mc(self):
        return self._scenario_mc[self._active_scenario_idx]

    @_last_mc.setter
    def _last_mc(self, value):
        self._scenario_mc[self._active_scenario_idx] = value

    @property
    def _last_yearly(self):
        return self._scenario_yearly[self._active_scenario_idx]

    @_last_yearly.setter
    def _last_yearly(self, value):
        self._scenario_yearly[self._active_scenario_idx] = value

    @property
    def _last_yearly_rows(self):
        return self._scenario_yearly[self._active_scenario_idx]

    @_last_yearly_rows.setter
    def _last_yearly_rows(self, value):
        self._scenario_yearly[self._active_scenario_idx] = value

    def _propagate_shared_fields(self):
        """Copy shared fields from the active scenario to all other scenarios."""
        import copy as _copy
        src = self.scenarios[self._active_scenario_idx]
        for i, tgt in enumerate(self.scenarios):
            if i == self._active_scenario_idx:
                continue
            for field in self._SHARED_FIELDS:
                setattr(tgt, field, _copy.deepcopy(getattr(src, field)))

    # ── UI ─────────────────────────────────────────────────────────────────────
    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(6, 6, 6, 6)

        top = QHBoxLayout()
        top.addWidget(QLabel("Investment Date:"))
        self.date_edit = QDateEdit()
        self.date_edit.setDisplayFormat("dd-MMM-yyyy")
        self.date_edit.setCalendarPopup(True)
        inv = self.state.investment_date
        self.date_edit.setDate(QDate(inv.year, inv.month, inv.day))
        self.date_edit.dateChanged.connect(self._on_date_changed)
        top.addWidget(self.date_edit)

        top.addWidget(QLabel("  FD Rate:"))
        self.btn_fd_rate = QPushButton("Configure FD Rates...")
        self.btn_fd_rate.setStyleSheet(
            "background:#e67e22;color:white;font-weight:bold;"
            "padding:4px 12px;border-radius:3px;font-size:11px;")
        self.btn_fd_rate.clicked.connect(self._open_fd_rate_chunks)
        top.addWidget(self.btn_fd_rate)
        self.lbl_fd_rate = QLabel("")
        self.lbl_fd_rate.setStyleSheet("color:#555;font-size:10px;")
        top.addWidget(self.lbl_fd_rate)
        self._update_fd_rate_label()

        top.addStretch()
        root.addLayout(top)

        self.lbl_summary = QLabel("  Configure inputs via menus, then click Run Calculations")
        self.lbl_summary.setStyleSheet("background:#ecf0f1;padding:4px;border-radius:4px;")
        root.addWidget(self.lbl_summary)

        # ── Outer 4-scenario tab widget ───────────────────────────────────
        from PySide6.QtWidgets import (QButtonGroup, QRadioButton, QComboBox)
        self.outer_tabs = QTabWidget()
        self.outer_tabs.currentChanged.connect(self._on_scenario_changed)
        root.addWidget(self.outer_tabs)

        # Each scenario gets its own controls and inner result tabs.
        # We store per-scenario UI refs in dicts keyed by scenario index.
        self._stabs = {}          # i → dict of inner tab layouts
        self._swidgets = {}       # i → dict of per-scenario widgets

        for si in range(4):
            scenario_w = QWidget()
            scenario_l = QVBoxLayout(scenario_w)
            scenario_l.setContentsMargins(4, 4, 4, 4)

            # ── Per-scenario toolbar ──────────────────────────────────────
            stbar = QHBoxLayout()

            btn_run = QPushButton("Run Calculations")
            btn_run.setStyleSheet(
                "background:#2ecc71;color:white;font-weight:bold;"
                "padding:6px 18px;border-radius:4px;")
            btn_run.clicked.connect(self._run_calculations)
            stbar.addWidget(btn_run)

            # Conservative / Historical mode toggle
            btn_mode = QPushButton("Mode: Historical")
            btn_mode.setCheckable(True)
            btn_mode.setChecked(False)
            btn_mode.setToolTip(
                "<b>Historical Mode</b> (default): per-fund returns use 5Y CAGR.<br>"
                "<b>Conservative Mode</b>: per-fund returns use Worst_Exp_Ret_%<br>"
                "(minimum historical rolling CAGR minus STT costs) — a genuine<br>"
                "stress-tested lower-bound for corpus survival.")
            btn_mode.toggled.connect(self._on_mode_toggled)
            btn_mode.setStyleSheet(
                "background:#95a5a6;color:white;font-weight:bold;"
                "padding:6px 12px;border-radius:4px;")
            stbar.addWidget(btn_mode)

            stbar.addSpacing(12)

            # ── Mode A / Mode B radio buttons (framed) ────────────────────
            from PySide6.QtWidgets import QFrame
            mode_frame = QFrame()
            mode_frame.setFrameShape(QFrame.Shape.StyledPanel)
            mode_frame.setStyleSheet(
                "QFrame { background: #eef3ff; border: 1px solid #b0c4de;"
                " border-radius: 4px; padding: 2px 6px; }")
            mode_lay = QHBoxLayout(mode_frame)
            mode_lay.setContentsMargins(6, 2, 6, 2)
            mode_lay.setSpacing(8)
            mode_lbl = QLabel("<b>Allocation:</b>")
            mode_lbl.setStyleSheet("border: none; background: transparent;")
            mode_lay.addWidget(mode_lbl)

            alloc_group = QButtonGroup(scenario_w)
            rb_a = QRadioButton("Mode A: Singular")
            rb_a.setStyleSheet("border: none; background: transparent;")
            rb_a.setToolTip(
                "<b>Mode A: Singular Lifetime Allocation</b><br>"
                "One portfolio held for all 30 years — buy and hold, no rebalancing.<br>"
                "Merges chunk constraints (strictest wins) into one allocation.")
            rb_b = QRadioButton("Mode B: Optimized Turnover")
            rb_b.setStyleSheet("border: none; background: transparent;")
            rb_b.setToolTip(
                "<b>Mode B: Chunk-by-Chunk Sticky Portfolio</b><br>"
                "Separate allocation per chunk, backward-induction minimizes turnover.<br>"
                "Glide-path interpolation spreads rebalancing over N years.")
            alloc_group.addButton(rb_a, 0)
            alloc_group.addButton(rb_b, 1)
            if self.scenarios[si].allocation_mode == "singular":
                rb_a.setChecked(True)
            else:
                rb_b.setChecked(True)
            alloc_group.buttonToggled.connect(self._on_alloc_mode_changed)
            mode_lay.addWidget(rb_a)
            mode_lay.addWidget(rb_b)
            stbar.addWidget(mode_frame)

            stbar.addSpacing(8)

            # ── Optimizer method radio buttons (framed) ───────────────────
            opt_frame = QFrame()
            opt_frame.setFrameShape(QFrame.Shape.StyledPanel)
            opt_frame.setStyleSheet(
                "QFrame { background: #f0f8ee; border: 1px solid #a8d5a2;"
                " border-radius: 4px; padding: 2px 6px; }")
            opt_lay = QHBoxLayout(opt_frame)
            opt_lay.setContentsMargins(6, 2, 6, 2)
            opt_lay.setSpacing(8)
            opt_lbl = QLabel("<b>Optimizer:</b>")
            opt_lbl.setStyleSheet("border: none; background: transparent;")
            opt_lay.addWidget(opt_lbl)

            opt_method_group = QButtonGroup(scenario_w)
            rb_pulp = QRadioButton("PuLP method")
            rb_pulp.setStyleSheet("border: none; background: transparent;")
            rb_pulp.setToolTip(
                "<b>PuLP Commonality Walk</b><br>"
                "Minimises portfolio std_dev via PuLP CBC MILP solver.<br>"
                "Maximises cross-chunk fund overlap (~60% common to all chunks).<br>"
                "Produces lower risk and better rebalancing efficiency.")
            rb_homegrown = QRadioButton("Home-grown method")
            rb_homegrown.setStyleSheet("border: none; background: transparent;")
            rb_homegrown.setToolTip(
                "<b>Home-grown α-blending / Frontier Walk</b><br>"
                "Original method: generates candidates via λ-blending or<br>"
                "frontier walk, scores combinations by quality-weighted overlap.")
            opt_method_group.addButton(rb_pulp, 0)
            opt_method_group.addButton(rb_homegrown, 1)
            # Default to PuLP
            _use_pulp = True
            if self.scenarios[si].allocation_params:
                _use_pulp = self.scenarios[si].allocation_params.get(
                    "pulp_commonality", True)
            if _use_pulp:
                rb_pulp.setChecked(True)
            else:
                rb_homegrown.setChecked(True)
            opt_method_group.buttonToggled.connect(self._on_opt_method_changed)
            opt_lay.addWidget(rb_pulp)
            opt_lay.addWidget(rb_homegrown)
            stbar.addWidget(opt_frame)

            stbar.addSpacing(6)
            lbl_spread = QLabel("Spread:")
            stbar.addWidget(lbl_spread)
            spin_spread = QSpinBox()
            spin_spread.setRange(2, 10)
            spin_spread.setValue(self.scenarios[si].rebalance_spread_years)
            spin_spread.setFixedWidth(50)
            spin_spread.setToolTip(
                "Glide-path rebalancing spread in years (Mode B only).\n"
                "Rebalancing is distributed over this many years around each\n"
                "chunk boundary, reducing tax incidence vs a cliff rebalance.")
            spin_spread.valueChanged.connect(self._on_spread_changed)
            stbar.addWidget(spin_spread)

            stbar.addSpacing(8)
            chk_tax = QCheckBox("Minimise taxes")
            chk_tax.setChecked(False)
            chk_tax.setToolTip(
                "When enabled, the engine runs a two-pass optimiser that shifts\n"
                "withdrawal load from Personal to HUF each FY to minimise the\n"
                "combined tax bill (Personal + HUF + rebalancing).")
            stbar.addWidget(chk_tax)

            stbar.addStretch()
            scenario_l.addLayout(stbar)

            # ── Inner result tabs ─────────────────────────────────────────
            inner_tabs = QTabWidget()
            scenario_l.addWidget(inner_tabs)

            tabs_dict = {}
            for title_key, tab_title in [
                ("pm",    "Personal - Monthly"),
                ("py",    "Personal - Annual Summary"),
                ("hm",    "HUF - Monthly"),
                ("ann",   "Annual Combined Summary"),
                ("sens",  "Sensitivity Analysis"),
                ("mc",    "Monte Carlo"),
                ("drift", "Allocation Glide Path"),
                ("rebal", "Rebalancing Cost"),
            ]:
                w = QWidget()
                l = QVBoxLayout(w)
                inner_tabs.addTab(w, tab_title)
                tabs_dict[title_key + "_w"] = w
                tabs_dict[title_key + "_l"] = l

            # Add placeholder messages
            placeholders = {
                "pm_l":    "Run calculations to see monthly personal SWP detail.",
                "py_l":    "Run calculations to see annual personal summary.",
                "hm_l":    "Run calculations to see monthly HUF detail.",
                "ann_l":   "Run calculations to see combined annual summary.",
                "sens_l":  "Use Analysis menu to run sensitivity analysis.",
                "mc_l":    "Use Analysis > Run Monte Carlo Simulation...",
                "drift_l": "Run Data → Optimize Sticky Portfolio first, then Run Calculations.",
                "rebal_l": "Run Data → Optimize Sticky Portfolio first, then Run Calculations.",
            }
            for key, msg in placeholders.items():
                ph = QLabel(msg)
                ph.setAlignment(Qt.AlignmentFlag.AlignCenter)
                ph.setStyleSheet("color:#888;font-size:14px;")
                tabs_dict[key].addWidget(ph)

            self._stabs[si] = tabs_dict
            self._swidgets[si] = {
                'btn_run': btn_run, 'btn_mode': btn_mode,
                'rb_mode_a': rb_a, 'rb_mode_b': rb_b,
                'alloc_group': alloc_group,
                'rb_pulp': rb_pulp, 'rb_homegrown': rb_homegrown,
                'opt_method_group': opt_method_group,
                'lbl_spread': lbl_spread, 'spin_spread': spin_spread,
                'chk_tax_optimise': chk_tax,
                'inner_tabs': inner_tabs,
            }

            self.outer_tabs.addTab(scenario_w, f"Option {si+1}")

        # ── Convenience aliases for the active scenario's tabs ────────────
        # These are resolved dynamically via properties so that existing
        # code (e.g. _populate_tabs) still works unchanged.
        self._update_active_tab_aliases()
        self._update_spread_visibility()

        self.setStatusBar(QStatusBar())

    def _setup_menus(self):
        mb = self.menuBar()

        fm = mb.addMenu("File")
        self._act(fm, "New",                  self._new_project)
        self._act(fm, "Open...",              self._open_project)
        self._act(fm, "Save...",              self._save_project)
        fm.addSeparator()
        self._act(fm, "Save All CSVs...",     self._save_all_csvs)
        fm.addSeparator()
        self._act(fm, "Quit",                 self.close)

        # ── NEW: Data menu ────────────────────────────────────────────────────
        dm = mb.addMenu("Data")
        self._act(dm, "Fetch Scheme and Fund Names...", self._fetch_scheme_names)
        self._act(dm, "Fetch Fund Metrics...",  self._fetch_fund_metrics)
        self._act(dm, "Allocate Capital...",    self._run_capital_allocation)
        dm.addSeparator()
        self._act(dm, "Optimize Sticky Portfolio...", self._run_sticky_optimization)
        self._act(dm, "Show Glide Path Charts...",    self._show_glide_path_charts)
        self._act(dm, "Show Optimization Report...", self._show_optimization_report)
        dm.addSeparator()
        self._act(dm, "Import Fund Metrics from CSV...", self._import_fund_metrics)
        self._act(dm, "Evaluate & Apply Best Chunk...", self._evaluate_best_chunk)

        cm = mb.addMenu("Configuration")
        # ── Shared (applies to ALL scenarios) ──
        cm.addSection("── Shared (all scenarios) ──")
        self._act(cm, "Tax Rules (Individual & HUF)...", self._open_tax_rules)
        self._act(cm, "Annual Withdrawal Requirements...", self._open_requirements)
        self._act(cm, "FD Interest Rate Chunks...",        self._open_fd_rate_chunks)
        self._act(cm, "Other Income Sources...",          self._open_income)
        self._act(cm, "Windfall Gains...",                self._open_windfalls)
        self._act(cm, "HUF Withdrawal Schedule...",       self._open_huf_withdrawals)
        cm.addSeparator()
        # ── Per-Scenario (active option tab only) ──
        cm.addSection("── Active Scenario Only ──")
        self._act(cm, "View Fund Selection && Allocation...",  self._open_funds)
        self._act(cm, "Portfolio Return Rate Chunks...",  self._open_return_rate)
        self._act(cm, "Debt:Equity Withdrawal Split...",  self._open_split)
        self._act(cm, "Glide-Path Parameters...",             self._open_glide_path_params)
        self._act(cm, "Rebalancing Constraints Audit...",     self._open_rebal_constraints)

        am = mb.addMenu("Analysis")
        self._act(am, "Run Sensitivity Analysis...", self._run_sensitivity)
        self._act(am, "Run Monte Carlo Simulation...", self._run_monte_carlo)

    def _act(self, menu, label, slot):
        a = QAction(label, self)
        a.triggered.connect(slot)
        menu.addAction(a)

    # ── Scenario management ───────────────────────────────────────────────────

    def _on_scenario_changed(self, idx):
        """Called when the user switches outer tabs."""
        self._active_scenario_idx = idx
        self._update_active_tab_aliases()
        self._update_spread_visibility()
        self._update_status()

    def _update_active_tab_aliases(self):
        """
        Point the legacy self.tab_*_l / self.tab_*_w aliases to the
        active scenario's inner tab widgets, so _populate_tabs, chart
        callbacks, and other existing code works unchanged.
        """
        si = self._active_scenario_idx
        t = self._stabs[si]
        for key in ('pm', 'py', 'hm', 'ann', 'sens', 'mc', 'drift', 'rebal'):
            setattr(self, f'tab_{key}_w', t[key + '_w'])
            setattr(self, f'tab_{key}_l', t[key + '_l'])
        # Also alias per-scenario widgets so existing code works
        sw = self._swidgets[si]
        self.btn_run = sw['btn_run']
        self.btn_mode = sw['btn_mode']
        self.rb_mode_a = sw['rb_mode_a']
        self.rb_mode_b = sw['rb_mode_b']
        self._alloc_mode_group = sw['alloc_group']
        self.rb_pulp = sw['rb_pulp']
        self.rb_homegrown = sw['rb_homegrown']
        self._opt_method_group = sw['opt_method_group']
        self.lbl_spread = sw['lbl_spread']
        self.spin_spread = sw['spin_spread']
        self.chk_tax_optimise = sw['chk_tax_optimise']
        self.tabs = sw['inner_tabs']

    # ── Helpers ────────────────────────────────────────────────────────────────
    def _on_date_changed(self, qd: QDate):
        self.state.investment_date = date(qd.year(), qd.month(), qd.day())
        self._propagate_shared_fields()

    def _update_status(self):
        import os, logging
        log_path = os.environ.get("SWP_DEBUG_LOG")
        _log = logging.getLogger("main.debug")
        if log_path and not _log.handlers:
            _fh = logging.FileHandler(log_path, mode="a", encoding="utf-8")
            _fh.setFormatter(logging.Formatter("%(message)s"))
            _log.addHandler(_fh)
            _log.setLevel(logging.DEBUG)

        t = self.state.total_allocation()
        d = self.state.total_debt_allocation()
        e = self.state.total_equity_allocation()
        o = self.state.total_other_allocation()

        _log.debug(f"\n_update_status: T={t:.2f} D={d:.2f} E={e:.2f} O={o:.2f}")
        _log.debug(f"  chunks: {len(self.state.allocation_chunks)}")
        _log.debug(f"  state.funds: {len(self.state.funds)} entries")

        # Show FD rate range from chunks
        if self.state.fd_rate_chunks:
            rates = [c.fd_rate for c in self.state.fd_rate_chunks]
            fd_str = f"FD: {min(rates)*100:.2f}–{max(rates)*100:.2f}%"
        else:
            fd_str = f"FD: {self.state.fd_rate*100:.2f}%"
        parts = f"  Total: Rs {t:.1f}L  |  Debt: Rs {d:.1f}L  |  Equity: Rs {e:.1f}L"
        if o > 0:
            parts += f"  |  Other: Rs {o:.1f}L"
        parts += f"  |  {fd_str}  |  Investment Date: {self.state.investment_date}"
        self.lbl_summary.setText(parts)

    def _update_fd_rate_label(self):
        if self.state.fd_rate_chunks:
            parts = [f"Yr{c.year_from}-{c.year_to}: {c.fd_rate*100:.1f}%"
                     for c in self.state.fd_rate_chunks]
            self.lbl_fd_rate.setText("  " + " | ".join(parts))
        else:
            self.lbl_fd_rate.setText(f"  {self.state.fd_rate*100:.2f}%")

    def _on_mode_toggled(self, checked: bool):
        # Find which scenario's button sent the signal
        sender = self.sender()
        if sender is None:
            sender = self.btn_mode
        if checked:
            sender.setText("Mode: Conservative ⚠")
            sender.setStyleSheet(
                "background:#e67e22;color:white;font-weight:bold;"
                "padding:6px 12px;border-radius:4px;")
        else:
            sender.setText("Mode: Historical")
            sender.setStyleSheet(
                "background:#95a5a6;color:white;font-weight:bold;"
                "padding:6px 12px;border-radius:4px;")

    def _on_alloc_mode_changed(self, btn, checked: bool):
        if not checked:
            return
        if self._alloc_mode_group.id(btn) == 0:
            self.state.allocation_mode = "singular"
        else:
            self.state.allocation_mode = "chunked_sticky"
        self._update_spread_visibility()

    def _on_opt_method_changed(self, btn, checked: bool):
        if not checked:
            return
        if self.state.allocation_params is None:
            self.state.allocation_params = {}
        # id 0 = PuLP, id 1 = home-grown
        group = btn.group()
        btn_id = group.id(btn) if group else -1
        use_pulp = (btn_id == 0)
        self.state.allocation_params["pulp_commonality"] = use_pulp
        print(f"[OPT-DIAG] _on_opt_method_changed: "
              f"btn_id={btn_id}, use_pulp={use_pulp}, "
              f"btn.text()={btn.text()!r}, "
              f"scenario_idx={self._active_scenario_idx}, "
              f"state id={id(self.state)}, "
              f"allocation_params={self.state.allocation_params!r}",
              flush=True)

    def _on_spread_changed(self, value: int):
        self.state.rebalance_spread_years = value

    def _update_spread_visibility(self):
        mode_b = (self.state.allocation_mode == "chunked_sticky")
        self.lbl_spread.setVisible(mode_b)
        self.spin_spread.setVisible(mode_b)

    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _keep_chart(self, win):
        """Keep a chart window alive and show it.
        Automatically removes the reference when the window is closed/destroyed
        (WA_DeleteOnClose is True) so Matplotlib figures are freed."""
        self._chart_windows.append(win)
        win.destroyed.connect(lambda: self._chart_windows.remove(win)
                              if win in self._chart_windows else None)
        win.show()
        win.raise_()

    # ── Menu actions ───────────────────────────────────────────────────────────
    def _open_tax_rules(self):
        TaxRulesDialog(self.state, self).exec()
        self._propagate_shared_fields()

    def _open_funds(self):
        import os, logging
        log_path = os.environ.get("SWP_DEBUG_LOG")
        _log = logging.getLogger("main.debug")
        if log_path and not _log.handlers:
            _fh = logging.FileHandler(log_path, mode="a", encoding="utf-8")
            _fh.setFormatter(logging.Formatter("%(message)s"))
            _log.addHandler(_fh)
            _log.setLevel(logging.DEBUG)

        _log.debug("\n" + "=" * 70)
        _log.debug("_open_funds CALLED (Fund Dialog opening)")
        _log.debug(f"  chunks: {len(self.state.allocation_chunks)}")
        for i, ac in enumerate(self.state.allocation_chunks):
            d = sum(f.allocation for f in ac.funds if f.fund_type == 'debt')
            e = sum(f.allocation for f in ac.funds if f.fund_type == 'equity')
            o = sum(f.allocation for f in ac.funds if f.fund_type == 'other')
            _log.debug(f"    chunk[{i}] yr{ac.year_from}-{ac.year_to}: D={d:.2f} E={e:.2f} O={o:.2f}")

        if FundAllocationDialog(self.state, self).exec():
            _log.debug("  Fund Dialog ACCEPTED (OK clicked)")
            _log.debug(f"  chunks after dialog: {len(self.state.allocation_chunks)}")
            for i, ac in enumerate(self.state.allocation_chunks):
                d = sum(f.allocation for f in ac.funds if f.fund_type == 'debt')
                e = sum(f.allocation for f in ac.funds if f.fund_type == 'equity')
                o = sum(f.allocation for f in ac.funds if f.fund_type == 'other')
                _log.debug(f"    chunk[{i}] yr{ac.year_from}-{ac.year_to}: D={d:.2f} E={e:.2f} O={o:.2f}")
                for f in ac.funds:
                    if f.allocation > 0:
                        _log.debug(f"      {f.name[:45]:<45s} type={f.fund_type:<6s} alloc={f.allocation:.2f}")
            self._update_fd_rate_label()
            self._update_status()
        else:
            _log.debug("  Fund Dialog CANCELLED")
    def _open_requirements(self):
        RequirementsDialog(self.state, self).exec()
        self._propagate_shared_fields()
    def _open_return_rate(self):    ReturnRateDialog(self.state, parent=self).exec()
    def _open_split(self):          SplitDialog(self.state, self).exec()
    def _open_income(self):
        IncomeDialog(self.state, self).exec()
        self._propagate_shared_fields()
    def _open_windfalls(self):
        WindfallDialog(self.state, self).exec()
        self._propagate_shared_fields()
    def _open_huf_withdrawals(self):
        HUFWithdrawalDialog(self.state, self).exec()
        self._propagate_shared_fields()

    def _open_glide_path_params(self):
        from dialogs import GlidePathParametersDialog
        dlg = GlidePathParametersDialog(self.state, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            # State already updated inside _save(); sync toolbar widgets too
            if self.state.allocation_mode == "singular":
                self.rb_mode_a.setChecked(True)
            else:
                self.rb_mode_b.setChecked(True)
            self.spin_spread.setValue(self.state.rebalance_spread_years)
            self._update_spread_visibility()
            # Persist BI tolerances so the next optimization picks them up
            tols = dlg.get_tolerances()
            self.state.bi_tolerances = tols
            self.statusBar().showMessage(
                f"Glide-path parameters saved — "
                f"mode={'A' if self.state.allocation_mode=='singular' else 'B'}, "
                f"spread={self.state.rebalance_spread_years}yr, "
                f"tolerances: ret={tols['return']*100:.3f}pp "
                f"std={tols['std_dev']*100:.3f}pp "
                f"dd={tols['max_dd']*100:.3f}pp"
            )

    def _open_rebal_constraints(self):
        from dialogs import RebalancingConstraintsDialog
        dlg = RebalancingConstraintsDialog(self.state, self)
        dlg.exec()
    def _open_fd_rate_chunks(self):
        from dialogs import FDRateChunksDialog
        if FDRateChunksDialog(self.state, self).exec():
            self._propagate_shared_fields()
            self._update_fd_rate_label()
            self._update_status()

    # ── Calculations ───────────────────────────────────────────────────────────
    def _run_calculations(self):
        self.btn_run.setEnabled(False)
        self.statusBar().showMessage("Calculating...")

        # ── Debug logging ──────────────────────────────────────────────
        import os, logging
        log_path = os.environ.get("SWP_DEBUG_LOG")
        if log_path:
            _log = logging.getLogger("main.debug")
            _fh = logging.FileHandler(log_path, mode="a", encoding="utf-8")
            _fh.setFormatter(logging.Formatter("%(message)s"))
            _log.addHandler(_fh)
            _log.setLevel(logging.DEBUG)
            _log.debug("\n" + "=" * 70)
            _log.debug("_run_calculations CALLED")
            _log.debug(f"  allocation_chunks: {len(self.state.allocation_chunks)}")
            for i, ac in enumerate(self.state.allocation_chunks):
                d = sum(f.allocation for f in ac.funds if f.fund_type == 'debt')
                e = sum(f.allocation for f in ac.funds if f.fund_type == 'equity')
                o = sum(f.allocation for f in ac.funds if f.fund_type == 'other')
                _log.debug(f"    chunk[{i}] yr{ac.year_from}-{ac.year_to}: "
                            f"D={d:.2f} E={e:.2f} O={o:.2f}")
                for f in ac.funds:
                    if f.allocation > 0:
                        _log.debug(f"      {f.name[:45]:<45s} type={f.fund_type:<6s} "
                                    f"alloc={f.allocation:.2f}")
            _log.debug(f"  state.funds: {len(self.state.funds)} entries")
            fd = sum(f.allocation for f in self.state.funds if f.fund_type == 'debt')
            fe = sum(f.allocation for f in self.state.funds if f.fund_type == 'equity')
            fo = sum(f.allocation for f in self.state.funds if f.fund_type == 'other')
            _log.debug(f"    flat: D={fd:.2f} E={fe:.2f} O={fo:.2f}")
            _log.debug(f"  return_chunks: {[(c.year_from, c.year_to, c.annual_return) for c in self.state.return_chunks]}")

        conservative = self.btn_mode.isChecked()

        # ── Diagnostic logging before engine run ──────────────────────────
        import logging as _clo, os as _cos
        _clog = _clo.getLogger("main.calc")
        _clog.setLevel(_clo.DEBUG)
        _clp = _cos.environ.get("SWP_DEBUG_LOG")
        if _clp and not _clog.handlers:
            _cfh = _clo.FileHandler(_clp, mode="a", encoding="utf-8")
            _cfh.setFormatter(_clo.Formatter("%(message)s"))
            _clog.addHandler(_cfh)
        _clog.debug("\n" + "="*70)
        _clog.debug("RUN CALCULATIONS — glide_path and chunk state:")
        _gp = self.state.glide_path
        _clog.debug(f"  state.glide_path is None: {_gp is None}")
        if _gp is not None:
            _trans = sorted(_gp.transition_years())
            _clog.debug(f"  transition_years: {_trans}  (empty = flat/broken)")
            for _yr in [1, 10, 11, 20, 21, 30]:
                _yw = _gp.weights_for_year(_yr)
                _clog.debug(
                    f"  yr{_yr}: {len(_yw)} funds  "
                    f"top={sorted(_yw.items(),key=lambda x:-x[1])[:2] if _yw else 'EMPTY'}"
                )
        for _i, _c in enumerate(self.state.allocation_chunks or []):
            _tw = _c.target_weights or {}
            _tr = getattr(_c, "_type_ratios", {})
            _same = all(
                _c2.target_weights == _tw
                for _c2 in self.state.allocation_chunks
            ) if len(self.state.allocation_chunks) > 1 else False
            _clog.debug(
                f"  Chunk {_i+1} Yr{_c.year_from}-{_c.year_to}: "
                f"{len(_tw)} target_weights  _type_ratios={_tr}  ALL_SAME={_same}"
            )

        try:
            p_monthly, p_yearly, h_monthly, _ = Engine(
                self.state,
                conservative_mode=conservative,
                glide_path=self.state.glide_path,
            ).run()

            # ── Tax-optimal HUF–Personal withdrawal split ─────────────────
            # Only runs when the user has explicitly enabled the checkbox.
            if (self.chk_tax_optimise.isChecked()
                    and self.state.huf_withdrawal_chunks):
                import copy as _copy
                saved_reqs = _copy.deepcopy(self.state.annual_requirements)
                saved_huf  = _copy.deepcopy(
                    getattr(self.state, 'huf_annual_requirements', {}))
                try:
                    p_monthly, p_yearly, h_monthly, _ = (
                        optimize_withdrawal_split(
                            self.state,
                            p_monthly, h_monthly, p_yearly,
                            user_cap=7.5,
                            conservative_mode=conservative,
                            glide_path=self.state.glide_path,
                        )
                    )
                except Exception:
                    # If optimizer fails, restore original settings and
                    # keep baseline results
                    self.state.annual_requirements = saved_reqs
                    self.state.huf_annual_requirements = saved_huf
                    import traceback
                    traceback.print_exc()
                    QMessageBox.warning(
                        self, "Tax Optimisation Skipped",
                        "The tax minimisation optimizer encountered an error "
                        "and was skipped.\n\n"
                        "Results shown use the standard (unoptimised) "
                        "withdrawal schedule.\n\n"
                        "You can uncheck 'Minimise taxes' or adjust HUF "
                        "withdrawal settings and try again."
                    )

            self._last_yearly  = p_yearly
            self._last_results = (p_monthly, p_yearly, h_monthly, p_yearly)
            self._last_yearly_rows = p_yearly
            self._populate_tabs(p_monthly, p_yearly, h_monthly, conservative)
            mode_str = "Conservative" if conservative else "Historical"
            self.statusBar().showMessage(
                f"Done [{mode_str} mode]. "
                f"{len(p_monthly)} personal months, {len(h_monthly)} HUF months.")
        except Exception:
            import traceback
            QMessageBox.critical(self, "Calculation Error", traceback.format_exc())
            self.statusBar().showMessage("Error.")
        finally:
            self.btn_run.setEnabled(True)

    def _populate_tabs(self, p_monthly, p_yearly, h_monthly,
                       conservative: bool = False):
        for l in (self.tab_pm_l, self.tab_py_l, self.tab_hm_l, self.tab_ann_l,
                  self.tab_drift_l, self.tab_rebal_l):
            self._clear_layout(l)

        # ── Personal Monthly ──
        hdr = QHBoxLayout()
        hdr.addWidget(QLabel(
            f"Personal SWP - {len(p_monthly)} months (FY 1-30).  "
            "April rows: red = tax paid, green = HUF transfer.  All amounts Rs Lakhs."))
        hdr.addStretch()
        hdr.addWidget(_chart_btn("Show Chart", lambda: self._show_chart_pm(p_monthly)))
        hdr_w = QWidget(); hdr_w.setLayout(hdr)
        self.tab_pm_l.addWidget(hdr_w)
        self.tab_pm_l.addWidget(build_monthly_table(p_monthly, "personal"))

        # ── Personal Annual ──
        hdr2 = QHBoxLayout()
        hdr2.addWidget(QLabel(
            "Annual summary. Tax column = paid in April of the FOLLOWING FY. (Rs Lakhs)"))
        hdr2.addStretch()
        hdr2.addWidget(_chart_btn("Show Chart", lambda: self._show_chart_py(p_yearly)))
        hdr2_w = QWidget(); hdr2_w.setLayout(hdr2)
        self.tab_py_l.addWidget(hdr2_w)
        self.tab_py_l.addWidget(build_yearly_table(p_yearly))

        # ── HUF Monthly ──
        hdr3 = QHBoxLayout()
        hdr3.addWidget(QLabel(
            f"HUF - {len(h_monthly)} months.  April rows show transfer-in (green).  Rs Lakhs."))
        hdr3.addStretch()
        hdr3.addWidget(_chart_btn("Show Chart", lambda: self._show_chart_hm(h_monthly)))
        hdr3_w = QWidget(); hdr3_w.setLayout(hdr3)
        self.tab_hm_l.addWidget(hdr3_w)
        self.tab_hm_l.addWidget(build_monthly_table(h_monthly, "huf"))

        # ── Combined Annual ──
        hdr4 = QHBoxLayout()
        hdr4.addWidget(QLabel("30-Year Combined Annual Summary (Rs Lakhs)"))
        hdr4.addStretch()
        hdr4.addWidget(_chart_btn("Show Chart", lambda: self._show_chart_ann(p_yearly)))
        hdr4_w = QWidget(); hdr4_w.setLayout(hdr4)
        self.tab_ann_l.addWidget(hdr4_w)
        self.tab_ann_l.addWidget(build_yearly_table(p_yearly))
        self._add_kpi_strip(p_yearly, conservative=conservative)

        # ── Allocation Glide Path tab ──
        gp = self.state.glide_path
        if gp is not None and not gp.is_flat():
            hdr_gp = QHBoxLayout()
            hdr_gp.addWidget(QLabel(
                f"Portfolio allocation glide path — "
                f"{len(gp.transition_years())} transition year(s). "
                "Open chart window for interactive views."
            ))
            hdr_gp.addStretch()
            hdr_gp.addWidget(_chart_btn(
                "Open Drift Chart",
                lambda: self._show_chart_drift(gp)
            ))
            hdr_gp_w = QWidget(); hdr_gp_w.setLayout(hdr_gp)
            self.tab_drift_l.addWidget(hdr_gp_w)
            self.tab_drift_l.addWidget(self._build_glide_path_table(gp))
        else:
            msg = QLabel(
                "No glide path available.\n\n"
                "Run Data → Optimize Sticky Portfolio to generate a glide path\n"
                "in Mode B, then click Run Calculations."
            )
            msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
            msg.setStyleSheet("color:#888;font-size:13px;")
            self.tab_drift_l.addWidget(msg)

        # ── Rebalancing Cost tab ──
        has_rebal = gp is not None and any(
            r.rebalance_tax_paid + getattr(r, 'rebalance_exit_loads', 0.0) > 0
            for r in p_yearly
        )
        if has_rebal:
            total_rebal_tax   = sum(r.rebalance_tax_paid for r in p_yearly)
            total_exit_loads  = sum(getattr(r, 'rebalance_exit_loads', 0.0) for r in p_yearly)
            total_rebal       = total_rebal_tax + total_exit_loads
            total_tax         = sum(r.tax_personal for r in p_yearly)
            total_saved       = sum(r.tax_saved for r in p_yearly)
            hdr_rb = QHBoxLayout()
            hdr_rb.addWidget(QLabel(
                f"30-yr rebalancing cost: ₹{total_rebal:.2f}L  "
                f"(tax ₹{total_rebal_tax:.2f}L + exit loads ₹{total_exit_loads:.2f}L)  |  "
                f"Total 30-yr tax: ₹{total_tax:.2f}L  |  "
                f"Total 30-yr savings vs FD: ₹{total_saved:.2f}L"
            ))
            hdr_rb.addStretch()
            hdr_rb.addWidget(_chart_btn(
                "Open Rebalancing Chart",
                lambda _gp=gp, _yr=p_yearly: self._show_chart_rebal(_gp, _yr)
            ))
            hdr_rb_w = QWidget(); hdr_rb_w.setLayout(hdr_rb)
            self.tab_rebal_l.addWidget(hdr_rb_w)
            self.tab_rebal_l.addWidget(self._build_rebal_table(p_yearly))
        else:
            msg2 = QLabel(
                "No rebalancing activity detected.\n\n"
                "Rebalancing costs appear here after running\n"
                "Optimize Sticky Portfolio (Mode B) → Run Calculations."
            )
            msg2.setAlignment(Qt.AlignmentFlag.AlignCenter)
            msg2.setStyleSheet("color:#888;font-size:13px;")
            self.tab_rebal_l.addWidget(msg2)

    def _add_kpi_strip(self, yearly, conservative: bool = False):
        if not yearly:
            return
        last = yearly[-1]
        mode_label = "  [Conservative Mode]" if conservative else ""
        strip = QGroupBox(f"30-Year End Summary{mode_label}")
        if conservative:
            strip.setStyleSheet(
                "QGroupBox { font-weight:bold; color:#e67e22; "
                "border:2px solid #e67e22; border-radius:5px; "
                "margin-top:8px; padding-top:6px; }"
                "QGroupBox::title { subcontrol-origin:margin; left:10px; "
                "padding:0 4px; }")
        sl = QHBoxLayout(strip)

        def kpi(lbl, val, colour, tooltip=""):
            w = QWidget()
            vl = QVBoxLayout(w)
            num = QLabel(f"Rs {val:,.1f}L")
            num.setFont(QFont("", 16, QFont.Weight.Bold))
            num.setStyleSheet(f"color:{colour};")
            tag = QLabel(lbl)
            tag.setStyleSheet("color:#555;font-size:11px;")
            if tooltip:
                num.setToolTip(tooltip)
                tag.setToolTip(tooltip)
            vl.addWidget(num, alignment=Qt.AlignmentFlag.AlignCenter)
            vl.addWidget(tag, alignment=Qt.AlignmentFlag.AlignCenter)
            return w

        p_corpus = (last.corpus_debt_personal + last.corpus_equity_personal +
                    last.corpus_other_personal)
        h_corpus = (last.corpus_debt_huf + last.corpus_equity_huf +
                    last.corpus_other_huf)
        total_tax        = sum(y.tax_personal         for y in yearly)
        total_rebal_tax  = sum(y.rebalance_tax_paid   for y in yearly)
        total_rebal_loads = sum(y.rebalance_exit_loads for y in yearly)
        total_saved      = sum(y.tax_saved            for y in yearly)
        total_net_cash   = sum(y.net_cash_total       for y in yearly)

        sl.addWidget(kpi("Personal Corpus (Yr 30)", p_corpus,  "#2980b9"))
        sl.addWidget(kpi("HUF Corpus (Yr 30)",       h_corpus,  "#8e44ad"))
        sl.addWidget(kpi("Total Tax Paid (30 yrs)",  total_tax, "#c0392b"))
        sl.addWidget(kpi(
            "Rebalancing Tax Paid",
            total_rebal_tax,
            "#d35400",
            tooltip=(
                "Total income tax paid specifically due to chunk-boundary "
                "rebalancing gains over 30 years (Rs Lakhs). "
                "= portion of Total Tax Paid attributable to gains realised "
                "when switching fund allocations at chunk boundaries. "
                "This is the switching cost of your allocation strategy -- "
                "the tax you pay to rebalance rather than for SWP withdrawals."
            )
        ))
        if total_rebal_loads > 0:
            sl.addWidget(kpi(
                "Glide-Path Exit Loads",
                total_rebal_loads,
                "#8e44ad",
                tooltip=(
                    "Total exit loads paid during glide-path micro-rebalancing "
                    "over 30 years (Rs Lakhs). Applies to lots redeemed within "
                    "12 months of purchase during an inter-chunk rebalancing step."
                )
            ))
        sl.addWidget(kpi("Total Tax Saved vs FD",   total_saved,        "#27ae60"))
        sl.addWidget(kpi("Net Cash - Year 30",       last.net_cash_total,"#e67e22"))
        sl.addWidget(kpi("Total Net Cash (30 yrs)", total_net_cash,     "#16a085"))
        self.tab_ann_l.addWidget(strip)

    # ── Chart launchers ────────────────────────────────────────────────────────
    def _show_chart_pm(self, rows):
        from chart_dialog import PersonalMonthlyChart
        self._keep_chart(PersonalMonthlyChart(rows, self))

    def _show_chart_py(self, rows):
        from chart_dialog import PersonalAnnualChart
        self._keep_chart(PersonalAnnualChart(rows, self))

    def _show_chart_hm(self, rows):
        from chart_dialog import HUFMonthlyChart
        self._keep_chart(HUFMonthlyChart(rows, self))

    def _show_chart_ann(self, rows):
        from chart_dialog import AnnualSummaryChart
        self._keep_chart(AnnualSummaryChart(rows, self))

    def _show_chart_sens(self, results):
        from chart_dialog import SensitivityChart
        self._keep_chart(SensitivityChart(results, self))

    def _show_chart_mc(self, results):
        from chart_dialog import MonteCarloChart
        self._keep_chart(MonteCarloChart(results, self))

    def _show_optimization_report(self):
        """Open the post-optimization report dialog."""
        if not _OPT_REPORT_AVAILABLE:
            QMessageBox.warning(self, "Not Available",
                "optimization_report.py not found in the project directory.")
            return
        gp = self.state.glide_path
        if gp is None:
            QMessageBox.information(
                self, "No Optimization",
                "No glide path available.\n\n"
                "Run Data \u2192 Optimize Sticky Portfolio first.")
            return
        yearly = getattr(self, "_last_yearly", None)
        show_optimization_report(self.state, yearly_rows=yearly, parent=self)

    def _show_chart_drift(self, glide_path):
        from chart_dialog import AllocationDriftChart
        self._keep_chart(AllocationDriftChart(glide_path, self.state, self))

    def _show_chart_rebal(self, glide_path, yearly_rows):
        from chart_dialog import RebalancingCostChart
        self._keep_chart(RebalancingCostChart(yearly_rows, glide_path, self.state, self))

    def _build_glide_path_table(self, gp) -> QTableWidget:
        """Mini table showing weight changes at each transition year."""
        from PySide6.QtWidgets import QTableWidget, QTableWidgetItem
        trans = gp.transition_years()
        if not trans:
            t = QTableWidget(1, 1)
            t.setItem(0, 0, QTableWidgetItem("No transitions — flat glide path."))
            return t

        # Columns: Year | Fund | Prior Weight | New Weight | Change
        cols = ["Year", "Fund", "Prior Weight %", "New Weight %", "Change (pp)"]
        rows_data = []
        for y in sorted(trans):
            curr = gp.weights_for_year(y)
            prev = gp.weights_for_year(y - 1) if y > 1 else curr
            all_keys = sorted(set(curr.keys()) | set(prev.keys()))
            for fn in all_keys:
                cw = curr.get(fn, 0.0)
                pw = prev.get(fn, 0.0)
                delta = cw - pw
                if abs(delta) > 0.001:
                    rows_data.append((y, fn, pw * 100, cw * 100, delta * 100))

        t = QTableWidget(len(rows_data), len(cols))
        t.setHorizontalHeaderLabels(cols)
        t.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for c in (0, 2, 3, 4):
            t.horizontalHeader().setSectionResizeMode(
                c, QHeaderView.ResizeMode.ResizeToContents)
        t.setAlternatingRowColors(True)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setMaximumHeight(320)

        for i, (yr, fn, pw, cw, delta) in enumerate(rows_data):
            def _it(txt, bold=False):
                item = QTableWidgetItem(txt)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if bold:
                    f = QFont(); f.setBold(True); item.setFont(f)
                return item

            t.setItem(i, 0, _it(str(yr)))
            t.setItem(i, 1, _it(fn))
            t.setItem(i, 2, _it(f"{pw:.2f}%"))
            t.setItem(i, 3, _it(f"{cw:.2f}%"))
            delta_item = _it(f"{delta:+.2f}pp", bold=abs(delta) > 5)
            delta_item.setForeground(QColor("#27ae60") if delta > 0 else QColor("#c0392b"))
            t.setItem(i, 4, delta_item)
        return t

    def _build_rebal_table(self, yearly_rows) -> QTableWidget:
        """Table showing per-year rebalancing tax and exit loads."""
        from PySide6.QtWidgets import QTableWidget, QTableWidgetItem
        cols = ["FY", "Rebal Tax (₹L)", "Exit Loads (₹L)", "Total Cost (₹L)",
                "Regular Tax (₹L)", "Rebal % of Tax"]
        t = QTableWidget(len(yearly_rows), len(cols))
        t.setHorizontalHeaderLabels(cols)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        t.setAlternatingRowColors(True)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setMaximumHeight(360)

        for i, r in enumerate(yearly_rows):
            rt   = r.rebalance_tax_paid
            el   = getattr(r, 'rebalance_exit_loads', 0.0)
            cost = rt + el
            reg  = r.tax_personal - rt
            pct  = 100 * cost / max(r.tax_personal, 1e-9) if r.tax_personal > 0 else 0.0

            def _it(txt, highlight=False):
                item = QTableWidgetItem(txt)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if highlight:
                    item.setBackground(QColor("#fff3e0"))
                return item

            t.setItem(i, 0, _it(str(r.year)))
            t.setItem(i, 1, _it(f"{rt:.3f}",   highlight=(rt > 0)))
            t.setItem(i, 2, _it(f"{el:.3f}",   highlight=(el > 0)))
            t.setItem(i, 3, _it(f"{cost:.3f}", highlight=(cost > 0)))
            t.setItem(i, 4, _it(f"{reg:.3f}"))
            pct_item = _it(f"{pct:.1f}%", highlight=(pct > 10))
            if pct > 20:
                pct_item.setForeground(QColor("#c0392b"))
            t.setItem(i, 5, pct_item)
        return t

    def _build_mc_table(self, res) -> QTableWidget:
        """Inline summary table shown in the Monte Carlo tab."""
        cols = ["FY", "P5 Corpus", "P25 Corpus", "Median Corpus",
                "P75 Corpus", "P95 Corpus", "Det. Corpus",
                "P5 Net Cash", "Median Cash", "Det. Cash", "Ruin %"]
        t = QTableWidget(30, len(cols))
        t.setHorizontalHeaderLabels(cols)
        t.setAlternatingRowColors(True)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)

        CLR_RISK  = QColor("#fdecea")
        CLR_MED   = QColor("#eaf4fb")
        CLR_UP    = QColor("#eafaf1")

        for i, fy in enumerate(res.fy_labels):
            rp = res.ruin_by_fy[i]
            row_bg = CLR_RISK if rp > 0.05 else None

            def it(txt, bg=None):
                item = QTableWidgetItem(txt)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if bg: item.setBackground(bg)
                return item

            t.setItem(i, 0,  it(str(fy), row_bg))
            t.setItem(i, 1,  it(f"{res.corpus_p5[i]:,.0f}",
                                 CLR_RISK if res.corpus_p5[i] < 50 else row_bg))
            t.setItem(i, 2,  it(f"{res.corpus_p25[i]:,.0f}", row_bg))
            t.setItem(i, 3,  it(f"{res.corpus_p50[i]:,.0f}", CLR_MED))
            t.setItem(i, 4,  it(f"{res.corpus_p75[i]:,.0f}", row_bg))
            t.setItem(i, 5,  it(f"{res.corpus_p95[i]:,.0f}", CLR_UP))
            t.setItem(i, 6,  it(f"{res.corpus_det[i]:,.0f}", row_bg))
            t.setItem(i, 7,  it(f"{res.cash_p5[i]:,.1f}",   row_bg))
            t.setItem(i, 8,  it(f"{res.cash_p50[i]:,.1f}",  CLR_MED))
            t.setItem(i, 9,  it(f"{res.cash_det[i]:,.1f}",  row_bg))
            ruin_item = it(f"{rp*100:.1f}%", CLR_RISK if rp > 0 else None)
            if rp > 0:
                ruin_item.setForeground(QColor("#c0392b"))
                f = QFont(); f.setBold(True); ruin_item.setFont(f)
            t.setItem(i, 10, ruin_item)
        return t

    def _build_marginal_ruin_panel(self, res) -> QWidget:
        """
        Detailed panel for the marginal ruin path:
        the best-performing (highest cumulative return) simulation that still
        results in personal corpus depletion.  This is the most instructive
        near-miss — it shows the best-case sequence of returns that still fails.
        """
        import numpy as np

        geom_ret = float(np.exp(np.log1p(res.marginal_ruin_returns).mean()) - 1)

        container = QGroupBox(
            f"⚠  Marginal Ruin Path Detail  —  "
            f"Simulation #{res.marginal_ruin_idx + 1}  |  "
            f"Ruin in FY {res.marginal_ruin_fy}  |  "
            f"Geometric mean return: {geom_ret*100:.3f}%  "
            f"(best performer among all {int(res.ruin_probability * res.n_sims):,} ruined paths)"
        )
        container.setStyleSheet(
            "QGroupBox { font-weight:bold; color:#c0392b; "
            "border:2px solid #c0392b; border-radius:5px; margin-top:8px; padding-top:6px; }"
            "QGroupBox::title { subcontrol-origin:margin; left:10px; padding:0 4px; }"
        )
        layout = QVBoxLayout(container)

        # Explanation label
        expl = QLabel(
            "This path had the highest cumulative portfolio return among all simulations that still depleted "
            "the personal corpus.  The sequence-of-returns effect is visible: even with an above-average "
            f"geometric mean return of {geom_ret*100:.3f}%, an unfavourable sequence of early low / negative "
            "returns forced excess withdrawals from a shrunken corpus, creating a death spiral."
        )
        expl.setWordWrap(True)
        expl.setStyleSheet("color:#555;font-size:11px;padding:2px 4px 6px 4px;")
        layout.addWidget(expl)

        # Year-by-year table
        cols = ["FY", "Annual Return (%)", "Corpus (Total)", "vs Deterministic", "vs Median P50"]
        t = QTableWidget(30, len(cols))
        t.setHorizontalHeaderLabels(cols)
        t.setAlternatingRowColors(True)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        t.setMaximumHeight(520)

        CLR_BAD   = QColor("#fdecea")
        CLR_GOOD  = QColor("#eafaf1")
        CLR_WARN  = QColor("#fef9e7")
        CLR_RUIN  = QColor("#c0392b")

        for i in range(30):
            fy       = i + 1
            ret_pct  = res.marginal_ruin_returns[i] * 100
            corpus   = res.marginal_ruin_corpus[i]
            det      = res.corpus_det[i]
            med      = res.corpus_p50[i]
            vs_det   = corpus - det
            vs_med   = corpus - med
            is_ruin  = (fy == res.marginal_ruin_fy)

            # Row background: red at ruin year, warning for bad return years
            if is_ruin:
                row_bg = CLR_BAD
            elif ret_pct < float(res.marginal_ruin_returns.mean() * 100) - 1:
                row_bg = CLR_WARN   # below-average return year
            else:
                row_bg = None

            def ri(txt, bg=None, bold=False, fg=None):
                item = QTableWidgetItem(txt)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if bg:  item.setBackground(bg)
                if fg:  item.setForeground(fg)
                if bold:
                    f = QFont(); f.setBold(True); item.setFont(f)
                return item

            t.setItem(i, 0, ri(str(fy), row_bg, bold=is_ruin,
                               fg=QColor("#c0392b") if is_ruin else None))

            ret_item = ri(f"{ret_pct:+.3f}%", row_bg,
                          bold=(ret_pct < 0),
                          fg=QColor("#c0392b") if ret_pct < 0 else
                             QColor("#27ae60") if ret_pct > geom_ret * 100 else None)
            t.setItem(i, 1, ret_item)

            corp_item = ri(f"{corpus:,.1f}", row_bg, bold=is_ruin,
                           fg=QColor("#c0392b") if corpus < 50 else None)
            t.setItem(i, 2, corp_item)

            t.setItem(i, 3, ri(
                f"{vs_det:+,.1f}", row_bg,
                fg=QColor("#c0392b") if vs_det < 0 else QColor("#27ae60")
            ))
            t.setItem(i, 4, ri(
                f"{vs_med:+,.1f}", row_bg,
                fg=QColor("#c0392b") if vs_med < 0 else QColor("#27ae60")
            ))

            # Annotate the ruin row
            if is_ruin:
                t.item(i, 0).setToolTip(f"Personal corpus depleted in FY {fy}")
                for j in range(len(cols)):
                    if t.item(i, j):
                        t.item(i, j).setBackground(CLR_BAD)

        layout.addWidget(t)
        return container
    def _run_sensitivity(self):
        dlg = SensitivityDialog(self.state, self)
        if not dlg.exec():
            return
        scenarios = dlg.get_scenarios()
        if not scenarios:
            return
        try:
            results = run_sensitivity(self.state, scenarios,
                                      glide_path=self.state.glide_path)
            self._last_sensitivity = results
            self._clear_layout(self.tab_sens_l)

            hdr = QHBoxLayout()
            hdr.addWidget(QLabel(
                f"{len(results)} scenarios (Base + {len(results)-1} alternates). Rs Lakhs."))
            hdr.addStretch()
            hdr.addWidget(_chart_btn("Show Chart",
                                     lambda: self._show_chart_sens(self._last_sensitivity)))
            hdr_w = QWidget(); hdr_w.setLayout(hdr)
            self.tab_sens_l.addWidget(hdr_w)
            self.tab_sens_l.addWidget(build_sensitivity_table(results))
            self.tabs.setCurrentWidget(self.tab_sens_w)
        except Exception:
            import traceback
            QMessageBox.critical(self, "Error", traceback.format_exc())

    def _run_monte_carlo(self):
        from dialogs import MonteCarloDialog

        dlg = MonteCarloDialog(self.state, self)
        if not dlg.exec():
            return

        n_sims, sigma_override, floor_multiplier, seed, \
            use_bootstrap, block_length = dlg.get_params()

        method_str = (f"Block Bootstrap (block={block_length}yr)"
                      if use_bootstrap else "Log-Normal")

        # ── Progress dialog ───────────────────────────────────────────────────
        prog = QProgressDialog(
            f"Running {n_sims:,} Monte Carlo simulations [{method_str}]…",
            None,           # no Cancel button
            0, 6,           # determinate: 6 named stages
            self,
        )
        prog.setWindowTitle("Monte Carlo Simulation")
        prog.setWindowModality(Qt.WindowModal)
        prog.setMinimumDuration(0)
        prog.setMinimumWidth(420)
        prog.setValue(0)
        prog.show()
        QApplication.processEvents()

        def _stage(msg: str, step: int) -> None:
            """Update progress dialog label + bar and flush the event queue."""
            print(msg, flush=True)
            prog.setLabelText(msg)
            prog.setValue(step)
            QApplication.processEvents()

        # ── Run directly on the main thread ──────────────────────────────────
        # Previously this used a QThread + spin-wait (worker.wait(50 ms)).
        # That pattern blocks the main thread during each 50 ms wait slice,
        # preventing signals from the worker being delivered, so the progress
        # dialog never updated and prints were buffered.  Running directly on
        # the main thread with QApplication.processEvents() between stages is
        # simpler and gives immediate, reliable feedback.
        try:
            from monte_carlo import (
                run_monte_carlo,
                _fetch_nifty50_annual_returns,
                _fetch_debt_index_annual_returns,
            )

            self.statusBar().showMessage(f"Running Monte Carlo [{method_str}]…")

            eq_hist = dbt_hist = None
            if use_bootstrap:
                _stage("Fetching Nifty 50 NAV history…", 1)
                eq_hist = _fetch_nifty50_annual_returns()

                _stage("Loading Debt Index data…", 2)
                dbt_hist = _fetch_debt_index_annual_returns()

                if eq_hist is not None and dbt_hist is not None:
                    _stage(f"Running {n_sims:,} bootstrap simulations…", 3)
                else:
                    _stage(
                        f"Running {n_sims:,} log-normal simulations "
                        f"(data fetch failed)…", 3)
            else:
                _stage(f"Running {n_sims:,} log-normal simulations…", 3)

            _stage("Computing 30-year corpus paths…", 4)

            mc_kwargs = dict(
                n_sims=n_sims, sigma_override=sigma_override,
                floor_multiplier=floor_multiplier,
                seed=seed, use_bootstrap=use_bootstrap, block_length=block_length,
            )
            res = run_monte_carlo(self.state, **mc_kwargs)

            _stage("Calculating percentiles & ruin statistics…", 5)
            _stage("Done.", 6)

        except Exception:
            prog.close()
            QMessageBox.critical(self, "Monte Carlo Error", traceback.format_exc())
            self.statusBar().showMessage("Monte Carlo error.")
            return

        prog.close()
        try:
            self._last_mc = res
            self._clear_layout(self.tab_mc_l)

            # Header strip
            hdr = QHBoxLayout()
            rp   = res.ruin_probability * 100
            rp_colour = "#c0392b" if rp > 5 else "#e67e22" if rp > 1 else "#27ae60"
            # Build method details string
            if res.method_used == "block_bootstrap":
                method_detail = (
                    f"<b>Block Bootstrap</b> (block={res.block_length}yr  |  "
                    f"Nifty50: {res.n_equity_years}yr  |  "
                    f"Debt idx: {res.n_debt_years}yr history)"
                )
            else:
                method_detail = (
                    f"<b>Log-Normal</b>  |  σ = {res.sigma_used*100:.2f}%"
                    + (" <i>(bootstrap data unavailable)</i>"
                       if use_bootstrap else "")
                )
            # Build per-chunk floor summary for display
            _floor_parts = []
            if hasattr(res, 'floors') and hasattr(res, 'sigmas'):
                seen = {}
                for fy in range(30):
                    fl = res.floors[fy]
                    sig = res.sigmas[fy]
                    key = (round(fl, 5), round(sig, 5))
                    if key not in seen:
                        seen[key] = [fy + 1, fy + 1]
                    else:
                        seen[key][1] = fy + 1
                for (fl, sig), (yr_from, yr_to) in seen.items():
                    _floor_parts.append(
                        f"FY{yr_from}–{yr_to}: floor={fl*100:.2f}% (σ={sig*100:.2f}%)")
            floor_str = "  |  ".join(_floor_parts) if _floor_parts else (
                f"Floor FY1–10: {res.floor_fy1_10*100:.2f}%  |  "
                f"Floor FY11–30: {res.floor_fy11_30*100:.2f}%")

            info = QLabel(
                f"{method_detail}  |  "
                f"<b>{res.n_sims:,} simulations</b>  |  "
                f"{floor_str}  |  "
                f"30-yr ruin: <span style='color:{rp_colour};font-weight:bold'>"
                f"{rp:.1f}%</span>  |  "
                f"Median final corpus: <b>Rs {res.median_final_corpus:,.0f}L</b>  |  "
                f"P5 final corpus: <b>Rs {res.p5_final_corpus:,.0f}L</b>"
            )
            info.setStyleSheet("padding:4px;background:#ecf0f1;border-radius:4px;")
            hdr.addWidget(info)
            hdr.addStretch()
            hdr.addWidget(_chart_btn(
                "Show Charts",
                lambda: self._show_chart_mc(self._last_mc)
            ))
            hdr_w = QWidget(); hdr_w.setLayout(hdr)
            self.tab_mc_l.addWidget(hdr_w)

            # Mini results table
            self.tab_mc_l.addWidget(self._build_mc_table(res))

            # Marginal ruin detail panel (only if ruin was observed)
            if res.ruin_probability > 0 and res.marginal_ruin_idx is not None:
                self.tab_mc_l.addWidget(self._build_marginal_ruin_panel(res))

            self.tabs.setCurrentWidget(self.tab_mc_w)
            self.statusBar().showMessage(
                f"Monte Carlo done. {n_sims:,} paths. "
                f"30-yr ruin probability: {rp:.1f}%."
            )
        except Exception:
            import traceback
            QMessageBox.critical(self, "Monte Carlo Error — display",
                                 traceback.format_exc())
            self.statusBar().showMessage("Monte Carlo display error.")

    # ── Data menu actions ──────────────────────────────────────────────────────

    def _fetch_scheme_names(self):
        """Open dialog to run get_amfi_fund_schemes_names.py."""
        dlg = FetchSchemeNamesDialog(self.output_dir, self)
        dlg.exec()

    def _fetch_fund_metrics(self):
        """Open dialog to run get_funds_data.py and save Fund_Metrics_Output.csv."""
        dlg = FetchFundMetricsDialog(self.output_dir, self)
        dlg.exec()

    def _run_capital_allocation(self):
        """Open the Allocate Capital dialog; on success loads result into state."""
        metrics_path = self.output_dir / "Fund_Metrics_Output.csv"
        if not metrics_path.exists():
            QMessageBox.warning(
                self, "Fund Metrics Required",
                f"Fund_Metrics_Output.csv not found in:\n{self.output_dir}\n\n"
                "Please run Data → Fetch Fund Metrics first."
            )
            return
        dlg = AllocateCapitalDialog(
            metrics_path=metrics_path,
            output_dir=self.output_dir,
            state=self.state,
            parent=self,
            all_scenarios=self.scenarios,
            active_scenario_idx=self._active_scenario_idx,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            # Reload fund list from the allocation result so Configuration →
            # Fund Selection & Allocation reflects the new weights.
            self._update_status()
            self.statusBar().showMessage(
                "Capital allocation complete. Funds updated in state."
            )

    def _import_fund_metrics(self):
        """Import fund score metrics from a Fund_Metrics_Output.csv into project funds."""
        # Default to output_dir
        start_dir = str(self.output_dir)
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Fund Metrics CSV", start_dir,
            "CSV Files (*.csv);;All Files (*)")
        if not path:
            return
        try:
            count = self.state.import_fund_metrics(path)
            QMessageBox.information(
                self, "Import Complete",
                f"Updated metrics for {count} fund entries.\n\n"
                "Scores (Sharpe, Sortino, Calmar, Alpha, Beta, etc.) "
                "are now populated in the Fund Selection & Allocation dialog."
            )
            self._update_status()
            self.statusBar().showMessage(f"Imported fund metrics from: {path}")
        except Exception as e:
            QMessageBox.critical(self, "Import Error", str(e))

    def _show_glide_path_charts(self):
        """
        Menu action: open AllocationDriftChart and/or RebalancingCostChart
        for the current state.glide_path.  If no glide path is set, prompts
        the user to run Optimize Sticky Portfolio first.
        """
        gp = self.state.glide_path
        if gp is None:
            QMessageBox.information(
                self, "No Glide Path",
                "No glide path is available.\n\n"
                "Run Data → Optimize Sticky Portfolio (Mode B) to compute one,\n"
                "then run Data → Show Glide Path Charts."
            )
            return

        # Always open the drift chart (always useful even for flat glide paths)
        from chart_dialog import AllocationDriftChart, RebalancingCostChart
        drift_win = AllocationDriftChart(gp, self.state, self)
        drift_win.show()
        self._keep_chart(drift_win)

        # Only open rebalancing cost chart if we have yearly results
        yearly = getattr(self, "_last_yearly_rows", None)
        if yearly:
            has_rebal = any(
                r.rebalance_tax_paid + getattr(r, "rebalance_exit_loads", 0.0) > 0
                for r in yearly
            )
            if has_rebal:
                rebal_win = RebalancingCostChart(yearly, gp, self.state, self)
                rebal_win.show()
                self._keep_chart(rebal_win)
            else:
                self.statusBar().showMessage(
                    "Glide path drift chart opened. "
                    "Run Calculations to generate rebalancing cost data."
                )
        else:
            self.statusBar().showMessage(
                "Drift chart opened. Run Calculations to enable rebalancing cost view."
            )

    def _run_sticky_optimization(self):
        """
        Run optimize_sticky_portfolio (Mode A or Mode B) using the current
        state.allocation_chunks and all known funds from the metrics CSV.

        On completion, stores state.glide_path so it's used by the next
        Run Calculations call.
        """
        if not self.state.allocation_chunks:
            QMessageBox.warning(
                self, "No Allocation Chunks",
                "No allocation chunks found in state.\n\n"
                "Run Data → Allocate Capital first to build per-chunk allocations."
            )
            return

        # Gather all available funds for the expanded universe
        all_funds = []
        for ac in self.state.allocation_chunks:
            all_funds.extend(ac.funds)
        # Deduplicate by name, keep highest combined_ratio
        fund_map: dict = {}
        for f in all_funds:
            if f.name not in fund_map or (f.combined_ratio or 0) > (fund_map[f.name].combined_ratio or 0):
                fund_map[f.name] = f
        all_funds_deduped = list(fund_map.values())

        mode_label = "Mode A (Singular)" if self.state.allocation_mode == "singular" \
                     else f"Mode B (Sticky, spread={self.state.rebalance_spread_years}yr)"
        reply = QMessageBox.question(
            self, "Optimize Sticky Portfolio",
            f"Run sticky portfolio optimization?\n\n"
            f"Mode: {mode_label}\n"
            f"Chunks: {len(self.state.allocation_chunks)}\n"
            f"Fund universe: {len(all_funds_deduped)} seed funds + up to 50 similar\n\n"
            "This may take 30–120 seconds depending on universe size.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        prog = QProgressDialog("Running optimization…", None, 0, 0, self)
        prog.setWindowTitle("Sticky Portfolio Optimization")
        prog.setWindowModality(Qt.WindowModal)
        prog.setMinimumDuration(0)
        prog.setMinimumWidth(380)
        prog.setValue(0)
        prog.show()
        QApplication.processEvents()

        try:
            from allocate_funds import optimize_sticky_portfolio

            def _progress(msg: str):
                # Highlight Pass 1 / Pass 2 headers in the progress dialog
                display = msg
                if "Pass 1" in msg or "[Aim]" in msg:
                    display = f"<b>🎯 {msg}</b>"
                elif "Pass 2" in msg or "[Track]" in msg:
                    display = f"<b>🔗 {msg}</b>"
                prog.setLabelText(display)
                QApplication.processEvents()

            # Log chunk state BEFORE optimizer
            import logging as _lo, os as _os2
            _olog = _lo.getLogger("main.optimizer")
            _olog.setLevel(_lo.DEBUG)
            _lp = _os2.environ.get("SWP_DEBUG_LOG")
            if _lp and not _olog.handlers:
                _fh2 = _lo.FileHandler(_lp, mode="a", encoding="utf-8")
                _fh2.setFormatter(_lo.Formatter("%(message)s"))
                _olog.addHandler(_fh2)
            _olog.debug("\n" + "="*70)
            _olog.debug("OPTIMIZER START — target_weights BEFORE:")
            for _i, _c in enumerate(self.state.allocation_chunks):
                _tw = _c.target_weights or {}
                _same = all(_c2.target_weights == _tw for _c2 in self.state.allocation_chunks)
                _olog.debug(f"  Chunk {_i+1} Yr{_c.year_from}-{_c.year_to}: {len(_tw)} funds  all_same={_same}")
                for _fn, _w in sorted(_tw.items(), key=lambda x: -x[1])[:2]:
                    _olog.debug(f"    {_fn}: {_w:.4f}")

            glide_path = optimize_sticky_portfolio(
                self.state,
                all_funds_deduped,
                progress_cb=_progress,
            )
            self.state.glide_path = glide_path

            # ── Sync target_weights back to chunk.funds[].allocation ─────────
            # The optimizer writes target_weights (normalised to 1.0) but does
            # NOT touch chunk.funds[].allocation.  portfolio_yield(), std_dev
            # displays, and the FundAllocationDialog all read from
            # chunk.funds[].allocation, so they would show stale numbers.
            # Fix: redistribute each chunk's total corpus according to the
            # new target_weights.
            for ac in self.state.allocation_chunks:
                tw = ac.target_weights
                if not tw:
                    continue
                total_corpus = sum(f.allocation for f in ac.funds)
                if total_corpus <= 0:
                    continue
                fund_map = {f.name: f for f in ac.funds}
                # Zero out all allocations first
                for f in ac.funds:
                    f.allocation = 0.0
                # Set allocations from target_weights
                for fname, w in tw.items():
                    if fname in fund_map:
                        fund_map[fname].allocation = round(w * total_corpus, 4)
                    else:
                        # New fund from expanded universe — add it to the chunk
                        # with minimal metrics (the user should re-import metrics)
                        from models import FundEntry
                        new_f = FundEntry(
                            name=fname, fund_type="debt",
                            allocation=round(w * total_corpus, 4))
                        # Try to copy metrics from seed funds in universe
                        for uf in all_funds_deduped:
                            if uf.name == fname:
                                for attr in ("fund_type", "std_dev", "sharpe",
                                             "sortino", "calmar", "alpha",
                                             "treynor", "max_dd", "beta",
                                             "combined_ratio", "cagr_1", "cagr_3",
                                             "cagr_5", "cagr_10", "worst_exp_ret",
                                             "amfi_fund_type"):
                                    setattr(new_f, attr, getattr(uf, attr))
                                break
                        ac.funds.append(new_f)
                # Remove funds with zero allocation (dropped by optimizer)
                ac.funds = [f for f in ac.funds if f.allocation > 0]
            # Also sync flat funds list from last chunk
            if self.state.allocation_chunks:
                import copy as _copy
                self.state.funds = [
                    _copy.deepcopy(f)
                    for f in self.state.allocation_chunks[-1].funds
                ]

            # Re-import fund metrics so newly-created FundEntry objects (from
            # expanded universe) carry the full score set.  Also normalises
            # max_dd to fraction convention.
            _metrics_csv = self.output_dir / "Fund_Metrics_Output.csv"
            if _metrics_csv.exists():
                try:
                    _n = self.state.import_fund_metrics(str(_metrics_csv))
                    print(f"  Re-imported fund metrics for {_n} unique funds.",
                          flush=True)
                except Exception as _exc:
                    print(f"  ⚠ Could not re-import fund metrics: {_exc}",
                          flush=True)

            # ── Refresh return_chunks from optimizer's updated target_weights ──
            # The optimizer may change fund weights per chunk, so recalculate
            # the per-chunk portfolio yield and update return_chunks accordingly.
            from models import ReturnChunk
            new_return_chunks = []
            for ac in self.state.allocation_chunks:
                rate = ac.optimized_yield()
                new_return_chunks.append(ReturnChunk(
                    year_from=ac.year_from, year_to=ac.year_to,
                    annual_return=round(rate, 5)))
            if new_return_chunks:
                if new_return_chunks[-1].year_to < 30:
                    last_rate = new_return_chunks[-1].annual_return
                    new_return_chunks.append(ReturnChunk(
                        new_return_chunks[-1].year_to + 1, 30, last_rate))
                self.state.return_chunks = new_return_chunks
                print(
                    "\n  Return rates refreshed from optimizer:\n" +
                    "\n".join(
                        f"    Yrs {c.year_from}–{c.year_to}: "
                        f"{c.annual_return*100:.3f}%"
                        for c in new_return_chunks
                    ), flush=True
                )

            # Log chunk state AFTER optimizer
            _olog.debug("\nOPTIMIZER DONE — target_weights AFTER:")
            for _i, _c in enumerate(self.state.allocation_chunks):
                _tw = _c.target_weights or {}
                _tr = getattr(_c, "_type_ratios", {})
                _same = all(_c2.target_weights == _tw for _c2 in self.state.allocation_chunks)
                _olog.debug(
                    f"  Chunk {_i+1} Yr{_c.year_from}-{_c.year_to}: "
                    f"{len(_tw)} funds  type_ratios={_tr}  ALL_IDENTICAL={_same}"
                )
                for _fn, _w in sorted(_tw.items(), key=lambda x: -x[1])[:2]:
                    _olog.debug(f"    {_fn}: {_w:.4f}")
            _olog.debug(
                f"  glide_path transition_years: "
                f"{sorted(glide_path.transition_years()) if glide_path else 'NO GLIDE PATH'}"
            )

            prog.close()

            flat = glide_path.is_flat()
            trans = glide_path.transition_years()
            # Show a summary dialog that also offers to open the report
            chunk_summary = ""
            for ci, c in enumerate(self.state.allocation_chunks):
                tr = getattr(c, "_type_ratios", {})
                if tr:
                    chunk_summary += (f"  Chunk {ci+1} (Yr {c.year_from}\u2013{c.year_to}): "
                                      f"D:{tr.get('debt',0)*100:.0f}% "
                                      f"E:{tr.get('equity',0)*100:.0f}% "
                                      f"O:{tr.get('other',0)*100:.0f}%\n")
            reply2 = QMessageBox.question(
                self, "Optimization Complete",
                f"Glide path computed.\n\n"
                f"Mode: {mode_label}\n"
                f"Flat portfolio: {'Yes (Mode A)' if flat else 'No'}\n"
                f"Transition years: {trans if trans else '(none)'}\n\n"
                + (f"Asset class ratios per chunk:\n{chunk_summary}\n" if chunk_summary else "")
                + "Open Optimization Report for full details?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes
            )
            if reply2 == QMessageBox.StandardButton.Yes and _OPT_REPORT_AVAILABLE:
                # Pass None: _last_yearly predates this new glide path,
                # so tax tabs would show stale data.  The report will show
                # a "Run Calculations first" placeholder on those tabs.
                show_optimization_report(self.state, yearly_rows=None, parent=self)
            self.statusBar().showMessage(
                f"Sticky optimization done [{mode_label}]. "
                f"{len(trans)} transition years. Click Run Calculations."
            )

        except Exception:
            prog.close()
            QMessageBox.critical(self, "Optimization Error", traceback.format_exc())
            self.statusBar().showMessage("Sticky optimization failed.")

    def _evaluate_best_chunk(self):
        """Evaluate all allocation chunks, show ranked options as radio buttons,
        and apply the user-selected rank."""
        import os, logging
        log_path = os.environ.get("SWP_DEBUG_LOG")
        _log = logging.getLogger("main.debug")
        if log_path and not _log.handlers:
            _fh = logging.FileHandler(log_path, mode="a", encoding="utf-8")
            _fh.setFormatter(logging.Formatter("%(message)s"))
            _log.addHandler(_fh)
            _log.setLevel(logging.DEBUG)

        scores = self.state.evaluate_chunk_scores()
        if not scores:
            QMessageBox.warning(self, "No Chunks",
                                "No allocation chunks with active funds found.")
            return

        # ── Build radio-button dialog ──────────────────────────────────────────
        dlg = QDialog(self)
        dlg.setWindowTitle("Chunk Robustness Ranking — Select Allocation")
        dlg.resize(860, 420)
        layout = QVBoxLayout(dlg)

        layout.addWidget(QLabel(
            "<b>Robustness Ranking</b> — select the allocation to apply for Years 1–30.<br>"
            "Rank #1 is the highest-scoring option. Lower ranks may offer different "
            "risk/return trade-offs.<br>"
            "Applying a rank replaces all chunks with a single Yr 1-30 allocation "
            "(no rebalancing = no switching tax)."
        ))

        # Table-style display with radio buttons
        from PySide6.QtWidgets import QScrollArea, QTableWidget, QTableWidgetItem
        tbl = QTableWidget(len(scores), 9)
        tbl.setHorizontalHeaderLabels([
            "Select", "Rank", "Chunk Label",
            "Return%", "Std Dev%", "Sharpe", "Comb Ratio",
            "Score", "Debt/Eq (L)"
        ])
        tbl.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for c in (0, 1, 3, 4, 5, 6, 7, 8):
            tbl.horizontalHeader().setSectionResizeMode(
                c, QHeaderView.ResizeMode.ResizeToContents)
        tbl.setAlternatingRowColors(True)
        tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

        from PySide6.QtWidgets import QButtonGroup, QRadioButton
        rb_group = QButtonGroup(dlg)
        current_rank = getattr(self.state, 'selected_robustness_rank', 0)

        for i, s in enumerate(scores):
            # Radio button in column 0
            rb = QRadioButton()
            rb.setChecked(i == current_rank)
            rb_group.addButton(rb, i)
            cell_w = QWidget()
            cell_l = QHBoxLayout(cell_w)
            cell_l.addWidget(rb)
            cell_l.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cell_l.setContentsMargins(0, 0, 0, 0)
            tbl.setCellWidget(i, 0, cell_w)

            def _ri(txt, bold=False):
                item = QTableWidgetItem(txt)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if bold:
                    f = QFont(); f.setBold(True); item.setFont(f)
                return item

            tbl.setItem(i, 1, _ri(f"#{i+1}", bold=(i == 0)))
            tbl.setItem(i, 2, _ri(s['label']))
            tbl.setItem(i, 3, _ri(f"{s['port_return']:.2f}"))
            tbl.setItem(i, 4, _ri(f"{s['port_std']:.2f}"))
            tbl.setItem(i, 5, _ri(f"{s['port_sharpe']:.3f}"))
            tbl.setItem(i, 6, _ri(f"{s['port_combined']:.3f}"))
            tbl.setItem(i, 7, _ri(f"{s['score']:.3f}", bold=(i == 0)))
            tbl.setItem(i, 8, _ri(f"{s['debt_alloc']:.0f} / {s['equity_alloc']:.0f}"))

            if i == 0:
                for col in range(1, 9):
                    if tbl.item(i, col):
                        tbl.item(i, col).setBackground(QColor("#e8f8e8"))

        layout.addWidget(tbl)

        formula_lbl = QLabel(
            "<small>Score formula:  return × combined_ratio × √(sharpe / std_dev)  "
            "— higher is better</small>"
        )
        formula_lbl.setStyleSheet("color:#666;")
        layout.addWidget(formula_lbl)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        layout.addWidget(btns)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        selected_rank = rb_group.checkedId()
        if selected_rank < 0:
            selected_rank = 0
        self.state.selected_robustness_rank = selected_rank

        result = self.state.apply_ranked_chunk(selected_rank)
        if result:
            _log.debug(f"  apply_ranked_chunk({selected_rank}) result: {result['label']}")
            self._update_fd_rate_label()
            self._update_status()
            QMessageBox.information(
                self, "Applied",
                f"Rank #{selected_rank + 1} applied: {result['label']}\n"
                f"Portfolio: Return={result['port_return']:.2f}%  "
                f"Std Dev={result['port_std']:.2f}%  "
                f"Score={result['score']:.3f}"
            )
            self.statusBar().showMessage(
                f"Rank #{selected_rank + 1} applied: {result['label']} "
                f"(score={result['score']:.3f})"
            )

    # ── File operations ────────────────────────────────────────────────────────
    def _new_project(self):
        if QMessageBox.question(
                self, "New Project", "Discard current data?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) == QMessageBox.StandardButton.Yes:
            import copy as _copy
            _s0 = default_state()
            self.scenarios = [_s0, _copy.deepcopy(_s0),
                              _copy.deepcopy(_s0), _copy.deepcopy(_s0)]
            self._scenario_results = [None, None, None, None]
            self._scenario_yearly  = [None, None, None, None]
            self._scenario_sensitivity = [None, None, None, None]
            self._scenario_mc = [None, None, None, None]
            # Sync all per-scenario widgets to defaults
            for si in range(4):
                sw = self._swidgets[si]
                sw['rb_mode_a'].setChecked(True)
                sw['spin_spread'].setValue(4)
                sw['chk_tax_optimise'].setChecked(False)
                sw['btn_mode'].setChecked(False)
            self._update_active_tab_aliases()
            self._update_fd_rate_label()
            self._update_status()

    def _open_project(self):
        import copy as _copy
        start_dir = str(self.output_dir)
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Project", start_dir,
            "SWP Project (*.swp.json);;All Files (*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # ── Multi-scenario format ─────────────────────────────────────
            if "scenarios" in data and isinstance(data["scenarios"], list):
                loaded = data["scenarios"]
                for i in range(4):
                    if i < len(loaded) and loaded[i]:
                        self.scenarios[i] = AppState.from_dict(loaded[i])
                    else:
                        self.scenarios[i] = _copy.deepcopy(self.scenarios[0])
                active = data.get("active_scenario", 0)
                self.outer_tabs.setCurrentIndex(active)
            else:
                # ── Legacy single-scenario format (backward compat) ───────
                s0 = AppState.from_dict(data)
                self.scenarios[0] = s0
                for i in range(1, 4):
                    self.scenarios[i] = _copy.deepcopy(s0)
                self.outer_tabs.setCurrentIndex(0)
            # Sync per-scenario widgets
            for si in range(4):
                sw = self._swidgets[si]
                s = self.scenarios[si]
                if s.allocation_mode == "singular":
                    sw['rb_mode_a'].setChecked(True)
                else:
                    sw['rb_mode_b'].setChecked(True)
                sw['spin_spread'].setValue(s.rebalance_spread_years)
            # Reset results
            self._scenario_results = [None, None, None, None]
            self._scenario_yearly  = [None, None, None, None]
            self._scenario_sensitivity = [None, None, None, None]
            self._scenario_mc = [None, None, None, None]
            # Update output_dir to the folder containing the project file
            project_dir = Path(path).parent
            if project_dir.exists():
                self.output_dir = project_dir
            self._update_active_tab_aliases()
            self._update_fd_rate_label()
            inv = self.state.investment_date
            self.date_edit.setDate(QDate(inv.year, inv.month, inv.day))
            self._update_status()
            self.statusBar().showMessage(f"Loaded: {path}")
        except Exception as e:
            QMessageBox.critical(self, "Load Error", str(e))

    def _save_project(self):
        # Default filename in the user's output directory
        default_path = str(self.output_dir / f"{self.user_name}_project.swp.json")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Project", default_path,
            "SWP Project (*.swp.json);;All Files (*)")
        if not path:
            return
        try:
            payload = {
                "scenarios": [s.to_dict() for s in self.scenarios],
                "active_scenario": self._active_scenario_idx,
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            self.statusBar().showMessage(f"Saved: {path}")
        except Exception as e:
            QMessageBox.critical(self, "Save Error", str(e))

    def _save_all_csvs(self):
        # Check if ANY scenario has results
        has_any = any(r is not None for r in self._scenario_results)
        if not has_any:
            QMessageBox.warning(self, "No Data", "Run calculations first.")
            return

        # Ask for prefix (pre-filled with user name)
        dlg = PrefixDialog(self, default_prefix=self.user_name)
        if not dlg.exec():
            return
        prefix = dlg.prefix()

        # Default to user's output directory (user can still change it)
        folder = QFileDialog.getExistingDirectory(
            self, "Choose folder to save CSVs", str(self.output_dir))
        if not folder:
            return
        folder = Path(folder)

        try:
            all_files = []
            for si in range(4):
                results = self._scenario_results[si]
                if results is None:
                    continue

                opt_label = f"Opt{si+1}"
                p_monthly, p_yearly, h_monthly, _ = results

                files = []

                # 1. Personal Monthly
                p = folder / f"{prefix}_{opt_label}_Personal_Monthly.csv"
                _write_csv(p, PERSONAL_MONTHLY_COLS, _personal_monthly_rows(p_monthly))
                files.append(p.name)

                # 2. Personal Annual Summary
                p = folder / f"{prefix}_{opt_label}_Personal_Annual_Summary.csv"
                _write_csv(p, YEARLY_COLS, _yearly_rows(p_yearly))
                files.append(p.name)

                # 3. HUF Monthly
                p = folder / f"{prefix}_{opt_label}_HUF_Monthly.csv"
                _write_csv(p, HUF_MONTHLY_COLS, _huf_monthly_rows(h_monthly))
                files.append(p.name)

                # 4. HUF Annual Summary
                p = folder / f"{prefix}_{opt_label}_HUF_Annual_Summary.csv"
                _write_csv(p, YEARLY_COLS, _yearly_rows(p_yearly))
                files.append(p.name)

                # 5. Sensitivity (if available)
                sens = self._scenario_sensitivity[si]
                if sens:
                    headers, rows = _sensitivity_rows(sens)
                    p = folder / f"{prefix}_{opt_label}_Sensitivity.csv"
                    _write_csv(p, headers, rows)
                    files.append(p.name)

                all_files.extend(files)

            QMessageBox.information(self, "Saved",
                "Files saved to:\n" + str(folder) + "\n\n" + "\n".join(all_files))
        except Exception as e:
            QMessageBox.critical(self, "Export Error", str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# DATA MENU DIALOG 1: Fetch Fund Metrics
# ═══════════════════════════════════════════════════════════════════════════════

class _MCWorkerSignals(QObject):
    """Signals for the Monte Carlo background worker."""
    stage    = Signal(str)   # short progress message to display
    finished = Signal(object)  # MCResults on success
    error    = Signal(str)     # traceback string on failure


class _MCWorker(QThread):
    """
    Runs run_monte_carlo() in a background thread so the UI stays responsive.

    Emits:
      signals.stage(msg)     – human-readable progress stage updates
      signals.finished(res)  – MCResults when complete
      signals.error(tb)      – traceback string if an exception occurs
    """

    def __init__(self, state, kwargs: dict, signals: _MCWorkerSignals):
        super().__init__()
        self._state   = state
        self._kwargs  = kwargs
        self._signals = signals

    def run(self):
        try:
            from monte_carlo import (
                run_monte_carlo,
                _fetch_nifty50_annual_returns,
                _fetch_debt_index_annual_returns,
            )
            use_bootstrap = self._kwargs.get("use_bootstrap", True)

            if use_bootstrap:
                self._signals.stage.emit("Fetching Nifty 50 NAV history…")
                eq_hist = _fetch_nifty50_annual_returns()

                self._signals.stage.emit("Loading Debt Index data…")
                dbt_hist = _fetch_debt_index_annual_returns()

                if eq_hist is not None and dbt_hist is not None:
                    self._signals.stage.emit(
                        f"Running {self._kwargs['n_sims']:,} bootstrap simulations…")
                else:
                    self._signals.stage.emit(
                        f"Running {self._kwargs['n_sims']:,} log-normal simulations "
                        f"(data fetch failed)…")
            else:
                self._signals.stage.emit(
                    f"Running {self._kwargs['n_sims']:,} log-normal simulations…")

            self._signals.stage.emit("Computing 30-year corpus paths…")
            res = run_monte_carlo(self._state, **self._kwargs)

            self._signals.stage.emit("Calculating percentiles & ruin statistics…")
            self._signals.finished.emit(res)

        except Exception:
            import traceback
            self._signals.error.emit(traceback.format_exc())


class _WorkerSignals(QObject):
    finished  = Signal(int, str)   # returncode, stderr text
    log_line  = Signal(str)


class _SubprocessWorker(QThread):
    """Runs a subprocess in a background thread, emitting log lines as they arrive."""

    def __init__(self, cmd: list, signals: _WorkerSignals):
        super().__init__()
        self._cmd     = cmd
        self._signals = signals

    def run(self):
        import subprocess, sys
        try:
            proc = subprocess.Popen(
                self._cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                # Do NOT use text=True without encoding= on Windows — it falls
                # back to the system locale (charmap/cp1252) which can't decode
                # UTF-8 output from the child process.  Read raw bytes instead
                # and decode explicitly.
                text=False,
                bufsize=0,
            )
            for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip()
                self._signals.log_line.emit(line)
            proc.wait()
            self._signals.finished.emit(proc.returncode, "")
        except Exception as exc:
            self._signals.finished.emit(-1, str(exc))


class FetchSchemeNamesDialog(QDialog):
    """
    Dialog for  Data → Fetch Scheme and Fund Names.

    Two modes, selected by the Min AUM spinbox:

    • Min AUM = 0  →  fetch all funds from AMFI NAVAll.txt, write mutual_funds.csv.
                      Fast — single HTTP request, no per-fund calls.

    • Min AUM > 0  →  Step 1: download NAVAll.txt (all funds).
                      Step 2: query AMFI fund-performance API (POST) for every
                              category × subcategory combination to get dailyAUM
                              for each fund.  Makes ~40 API calls total, taking
                              roughly 20–40 seconds.
                      Step 3: match fund names, keep those ≥ min AUM, write
                              mutual_funds_min<N>cr.csv.
                      Both the all-funds CSV and the filtered CSV are produced.

    Requires get_amfi_fund_schemes_names.py AND fetch_amfi_aum.py to be in the
    same directory as main.py.
    """

    _SUBDIR = "Schemes_and_Funds"

    def __init__(self, output_dir: Path, parent=None):
        super().__init__(parent)
        self.output_dir = output_dir
        self._worker    = None
        self._out_path  = output_dir / self._SUBDIR / "mutual_funds.csv"

        self.setWindowTitle("Data → Fetch Scheme and Fund Names")
        self.resize(760, 580)

        layout = QVBoxLayout(self)

        # ── Description ───────────────────────────────────────────────────────
        desc = QLabel(
            "Downloads <b>AMFI NAVAll.txt</b> and extracts a de-duplicated fund list.<br>"
            "With <b>Min AUM &gt; 0</b>, queries the AMFI fund-performance API "
            "(~40 calls, ~30 sec) to get <b>dailyAUM</b> per fund and filters accordingly.<br>"
            "<b>AUM Date</b> must be a past business day — not today, not a market holiday."
        )
        desc.setTextFormat(Qt.TextFormat.RichText)
        desc.setWordWrap(True)
        layout.addWidget(desc)

        layout.addSpacing(6)

        # ── Controls grid ─────────────────────────────────────────────────────
        grid = QGridLayout()
        grid.setColumnStretch(2, 1)

        # Row 0: Min AUM
        grid.addWidget(QLabel("<b>Min AUM (₹ Cr):</b>"), 0, 0)
        self.spin_aum = QSpinBox()
        self.spin_aum.setRange(0, 10_000)
        self.spin_aum.setValue(1_000)
        self.spin_aum.setSingleStep(100)
        self.spin_aum.setFixedWidth(90)
        self.spin_aum.setToolTip(
            "0 = no filter (write all funds to mutual_funds.csv).\n"
            "1–10000 = only keep funds with AUM ≥ this value.\n"
            "AUM is fetched from the AMFI fund-performance API (~30 sec)."
        )
        self.spin_aum.valueChanged.connect(self._on_controls_changed)
        grid.addWidget(self.spin_aum, 0, 1)

        self.lbl_aum_note = QLabel("")
        self.lbl_aum_note.setStyleSheet("color:#888; font-size:10px;")
        grid.addWidget(self.lbl_aum_note, 0, 2)

        # Row 1: AUM Date (only relevant when min_aum > 0)
        self.lbl_date_label = QLabel("<b>AUM Date:</b>")
        grid.addWidget(self.lbl_date_label, 1, 0)
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("dd-MMM-yyyy")
        # Default: 7 days ago
        from datetime import date as _date, timedelta as _td
        default_date = _date.today() - _td(days=7)
        self.date_edit.setDate(
            QDate(default_date.year, default_date.month, default_date.day)
        )
        self.date_edit.setMaximumDate(QDate.currentDate().addDays(-1))
        self.date_edit.setFixedWidth(130)
        self.date_edit.setToolTip(
            "Date for AUM data — must be a past business day.\n"
            "Weekends and market holidays will return no data; try the nearest weekday."
        )
        grid.addWidget(self.date_edit, 1, 1)

        lbl_date_hint = QLabel("Must be a past business day (not a holiday)")
        lbl_date_hint.setStyleSheet("color:#888; font-size:10px;")
        grid.addWidget(lbl_date_hint, 1, 2)

        layout.addLayout(grid)

        # ── Output path label ─────────────────────────────────────────────────
        self.lbl_out = QLabel("")
        self.lbl_out.setStyleSheet("color:#2980b9; font-size:10px;")
        self.lbl_out.setWordWrap(True)
        layout.addWidget(self.lbl_out)

        layout.addSpacing(4)

        # ── Log area ──────────────────────────────────────────────────────────
        layout.addWidget(QLabel("Log:"))
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setFont(QFont("Courier", 9))
        self.log_area.setStyleSheet("background:#1e1e1e;color:#dcdcdc;")
        layout.addWidget(self.log_area)

        # ── Button row ────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()

        self.btn_run = QPushButton("▶  Fetch Now")
        self.btn_run.setStyleSheet(
            "background:#2ecc71;color:white;font-weight:bold;"
            "padding:6px 20px;border-radius:4px;")
        self.btn_run.clicked.connect(self._run)
        btn_row.addWidget(self.btn_run)

        self.btn_abort = QPushButton("⏹  Abort")
        self.btn_abort.setEnabled(False)
        self.btn_abort.setStyleSheet(
            "background:#e74c3c;color:white;font-weight:bold;"
            "padding:6px 16px;border-radius:4px;")
        self.btn_abort.setToolTip("Terminate the running fetch.")
        self.btn_abort.clicked.connect(self._abort)
        btn_row.addWidget(self.btn_abort)

        self.btn_open = QPushButton("📂  Open CSV")
        self.btn_open.setEnabled(False)
        self.btn_open.setToolTip("Open the output CSV in the default application.")
        self.btn_open.clicked.connect(self._open_csv)
        btn_row.addWidget(self.btn_open)

        btn_row.addStretch()
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

        # Initialise labels
        self._on_controls_changed()
        self._note_existing()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _on_controls_changed(self, *_):
        value  = self.spin_aum.value()
        subdir = self.output_dir / self._SUBDIR
        aum_mode = value > 0

        # Show/hide date row
        self.lbl_date_label.setVisible(aum_mode)
        self.date_edit.setVisible(aum_mode)

        if not aum_mode:
            self._out_path = subdir / "mutual_funds.csv"
            self.lbl_aum_note.setText("(no filter — all funds saved, fast)")
        else:
            self._out_path = subdir / f"mutual_funds_min{value}cr.csv"
            self.lbl_aum_note.setText(
                "AMFI API queried for AUM — ~40 calls, ~30 sec"
            )
        self.lbl_out.setText(f"Output → {self._out_path}")

    def _note_existing(self):
        subdir = self.output_dir / self._SUBDIR
        existing = []
        if (subdir / "mutual_funds.csv").exists():
            existing.append(f"  {subdir / 'mutual_funds.csv'}")
        if subdir.exists():
            for p in sorted(subdir.glob("mutual_funds_min*cr.csv")):
                existing.append(f"  {p}")
        if existing:
            self._log("ℹ  Existing file(s):\n" + "\n".join(existing)
                      + "\n  Click 'Fetch Now' to refresh.\n")

    def _log(self, text: str):
        self.log_area.append(text)
        self.log_area.verticalScrollBar().setValue(
            self.log_area.verticalScrollBar().maximum()
        )

    def _open_csv(self):
        import os, subprocess as _sp
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(self._out_path))
            elif sys.platform == "darwin":
                _sp.Popen(["open", str(self._out_path)])
            else:
                _sp.Popen(["xdg-open", str(self._out_path)])
        except Exception as exc:
            QMessageBox.warning(self, "Cannot Open", str(exc))

    def _abort(self):
        if self._worker and self._worker.isRunning():
            self._worker.terminate()
            self._worker.wait(3000)
            self._log("\n⏹  Aborted.")
            self.btn_abort.setEnabled(False)

    def closeEvent(self, event):
        self._abort()
        super().closeEvent(event)

    def reject(self):
        self._abort()
        super().reject()

    # ── run ───────────────────────────────────────────────────────────────────

    def _run(self):
        import sys as _sys
        script = Path(_sys.argv[0]).parent / "get_amfi_fund_schemes_names.py"
        if not script.exists():
            script = Path(__file__).parent / "get_amfi_fund_schemes_names.py"
        if not script.exists():
            QMessageBox.critical(
                self, "Script Not Found",
                "get_amfi_fund_schemes_names.py not found alongside main.py.\n"
                f"Looked in: {script.parent}"
            )
            return

        min_aum = self.spin_aum.value()
        cmd = [_sys.executable, str(script),
               "--output-dir", str(self.output_dir)]
        if min_aum > 0:
            cmd += ["--min-aum", str(min_aum)]
            # Date in DD-Mon-YYYY format (e.g. "17-Feb-2026")
            qd = self.date_edit.date()
            date_str = qd.toString("dd-MMM-yyyy")
            cmd += ["--date", date_str]

        self._log(f"\n$ {' '.join(cmd)}\n")
        self.btn_run.setEnabled(False)
        self.btn_open.setEnabled(False)
        self.btn_abort.setEnabled(True)

        signals = _WorkerSignals()
        signals.log_line.connect(self._log)
        signals.finished.connect(self._on_finished)
        self._worker = _SubprocessWorker(cmd, signals)
        self._worker.start()

    def _on_finished(self, returncode: int, err: str):
        self.btn_run.setEnabled(True)
        self.btn_abort.setEnabled(False)
        min_aum = self.spin_aum.value()

        if returncode == 0 and self._out_path.exists():
            self._log(f"\n✓ Done.  Output: {self._out_path}")
            self.btn_open.setEnabled(True)
            all_csv = self.output_dir / self._SUBDIR / "mutual_funds.csv"
            if min_aum > 0:
                msg = (f"AUM-filtered fund list saved to:\n{self._out_path}\n\n"
                       f"All-funds list also at:\n{all_csv}\n\n"
                       "Use the filtered CSV as input for Fetch Fund Metrics.")
            else:
                msg = (f"All fund names saved to:\n{self._out_path}\n\n"
                       "Use this file as input for Fetch Fund Metrics.")
            QMessageBox.information(self, "Complete", msg)
        else:
            msg = err or f"Process exited with code {returncode}"
            self._log(f"\n✗ Error: {msg}")
            QMessageBox.critical(self, "Fetch Error", msg)


class FetchFundMetricsDialog(QDialog):
    """
    Dialog for  Data → Fetch Fund Metrics.

    1. User picks an input CSV (must contain at least Fund Type and Fund Name columns).
    2. Output is written to  <output_dir>/Fund_Metrics_Output.csv.
    3. get_funds_data.py is invoked as a subprocess; live log is shown in a text area.
    4. Optional: number of worker threads (default 4).
    """

    def __init__(self, output_dir: Path, parent=None):
        super().__init__(parent)
        self.output_dir = output_dir
        self.setWindowTitle("Data → Fetch Fund Metrics")
        self.resize(700, 560)

        layout = QVBoxLayout(self)

        # ── Input file row ────────────────────────────────────────────────────
        layout.addWidget(QLabel(
            "Select the input CSV file listing funds to analyse.\n"
            "Required columns: <b>Fund Type</b>, <b>Fund Name</b>  "
            "(optional: Fund Size, AMFI Code).",
        ))
        row1 = QHBoxLayout()
        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("Path to input CSV…")
        row1.addWidget(self.input_edit)
        btn_browse = QPushButton("Browse…")
        btn_browse.clicked.connect(self._browse_input)
        row1.addWidget(btn_browse)
        layout.addLayout(row1)

        # ── Workers spinner ───────────────────────────────────────────────────
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Parallel workers:"))
        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, 16)
        self.workers_spin.setValue(4)
        row2.addWidget(self.workers_spin)
        row2.addStretch()
        lbl_out = QLabel(
            f"Output: {self.output_dir / 'Fund_Metrics_Output.csv'}"
        )
        lbl_out.setStyleSheet("color:#555;font-size:10px;")
        row2.addWidget(lbl_out)
        layout.addLayout(row2)

        # ── Log area ──────────────────────────────────────────────────────────
        layout.addWidget(QLabel("Log:"))
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setFont(QFont("Courier", 9))
        self.log_area.setStyleSheet("background:#1e1e1e;color:#dcdcdc;")
        layout.addWidget(self.log_area)

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        self.btn_run = QPushButton("▶  Run")
        self.btn_run.setStyleSheet(
            "background:#2ecc71;color:white;font-weight:bold;"
            "padding:6px 20px;border-radius:4px;")
        self.btn_run.clicked.connect(self._run)
        btn_row.addWidget(self.btn_run)
        btn_row.addStretch()
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

        self._worker = None

    def _browse_input(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Fund Input CSV", "",
            "CSV files (*.csv);;All Files (*)")
        if path:
            self.input_edit.setText(path)
            self._validate_input(path)

    def _validate_input(self, path: str) -> bool:
        """Check that the file has the required columns; show warning if not."""
        try:
            import csv as _csv
            with open(path, newline="", encoding="utf-8-sig") as f:
                reader = _csv.reader(f)
                headers = [h.strip() for h in next(reader)]
            required = {"Fund Type", "Fund Name"}
            missing  = required - set(headers)
            if missing:
                QMessageBox.warning(
                    self, "Missing Columns",
                    f"The selected file is missing required column(s):\n"
                    f"  {', '.join(sorted(missing))}\n\n"
                    f"Found columns: {', '.join(headers)}"
                )
                return False
            self._log(f"✓ Input validated: {len(headers)} columns found.")
            return True
        except Exception as exc:
            QMessageBox.warning(self, "Validation Error", str(exc))
            return False

    def _log(self, text: str):
        self.log_area.append(text)
        sb = self.log_area.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _run(self):
        input_path = self.input_edit.text().strip()
        if not input_path:
            QMessageBox.warning(self, "No Input", "Please select an input CSV file.")
            return
        if not Path(input_path).exists():
            QMessageBox.warning(self, "Not Found",
                                f"File not found:\n{input_path}")
            return
        if not self._validate_input(input_path):
            return

        output_path = str(self.output_dir / "Fund_Metrics_Output.csv")
        error_path  = str(self.output_dir / "Fund_Metrics_Errors.csv")

        # Build command: invoke get_funds_data.py via the same Python interpreter
        import sys as _sys
        script = Path(_sys.argv[0]).parent / "get_funds_data.py"
        if not script.exists():
            # Try alongside the running script
            script = Path(__file__).parent / "get_funds_data.py"
        cmd = [
            _sys.executable, str(script),
            "--input",   input_path,
            "--output",  output_path,
            "--workers", str(self.workers_spin.value()),
        ]
        self._log(f"\n$ {' '.join(cmd)}\n")
        self.btn_run.setEnabled(False)

        signals = _WorkerSignals()
        signals.log_line.connect(self._log)
        signals.finished.connect(self._on_finished)
        self._worker = _SubprocessWorker(cmd, signals)
        self._worker.start()

    def _on_finished(self, returncode: int, err: str):
        self.btn_run.setEnabled(True)
        if returncode == 0:
            out = self.output_dir / "Fund_Metrics_Output.csv"
            self._log(f"\n✓ Done. Output saved to:\n  {out}")
            QMessageBox.information(
                self, "Complete",
                f"Fund metrics saved to:\n{out}"
            )
        else:
            msg = err or f"Process exited with code {returncode}"
            self._log(f"\n✗ Error: {msg}")
            QMessageBox.critical(self, "Error", msg)

    def _stop_worker(self):
        if self._worker and self._worker.isRunning():
            self._worker.terminate()
            self._worker.wait(3000)
            self._worker = None

    def closeEvent(self, event):
        self._stop_worker()
        super().closeEvent(event)

    def reject(self):
        self._stop_worker()
        super().reject()


# ═══════════════════════════════════════════════════════════════════════════════
# DATA MENU DIALOG 2: Allocate Capital
# ═══════════════════════════════════════════════════════════════════════════════

class AllocateCapitalDialog(QDialog):
    """
    Dialog for  Data → Allocate Capital  (multi-chunk version).

    Two modes:
      - "Coarse" (default): user specifies min_return, max/fund, min/fund, max/type.
        Objective: minimise portfolio variance + drawdown while achieving >= min_return.
      - "Fine": user specifies all 9 parameters (std_dev, max_dd, per-fund filters, etc.).
        Objective: maximise return subject to all constraints (original behaviour).
    """

    _DEFAULT_CHUNK = staticmethod(lambda yf, yt: {
        "year_from":    yf,
        "year_to":      yt,
        "min_return":   6.85,
        "max_std_dev":  0.97,
        "max_dd":       0.75,
        "max_per_fund": 8.0,
        "max_per_type": 24.0,
        "min_per_fund": 2.0,
        "min_history":  7,
        "max_fund_std": 1.5,
        "max_fund_dd":  1.5,
        "max_per_amc":  16.0,
    })

    # Columns shown in FINE mode (all parameters)
    _FINE_COLUMNS = [
        ("min_return",   "Min Ret%",   "float", 0.1,  30.0,  2),
        ("max_std_dev",  "Max Std%",   "float", 0.1,  10.0,  2),
        ("max_dd",       "Max DD%",    "float", 0.1,  10.0,  2),
        ("max_per_fund", "Max/Fund%",  "float", 1.0, 100.0,  1),
        ("max_per_amc",  "Max/AMC%",   "float", 1.0, 100.0,  1),
        ("max_per_type", "Max/Type%",  "float", 1.0, 100.0,  1),
        ("min_per_fund", "Min/Fund%",  "float", 0.0,  50.0,  1),
        ("min_history",  "Min Hist Y", "int",   1,    30,    0),
        ("max_fund_std", "FStd%≤",     "float", 0.0,  10.0,  2),
        ("max_fund_dd",  "FDD%≤",      "float", 0.0,  10.0,  2),
    ]

    # Columns shown in COARSE mode
    _COARSE_COLUMNS = [
        ("min_return",   "Min Ret%",   "float", 0.1,  30.0,  2),
        ("max_per_fund", "Max/Fund%",  "float", 1.0, 100.0,  1),
        ("min_per_fund", "Min/Fund%",  "float", 0.0,  50.0,  1),
        ("max_per_type", "Max/Type%",  "float", 1.0, 100.0,  1),
        ("max_per_amc",  "Max/AMC%",   "float", 1.0, 100.0,  1),
        ("min_history",  "Min Hist Y", "int",   1,    30,    0),
    ]

    # Default values when switching to coarse mode (slightly different defaults)
    _COARSE_DEFAULT_CHUNK = staticmethod(lambda yf, yt: {
        "year_from":    yf,
        "year_to":      yt,
        "min_return":   6.85,
        "max_std_dev":  0.97,     # kept for JSON compat even though not shown
        "max_dd":       0.75,     # kept for JSON compat
        "max_per_fund": 8.0,
        "max_per_type": 24.0,
        "min_per_fund": 2.0,
        "min_history":  7,
        "max_fund_std": 1.5,      # kept for JSON compat
        "max_fund_dd":  1.5,      # kept for JSON compat
        "max_per_amc":  16.0,
    })

    def __init__(self, metrics_path: Path, output_dir: Path, state,
                 parent=None, all_scenarios=None, active_scenario_idx=0):
        super().__init__(parent)
        self.metrics_path = metrics_path
        self.output_dir   = output_dir
        self.state        = state
        self._all_scenarios = all_scenarios or []
        self._active_idx    = active_scenario_idx
        self.setWindowTitle(f"Data → Allocate Capital (Option {active_scenario_idx+1})")
        self.resize(1100, 740)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(
            f"<b>Input:</b> {metrics_path}<br>"
            f"<b>Outputs:</b> {output_dir}/allocation_chunk_N_yrX-Y.csv"
            f"  +  allocation_summary.csv"
        ))

        # ── Mode radio buttons ────────────────────────────────────────────────
        from PySide6.QtWidgets import QRadioButton, QButtonGroup
        mode_box = QGroupBox("Optimisation mode")
        mode_lay = QHBoxLayout(mode_box)
        self._radio_coarse = QRadioButton(
            "Coarser parameters  (minimise risk ≥ target return)")
        self._radio_fine = QRadioButton(
            "Finer parameters  (maximise return with explicit std/dd limits)")
        self._radio_coarse.setChecked(True)   # default
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self._radio_coarse, 0)
        self._mode_group.addButton(self._radio_fine, 1)
        self._mode_group.idToggled.connect(self._on_mode_changed)
        mode_lay.addWidget(self._radio_coarse)
        mode_lay.addWidget(self._radio_fine)
        self._radio_coarse.setToolTip(
            "You only specify:\n"
            "  • Min expected return (%)\n"
            "  • Max allocation per fund (%)\n"
            "  • Min allocation per fund (%)\n"
            "  • Max allocation per AMFI sub-category (%)\n\n"
            "The optimizer will automatically minimise portfolio variance\n"
            "and drawdown while meeting your return target.\n"
            "Std dev and max drawdown limits are determined by the optimizer.")
        self._radio_fine.setToolTip(
            "You specify all parameters explicitly:\n"
            "  • Min return, Max std dev, Max drawdown\n"
            "  • Max/Min per fund, Max per type\n"
            "  • Min fund history, Per-fund std/dd filters\n\n"
            "The optimizer will maximise return subject to all constraints.")
        layout.addWidget(mode_box)

        # ── Top controls: corpus + commonality bonus ──────────────────────────
        top = QHBoxLayout()
        top.addWidget(QLabel("Total corpus (Rs lakhs):"))
        self.w_total = QDoubleSpinBox()
        self.w_total.setRange(1.0, 100000.0)
        self.w_total.setDecimals(1)
        self.w_total.setSingleStep(10.0)
        self.w_total.setValue(350.0)
        self.w_total.setFixedWidth(100)
        top.addWidget(self.w_total)

        top.addSpacing(30)
        top.addWidget(QLabel("Commonality bonus (%):"))
        self.w_bonus = QDoubleSpinBox()
        self.w_bonus.setRange(0.0, 2.0)
        self.w_bonus.setDecimals(2)
        self.w_bonus.setSingleStep(0.05)
        self.w_bonus.setValue(0.20)
        self.w_bonus.setToolTip(
            "Extra return credit (%) given to funds already selected in an\n"
            "earlier chunk. Higher = stronger preference for reusing funds\n"
            "across periods (lower switching cost). 0 = fully independent."
        )
        self.w_bonus.setFixedWidth(80)
        top.addWidget(self.w_bonus)

        top.addSpacing(30)
        from PySide6.QtWidgets import QCheckBox
        self.chk_frontier = QCheckBox("Frontier walk")
        self.chk_frontier.setToolTip(
            "Use progressive risk-floor approach instead of α-blending\n"
            "for candidate generation.  Produces more diverse portfolios\n"
            "by forcing each candidate to have strictly higher risk than\n"
            "the previous one."
        )
        self.chk_frontier.setChecked(True)
        top.addWidget(self.chk_frontier)

        # ── Risk reference radio buttons (only relevant when frontier walk on) ──
        from PySide6.QtWidgets import QRadioButton, QButtonGroup
        top.addSpacing(20)
        self._risk_ref_group = QButtonGroup(self)
        self.rb_portfolio = QRadioButton("3×std / 10×dd portfolio ref")
        self.rb_portfolio.setToolTip(
            "Cap per-fund std at 3× P0's portfolio weighted-avg std.\n"
            "Cap per-fund dd at 10× (loose safety net — std is the\n"
            "binding constraint, dd just prevents runaways)."
        )
        self.rb_pct75 = QRadioButton("2×std / 10×dd 75th pctl ref")
        self.rb_pct75.setToolTip(
            "Cap per-fund std at 2× the 75th percentile of P0's\n"
            "selected fund stds.  Cap per-fund dd at 10× (loose).\n"
            "Tighter std control than portfolio mode."
        )
        self._risk_ref_group.addButton(self.rb_portfolio, 0)
        self._risk_ref_group.addButton(self.rb_pct75, 1)
        self.rb_portfolio.setChecked(True)  # default
        top.addWidget(self.rb_portfolio)
        top.addWidget(self.rb_pct75)

        top.addStretch()
        layout.addLayout(top)

        # ── Chunk table ───────────────────────────────────────────────────────
        self._chunk_label = QLabel(
            "<b>Time periods:</b>  Each row is one allocation period.  "
            "Year From/To refer to years 1–30 of the SWP plan.  "
            "Use ➕ Add Chunk to split the horizon into periods with different targets."
        )
        layout.addWidget(self._chunk_label)

        # Placeholder for the chunk table — will be (re)built by _rebuild_chunk_table
        self._chunk_container = QVBoxLayout()
        layout.addLayout(self._chunk_container)
        self.chunk_table = None   # set by _rebuild_chunk_table

        # ── Coarse-mode history status label ─────────────────────────────────
        # Shows "{n} funds with minimum history of {h} years selected out of {total}"
        # Updated live whenever the chunk table changes (any min_history value edit).
        self._lbl_history_status = QLabel("")
        self._lbl_history_status.setStyleSheet(
            "color: #2c3e50; font-size: 11px; padding: 2px 0px;")
        layout.addWidget(self._lbl_history_status)

        # Build the initial chunk table for the default mode (coarse)
        self._rebuild_chunk_table("coarse")

        # ── Copy allocation params from another scenario ──────────────────────
        if len(self._all_scenarios) > 1:
            from PySide6.QtWidgets import QComboBox
            copy_row = QHBoxLayout()
            copy_row.addWidget(QLabel("Copy allocation parameters from:"))
            self._cmb_copy = QComboBox()
            self._cmb_copy.addItem("—")
            for j in range(len(self._all_scenarios)):
                if j != self._active_idx:
                    has_params = bool(
                        self._all_scenarios[j].allocation_params
                        and self._all_scenarios[j].allocation_params.get("chunks"))
                    label = f"Option {j+1}"
                    if has_params:
                        n = len(self._all_scenarios[j].allocation_params["chunks"])
                        label += f" ({n} chunks)"
                    else:
                        label += " (no params)"
                    self._cmb_copy.addItem(label)
            self._cmb_copy.setFixedWidth(200)
            self._cmb_copy.setToolTip(
                "Copy the allocation parameters (corpus, bonus, chunk table)\n"
                "from another option into this dialog.\n"
                "You can then modify them before running.")
            self._cmb_copy.currentIndexChanged.connect(self._on_copy_from)
            copy_row.addWidget(self._cmb_copy)
            copy_row.addStretch()
            layout.addLayout(copy_row)

        # ── Load saved params from state if available ─────────────────────────
        self._load_saved_params()

        # ── Log area ──────────────────────────────────────────────────────────
        layout.addWidget(QLabel("Log:"))
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setFont(QFont("Courier", 9))
        self.log_area.setStyleSheet("background:#1e1e1e;color:#dcdcdc;")
        self.log_area.setMinimumHeight(180)
        layout.addWidget(self.log_area)

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        self.btn_run = QPushButton("▶  Run Allocation")
        self.btn_run.setStyleSheet(
            "background:#2ecc71;color:white;font-weight:bold;"
            "padding:6px 20px;border-radius:4px;")
        self.btn_run.clicked.connect(self._run)
        btn_row.addWidget(self.btn_run)

        self.btn_apply_subs = QPushButton("⟳  Apply Substitutions")
        self.btn_apply_subs.setEnabled(False)
        self.btn_apply_subs.setToolTip(
            "Apply the substitution advisor's recommendations.\n"
            "Replaces outlier funds with lower-risk alternatives\n"
            "(subject to 10 bps / 0.1% return floor).")
        self.btn_apply_subs.setStyleSheet(
            "QPushButton{background:#e8f5e9;border:1px solid #66bb6a;"
            "padding:6px 14px;border-radius:4px;}"
            "QPushButton:hover{background:#c8e6c9;}"
            "QPushButton:disabled{background:#f5f5f5;color:#999;}")
        self.btn_apply_subs.clicked.connect(self._apply_substitutions)
        btn_row.addWidget(self.btn_apply_subs)

        btn_row.addStretch()
        self.btn_ok = QPushButton("Accept & Close")
        self.btn_ok.setEnabled(False)
        self.btn_ok.clicked.connect(self.accept)
        btn_row.addWidget(self.btn_ok)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)
        layout.addLayout(btn_row)

        self._worker       = None
        self._result_paths = []

    def _current_mode(self) -> str:
        return "coarse" if self._radio_coarse.isChecked() else "fine"

    def _on_mode_changed(self, button_id: int, checked: bool):
        if not checked:
            return
        mode = "coarse" if button_id == 0 else "fine"
        # Save current chunk data before rebuilding
        old_data = self.chunk_table.get_data() if self.chunk_table else []
        self._rebuild_chunk_table(mode, old_data)
        # Show/hide history status label based on mode
        self._lbl_history_status.setVisible(mode == "coarse")

    def _rebuild_chunk_table(self, mode: str, preserve_data: list = None):
        """Tear down and rebuild the chunk table for the given mode."""
        from chunk_editor import ChunkTableWidget

        # Remove old widget if present
        if self.chunk_table is not None:
            self._chunk_container.removeWidget(self.chunk_table)
            self.chunk_table.setParent(None)
            self.chunk_table.deleteLater()

        columns = self._COARSE_COLUMNS if mode == "coarse" else self._FINE_COLUMNS
        default_fn = self._COARSE_DEFAULT_CHUNK if mode == "coarse" else self._DEFAULT_CHUNK

        self.chunk_table = ChunkTableWidget(
            columns            = columns,
            make_default_chunk = default_fn,
            parent             = self,
        )
        self.chunk_table.setMinimumHeight(180)
        self._chunk_container.addWidget(self.chunk_table)

        # Connect live history-status update
        self.chunk_table.chunks_changed.connect(self._update_history_status)

        # Restore data — carry forward any values that exist in both column sets
        if preserve_data:
            # Merge old chunk data with new defaults so missing keys get defaults
            merged = []
            for old_chunk in preserve_data:
                new_c = default_fn(old_chunk.get("year_from", 1),
                                   old_chunk.get("year_to", 30))
                new_c.update({k: v for k, v in old_chunk.items()
                              if k in new_c})
                merged.append(new_c)
            self.chunk_table.set_data(merged)
        else:
            self.chunk_table.set_data([default_fn(1, 30)])

        # Trigger an initial status update
        self._update_history_status()

    def _update_history_status(self):
        """
        Recount eligible funds given the tightest min_history across all chunks
        and update the status label.  Reads the CSV lazily (cached after first load).
        """
        # Only show the label in coarse mode where min_history is a column
        if self._current_mode() != "coarse":
            self._lbl_history_status.setText("")
            return

        # Get the tightest (largest) min_history value across all chunks
        chunks_data = self.chunk_table.get_data() if self.chunk_table else []
        min_hist_years = max(
            (int(c.get("min_history", 7)) for c in chunks_data),
            default=7,
        )
        min_hist_months = min_hist_years * 12

        # Load the CSV and count — use a simple cache to avoid re-reading on every keystroke
        if not hasattr(self, "_hist_status_cache"):
            self._hist_status_cache = {}   # (path_str, months) -> (n_selected, n_total)

        cache_key = (str(self.metrics_path), min_hist_months)
        if cache_key not in self._hist_status_cache:
            try:
                import pandas as pd
                df = pd.read_csv(str(self.metrics_path))
                n_total = len(df)
                df["History_Months"] = pd.to_numeric(
                    df.get("History_Months", pd.Series(dtype=float)),
                    errors="coerce"
                ).fillna(0)
                n_selected = int((df["History_Months"] >= min_hist_months).sum())
                self._hist_status_cache[cache_key] = (n_selected, n_total)
            except Exception:
                self._hist_status_cache[cache_key] = (0, 0)

        n_sel, n_tot = self._hist_status_cache[cache_key]
        if n_tot > 0:
            self._lbl_history_status.setText(
                f"  📊  {n_sel} funds with minimum history of "
                f"{min_hist_years} year{'s' if min_hist_years != 1 else ''} "
                f"selected out of {n_tot} funds"
            )
        else:
            self._lbl_history_status.setText(
                f"  ⚠  Could not read fund count from metrics CSV"
            )

    def _load_saved_params(self):
        """Load allocation params from state.allocation_params if available."""
        params = getattr(self.state, 'allocation_params', None)
        if not params:
            return
        if "total_money" in params:
            self.w_total.setValue(params["total_money"])
        if "commonality_bonus" in params:
            self.w_bonus.setValue(params["commonality_bonus"] * 100.0)
        # Restore mode (default to "coarse" if not saved)
        saved_mode = params.get("mode", "coarse")
        if saved_mode == "fine":
            self._radio_fine.setChecked(True)
        else:
            self._radio_coarse.setChecked(True)
        if "chunks" in params and params["chunks"]:
            self._rebuild_chunk_table(saved_mode, params["chunks"])

    def _on_copy_from(self, combo_idx):
        """Copy allocation params from another scenario into this dialog."""
        if combo_idx == 0:
            return  # "—" selected
        other_indices = [j for j in range(len(self._all_scenarios))
                         if j != self._active_idx]
        source_idx = other_indices[combo_idx - 1]
        source = self._all_scenarios[source_idx]
        params = getattr(source, 'allocation_params', None)
        if not params or not params.get("chunks"):
            QMessageBox.information(
                self, "No Parameters",
                f"Option {source_idx+1} has no saved allocation parameters.\n"
                "Run Allocate Capital on that option first.")
            self._cmb_copy.setCurrentIndex(0)
            return
        # Apply the copied params
        if "total_money" in params:
            self.w_total.setValue(params["total_money"])
        if "commonality_bonus" in params:
            self.w_bonus.setValue(params["commonality_bonus"] * 100.0)
        # Restore mode
        copied_mode = params.get("mode", "coarse")
        if copied_mode == "fine":
            self._radio_fine.setChecked(True)
        else:
            self._radio_coarse.setChecked(True)
        if "chunks" in params:
            import copy as _copy
            self._rebuild_chunk_table(copied_mode, _copy.deepcopy(params["chunks"]))
        self._cmb_copy.setCurrentIndex(0)
        self._log(f"Copied allocation parameters from Option {source_idx+1}")

    def _log(self, text: str):
        self.log_area.append(text)
        sb = self.log_area.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _run(self):
        # Guard: don't start a second run while one is already in progress
        if self._worker and self._worker.isRunning():
            QMessageBox.warning(self, "Already Running",
                                "An allocation is still in progress.\n"
                                "Wait for it to finish or close and reopen the dialog.")
            return

        chunks = self.chunk_table.get_data()
        if not chunks:
            QMessageBox.warning(self, "No Chunks",
                                "Add at least one time-period chunk.")
            return
        for i, c in enumerate(chunks):
            if c.get("year_from", 0) > c.get("year_to", 0):
                QMessageBox.warning(self, "Invalid Chunk",
                    f"Chunk {i+1}: Year From > Year To.")
                return

        mode = self._current_mode()

        params = {
            "total_money":       self.w_total.value(),
            "commonality_bonus": round(self.w_bonus.value() / 100.0, 6),
            "chunks":            chunks,
            "mode":              mode,
        }

        # Carry forward optimizer method choice from current state
        # (set by the PuLP/Home-grown radio group)
        _cur = getattr(self.state, 'allocation_params', None) or {}
        if "pulp_commonality" in _cur:
            params["pulp_commonality"] = _cur["pulp_commonality"]

        # Persist to state so it survives project save/load
        import copy as _copy
        self.state.allocation_params = _copy.deepcopy(params)

        params_path = self.output_dir / "allocation_params.json"
        try:
            with open(params_path, "w", encoding="utf-8") as f:
                json.dump(params, f, indent=2)
            self._log(f"Params saved → {params_path}")
        except Exception as exc:
            QMessageBox.critical(self, "Save Error", str(exc))
            return

        # Write chunks to a temp JSON file (avoids Windows cmd-line length limits)
        chunks_file = str(self.output_dir / "_chunks_tmp.json")
        try:
            with open(chunks_file, "w", encoding="utf-8") as f:
                json.dump(chunks, f, indent=2)
        except Exception as exc:
            QMessageBox.critical(self, "Save Error", str(exc))
            return

        import sys as _sys
        script = Path(_sys.argv[0]).parent / "allocate_funds.py"
        if not script.exists():
            script = Path(__file__).parent / "allocate_funds.py"

        cmd = [
            _sys.executable, str(script),
            "--input",             str(self.metrics_path),
            "--output-dir",        str(self.output_dir),
            "--total",             str(self.w_total.value()),
            "--commonality-bonus", str(round(self.w_bonus.value() / 100.0, 6)),
            "--chunks-file",       chunks_file,
            "--mode",              mode,
        ]
        # Determine optimizer method from state (set by PuLP/Home-grown radio)
        _ap = getattr(self.state, 'allocation_params', None) or {}
        _use_pulp = _ap.get("pulp_commonality", True)  # default to PuLP

        if _use_pulp:
            cmd.append("--pulp-commonality")
            self._log(f"[OPT-DIAG] CLI: using --pulp-commonality")
        elif self.chk_frontier.isChecked():
            cmd.append("--frontier-walk")
            if self.rb_pct75.isChecked():
                cmd.extend(["--risk-ref", "pct75"])
            else:
                cmd.extend(["--risk-ref", "portfolio"])
            self._log(f"[OPT-DIAG] CLI: using --frontier-walk")
        else:
            self._log(f"[OPT-DIAG] CLI: using home-grown α-blending (no flag)")
        self._log(f"[OPT-DIAG] state.allocation_params={_ap!r}")
        self._log(f"\n$ {' '.join(cmd)}\n")
        self.btn_run.setEnabled(False)
        self.btn_ok.setEnabled(False)

        signals = _WorkerSignals()
        signals.log_line.connect(self._log)
        signals.finished.connect(self._on_finished)
        self._worker = _SubprocessWorker(cmd, signals)
        self._worker.start()

    def _on_finished(self, returncode: int, err: str):
        self.btn_run.setEnabled(True)
        summary_path = self.output_dir / "allocation_summary.csv"

        if returncode == 0 and summary_path.exists():
            self._log(f"\n✓ All chunks complete.  Summary → {summary_path}")

            chunks = self.chunk_table.get_data()
            self._result_paths = []
            for i, c in enumerate(chunks):
                p = (self.output_dir /
                     f"allocation_chunk_{i+1}"
                     f"_yr{c['year_from']}-{c['year_to']}.csv")
                if p.exists():
                    self._result_paths.append((c, p))

            if self._result_paths:
                # Build allocation_chunks in state from all chunk CSVs
                self._build_allocation_chunks()
                # Also load last chunk into flat state.funds for backward compat
                self._load_chunk_into_state(self._result_paths[-1][1])

                # Re-import fund metrics so FundEntry objects carry the full
                # score set (Sharpe, Sortino, Calmar, etc.) that the allocation
                # CSVs do not include.  Also normalises max_dd to fraction form.
                if self.metrics_path and self.metrics_path.exists():
                    try:
                        n = self.state.import_fund_metrics(str(self.metrics_path))
                        self._log(f"  Re-imported fund metrics for {n} funds.")
                    except Exception as exc:
                        self._log(f"  ⚠ Could not re-import fund metrics: {exc}")

            self.btn_ok.setEnabled(True)

            # Enable Apply Substitutions if viz HTML exists (contains sub_advice)
            viz_path = self.output_dir / "portfolio_viz.html"
            self.btn_apply_subs.setEnabled(viz_path.exists())

            # Launch portfolio visualization in browser
            if viz_path.exists():
                import webbrowser
                webbrowser.open(viz_path.as_uri())

            QMessageBox.information(
                self, "Complete",
                f"Allocation complete.\n\n"
                f"{len(self._result_paths)} chunk CSV(s) + summary saved to:\n"
                f"{self.output_dir}\n\n"
                f"state.allocation_chunks updated ({len(self._result_paths)} chunks).\n"
                f"state.funds loaded from chunk {len(self._result_paths)} "
                f"(most recent period).\n"
                f"Use File → Save to persist the full project."
            )
        else:
            msg = err or f"Process exited with code {returncode}"
            self._log(f"\n✗ Error: {msg}")
            QMessageBox.critical(self, "Allocation Error", msg)

    def _apply_substitutions(self):
        """
        Read sub_advice from the viz HTML, apply swaps to chunk CSVs,
        rebuild allocation_chunks, and regenerate the viz.
        """
        import json, re

        viz_path = self.output_dir / "portfolio_viz.html"
        if not viz_path.exists():
            QMessageBox.warning(self, "No Data",
                                "portfolio_viz.html not found.\n"
                                "Run allocation first.")
            return

        # Parse SUB array from the HTML
        html_text = viz_path.read_text(encoding="utf-8")
        m = re.search(r"var\s+SUB\s*=\s*(\[.*?\])\s*;", html_text, re.DOTALL)
        if not m:
            # Try const SUB = ...
            m = re.search(r"const\s+SUB\s*=\s*(\[.*?\])\s*;", html_text, re.DOTALL)
        if not m:
            QMessageBox.warning(self, "No Substitutions",
                                "No substitution data found in portfolio_viz.html.\n"
                                "The advisor may not have found any outliers.")
            return

        try:
            sub_data = json.loads(m.group(1))
        except json.JSONDecodeError as e:
            QMessageBox.warning(self, "Parse Error",
                                f"Failed to parse substitution data:\n{e}")
            return

        # Filter to chunks with active substitutions (not dropped)
        active = [s for s in sub_data
                  if s.get("has_outliers") and s.get("substitutions")
                  and any(sub.get("candidate") for sub in s["substitutions"])]
        if not active:
            QMessageBox.information(self, "No Substitutions",
                                   "No actionable substitutions found.\n"
                                   "All swaps may have been dropped due to\n"
                                   "the return floor constraint.")
            return

        # Build summary for confirmation dialog
        summary_lines = []
        for sa in active:
            subs = [s for s in sa["substitutions"] if s.get("candidate")]
            label = sa.get("chunk_label", f"Chunk {sa.get('chunk_num', '?')}")
            floor = sa.get("return_floor", "?")
            summary_lines.append(f"{label}  (return floor: {floor}%)")
            for s in subs:
                c = s["candidate"]
                summary_lines.append(f"  − {s['outlier_name'][:45]}")
                summary_lines.append(f"  + {c['name'][:45]}")
            if sa.get("after_swap"):
                a = sa["after_swap"]
                cur = sa["current"]
                summary_lines.append(
                    f"  Ret: {cur['ret']:.2f}% → {a['ret']:.2f}% | "
                    f"Std: {cur['std']:.3f}% → {a['std']:.3f}%")
            dropped = sa.get("dropped", [])
            if dropped:
                for d in dropped:
                    summary_lines.append(f"  ⚠ Kept: {d['outlier_name'][:40]} "
                                         f"({d.get('reason','floor')})")
            rb_list = sa.get("rebalances", [])
            if rb_list:
                for rb in rb_list:
                    summary_lines.append(
                        f"  ↻ Shift {rb['shift_pct']:.1f}%: "
                        f"{rb['from'][:30]} → {rb['to'][:30]}")
            summary_lines.append("")

        reply = QMessageBox.question(
            self, "Apply Substitutions?",
            "The following fund swaps will be applied:\n\n" +
            "\n".join(summary_lines) +
            "\nThis will modify the chunk CSVs and summary.\n"
            "Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self._log("\n" + "═" * 70)
        self._log("  APPLYING SUBSTITUTIONS (GUI)")
        self._log("═" * 70)

        import pandas as pd
        import numpy as np

        applied = 0
        for sa in active:
            chunk_num = sa.get("chunk_num")
            subs = [s for s in sa["substitutions"] if s.get("candidate")]
            if not subs:
                continue

            # Find the CSV path for this chunk
            csv_path = None
            if chunk_num and 1 <= chunk_num <= len(self._result_paths):
                _, csv_path = self._result_paths[chunk_num - 1]

            if not csv_path or not csv_path.exists():
                self._log(f"  ⚠ CSV not found for chunk {chunk_num}")
                continue

            df = pd.read_csv(csv_path, encoding="utf-8-sig")
            df = df[df["Fund Name"].notna()].copy()
            # Separate portfolio total row if present
            total_mask = df["Fund Type"].fillna("") == "PORTFOLIO TOTAL"
            df = df[~total_mask].copy()

            for s in subs:
                oname = s["outlier_name"]
                cand  = s["candidate"]

                # Remove outlier
                o_mask = df["Fund Name"].str.strip() == oname.strip()
                if not o_mask.any():
                    self._log(f"  ⚠ {oname} not found in CSV — skipping")
                    continue

                o_row = df[o_mask].iloc[0]
                o_alloc = float(o_row.get("Allocation_L", 0))
                o_wt    = float(o_row.get("Weight_Pct", 0))
                df = df[~o_mask]

                # Add candidate with same allocation
                new_row = {
                    "Fund Name":    cand["name"],
                    "Fund Type":    cand.get("type", ""),
                    "Allocation_L": o_alloc,
                    "Weight_Pct":   o_wt,
                    "Ret_Pct":      cand.get("ret", 0),
                    "Std_Pct":      cand.get("std", 0),
                    "DD_Pct":       cand.get("dd", 0),
                }
                # Carry forward per-fund metrics from substitution advisor
                # (enriched by allocate_funds._substitution_advisor)
                #
                # CSV convention (set by allocator's save_allocation):
                #   Worst_Exp_Ret_%, Std_Dev_used, Max_DD_used, *_CAGR → percentage
                #   Sharpe_*, Sortino_*, Calmar_*, Combined_Ratio_* → raw ratios
                #
                # Candidate dict values:
                #   ret/std/dd → already percentage (advisor did ×100)
                #   cagr_*     → fractions from df_full (need ×100)
                #   sharpe/sortino/calmar/combined_ratio → raw ratios (no scaling)
                _metric_map = {
                    "Worst_Exp_Ret_%": ("ret", 1.0),
                    "Std_Dev_used":    ("std", 1.0),
                    "Max_DD_used":     ("dd", -1.0),  # dd stored as negative
                    "Sharpe_5Y":       ("sharpe_5y", None),
                    "Sharpe_10Y":      ("sharpe_10y", None),
                    "Sortino_5Y":      ("sortino_5y", None),
                    "Sortino_10Y":     ("sortino_10y", None),
                    "Calmar_5Y":       ("calmar_5y", None),
                    "Calmar_10Y":      ("calmar_10y", None),
                    "Combined_Ratio_10Y": ("combined_ratio_10y", None),
                    "1Y_CAGR":         ("cagr_1y", 100.0),   # fraction → %
                    "3Y_CAGR":         ("cagr_3y", 100.0),   # fraction → %
                    "5Y_CAGR":         ("cagr_5y", 100.0),   # fraction → %
                    "10Y_CAGR":        ("cagr_10y", 100.0),  # fraction → %
                }
                for csv_col, (cand_key, scale) in _metric_map.items():
                    v = cand.get(cand_key)
                    if v is not None and scale is not None:
                        new_row[csv_col] = v * scale
                    elif v is not None:
                        new_row[csv_col] = v
                df = pd.concat([df, pd.DataFrame([new_row])],
                               ignore_index=True)
                self._log(f"  C{chunk_num}: − {oname[:45]}")
                self._log(f"           + {cand['name'][:45]} "
                          f"(₹{o_alloc:.1f}L)")

            # Rewrite CSV
            # Apply weight rebalances (shift weight between existing funds)
            rb_list = sa.get("rebalances", [])
            if rb_list:
                total_alloc = df["Allocation_L"].sum()
                for rb in rb_list:
                    frm = rb.get("from", "")
                    to_name = rb.get("to", "")
                    shift_pct = rb.get("shift_pct", 0)
                    shift_frac = shift_pct / 100.0

                    fm = df["Fund Name"].str.strip() == frm.strip()
                    tm = df["Fund Name"].str.strip() == to_name.strip()
                    if fm.any() and tm.any():
                        shift_alloc = shift_frac * total_alloc
                        df.loc[fm, "Allocation_L"] = (
                            df.loc[fm, "Allocation_L"].astype(float) - shift_alloc)
                        df.loc[fm, "Weight_Pct"] = (
                            df.loc[fm, "Weight_Pct"].astype(float) - shift_pct)
                        df.loc[tm, "Allocation_L"] = (
                            df.loc[tm, "Allocation_L"].astype(float) + shift_alloc)
                        df.loc[tm, "Weight_Pct"] = (
                            df.loc[tm, "Weight_Pct"].astype(float) + shift_pct)
                        self._log(f"  C{chunk_num}: ↻ shift {shift_pct:.1f}% "
                                  f"from {frm[:35]} → {to_name[:35]}")

            # ── Recompute PORTFOLIO TOTAL summary row ────────────────────────
            # The original summary row was stripped; rebuild it from fund data.
            total_alloc_final = df["Allocation_L"].astype(float).sum()
            if total_alloc_final > 0:
                w = df["Allocation_L"].astype(float).values / total_alloc_final

                def _wtd_col(col):
                    if col not in df.columns:
                        return None
                    vals = pd.to_numeric(df[col], errors="coerce").fillna(0).values
                    return float(np.dot(w, vals))

                wtd_ret = _wtd_col("Worst_Exp_Ret_%")
                wtd_std = _wtd_col("Std_Dev_used")
                wtd_dd  = _wtd_col("Max_DD_used")

                # Sharpe/Sortino: weighted average, prefer 5Y, fall back to 10Y
                def _wtd_ratio(col5, col10):
                    if col5 not in df.columns and col10 not in df.columns:
                        return None
                    s5  = pd.to_numeric(df.get(col5,  np.nan), errors="coerce")
                    s10 = pd.to_numeric(df.get(col10, np.nan), errors="coerce")
                    vals = s5.where(s5.notna(), s10).fillna(0).values
                    valid_mask = s5.notna() | s10.notna()
                    w_valid = w[valid_mask.values].sum()
                    return float(np.dot(w, vals) / w_valid) if w_valid > 1e-9 else None

                port_sharpe  = _wtd_ratio("Sharpe_5Y",  "Sharpe_10Y")
                port_sortino = _wtd_ratio("Sortino_5Y", "Sortino_10Y")
                port_calmar  = (wtd_ret / abs(wtd_dd)) if (wtd_dd and abs(wtd_dd) > 1e-9) else None

                summary_row = {c: "" for c in df.columns}
                summary_row.update({
                    "Fund Type":       "PORTFOLIO TOTAL",
                    "Allocation_L":    round(total_alloc_final, 2),
                })
                if wtd_ret is not None:
                    summary_row["Worst_Exp_Ret_%"] = round(wtd_ret, 4)
                if wtd_std is not None:
                    summary_row["Std_Dev_used"]    = round(wtd_std, 4)
                if wtd_dd is not None:
                    summary_row["Max_DD_used"]     = round(wtd_dd, 4)
                if port_sharpe is not None:
                    summary_row["Port_Sharpe"]     = round(port_sharpe, 4)
                if port_sortino is not None:
                    summary_row["Port_Sortino"]    = round(port_sortino, 4)
                if port_calmar is not None:
                    summary_row["Port_Calmar"]     = round(port_calmar, 4)
                df = pd.concat([df, pd.DataFrame([summary_row])],
                               ignore_index=True)

            df.to_csv(csv_path, index=False, encoding="utf-8-sig")
            self._log(f"  CSV updated: {csv_path.name}")
            applied += 1

        if applied:
            # Rebuild allocation_chunks from updated CSVs
            self._build_allocation_chunks()
            self._load_chunk_into_state(self._result_paths[-1][1])

            # Re-import fund metrics so the rebuilt FundEntry objects get the
            # full score set (Sharpe, Sortino, Calmar, Alpha, Beta, etc.).
            # The allocation CSVs only carry Worst_Exp_Ret_%, Std_Dev_used,
            # Max_DD_used and CAGRs; _build_allocation_chunks therefore leaves
            # many FundEntry fields at 0.  Re-importing from the metrics CSV
            # restores them and — critically — resets max_dd to the fraction
            # convention (e.g. −0.004) that _update_chunk_yield_label expects.
            if self.metrics_path and self.metrics_path.exists():
                try:
                    n = self.state.import_fund_metrics(str(self.metrics_path))
                    self._log(f"  Re-imported fund metrics for {n} funds.")
                except Exception as exc:
                    self._log(f"  ⚠ Could not re-import fund metrics: {exc}")

            self.btn_apply_subs.setEnabled(False)

            # Refresh the portfolio viz HTML so browser shows updated stats
            self._refresh_viz_html()

            self._log(f"\n  ✓ {applied} chunk(s) updated with substitutions.")
            self._log("    state.allocation_chunks refreshed.")

            QMessageBox.information(
                self, "Substitutions Applied",
                f"{applied} chunk(s) updated.\n\n"
                f"state.allocation_chunks refreshed.\n"
                f"You can re-run allocation or Accept & Close.")
        else:
            QMessageBox.information(self, "No Changes",
                                   "No substitutions were applied.")

    def _refresh_viz_html(self):
        """
        Recompute per-chunk portfolio stats from the current chunk CSVs
        and patch the ``const C = [...]`` block in portfolio_viz.html so the
        browser view reflects substitutions / optimization changes.
        """
        import json as _json, re
        import pandas as pd
        import numpy as np

        viz_path = self.output_dir / "portfolio_viz.html"
        if not viz_path.exists() or not self._result_paths:
            return

        chunks_data = []
        for idx, (chunk_def, csv_path) in enumerate(self._result_paths, 1):
            if not csv_path.exists():
                continue
            df = pd.read_csv(csv_path, encoding="utf-8-sig")
            df = df[df["Fund Name"].notna()].copy()

            # Separate and read PORTFOLIO TOTAL row for Sharpe/Sortino/Calmar
            total_mask = df["Fund Type"].fillna("") == "PORTFOLIO TOTAL"
            port_sharpe = port_sortino = port_calmar = 0.0
            if total_mask.any():
                srow = total_mask.idxmax()
                def _rv(col):
                    try:
                        return float(df.at[srow, col])
                    except (KeyError, ValueError, TypeError):
                        return 0.0
                port_sharpe  = _rv("Port_Sharpe")
                port_sortino = _rv("Port_Sortino")
                port_calmar  = _rv("Port_Calmar")

            df = df[~total_mask].copy()
            df = df[df["Allocation_L"].fillna(0).astype(float) > 0].copy()
            if df.empty:
                continue

            alloc = df["Allocation_L"].astype(float).values
            total_alloc = alloc.sum()
            if total_alloc <= 0:
                continue
            w = alloc / total_alloc

            def _col(col):
                if col not in df.columns:
                    return np.zeros(len(df))
                return pd.to_numeric(df[col], errors="coerce").fillna(0).values

            # Worst_Exp_Ret_%, Std_Dev_used, Max_DD_used are in percentage
            ret_vals = _col("Worst_Exp_Ret_%")
            std_vals = _col("Std_Dev_used")
            dd_vals  = np.abs(_col("Max_DD_used"))

            wtd_ret = float(np.dot(w, ret_vals))
            wtd_std = float(np.dot(w, std_vals))
            wtd_dd  = float(np.dot(w, dd_vals))

            funds_list = []
            for i, (_, row) in enumerate(df.iterrows()):
                funds_list.append({
                    "name": str(row["Fund Name"]).strip(),
                    "wt":   round(float(w[i]) * 100, 2),
                    "ret":  round(float(ret_vals[i]), 2),
                    "std":  round(float(std_vals[i]), 2),
                    "dd":   round(float(dd_vals[i]), 2),
                    "type": str(row.get("Fund Type", "")),
                })

            chunks_data.append({
                "label":        f"C{idx} (Yr {chunk_def['year_from']}\u2013{chunk_def['year_to']})",
                "target_ret":   chunk_def.get("min_return", 6.85),
                "achieved_ret": round(wtd_ret, 3),
                "wtd_std":      round(wtd_std, 3),
                "wtd_dd":       round(wtd_dd, 3),
                "calmar":       round(port_calmar, 2),
                "sharpe":       round(port_sharpe, 3),
                "sortino":      round(port_sortino, 3),
                "n_funds":      len(df),
                "funds":        funds_list,
            })

        if not chunks_data:
            return

        # Patch const C = [...]; in the HTML
        html_text = viz_path.read_text(encoding="utf-8")
        new_json = _json.dumps(chunks_data, indent=2)
        patched = re.sub(
            r"const\s+C\s*=\s*\[.*?\]\s*;",
            f"const C = {new_json};",
            html_text,
            count=1,
            flags=re.DOTALL,
        )
        if patched != html_text:
            viz_path.write_text(patched, encoding="utf-8")
            self._log("  Viz HTML updated with new chunk stats.")

    def _stop_worker(self):
        """Terminate the subprocess worker if it's still running."""
        if self._worker and self._worker.isRunning():
            self._worker.terminate()
            self._worker.wait(3000)   # wait up to 3 s for thread to finish
            self._worker = None

    def closeEvent(self, event):
        self._stop_worker()
        super().closeEvent(event)

    def reject(self):
        self._stop_worker()
        super().reject()

    def _build_allocation_chunks(self):
        """
        Read every chunk CSV and populate state.allocation_chunks.
        Also auto-populates state.return_chunks from each chunk's portfolio yield.
        """
        import os, logging
        log_path = os.environ.get("SWP_DEBUG_LOG")
        _log = logging.getLogger("main.debug")
        if log_path and not _log.handlers:
            _fh = logging.FileHandler(log_path, mode="a", encoding="utf-8")
            _fh.setFormatter(logging.Formatter("%(message)s"))
            _log.addHandler(_fh)
            _log.setLevel(logging.DEBUG)

        _log.debug("\n" + "=" * 70)
        _log.debug("_build_allocation_chunks CALLED")

        try:
            import pandas as pd
            from models import AllocationChunk, FundEntry, ReturnChunk

            new_alloc_chunks = []
            new_return_chunks = []

            for chunk_def, csv_path in self._result_paths:
                df = pd.read_csv(csv_path, encoding="utf-8-sig")
                if "Allocation_L" not in df.columns or "Fund Name" not in df.columns:
                    self._log(f"⚠  {csv_path.name}: missing columns, skipped.")
                    continue

                # Filter to real fund rows
                df = df[df["Fund Name"].notna()].copy()
                df = df[df["Fund Type"].fillna("") != "PORTFOLIO TOTAL"].copy()
                df = df[df["Allocation_L"].fillna(0) > 0].copy()

                col_type = "Fund Type" if "Fund Type" in df.columns else None

                def _fn(row, col):
                    return float(row[col]) if col in df.columns and pd.notna(row.get(col)) else None
                def _fb(row, *cols):
                    for c in cols:
                        v = _fn(row, c)
                        if v is not None:
                            return v
                    return 0.0

                funds = []
                for _, row in df.iterrows():
                    name  = str(row["Fund Name"]).strip()
                    alloc = float(row.get("Allocation_L", 0) or 0)
                    ftype = "debt"
                    amfi_ft = ""
                    if col_type:
                        t = str(row.get(col_type, ""))
                        amfi_ft = t.strip()
                        ftype = _classify_fund_type(t)
                        _log.debug(f"    {name[:40]:<40s} AMFI='{t[:35]}' → {ftype}  alloc={alloc:.2f}")
                    std_used = _fb(row, "Std_Dev_used", "Std_Dev_10Y", "Std_Dev_5Y", "Std_Dev_3Y")
                    dd_used  = _fb(row, "Max_DD_used",  "Max_DD_10Y",  "Max_DD_5Y",  "Max_DD_3Y")
                    funds.append(FundEntry(
                        name=name, fund_type=ftype, allocation=alloc,
                        std_dev=std_used,
                        sharpe  = _fb(row, "Sharpe_10Y",  "Sharpe_5Y",  "Sharpe_3Y"),
                        sortino = _fb(row, "Sortino_10Y", "Sortino_5Y", "Sortino_3Y"),
                        calmar  = _fb(row, "Calmar_10Y",  "Calmar_5Y",  "Calmar_3Y"),
                        alpha   = _fb(row, "Alpha_10Y"),
                        treynor = _fb(row, "Treynor_10Y"),
                        max_dd  = dd_used,
                        beta    = _fb(row, "Beta_10Y"),
                        combined_ratio = _fb(row, "Combined_Ratio_10Y", "Combined_Ratio_5Y", "Combined_Ratio_3Y"),
                        cagr_1=_fn(row, "1Y_CAGR"), cagr_3=_fn(row, "3Y_CAGR"),
                        cagr_5=_fn(row, "5Y_CAGR"), cagr_10=_fn(row, "10Y_CAGR"),
                        amfi_fund_type=amfi_ft or None,
                    ))

                ac = AllocationChunk(
                    year_from=int(chunk_def["year_from"]),
                    year_to=int(chunk_def["year_to"]),
                    funds=funds,
                )
                # Write optimiser constraints (fixes 10-fund cap bug)
                ac.min_return   = float(chunk_def.get("min_return",   6.85)) / 100.0
                ac.max_std_dev  = float(chunk_def.get("max_std_dev",  0.97)) / 100.0
                ac.max_dd       = float(chunk_def.get("max_dd",       0.75)) / 100.0
                ac.max_per_fund = float(chunk_def.get("max_per_fund", 8.0))  / 100.0
                ac.min_per_fund = float(chunk_def.get("min_per_fund", 2.0))  / 100.0
                ac.max_per_type = float(chunk_def.get("max_per_type", 24.0)) / 100.0
                ac.max_per_amc  = float(chunk_def.get("max_per_amc",  16.0)) / 100.0

                # ── Seed target_weights from THIS chunk's CSV fund allocations ─
                # Ground-truth weights come from the CSV, not from whatever was
                # previously in state (which may be stale from an old optimizer run).
                # The optimizer will overwrite these with its own results when it runs.
                total_alloc = sum(f.allocation for f in funds if f.allocation > 0)
                if total_alloc > 1e-9:
                    ac.target_weights = {
                        f.name: f.allocation / total_alloc
                        for f in funds if f.allocation > 0
                    }
                    _log.debug(
                        f"  Chunk Yr{ac.year_from}-{ac.year_to}: seeded "
                        f"{len(ac.target_weights)} target_weights from CSV"
                    )

                new_alloc_chunks.append(ac)

                # Auto-derive return rate from portfolio yield
                rate = ac.portfolio_yield()
                new_return_chunks.append(ReturnChunk(
                    year_from=ac.year_from, year_to=ac.year_to,
                    annual_return=round(rate, 5)))

            self.state.allocation_chunks = new_alloc_chunks

            # ── Invalidate stale glide_path ───────────────────────────────
            # The glide path was computed for the PREVIOUS set of chunks/
            # target_weights.  If the chunk structure has changed (different
            # number of chunks, different funds), the old glide path would
            # cause the engine to apply phantom rebalancing transitions.
            # Clear it so the engine runs without glide-path rebalancing
            # until the user explicitly re-runs Optimize Sticky Portfolio.
            self.state.glide_path = None

            # Auto-populate return_chunks (user can override via Return Rate dialog)
            if new_return_chunks:
                # Fill any gap to year 30
                if new_return_chunks[-1].year_to < 30:
                    last_rate = new_return_chunks[-1].annual_return
                    new_return_chunks.append(ReturnChunk(
                        new_return_chunks[-1].year_to + 1, 30, last_rate))
                self.state.return_chunks = new_return_chunks
                self._log(
                    f"\n  Return rates auto-set from portfolio yields:\n" +
                    "\n".join(
                        f"    Yrs {c.year_from}–{c.year_to}: "
                        f"{c.annual_return*100:.3f}%"
                        for c in new_return_chunks
                    )
                )

            self._log(
                f"\n  state.allocation_chunks: "
                f"{len(new_alloc_chunks)} chunk(s) stored in config."
            )
        except Exception as exc:
            self._log(f"⚠  Could not build allocation_chunks: {exc}")

    def _load_chunk_into_state(self, csv_path: Path):
        """Read one chunk CSV, replace state.funds, log portfolio ratios."""
        import os, logging
        log_path = os.environ.get("SWP_DEBUG_LOG")
        _log = logging.getLogger("main.debug")
        if log_path and not _log.handlers:
            _fh = logging.FileHandler(log_path, mode="a", encoding="utf-8")
            _fh.setFormatter(logging.Formatter("%(message)s"))
            _log.addHandler(_fh)
            _log.setLevel(logging.DEBUG)

        _log.debug(f"\n  _load_chunk_into_state: {csv_path}")

        try:
            import pandas as pd
            from models import FundEntry

            df = pd.read_csv(csv_path, encoding="utf-8-sig")
            if "Allocation_L" not in df.columns or "Fund Name" not in df.columns:
                self._log(f"⚠  {csv_path.name}: missing expected columns.")
                return

            # Extract portfolio ratios from PORTFOLIO TOTAL summary row
            summary_mask = df["Fund Type"].fillna("") == "PORTFOLIO TOTAL"
            port_sharpe = port_sortino = port_calmar = None
            if summary_mask.any():
                srow = df[summary_mask].iloc[0]
                def _ratio(col):
                    v = srow.get(col, "")
                    try:
                        return float(v) if str(v) not in ("", "nan") else None
                    except (ValueError, TypeError):
                        return None
                port_sharpe  = _ratio("Port_Sharpe")
                port_sortino = _ratio("Port_Sortino")
                port_calmar  = _ratio("Port_Calmar")

            # Drop summary row and zero-allocation rows
            df = df[df["Fund Name"].notna()].copy()
            df = df[df["Fund Type"].fillna("") != "PORTFOLIO TOTAL"].copy()
            df = df[df["Allocation_L"].fillna(0) > 0].copy()

            col_type = "Fund Type" if "Fund Type" in df.columns else None

            def _f(row, col):
                return float(row[col]) if col in df.columns and pd.notna(row.get(col)) else 0.0
            def _fn(row, col):
                return float(row[col]) if col in df.columns and pd.notna(row.get(col)) else None
            def _fb(row, *cols):
                """Return first non-None value across fallback columns (10Y->5Y->3Y)."""
                for c in cols:
                    v = _fn(row, c)
                    if v is not None:
                        return v
                return 0.0

            new_funds = []
            for _, row in df.iterrows():
                name  = str(row["Fund Name"]).strip()
                alloc = float(row.get("Allocation_L", 0) or 0)
                ftype = "debt"
                amfi_ft = ""
                if col_type:
                    t = str(row.get(col_type, ""))
                    amfi_ft = t.strip()
                    ftype = _classify_fund_type(t)
                std_used = _fb(row, "Std_Dev_used", "Std_Dev_10Y", "Std_Dev_5Y", "Std_Dev_3Y")
                dd_used  = _fb(row, "Max_DD_used",  "Max_DD_10Y",  "Max_DD_5Y",  "Max_DD_3Y")
                new_funds.append(FundEntry(
                    name=name, fund_type=ftype, allocation=alloc,
                    std_dev=std_used,
                    sharpe  = _fb(row, "Sharpe_10Y",        "Sharpe_5Y",        "Sharpe_3Y"),
                    sortino = _fb(row, "Sortino_10Y",       "Sortino_5Y",       "Sortino_3Y"),
                    calmar  = _fb(row, "Calmar_10Y",        "Calmar_5Y",        "Calmar_3Y"),
                    alpha   = _fb(row, "Alpha_10Y"),
                    treynor = _fb(row, "Treynor_10Y"),
                    max_dd  = dd_used,
                    beta    = _fb(row, "Beta_10Y"),
                    combined_ratio = _fb(row, "Combined_Ratio_10Y", "Combined_Ratio_5Y", "Combined_Ratio_3Y"),
                    cagr_1=_fn(row, "1Y_CAGR"), cagr_3=_fn(row, "3Y_CAGR"),
                    cagr_5=_fn(row, "5Y_CAGR"), cagr_10=_fn(row, "10Y_CAGR"),
                    amfi_fund_type=amfi_ft or None,
                ))

            self.state.funds    = new_funds
            self.state.fd_rate  = self.state.portfolio_yield()

            # ── Compute portfolio ratios from loaded funds if not in CSV ──
            if (port_sharpe is None or port_sortino is None or port_calmar is None) and new_funds:
                from models import _first_available
                total_alloc = sum(f.allocation for f in new_funds if f.allocation > 0)
                if total_alloc > 0:
                    w_sharpe = w_sortino = 0.0
                    w_ret = w_dd = 0.0
                    for f in new_funds:
                        if f.allocation <= 0:
                            continue
                        w = f.allocation / total_alloc
                        w_sharpe  += w * (f.sharpe or 0.0)
                        w_sortino += w * (f.sortino or 0.0)
                        cagr = _first_available(f.cagr_5, f.cagr_3, f.cagr_1, default=7.0)
                        w_ret += w * cagr
                        w_dd  += w * abs(f.max_dd or 0.0)
                    if port_sharpe is None:
                        port_sharpe = w_sharpe
                    if port_sortino is None:
                        port_sortino = w_sortino
                    if port_calmar is None and w_dd > 1e-9:
                        port_calmar = w_ret / w_dd

            _log.debug(f"  _load_chunk_into_state: loaded {len(new_funds)} funds")
            fd = sum(f.allocation for f in new_funds if f.fund_type == 'debt')
            fe = sum(f.allocation for f in new_funds if f.fund_type == 'equity')
            fo = sum(f.allocation for f in new_funds if f.fund_type == 'other')
            _log.debug(f"    flat state.funds: D={fd:.2f} E={fe:.2f} O={fo:.2f}")
            for f in new_funds:
                if f.allocation > 0:
                    _log.debug(f"      {f.name[:45]:<45s} type={f.fund_type:<6s} alloc={f.allocation:.2f}")

            def _rs(v): return f"{v:.3f}" if v is not None else "n/a"
            self._log(
                f"\n  Loaded from {csv_path.name}: "
                f"{len(new_funds)} funds, "
                f"\u20b9{self.state.total_allocation():.1f} L  |  "
                f"Sharpe {_rs(port_sharpe)}  "
                f"Sortino {_rs(port_sortino)}  "
                f"Calmar {_rs(port_calmar)}"
            )
        except Exception as exc:
            self._log(f"⚠  Could not update state.funds from {csv_path.name}: {exc}")



# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT (when run directly — normally launched via run.py)
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
