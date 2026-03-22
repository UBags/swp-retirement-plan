# Copyright (c) 2025 Uddipan Bagchi. All rights reserved.
# See LICENSE in the project root for license information.package com.costheta.cortexa.action

"""
Tax rules editor dialogs.
"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QWidget, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem, QHeaderView,
    QScrollArea, QFrame, QSplitter, QGroupBox, QComboBox,
    QDialogButtonBox, QMessageBox, QDoubleSpinBox, QSpinBox,
    QAbstractItemView, QSizePolicy, QFormLayout
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from typing import List
import copy

from models import AppState, TaxChunk, TaxSlab, EquityTaxChunk


class SlabTableWidget(QWidget):
    """Edit a list of TaxSlab for one TaxChunk."""
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        bar = QHBoxLayout()
        btn_add = QPushButton("➕ Add Slab")
        btn_del = QPushButton("🗑 Remove Last")
        btn_add.clicked.connect(self._add_slab)
        btn_del.clicked.connect(self._remove_last)
        bar.addWidget(btn_add)
        bar.addWidget(btn_del)
        bar.addStretch()
        layout.addLayout(bar)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Lower (L)", "Upper (L, 1e9=∞)", "Rate (0-1)"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)

    def set_slabs(self, slabs: List[TaxSlab]):
        self.table.setRowCount(0)
        for s in slabs:
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(str(s.lower)))
            self.table.setItem(r, 1, QTableWidgetItem(str(s.upper)))
            self.table.setItem(r, 2, QTableWidgetItem(str(s.rate)))

    def get_slabs(self) -> List[TaxSlab]:
        slabs = []
        bad_rows = []
        for r in range(self.table.rowCount()):
            try:
                lo_item = self.table.item(r, 0)
                hi_item = self.table.item(r, 1)
                rate_item = self.table.item(r, 2)
                lo_text = lo_item.text().strip() if lo_item else ""
                hi_text = hi_item.text().strip() if hi_item else ""
                rate_text = rate_item.text().strip() if rate_item else ""
                if not lo_text and not hi_text and not rate_text:
                    continue   # entirely blank row — skip silently
                lo = float(lo_text)
                hi = float(hi_text)
                rate = float(rate_text)
                slabs.append(TaxSlab(lo, hi, rate))
            except (ValueError, TypeError, AttributeError):
                bad_rows.append(r + 1)
        if bad_rows:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(
                self, "Invalid Slab Data",
                f"Row(s) {', '.join(str(r) for r in bad_rows)} contain non-numeric "
                f"values and were skipped.  Please correct them.")
        return slabs

    def _add_slab(self):
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem("0"))
        self.table.setItem(r, 1, QTableWidgetItem("1000000000"))
        self.table.setItem(r, 2, QTableWidgetItem("0.30"))

    def _remove_last(self):
        if self.table.rowCount():
            self.table.removeRow(self.table.rowCount() - 1)


class DebtTaxEditor(QWidget):
    """Editor for a list of TaxChunk (debt fund tax rules)."""
    def __init__(self, entity: str, parent=None):
        super().__init__(parent)
        self.entity = entity
        self.chunks: List[TaxChunk] = []
        self._build_ui()

    def _build_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # Left: chunk list
        left = QGroupBox("Time Chunks")
        left_l = QVBoxLayout(left)
        left.setMaximumWidth(260)

        btn_bar = QHBoxLayout()
        self.btn_add_chunk = QPushButton("➕ Add")
        self.btn_del_chunk = QPushButton("🗑 Remove")
        self.btn_add_chunk.clicked.connect(self._add_chunk)
        self.btn_del_chunk.clicked.connect(self._remove_chunk)
        btn_bar.addWidget(self.btn_add_chunk)
        btn_bar.addWidget(self.btn_del_chunk)
        left_l.addLayout(btn_bar)

        self.chunk_table = QTableWidget(0, 3)
        self.chunk_table.setHorizontalHeaderLabels(["From", "To", "87A Limit(L)"])
        self.chunk_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.chunk_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.chunk_table.selectionModel().selectionChanged.connect(self._on_chunk_selected)
        left_l.addWidget(self.chunk_table)

        self.lbl_hint = QLabel("Select a chunk to edit slabs →")
        self.lbl_hint.setWordWrap(True)
        self.lbl_hint.setStyleSheet("color: #555; font-size: 11px;")
        left_l.addWidget(self.lbl_hint)

        layout.addWidget(left)

        # Right: slab editor for selected chunk
        right = QGroupBox("Tax Slabs for Selected Chunk")
        right_l = QVBoxLayout(right)
        self.slab_editor = SlabTableWidget()
        right_l.addWidget(self.slab_editor)
        self.btn_save_slabs = QPushButton("💾 Apply Slabs to Chunk")
        self.btn_save_slabs.clicked.connect(self._save_slabs)
        right_l.addWidget(self.btn_save_slabs)
        layout.addWidget(right)

        self._current_chunk_idx = -1

    def set_chunks(self, chunks: List[TaxChunk]):
        self.chunks = [copy.deepcopy(c) for c in chunks]
        self._refresh_chunk_table()

    def get_chunks(self) -> List[TaxChunk]:
        return copy.deepcopy(self.chunks)

    def _refresh_chunk_table(self):
        self.chunk_table.blockSignals(True)
        self.chunk_table.setRowCount(0)
        for i, c in enumerate(self.chunks):
            r = self.chunk_table.rowCount()
            self.chunk_table.insertRow(r)
            self.chunk_table.setItem(r, 0, QTableWidgetItem(str(c.year_from)))
            self.chunk_table.setItem(r, 1, QTableWidgetItem(str(c.year_to)))
            self.chunk_table.setItem(r, 2, QTableWidgetItem(str(c.exempt_limit)))
        self.chunk_table.blockSignals(False)

    def _on_chunk_selected(self):
        rows = self.chunk_table.selectedItems()
        if not rows:
            return
        row = self.chunk_table.currentRow()
        self._current_chunk_idx = row
        if row < len(self.chunks):
            self.slab_editor.set_slabs(self.chunks[row].slabs)

    def _save_slabs(self):
        if self._current_chunk_idx < 0 or self._current_chunk_idx >= len(self.chunks):
            return
        # Also read year_from, year_to, exempt_limit from chunk table
        row = self._current_chunk_idx
        try:
            yf = int(self.chunk_table.item(row, 0).text())
            yt = int(self.chunk_table.item(row, 1).text())
            ex = float(self.chunk_table.item(row, 2).text())
        except Exception:
            yf = self.chunks[row].year_from
            yt = self.chunks[row].year_to
            ex = self.chunks[row].exempt_limit
        slabs = self.slab_editor.get_slabs()
        self.chunks[row] = TaxChunk(yf, yt, ex, slabs)
        self._refresh_chunk_table()
        QMessageBox.information(self, "Saved", f"Slabs saved for chunk Yr {yf}-{yt}.")

    def _add_chunk(self):
        if self.chunks:
            last = self.chunks[-1]
            new_from = last.year_to + 1
        else:
            new_from = 1
        if new_from > 30:
            QMessageBox.warning(self, "Limit", "Already covers all 30 years.")
            return
        new_to = min(new_from + 4, 30)
        prev_chunk = self.chunks[-1] if self.chunks else None
        default_slabs = copy.deepcopy(prev_chunk.slabs) if prev_chunk else [
            TaxSlab(0, 4, 0), TaxSlab(4, 8, 0.05), TaxSlab(8, 12, 0.10),
            TaxSlab(12, 1e9, 0.30)
        ]
        default_exempt = prev_chunk.exempt_limit if prev_chunk else 12.0
        self.chunks.append(TaxChunk(new_from, new_to, default_exempt, default_slabs))
        self._refresh_chunk_table()

    def _remove_chunk(self):
        if self.chunks:
            self.chunks.pop()
            self.chunk_table.removeRow(self.chunk_table.rowCount() - 1)
            self._current_chunk_idx = -1


class EquityTaxEditor(QWidget):
    """Editor for list of EquityTaxChunk."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.chunks: List[EquityTaxChunk] = []
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        btn_bar = QHBoxLayout()
        btn_add = QPushButton("➕ Add Chunk")
        btn_del = QPushButton("🗑 Remove Last")
        btn_add.clicked.connect(self._add_chunk)
        btn_del.clicked.connect(self._remove_last)
        btn_bar.addWidget(btn_add)
        btn_bar.addWidget(btn_del)
        btn_bar.addStretch()
        layout.addLayout(btn_bar)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Year From", "Year To", "Tax Rate (0-1)", "Exempt Limit (L)"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.itemChanged.connect(self._on_changed)
        layout.addWidget(self.table)

    def set_chunks(self, chunks: List[EquityTaxChunk]):
        self.chunks = [copy.deepcopy(c) for c in chunks]
        self._refresh()

    def get_chunks(self) -> List[EquityTaxChunk]:
        return copy.deepcopy(self.chunks)

    def _refresh(self):
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for c in self.chunks:
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(str(c.year_from)))
            self.table.setItem(r, 1, QTableWidgetItem(str(c.year_to)))
            self.table.setItem(r, 2, QTableWidgetItem(str(c.tax_rate)))
            self.table.setItem(r, 3, QTableWidgetItem(str(c.exempt_limit)))
        self.table.blockSignals(False)

    def _on_changed(self, item):
        row = item.row()
        col = item.column()
        try:
            val = float(item.text())
        except ValueError:
            return
        if row < len(self.chunks):
            c = self.chunks[row]
            if col == 0:
                self.chunks[row] = EquityTaxChunk(int(val), c.year_to, c.tax_rate, c.exempt_limit)
            elif col == 1:
                self.chunks[row] = EquityTaxChunk(c.year_from, int(val), c.tax_rate, c.exempt_limit)
                # propagate continuity
                if row + 1 < len(self.chunks):
                    self.chunks[row+1] = EquityTaxChunk(int(val)+1, self.chunks[row+1].year_to,
                                                         self.chunks[row+1].tax_rate, self.chunks[row+1].exempt_limit)
                    self.table.blockSignals(True)
                    self.table.item(row+1, 0).setText(str(int(val)+1))
                    self.table.blockSignals(False)
            elif col == 2:
                self.chunks[row] = EquityTaxChunk(c.year_from, c.year_to, val, c.exempt_limit)
            elif col == 3:
                self.chunks[row] = EquityTaxChunk(c.year_from, c.year_to, c.tax_rate, val)

    def _add_chunk(self):
        if self.chunks:
            last = self.chunks[-1]
            new_from = last.year_to + 1
            new_to = min(new_from + 4, 30)
            self.chunks.append(EquityTaxChunk(new_from, new_to, last.tax_rate, last.exempt_limit))
        else:
            self.chunks.append(EquityTaxChunk(1, 5, 0.125, 1.25))
        self._refresh()

    def _remove_last(self):
        if self.chunks:
            self.chunks.pop()
            self.table.removeRow(self.table.rowCount()-1)


class TaxRulesDialog(QDialog):
    """Main dialog for editing all tax rules."""
    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Tax Rules Configuration")
        self.resize(1000, 700)
        self.state = state

        layout = QVBoxLayout(self)

        tabs = QTabWidget()

        # Individual debt
        self.ind_debt_ed = DebtTaxEditor("individual")
        self.ind_debt_ed.set_chunks(state.individual_debt_chunks)
        tabs.addTab(self.ind_debt_ed, "Individual – Debt Fund Tax")

        # Individual equity
        self.ind_eq_ed = EquityTaxEditor()
        self.ind_eq_ed.set_chunks(state.individual_equity_chunks)
        tabs.addTab(self.ind_eq_ed, "Individual – Equity/Arb LTCG")

        # HUF debt
        self.huf_debt_ed = DebtTaxEditor("huf")
        self.huf_debt_ed.set_chunks(state.huf_debt_chunks)
        tabs.addTab(self.huf_debt_ed, "HUF – Debt Fund Tax")

        # HUF equity
        self.huf_eq_ed = EquityTaxEditor()
        self.huf_eq_ed.set_chunks(state.huf_equity_chunks)
        tabs.addTab(self.huf_eq_ed, "HUF – Equity/Arb LTCG")

        layout.addWidget(tabs)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._save_and_accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _save_and_accept(self):
        self.state.individual_debt_chunks = self.ind_debt_ed.get_chunks()
        self.state.individual_equity_chunks = self.ind_eq_ed.get_chunks()
        self.state.huf_debt_chunks = self.huf_debt_ed.get_chunks()
        self.state.huf_equity_chunks = self.huf_eq_ed.get_chunks()
        self.accept()