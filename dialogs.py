"""
Additional dialogs for SWP Planner.
"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QHeaderView, QDialogButtonBox, QComboBox,
    QDoubleSpinBox, QSpinBox, QGroupBox, QFormLayout, QWidget,
    QTabWidget, QMessageBox, QAbstractItemView, QCheckBox, QDateEdit,
    QScrollArea, QSizePolicy
)
from PySide6.QtCore import Qt, QDate
from PySide6.QtGui import QColor, QFont
from typing import List
import copy
from datetime import date

from models import (AppState, ReturnChunk, SplitChunk, HUFWithdrawalChunk,
                    WindfallEntry, OtherIncome, FDRateChunk)


# ─────────────────────────────────────────────────────────────
# Annual fund requirements
# ─────────────────────────────────────────────────────────────
class RequirementsDialog(QDialog):
    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Annual SWP Withdrawal Requirements (Rs Lakhs)")
        self.resize(420, 600)
        self.state = state

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Set the annual SWP withdrawal target per year (Rs Lakhs).\n"
            "Setting a value in year N automatically applies it to all subsequent years."))

        self.table = QTableWidget(30, 2)
        self.table.setHorizontalHeaderLabels(["Year", "Annual Withdrawal (L)"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)

        # Pre-fill
        reqs = {}
        for y in range(1, 31):
            reqs[y] = state.get_requirement(y)

        for yr in range(1, 31):
            yr_item = QTableWidgetItem(str(yr))
            yr_item.setFlags(yr_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(yr - 1, 0, yr_item)
            self.table.setItem(yr - 1, 1, QTableWidgetItem(f"{reqs[yr]:.2f}"))

        self.table.itemChanged.connect(self._propagate)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _propagate(self, item):
        if item.column() != 1:
            return
        row = item.row()
        try:
            val = float(item.text())
        except ValueError:
            return
        self.table.blockSignals(True)
        for r in range(row + 1, 30):
            self.table.item(r, 1).setText(f"{val:.2f}")
        self.table.blockSignals(False)

    def _save(self):
        reqs = {}
        for yr in range(1, 31):
            try:
                val = float(self.table.item(yr - 1, 1).text())
                reqs[yr] = val
            except Exception:
                pass
        self.state.annual_requirements = reqs
        self.accept()


# ─────────────────────────────────────────────────────────────
# Return rate chunks
# ─────────────────────────────────────────────────────────────
class ReturnRateDialog(QDialog):
    def __init__(self, state: AppState, title: str = "Portfolio Return Rate", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(560, 420)
        self.state = state
        self.chunks: List[ReturnChunk] = [copy.deepcopy(c) for c in state.return_chunks]

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Define the expected annual return rate for the portfolio in time chunks.\n"
            "Chunks must be contiguous and cover years 1–30."))

        # ── Auto-populate button (shown when allocation_chunks exist) ─────────
        if state.allocation_chunks:
            info_bar = QHBoxLayout()
            self.lbl_alloc_hint = QLabel(
                f"<b>{len(state.allocation_chunks)} allocation chunk(s)</b> found "
                f"from fund allocation exercise.")
            self.lbl_alloc_hint.setStyleSheet("color:#27ae60;")
            info_bar.addWidget(self.lbl_alloc_hint)
            btn_auto = QPushButton("⟳ Auto-fill from Fund Allocation")
            btn_auto.setStyleSheet(
                "background:#2ecc71;color:white;font-weight:bold;"
                "padding:4px 12px;border-radius:3px;")
            btn_auto.clicked.connect(self._auto_populate)
            info_bar.addWidget(btn_auto)
            info_bar.addStretch()
            layout.addLayout(info_bar)

        layout.addWidget(QLabel(
            "Define the expected annual return rate for the portfolio in time chunks.\n"
            "Chunks must be contiguous and cover years 1–30."))

        btn_bar = QHBoxLayout()
        btn_add = QPushButton("➕ Add Chunk")
        btn_del = QPushButton("🗑 Remove Last")
        btn_add.clicked.connect(self._add)
        btn_del.clicked.connect(self._remove)
        btn_bar.addWidget(btn_add)
        btn_bar.addWidget(btn_del)
        btn_bar.addStretch()
        layout.addLayout(btn_bar)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Year From", "Year To", "Annual Return (e.g. 0.07 = 7%)"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.itemChanged.connect(self._on_changed)
        layout.addWidget(self.table)

        self.lbl = QLabel("")
        layout.addWidget(self.lbl)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        # Auto-populate on first open if return_chunks don't match allocation
        if state.allocation_chunks and not self._chunks_match_allocation():
            self._auto_populate(silent=True)

        self._refresh()

    def _chunks_match_allocation(self) -> bool:
        """True if current return_chunks already match the allocation_chunks."""
        acs = self.state.allocation_chunks
        if len(self.chunks) != len(acs):
            return False
        return all(
            c.year_from == ac.year_from and c.year_to == ac.year_to
            for c, ac in zip(self.chunks, acs)
        )

    def _auto_populate(self, silent: bool = False):
        """
        Replace return_chunks with one entry per allocation_chunk, using the
        weighted-average 5Y CAGR of that chunk's funds as the return rate.
        """
        new_chunks = []
        for ac in self.state.allocation_chunks:
            rate = ac.portfolio_yield()
            new_chunks.append(ReturnChunk(ac.year_from, ac.year_to, rate))
        # Fill any gap to year 30 with the last rate
        if new_chunks and new_chunks[-1].year_to < 30:
            last_rate = new_chunks[-1].annual_return
            new_chunks.append(ReturnChunk(new_chunks[-1].year_to + 1, 30, last_rate))
        self.chunks = new_chunks
        self._refresh()
        if not silent:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(
                self, "Auto-filled",
                "Return rates populated from fund allocation portfolio yields:\n\n"
                + "\n".join(
                    f"  Years {c.year_from}–{c.year_to}: "
                    f"{c.annual_return*100:.3f}%"
                    for c in self.chunks
                )
            )

    def _refresh(self):
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for c in self.chunks:
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(str(c.year_from)))
            self.table.setItem(r, 1, QTableWidgetItem(str(c.year_to)))
            self.table.setItem(r, 2, QTableWidgetItem(f"{c.annual_return:.4f}"))
        self.table.blockSignals(False)
        self._validate()

    def _add(self):
        last = self.chunks[-1] if self.chunks else None
        new_from = (last.year_to + 1) if last else 1
        if new_from > 30:
            return
        self.chunks.append(ReturnChunk(new_from, min(new_from + 4, 30),
                                       last.annual_return if last else 0.07))
        self._refresh()

    def _remove(self):
        if self.chunks:
            self.chunks.pop()
            self.table.removeRow(self.table.rowCount() - 1)
            self._validate()

    def _on_changed(self, item):
        row, col = item.row(), item.column()
        try:
            val = float(item.text())
        except ValueError:
            return
        if row < len(self.chunks):
            c = self.chunks[row]
            if col == 0:
                self.chunks[row] = ReturnChunk(int(val), c.year_to, c.annual_return)
            elif col == 1:
                self.chunks[row] = ReturnChunk(c.year_from, int(val), c.annual_return)
                if row + 1 < len(self.chunks):
                    self.chunks[row+1] = ReturnChunk(int(val)+1, self.chunks[row+1].year_to, self.chunks[row+1].annual_return)
                    self.table.blockSignals(True)
                    self.table.item(row+1, 0).setText(str(int(val)+1))
                    self.table.blockSignals(False)
            elif col == 2:
                self.chunks[row] = ReturnChunk(c.year_from, c.year_to, val)
        self._validate()

    def _validate(self):
        covered = set()
        for c in self.chunks:
            covered.update(range(c.year_from, c.year_to + 1))
        missing = [y for y in range(1, 31) if y not in covered]
        if missing:
            self.lbl.setText(f"⚠ Uncovered years: {missing[:8]}{'…' if len(missing)>8 else ''}")
            self.lbl.setStyleSheet("color: red;")
        else:
            self.lbl.setText("✓ All 30 years covered")
            self.lbl.setStyleSheet("color: green;")

    def _save(self):
        self.state.return_chunks = [copy.deepcopy(c) for c in self.chunks]
        self.accept()

    def get_chunks(self) -> List[ReturnChunk]:
        return [copy.deepcopy(c) for c in self.chunks]


# ─────────────────────────────────────────────────────────────
# Withdrawal split (debt:equity ratio)
# ─────────────────────────────────────────────────────────────
class SplitDialog(QDialog):
    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Debt:Equity Withdrawal Split Ratio")
        self.resize(500, 400)
        self.state = state
        self.chunks: List[SplitChunk] = [copy.deepcopy(c) for c in state.split_chunks]

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Define the fraction of monthly SWP withdrawal taken from Debt funds.\n"
            "Equity fraction = 1 – Debt fraction. Chunks must cover years 1–30."))

        btn_bar = QHBoxLayout()
        btn_add = QPushButton("➕ Add Chunk")
        btn_del = QPushButton("🗑 Remove Last")
        btn_add.clicked.connect(self._add)
        btn_del.clicked.connect(self._remove)
        btn_bar.addWidget(btn_add)
        btn_bar.addWidget(btn_del)
        btn_bar.addStretch()
        layout.addLayout(btn_bar)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Year From", "Year To", "Debt Ratio (0-1)"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.itemChanged.connect(self._on_changed)
        layout.addWidget(self.table)

        default_ratio = state.total_debt_allocation() / max(1.0, state.total_allocation())
        self.lbl_default = QLabel(f"Current portfolio debt ratio: {default_ratio:.3f} (used if no chunks defined)")
        self.lbl_default.setStyleSheet("color: #555; font-size: 11px;")
        layout.addWidget(self.lbl_default)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self._refresh()

    def _refresh(self):
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for c in self.chunks:
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(str(c.year_from)))
            self.table.setItem(r, 1, QTableWidgetItem(str(c.year_to)))
            self.table.setItem(r, 2, QTableWidgetItem(f"{c.debt_ratio:.4f}"))
        self.table.blockSignals(False)

    def _add(self):
        last = self.chunks[-1] if self.chunks else None
        new_from = (last.year_to + 1) if last else 1
        if new_from > 30:
            return
        default_r = self.state.total_debt_allocation() / max(1.0, self.state.total_allocation())
        self.chunks.append(SplitChunk(new_from, min(new_from + 4, 30),
                                      last.debt_ratio if last else default_r))
        self._refresh()

    def _remove(self):
        if self.chunks:
            self.chunks.pop()
            self.table.removeRow(self.table.rowCount() - 1)

    def _on_changed(self, item):
        row, col = item.row(), item.column()
        try:
            val = float(item.text())
        except ValueError:
            return
        if row < len(self.chunks):
            c = self.chunks[row]
            if col == 0:
                self.chunks[row] = SplitChunk(int(val), c.year_to, c.debt_ratio)
            elif col == 1:
                self.chunks[row] = SplitChunk(c.year_from, int(val), c.debt_ratio)
                if row + 1 < len(self.chunks):
                    self.chunks[row+1] = SplitChunk(int(val)+1, self.chunks[row+1].year_to, self.chunks[row+1].debt_ratio)
                    self.table.blockSignals(True)
                    self.table.item(row+1, 0).setText(str(int(val)+1))
                    self.table.blockSignals(False)
            elif col == 2:
                self.chunks[row] = SplitChunk(c.year_from, c.year_to, max(0.0, min(1.0, val)))

    def _save(self):
        self.state.split_chunks = [copy.deepcopy(c) for c in self.chunks]
        self.accept()


# ─────────────────────────────────────────────────────────────
# Other income sources
# ─────────────────────────────────────────────────────────────
def _income_form(income: OtherIncome) -> tuple:
    """Returns (widget, getter_callable)."""
    w = QWidget()
    form = QFormLayout(w)

    fields = {}
    labels = [
        ("salary", "Salary (L/yr)"),
        ("taxable_interest", "Taxable Interest (L/yr)"),
        ("tax_free_interest", "Tax-Free Interest (L/yr)"),
        ("pension", "Pension (L/yr)"),
        ("rental", "Rental Income (L/yr)"),
        ("other_taxable", "Other Taxable (L/yr)"),
        ("other_non_taxable", "Other Non-Taxable (L/yr)"),
    ]
    for key, label in labels:
        spin = QDoubleSpinBox()
        spin.setRange(0, 99999)
        spin.setDecimals(2)
        spin.setSuffix(" L")
        spin.setValue(getattr(income, key, 0.0))
        form.addRow(label + ":", spin)
        fields[key] = spin

    def getter():
        return OtherIncome(**{k: v.value() for k, v in fields.items()})

    return w, getter


class IncomeDialog(QDialog):
    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Other Income Sources")
        self.resize(500, 500)
        self.state = state

        layout = QVBoxLayout(self)
        tabs = QTabWidget()

        self.ind_widget, self.ind_getter = _income_form(state.personal_income)
        tabs.addTab(self.ind_widget, "Individual")

        self.huf_widget, self.huf_getter = _income_form(state.huf_income)
        tabs.addTab(self.huf_widget, "HUF")

        layout.addWidget(tabs)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _save(self):
        self.state.personal_income = self.ind_getter()
        self.state.huf_income = self.huf_getter()
        self.accept()


# ─────────────────────────────────────────────────────────────
# Windfalls
# ─────────────────────────────────────────────────────────────
class WindfallDialog(QDialog):
    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Windfall Gains (Inheritance, etc.)")
        self.resize(500, 450)
        self.state = state
        self.entries: List[WindfallEntry] = [copy.deepcopy(w) for w in state.windfalls]

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Add one-time windfall amounts by FY year (1–30). Default = 0."))

        btn_bar = QHBoxLayout()
        btn_add = QPushButton("➕ Add Entry")
        btn_del = QPushButton("🗑 Remove Selected")
        btn_add.clicked.connect(self._add)
        btn_del.clicked.connect(self._remove)
        btn_bar.addWidget(btn_add)
        btn_bar.addWidget(btn_del)
        btn_bar.addStretch()
        layout.addLayout(btn_bar)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["FY Year", "Amount (L)", "Target"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.itemChanged.connect(self._on_changed)
        layout.addWidget(self.table)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self._refresh()

    def _refresh(self):
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for e in self.entries:
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(str(e.year)))
            self.table.setItem(r, 1, QTableWidgetItem(f"{e.amount:.2f}"))
            # Target combo embedded
            combo = QComboBox()
            combo.addItems(["personal", "huf"])
            combo.setCurrentText(e.target)
            combo.currentTextChanged.connect(lambda txt, row=r: self._set_target(row, txt))
            self.table.setCellWidget(r, 2, combo)
        self.table.blockSignals(False)

    def _set_target(self, row, txt):
        if row < len(self.entries):
            self.entries[row] = WindfallEntry(self.entries[row].year, self.entries[row].amount, txt)

    def _add(self):
        self.entries.append(WindfallEntry(5, 0.0, "personal"))
        self._refresh()

    def _remove(self):
        rows = set(i.row() for i in self.table.selectedItems())
        for r in sorted(rows, reverse=True):
            if r < len(self.entries):
                self.entries.pop(r)
        self._refresh()

    def _on_changed(self, item):
        row, col = item.row(), item.column()
        if row >= len(self.entries):
            return
        e = self.entries[row]
        try:
            val = float(item.text())
        except ValueError:
            return
        if col == 0:
            self.entries[row] = WindfallEntry(int(val), e.amount, e.target)
        elif col == 1:
            self.entries[row] = WindfallEntry(e.year, val, e.target)

    def _save(self):
        self.state.windfalls = [copy.deepcopy(e) for e in self.entries]
        self.accept()


# ─────────────────────────────────────────────────────────────
# HUF withdrawals
# ─────────────────────────────────────────────────────────────
class HUFWithdrawalDialog(QDialog):
    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.setWindowTitle("HUF Annual Withdrawal Schedule")
        self.resize(500, 400)
        self.state = state
        self.chunks: List[HUFWithdrawalChunk] = [copy.deepcopy(c) for c in state.huf_withdrawal_chunks]

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Define HUF annual withdrawal amounts per time chunk (Rs Lakhs)."))

        btn_bar = QHBoxLayout()
        btn_add = QPushButton("➕ Add Chunk")
        btn_del = QPushButton("🗑 Remove Last")
        btn_add.clicked.connect(self._add)
        btn_del.clicked.connect(self._remove)
        btn_bar.addWidget(btn_add)
        btn_bar.addWidget(btn_del)
        btn_bar.addStretch()
        layout.addLayout(btn_bar)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Year From", "Year To", "Annual Withdrawal (L)"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.itemChanged.connect(self._on_changed)
        layout.addWidget(self.table)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self._refresh()

    def _refresh(self):
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for c in self.chunks:
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(str(c.year_from)))
            self.table.setItem(r, 1, QTableWidgetItem(str(c.year_to)))
            self.table.setItem(r, 2, QTableWidgetItem(f"{c.annual_withdrawal:.2f}"))
        self.table.blockSignals(False)

    def _add(self):
        last = self.chunks[-1] if self.chunks else None
        new_from = (last.year_to + 1) if last else 1
        if new_from > 30:
            return
        self.chunks.append(HUFWithdrawalChunk(new_from, min(new_from + 4, 30),
                                               last.annual_withdrawal if last else 0.0))
        self._refresh()

    def _remove(self):
        if self.chunks:
            self.chunks.pop()
            self.table.removeRow(self.table.rowCount() - 1)

    def _on_changed(self, item):
        row, col = item.row(), item.column()
        try:
            val = float(item.text())
        except ValueError:
            return
        if row < len(self.chunks):
            c = self.chunks[row]
            if col == 0:
                self.chunks[row] = HUFWithdrawalChunk(int(val), c.year_to, c.annual_withdrawal)
            elif col == 1:
                self.chunks[row] = HUFWithdrawalChunk(c.year_from, int(val), c.annual_withdrawal)
                if row + 1 < len(self.chunks):
                    self.chunks[row+1] = HUFWithdrawalChunk(int(val)+1, self.chunks[row+1].year_to, self.chunks[row+1].annual_withdrawal)
                    self.table.blockSignals(True)
                    self.table.item(row+1, 0).setText(str(int(val)+1))
                    self.table.blockSignals(False)
            elif col == 2:
                self.chunks[row] = HUFWithdrawalChunk(c.year_from, c.year_to, val)

    def _save(self):
        self.state.huf_withdrawal_chunks = [copy.deepcopy(c) for c in self.chunks]
        self.accept()


# ─────────────────────────────────────────────────────────────
# FD Interest Rate Chunks
# ─────────────────────────────────────────────────────────────
class FDRateChunksDialog(QDialog):
    """Edit FD interest rate per time-chunk (for FD tax benchmark comparison)."""
    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.setWindowTitle("FD Interest Rate Chunks (for Tax Benchmark)")
        self.resize(560, 420)
        self.state = state
        self.chunks: List[FDRateChunk] = [copy.deepcopy(c) for c in state.fd_rate_chunks]

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Define the FD interest rate used for the tax benchmark comparison.\n"
            "This determines the hypothetical FD tax that the SWP structure saves against.\n"
            "Chunks must be contiguous and cover years 1–30."))

        btn_bar = QHBoxLayout()
        btn_add = QPushButton("➕ Add Chunk")
        btn_del = QPushButton("🗑 Remove Last")
        btn_add.clicked.connect(self._add)
        btn_del.clicked.connect(self._remove)
        btn_bar.addWidget(btn_add)
        btn_bar.addWidget(btn_del)
        btn_bar.addStretch()
        layout.addLayout(btn_bar)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Year From", "Year To", "FD Interest Rate (e.g. 0.07 = 7%)"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.itemChanged.connect(self._on_changed)
        layout.addWidget(self.table)

        self.lbl = QLabel("")
        layout.addWidget(self.lbl)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self._refresh()

    def _refresh(self):
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for c in self.chunks:
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(str(c.year_from)))
            self.table.setItem(r, 1, QTableWidgetItem(str(c.year_to)))
            self.table.setItem(r, 2, QTableWidgetItem(f"{c.fd_rate:.4f}"))
        self.table.blockSignals(False)
        self._validate()

    def _add(self):
        last = self.chunks[-1] if self.chunks else None
        new_from = (last.year_to + 1) if last else 1
        if new_from > 30:
            return
        self.chunks.append(FDRateChunk(new_from, min(new_from + 4, 30),
                                        last.fd_rate if last else 0.07))
        self._refresh()

    def _remove(self):
        if self.chunks:
            self.chunks.pop()
            self.table.removeRow(self.table.rowCount() - 1)
            self._validate()

    def _on_changed(self, item):
        row, col = item.row(), item.column()
        try:
            val = float(item.text())
        except ValueError:
            return
        if row < len(self.chunks):
            c = self.chunks[row]
            if col == 0:
                self.chunks[row] = FDRateChunk(int(val), c.year_to, c.fd_rate)
            elif col == 1:
                self.chunks[row] = FDRateChunk(c.year_from, int(val), c.fd_rate)
                if row + 1 < len(self.chunks):
                    self.chunks[row+1] = FDRateChunk(int(val)+1, self.chunks[row+1].year_to, self.chunks[row+1].fd_rate)
                    self.table.blockSignals(True)
                    self.table.item(row+1, 0).setText(str(int(val)+1))
                    self.table.blockSignals(False)
            elif col == 2:
                self.chunks[row] = FDRateChunk(c.year_from, c.year_to, val)
        self._validate()

    def _validate(self):
        covered = set()
        for c in self.chunks:
            covered.update(range(c.year_from, c.year_to + 1))
        missing = [y for y in range(1, 31) if y not in covered]
        if missing:
            self.lbl.setText(f"⚠ Uncovered years: {missing[:8]}{'…' if len(missing)>8 else ''}")
            self.lbl.setStyleSheet("color: red;")
        else:
            self.lbl.setText("✓ All 30 years covered")
            self.lbl.setStyleSheet("color: green;")

    def _save(self):
        self.state.fd_rate_chunks = [copy.deepcopy(c) for c in self.chunks]
        # Also update the legacy scalar to the first chunk's rate for backward compat
        if self.chunks:
            self.state.fd_rate = self.chunks[0].fd_rate
        self.accept()


# ─────────────────────────────────────────────────────────────
# Sensitivity analysis
# ─────────────────────────────────────────────────────────────
class SensitivityDialog(QDialog):
    """Define multiple return-rate scenarios for comparison."""
    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sensitivity Analysis – Scenario Definitions")
        self.resize(700, 500)
        self.state = state
        self.scenarios = []   # list of {"name": str, "return_chunks": [ReturnChunk]}

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Define alternative return rate scenarios to compare against the base case.\n"
            "Each scenario overrides the return rate for the given year ranges."))

        btn_bar = QHBoxLayout()
        btn_add = QPushButton("➕ Add Scenario")
        btn_del = QPushButton("🗑 Remove Selected")
        btn_add.clicked.connect(self._add_scenario)
        btn_del.clicked.connect(self._remove_scenario)
        btn_bar.addWidget(btn_add)
        btn_bar.addWidget(btn_del)
        btn_bar.addStretch()
        layout.addLayout(btn_bar)

        self.scenario_tabs = QTabWidget()
        self.scenario_tabs.setTabsClosable(False)
        layout.addWidget(self.scenario_tabs)

        # Start with 2 default scenarios
        self._add_scenario("Optimistic (+1%)", offset=0.01)
        self._add_scenario("Stressed (−1%)", offset=-0.01)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _add_scenario(self, name: str = None, offset: float = 0.0):
        from models import ReturnChunk as RC
        idx = len(self.scenarios) + 1
        sc_name = name or f"Scenario {idx}"

        # Clone base chunks with offset
        base_chunks = [copy.deepcopy(c) for c in self.state.return_chunks]
        for c in base_chunks:
            c.annual_return = max(0.001, c.annual_return + offset)

        sc = {"name": sc_name, "return_chunks": base_chunks}
        self.scenarios.append(sc)

        # Build tab with a mini ReturnRateDialog-like editor
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)

        from PySide6.QtWidgets import QLineEdit
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Scenario Name:"))
        name_edit = QLineEdit(sc_name)
        name_edit.textChanged.connect(lambda txt, s=sc: s.update({"name": txt}))
        name_row.addWidget(name_edit)
        tab_layout.addLayout(name_row)

        # Simplified return chunk table
        tbl = QTableWidget(0, 3)
        tbl.setHorizontalHeaderLabels(["Year From", "Year To", "Annual Return"])
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        tbl.setAlternatingRowColors(True)

        def refresh_tbl(chunks, table):
            table.blockSignals(True)
            table.setRowCount(0)
            for c in chunks:
                r = table.rowCount()
                table.insertRow(r)
                table.setItem(r, 0, QTableWidgetItem(str(c.year_from)))
                table.setItem(r, 1, QTableWidgetItem(str(c.year_to)))
                table.setItem(r, 2, QTableWidgetItem(f"{c.annual_return:.4f}"))
            table.blockSignals(False)

        refresh_tbl(base_chunks, tbl)

        def on_changed(item, chunks=base_chunks, table=tbl):
            row, col = item.row(), item.column()
            try:
                val = float(item.text())
            except ValueError:
                return
            if row < len(chunks):
                c = chunks[row]
                if col == 0:
                    chunks[row] = type(c)(int(val), c.year_to, c.annual_return)
                elif col == 1:
                    chunks[row] = type(c)(c.year_from, int(val), c.annual_return)
                    if row + 1 < len(chunks):
                        chunks[row+1] = type(c)(int(val)+1, chunks[row+1].year_to, chunks[row+1].annual_return)
                        table.blockSignals(True)
                        table.item(row+1, 0).setText(str(int(val)+1))
                        table.blockSignals(False)
                elif col == 2:
                    chunks[row] = type(c)(c.year_from, c.year_to, val)

        tbl.itemChanged.connect(on_changed)
        tab_layout.addWidget(tbl)

        self.scenario_tabs.addTab(tab, sc_name)
        name_edit.textChanged.connect(lambda txt, i=self.scenario_tabs.count()-1: self.scenario_tabs.setTabText(i, txt))

    def _remove_scenario(self):
        idx = self.scenario_tabs.currentIndex()
        if idx >= 0 and idx < len(self.scenarios):
            self.scenario_tabs.removeTab(idx)
            self.scenarios.pop(idx)

    def get_scenarios(self):
        return self.scenarios


# ─────────────────────────────────────────────────────────────
# Monte Carlo Dialog
# ─────────────────────────────────────────────────────────────
class MonteCarloDialog(QDialog):
    """
    Parameter dialog for the Monte Carlo sequence-of-returns simulation.
    Supports both Historical Block Bootstrap and Log-Normal modes.
    """
    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Monte Carlo: Sequence-of-Returns Risk")
        self.resize(520, 580)
        self.state = state

        from monte_carlo import _portfolio_sigma, _per_chunk_sigmas
        import numpy as _np
        auto_sigma = _portfolio_sigma(state) * 100.0   # in %
        chunk_sigmas = _per_chunk_sigmas(state)
        mu_det = _np.array([state.get_return_rate(fy) for fy in range(1, 31)])

        layout = QVBoxLayout(self)

        # ── Explanation ───────────────────────────────────────────────────────
        info = QLabel(
            "<b>Sequence-of-Returns Risk Simulation</b><br>"
            "Simulates N return paths and shows how retirement corpus and "
            "net cash vary across the distribution.<br>"
            "<b>Block Bootstrap</b> (recommended): resamples actual Nifty 50 "
            "and Debt Index history — captures fat tails and volatility "
            "clustering that log-normal misses.<br>"
            "<b>Log-Normal</b> (legacy): synthetic draws centred on your "
            "return chunks with portfolio-derived σ."
        )
        info.setWordWrap(True)
        info.setStyleSheet("background:#eaf4fb;padding:8px;border-radius:4px;")
        layout.addWidget(info)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # ── Method selection ──────────────────────────────────────────────────
        self.chk_bootstrap = QCheckBox(
            "Use Historical Block Bootstrap  (recommended — fetches live Nifty data)")
        self.chk_bootstrap.setChecked(True)
        self.chk_bootstrap.setToolTip(
            "Resamples actual Nifty 50 + Debt Index annual returns to capture "
            "fat tails and volatility clustering in Indian markets. "
            "Requires internet access. Falls back to log-normal if unavailable.")
        form.addRow("Simulation method:", self.chk_bootstrap)

        # Block length (only relevant when bootstrap is on)
        self.spin_block = QSpinBox()
        self.spin_block.setRange(1, 5)
        self.spin_block.setValue(3)
        self.spin_block.setSuffix(" years")
        self.spin_block.setToolTip(
            "Number of consecutive historical years per bootstrap block. "
            "3 (default) preserves multi-year bear markets as a unit. "
            "1 = i.i.d. resampling (no clustering).  5 = strong clustering.")
        form.addRow("Bootstrap block length:", self.spin_block)
        self.chk_bootstrap.toggled.connect(self.spin_block.setEnabled)

        form.addRow(QLabel(""))  # spacer

        # N simulations
        self.spin_n = QSpinBox()
        self.spin_n.setRange(500, 999999)
        self.spin_n.setSingleStep(500)
        self.spin_n.setValue(2000)
        self.spin_n.setSuffix(" simulations")
        form.addRow("Number of simulations:", self.spin_n)

        # Volatility (log-normal fallback / override)
        self.spin_sigma = QDoubleSpinBox()
        self.spin_sigma.setRange(0.5, 30.0)
        self.spin_sigma.setDecimals(2)
        self.spin_sigma.setSingleStep(0.25)
        self.spin_sigma.setValue(round(auto_sigma, 2))
        self.spin_sigma.setSuffix(" % (annualised σ)")
        self.spin_sigma.setEnabled(False)
        form.addRow("Portfolio volatility σ:", self.spin_sigma)

        sigma_note = QLabel(
            f"<i>Auto-derived from fund std_devs: {auto_sigma:.3f}%.  "
            "Only used in log-normal mode or as fallback.</i>"
        )
        sigma_note.setWordWrap(True)
        sigma_note.setStyleSheet("color:#666;font-size:10px;")
        form.addRow("", sigma_note)

        self.chk_custom = QCheckBox("Override auto-derived σ")
        self.chk_custom.setChecked(False)
        self.chk_custom.toggled.connect(self.spin_sigma.setEnabled)
        form.addRow("", self.chk_custom)

        # Seed
        self.spin_seed = QSpinBox()
        self.spin_seed.setRange(0, 99999)
        self.spin_seed.setValue(42)
        form.addRow("Random seed:", self.spin_seed)

        # ── Return floor controls ─────────────────────────────────────────────
        form.addRow(QLabel(""))

        self.spin_floor_mult = QDoubleSpinBox()
        self.spin_floor_mult.setRange(1.0, 5.0)
        self.spin_floor_mult.setDecimals(1)
        self.spin_floor_mult.setSingleStep(0.5)
        self.spin_floor_mult.setValue(3.0)
        self.spin_floor_mult.setSuffix(" σ")
        self.spin_floor_mult.setToolTip(
            "Floor = μ − Nσ per chunk.  Default 3σ means floors are set at "
            "3 standard deviations below each chunk's expected return.")
        form.addRow("Floor multiplier (N × σ):", self.spin_floor_mult)

        # Build per-chunk floor display
        floor_lines = []
        if state.allocation_chunks:
            for ac in state.allocation_chunks:
                yr_from, yr_to = max(1, ac.year_from), min(30, ac.year_to)
                idx = yr_from - 1
                mu_c  = mu_det[idx] * 100.0
                sig_c = chunk_sigmas[idx] * 100.0
                fl_c  = (mu_det[idx] - 3.0 * chunk_sigmas[idx]) * 100.0
                floor_lines.append(
                    f"Yr {yr_from}–{yr_to}: μ={mu_c:.2f}%  σ={sig_c:.2f}%  "
                    f"→ floor={fl_c:.2f}%")
        else:
            fl_val = (mu_det[0] - 3.0 * chunk_sigmas[0]) * 100.0
            floor_lines.append(
                f"All years: μ={mu_det[0]*100:.2f}%  σ={chunk_sigmas[0]*100:.2f}%  "
                f"→ floor={fl_val:.2f}%")

        self._floor_info_label = QLabel(
            "<i>" + "<br>".join(floor_lines) + "</i>")
        self._floor_info_label.setWordWrap(True)
        self._floor_info_label.setStyleSheet("color:#2980b9;font-size:10px;")
        form.addRow("Per-chunk floors (at 3σ):", self._floor_info_label)

        layout.addLayout(form)

        # ── What will be shown ────────────────────────────────────────────────
        output_box = QGroupBox("Output charts")
        ob_layout = QVBoxLayout(output_box)
        for line in [
            "📊  Fan chart — corpus percentiles (P5/P25/P50/P75/P95) vs deterministic",
            "📊  Net cash fan chart — annual spendable income by percentile",
            "📊  Ruin probability curve — % of paths where corpus depletes by FY N",
            "📊  Sequence-of-returns illustrator — best/worst/median single paths",
            "📊  Summary table — key statistics across all simulations",
        ]:
            ob_layout.addWidget(QLabel(line))
        layout.addWidget(output_box)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def get_params(self):
        """
        Returns (n_sims, sigma_override_or_None, floor_multiplier,
                 seed, use_bootstrap, block_length).
        """
        n             = self.spin_n.value()
        seed          = self.spin_seed.value()
        sigma         = (self.spin_sigma.value() / 100.0
                         if self.chk_custom.isChecked() else None)
        floor_mult    = self.spin_floor_mult.value()
        use_bootstrap = self.chk_bootstrap.isChecked()
        block_length  = self.spin_block.value()
        return n, sigma, floor_mult, seed, use_bootstrap, block_length

# ─────────────────────────────────────────────────────────────
# Glide-Path Parameters Dialog
# ─────────────────────────────────────────────────────────────
class GlidePathParametersDialog(QDialog):
    """
    Configure and review the sticky-portfolio glide-path settings:
      • Mode A (singular) vs Mode B (chunk-by-chunk with backward induction)
      • Rebalance spread (years around each chunk boundary to phase turnover)
      • Backward-induction slack tolerances (return / std_dev / max_dd)
      • Live preview of the current GlidePath schedule
    """

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Glide-Path Parameters")
        self.resize(720, 600)
        self.state = state

        main = QVBoxLayout(self)
        tabs = QTabWidget()
        main.addWidget(tabs)

        # ── Tab 1: Mode & Spread ──────────────────────────────────────────────
        t1 = QWidget()
        f1 = QVBoxLayout(t1)
        tabs.addTab(t1, "Mode & Spread")

        mode_grp = QGroupBox("Allocation Mode")
        mg_layout = QVBoxLayout(mode_grp)

        from PySide6.QtWidgets import QRadioButton, QButtonGroup
        self.rb_mode_a = QRadioButton(
            "Mode A — Singular Lifetime Allocation  "
            "(one optimised portfolio held throughout, no rebalancing)")
        self.rb_mode_b = QRadioButton(
            "Mode B — Chunked Sticky Portfolio  "
            "(separate allocation per chunk, glide-path rebalancing at boundaries)")
        self.rb_mode_a.setChecked(state.allocation_mode == "singular")
        self.rb_mode_b.setChecked(state.allocation_mode != "singular")
        mg_layout.addWidget(self.rb_mode_a)
        mg_layout.addWidget(self.rb_mode_b)
        mode_note = QLabel(
            "<i>Mode A: tax-efficient, no switching costs — suitable when you want a "
            "set-and-forget portfolio.<br>"
            "Mode B: adapts portfolio risk over time — introduces some switching "
            "tax at each chunk boundary but reduces risk in later years.</i>"
        )
        mode_note.setWordWrap(True)
        mode_note.setStyleSheet("color:#555;font-size:10px;")
        mg_layout.addWidget(mode_note)
        f1.addWidget(mode_grp)

        spread_grp = QGroupBox("Rebalance Spread (Mode B only)")
        sg_layout = QFormLayout(spread_grp)
        self.spin_spread = QSpinBox()
        self.spin_spread.setRange(1, 10)
        self.spin_spread.setValue(state.rebalance_spread_years)
        self.spin_spread.setSuffix(" year(s)")
        self.spin_spread.setToolTip(
            "Number of years over which the portfolio is gradually transitioned "
            "at each chunk boundary.  Spreading reduces annual tax incidence:\n"
            "  1 = cliff rebalance (all in one year)\n"
            "  4 = 25%/yr over 4 years (default)\n"
            " 10 = very gradual glide\n"
            "Larger spread → smaller per-year cost but longer transition window."
        )
        sg_layout.addRow("Spread (years):", self.spin_spread)
        spread_note = QLabel(
            "<i>The glide path interpolates weights linearly from the "
            "current chunk to the next over this many years on either side of the "
            "chunk boundary.  The backward-induction optimizer already minimises "
            "overall turnover; spread further reduces per-year incidence.</i>"
        )
        spread_note.setWordWrap(True)
        spread_note.setStyleSheet("color:#555;font-size:10px;")
        sg_layout.addRow("", spread_note)
        f1.addWidget(spread_grp)
        f1.addStretch()

        # Disable spread when Mode A is active
        def _toggle_spread():
            self.spin_spread.setEnabled(self.rb_mode_b.isChecked())
        self.rb_mode_a.toggled.connect(_toggle_spread)
        self.rb_mode_b.toggled.connect(_toggle_spread)
        _toggle_spread()

        # ── Tab 2: Backward-Induction Tolerances ─────────────────────────────
        t2 = QWidget()
        f2 = QVBoxLayout(t2)
        tabs.addTab(t2, "Backward-Induction Tolerances")

        f2.addWidget(QLabel(
            "<b>Backward-induction soft tolerances</b><br>"
            "These values allow each chunk's constraints to be slightly relaxed "
            "during the backward-induction pass so that the solver can minimise "
            "turnover without always hitting the constraint boundary hard.<br>"
            "Increase tolerances if the solver struggles to reduce turnover "
            "(especially for tightly constrained chunks).  Decrease to enforce "
            "stricter adherence to the original constraints."
        ))
        tol_grp = QGroupBox("Soft Tolerance Values")
        tol_form = QFormLayout(tol_grp)

        # Read existing tolerances from state (if set) or use defaults
        existing_tol = getattr(state, 'bi_tolerances', None) or {
            "return": 0.0025, "std_dev": 0.0025, "max_dd": 0.0050
        }

        self.spin_tol_ret = QDoubleSpinBox()
        self.spin_tol_ret.setRange(0.0, 2.0)
        self.spin_tol_ret.setDecimals(3)
        self.spin_tol_ret.setSingleStep(0.05)
        self.spin_tol_ret.setValue(existing_tol["return"] * 100)
        self.spin_tol_ret.setSuffix(" pp  (percentage-points)")
        tol_form.addRow("Return tolerance:", self.spin_tol_ret)
        tol_form.addRow("", QLabel(
            "<i>How much the return target can be lowered for commonality.  "
            "Default 0.25pp.</i>"))

        self.spin_tol_std = QDoubleSpinBox()
        self.spin_tol_std.setRange(0.0, 2.0)
        self.spin_tol_std.setDecimals(3)
        self.spin_tol_std.setSingleStep(0.05)
        self.spin_tol_std.setValue(existing_tol["std_dev"] * 100)
        self.spin_tol_std.setSuffix(" pp")
        tol_form.addRow("Std-dev tolerance:", self.spin_tol_std)
        tol_form.addRow("", QLabel(
            "<i>How much the std-dev limit can be raised.  Default 0.25pp.</i>"))

        self.spin_tol_dd = QDoubleSpinBox()
        self.spin_tol_dd.setRange(0.0, 5.0)
        self.spin_tol_dd.setDecimals(3)
        self.spin_tol_dd.setSingleStep(0.1)
        self.spin_tol_dd.setValue(existing_tol["max_dd"] * 100)
        self.spin_tol_dd.setSuffix(" pp")
        tol_form.addRow("Max-drawdown tolerance:", self.spin_tol_dd)
        tol_form.addRow("", QLabel(
            "<i>How much the drawdown limit can be raised.  Default 0.50pp.</i>"))

        f2.addWidget(tol_grp)
        f2.addStretch()

        # ── Tab 3: Current Glide Path Preview ─────────────────────────────────
        t3 = QWidget()
        f3 = QVBoxLayout(t3)
        tabs.addTab(t3, "Current Glide Path Preview")
        self._build_preview(f3)

        # ── Buttons ───────────────────────────────────────────────────────────
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        main.addWidget(btns)

    def _build_preview(self, layout):
        gp = self.state.glide_path
        if gp is None:
            lbl = QLabel(
                "No glide path available yet.\n\n"
                "Configure Mode B parameters above, run\n"
                "Data → Optimize Sticky Portfolio, then re-open this dialog\n"
                "to inspect the full year-by-year schedule."
            )
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("color:#888;font-size:13px;")
            layout.addWidget(lbl)
            return

        trans = gp.transition_years()
        summary = QLabel(
            f"<b>Glide path loaded</b> — "
            f"{'Flat (Mode A)' if gp.is_flat() else f'{len(trans)} transition year(s) (Mode B)'}<br>"
            f"Transition years: {list(sorted(trans)) if trans else '(none)'}"
        )
        summary.setWordWrap(True)
        summary.setStyleSheet(
            "background:#eaf4fb;padding:8px;border-radius:4px;"
            "color:#2c3e50;")
        layout.addWidget(summary)

        # Year-by-year weight table (scrollable)
        cols_set = set()
        for y in range(1, 31):
            cols_set.update(gp.weights_for_year(y).keys())
        fund_cols = sorted(cols_set)

        tbl = QTableWidget(30, 1 + len(fund_cols))
        tbl.setHorizontalHeaderLabels(["Year"] + [fn[:25] for fn in fund_cols])
        tbl.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        for c in range(1, 1 + len(fund_cols)):
            tbl.horizontalHeader().setSectionResizeMode(
                c, QHeaderView.ResizeMode.Interactive)
        tbl.setAlternatingRowColors(True)
        tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tbl.verticalHeader().setVisible(False)

        # Colour transition rows
        trans_set = set(trans)
        for yi, yr in enumerate(range(1, 31)):
            w = gp.weights_for_year(yr)
            yr_item = QTableWidgetItem(str(yr))
            yr_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if yr in trans_set:
                yr_item.setBackground(QColor("#fff3cd"))
                yr_item.setForeground(QColor("#856404"))
            tbl.setItem(yi, 0, yr_item)
            for ci, fn in enumerate(fund_cols):
                wval = w.get(fn, 0.0)
                cell = QTableWidgetItem(f"{wval*100:.1f}%" if wval > 0.001 else "—")
                cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if yr in trans_set and wval > 0.001:
                    cell.setBackground(QColor("#fff3cd"))
                elif wval > 0.001:
                    cell.setForeground(QColor("#155724"))
                tbl.setItem(yi, 1 + ci, cell)

        scroll = QScrollArea()
        scroll.setWidget(tbl)
        scroll.setWidgetResizable(True)
        layout.addWidget(scroll)

        note = QLabel(
            "<i>Yellow rows = transition years.  "
            "Weights shown as % of portfolio.  "
            "Run Data → Optimize Sticky Portfolio to update.</i>"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#666;font-size:10px;")
        layout.addWidget(note)

    def _save(self):
        self.state.allocation_mode = (
            "singular" if self.rb_mode_a.isChecked() else "chunked_sticky"
        )
        self.state.rebalance_spread_years = self.spin_spread.value()
        self.state.bi_tolerances = {
            "return":  self.spin_tol_ret.value() / 100.0,
            "std_dev": self.spin_tol_std.value() / 100.0,
            "max_dd":  self.spin_tol_dd.value()  / 100.0,
        }
        self.accept()

    def get_tolerances(self) -> dict:
        return {
            "return":  self.spin_tol_ret.value() / 100.0,
            "std_dev": self.spin_tol_std.value() / 100.0,
            "max_dd":  self.spin_tol_dd.value()  / 100.0,
        }


# ─────────────────────────────────────────────────────────────
# Rebalancing Constraints Audit Dialog
# ─────────────────────────────────────────────────────────────
class RebalancingConstraintsDialog(QDialog):
    """
    Per-chunk audit of:
      • Target weights from optimizer vs current actual allocation
      • Constraint slack consumed by backward induction
      • Estimated turnover cost at each chunk boundary

    Read-only reference view — no editable fields.  Opens one tab per
    allocation chunk.
    """

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Rebalancing Constraints Audit")
        self.resize(900, 580)
        self.state = state

        main = QVBoxLayout(self)

        # ── Header ────────────────────────────────────────────────────────────
        gp = state.glide_path
        mode_label = (
            "Mode A — Singular (no rebalancing)"
            if state.allocation_mode == "singular"
            else f"Mode B — Chunked Sticky  |  spread = {state.rebalance_spread_years} yr"
        )
        header = QLabel(
            f"<b>Allocation mode:</b> {mode_label}<br>"
            f"<b>Chunks:</b> {len(state.allocation_chunks)}  "
            f"<b>Glide path:</b> "
            + ("Not computed yet" if gp is None
               else ("Flat (Mode A)" if gp.is_flat()
                     else f"{len(gp.transition_years())} transition year(s)"))
        )
        header.setWordWrap(True)
        header.setStyleSheet("background:#eaf4fb;padding:8px;border-radius:4px;")
        main.addWidget(header)

        if not state.allocation_chunks:
            lbl = QLabel(
                "No allocation chunks defined.\n\n"
                "Use Data → Allocate Capital to create fund allocations per chunk."
            )
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("color:#888;font-size:13px;")
            main.addWidget(lbl)
        else:
            tabs = QTabWidget()
            for chunk in state.allocation_chunks:
                tab = self._build_chunk_tab(chunk)
                tabs.addTab(tab, f"Yrs {chunk.year_from}–{chunk.year_to}")
            main.addWidget(tabs)

        # ── Turnover summary table ────────────────────────────────────────────
        if len(state.allocation_chunks) > 1:
            main.addWidget(self._build_turnover_summary())

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(self.reject)
        btns.accepted.connect(self.accept)
        main.addWidget(btns)

    def _build_chunk_tab(self, chunk) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        # Constraint summary row
        min_ret  = getattr(chunk, 'min_return',  0.0685)
        max_std  = getattr(chunk, 'max_std_dev', 0.0099)
        max_dd   = getattr(chunk, 'max_dd',      0.0075)
        slack    = getattr(chunk, 'constraint_slack_used', None) or {}
        act_ret_slack = slack.get('return',  0.0)
        act_std_slack = slack.get('std_dev', 0.0)
        act_dd_slack  = slack.get('max_dd',  0.0)

        def _slk(val):
            if val < 1e-5:
                return "<span style='color:#27ae60'>✓ 0.000pp (none used)</span>"
            colour = "#c0392b" if val * 100 > 0.25 else "#e67e22"
            return f"<span style='color:{colour}'>{val*100:.3f}pp used</span>"

        constraints_html = (
            f"<b>Constraints:</b> "
            f"min return {min_ret*100:.3f}%  "
            f"max std {max_std*100:.3f}%  "
            f"max dd {max_dd*100:.3f}%<br>"
            f"<b>Backward-induction slack consumed:</b> "
            f"return {_slk(act_ret_slack)} &nbsp; "
            f"std-dev {_slk(act_std_slack)} &nbsp; "
            f"max-dd {_slk(act_dd_slack)}"
        )
        constraints_lbl = QLabel(constraints_html)
        constraints_lbl.setWordWrap(True)
        constraints_lbl.setStyleSheet("background:#f8f9fa;padding:6px;border-radius:3px;")
        layout.addWidget(constraints_lbl)

        # Fund comparison table: Name | Current % | Target % | Delta (pp) | Type
        target_w = getattr(chunk, 'target_weights', None) or {}
        current_funds = {f.name: f for f in chunk.funds}
        current_w_total = sum(f.allocation for f in chunk.funds)

        all_names = sorted(set(target_w.keys()) | set(current_funds.keys()))

        tbl = QTableWidget(len(all_names), 5)
        tbl.setHorizontalHeaderLabels([
            "Fund Name", "Type",
            "Current Alloc %", "Optimizer Target %", "Delta (pp)"
        ])
        tbl.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        for c in (1, 2, 3, 4):
            tbl.horizontalHeader().setSectionResizeMode(
                c, QHeaderView.ResizeMode.ResizeToContents)
        tbl.setAlternatingRowColors(True)
        tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tbl.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)

        def _item(txt, bold=False, align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter):
            it = QTableWidgetItem(txt)
            it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
            it.setTextAlignment(align)
            if bold:
                f = QFont(); f.setBold(True); it.setFont(f)
            return it

        for ri, name in enumerate(all_names):
            fund_obj = current_funds.get(name)
            ftype    = fund_obj.fund_type if fund_obj else "—"
            cur_pct  = (fund_obj.allocation / max(current_w_total, 1e-9) * 100) if fund_obj else 0.0
            tgt_pct  = target_w.get(name, 0.0) * 100
            delta    = tgt_pct - cur_pct

            name_item = _item(name, align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            if not fund_obj:
                name_item.setForeground(QColor("#27ae60"))
                name_item.setToolTip("New fund added by optimizer (not in current allocation)")
            elif name not in target_w:
                name_item.setForeground(QColor("#c0392b"))
                name_item.setToolTip("Fund present in current allocation but dropped by optimizer")
            tbl.setItem(ri, 0, name_item)
            tbl.setItem(ri, 1, _item(ftype, align=Qt.AlignmentFlag.AlignCenter))
            tbl.setItem(ri, 2, _item(f"{cur_pct:.2f}%"))
            tbl.setItem(ri, 3, _item(f"{tgt_pct:.2f}%" if tgt_pct > 0 else "—"))

            delta_item = _item(f"{delta:+.2f}pp", bold=(abs(delta) > 5))
            if delta > 0.5:
                delta_item.setForeground(QColor("#155724"))
            elif delta < -0.5:
                delta_item.setForeground(QColor("#721c24"))
            tbl.setItem(ri, 4, delta_item)

        layout.addWidget(tbl)

        # Weighted metrics summary
        if target_w and chunk.funds:
            metrics_lbl = self._build_metrics_label(chunk, target_w)
            layout.addWidget(metrics_lbl)

        return w

    def _build_metrics_label(self, chunk, target_w) -> QLabel:
        """Compute and display weighted portfolio metrics for target vs current."""
        fund_map = {f.name: f for f in chunk.funds}
        total_cur = sum(f.allocation for f in chunk.funds)

        def _wtd_metric(weights_dict, attr, default=0.0):
            total = 0.0
            for name, w in weights_dict.items():
                f = fund_map.get(name)
                total += w * getattr(f, attr, default) if f else 0.0
            return total

        # Target weights (normalised fractions)
        tgt_ret = _wtd_metric(target_w, "cagr_5") or _wtd_metric(target_w, "cagr_3")
        tgt_std = _wtd_metric(target_w, "std_dev")
        tgt_dd  = _wtd_metric(target_w, "max_dd")

        # Current weights (proportional to amounts)
        cur_w = {f.name: f.allocation / max(total_cur, 1e-9) for f in chunk.funds}
        cur_ret = _wtd_metric(cur_w, "cagr_5") or _wtd_metric(cur_w, "cagr_3")
        cur_std = _wtd_metric(cur_w, "std_dev")
        cur_dd  = _wtd_metric(cur_w, "max_dd")

        def _fmt(v, target_v, good_higher=True):
            diff = target_v - v
            if abs(diff) < 0.001:
                return f"{target_v:.3f}"
            colour = "#27ae60" if (diff > 0) == good_higher else "#c0392b"
            return f"<span style='color:{colour}'>{target_v:.3f} ({diff:+.3f})</span>"

        html = (
            "<b>Portfolio metrics — Current vs Optimizer Target:</b><br>"
            f"  Weighted CAGR: {cur_ret:.3f}% → {_fmt(cur_ret, tgt_ret, True)}&nbsp;&nbsp; "
            f"  Std Dev: {cur_std:.3f}% → {_fmt(cur_std, tgt_std, False)}&nbsp;&nbsp; "
            f"  Max DD: {cur_dd:.3f}% → {_fmt(cur_dd, tgt_dd, False)}"
        )
        lbl = QLabel(html)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("background:#f0fff4;padding:5px;border-radius:3px;font-size:10px;")
        return lbl

    def _build_turnover_summary(self) -> QGroupBox:
        """Build a summary table of estimated turnover at each chunk boundary."""
        grp = QGroupBox("Estimated Boundary Turnover")
        layout = QVBoxLayout(grp)

        chunks = self.state.allocation_chunks
        # Columns: Boundary | Funds Added | Funds Dropped | Funds Continued | Est. Turnover %
        cols = ["Boundary", "Funds Added", "Funds Dropped", "Funds Common", "Est. Turnover (pp)"]
        tbl = QTableWidget(len(chunks) - 1, len(cols))
        tbl.setHorizontalHeaderLabels(cols)
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        tbl.setAlternatingRowColors(True)
        tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tbl.setMaximumHeight(200)

        for i in range(len(chunks) - 1):
            c1 = chunks[i]
            c2 = chunks[i + 1]
            w1 = getattr(c1, 'target_weights', None) or {f.name: 1.0/len(c1.funds) for f in c1.funds}
            w2 = getattr(c2, 'target_weights', None) or {f.name: 1.0/len(c2.funds) for f in c2.funds}
            # normalise
            t1 = sum(w1.values()) or 1.0
            t2 = sum(w2.values()) or 1.0
            w1n = {k: v / t1 for k, v in w1.items()}
            w2n = {k: v / t2 for k, v in w2.items()}

            all_k = set(w1n.keys()) | set(w2n.keys())
            added    = sum(1 for k in all_k if k in w2n and k not in w1n)
            dropped  = sum(1 for k in all_k if k in w1n and k not in w2n)
            common   = sum(1 for k in all_k if k in w1n and k in w2n)
            turnover = sum(abs(w2n.get(k, 0.0) - w1n.get(k, 0.0)) for k in all_k) * 100

            def _it(txt):
                it = QTableWidgetItem(txt)
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                return it

            tbl.setItem(i, 0, _it(f"Yr {c1.year_to} → {c2.year_from}"))
            tbl.setItem(i, 1, _it(str(added)))
            tbl.setItem(i, 2, _it(str(dropped)))
            tbl.setItem(i, 3, _it(str(common)))
            tv_item = _it(f"{turnover:.1f}pp")
            if turnover > 50:
                tv_item.setForeground(QColor("#c0392b"))
            elif turnover > 20:
                tv_item.setForeground(QColor("#e67e22"))
            else:
                tv_item.setForeground(QColor("#27ae60"))
            tbl.setItem(i, 4, tv_item)

        layout.addWidget(tbl)
        layout.addWidget(QLabel(
            "<i>Turnover = Σ |w₂ − w₁| across all funds at the chunk boundary. "
            "Higher values mean larger rebalancing tax at transition.</i>",
        ))
        return grp