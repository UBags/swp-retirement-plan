# Copyright (c) 2025 Uddipan Bagchi. All rights reserved.
# See LICENSE in the project root for license information.

"""
Reusable chunk editor: allows editing year-range chunks with continuity enforcement.
"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QHeaderView, QMessageBox, QSpinBox, QDoubleSpinBox,
    QAbstractItemView, QSizePolicy
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from typing import List, Callable, Any


class ChunkTableWidget(QWidget):
    """
    Generic chunk editor. A chunk is a dict with keys including 'year_from' and 'year_to'
    plus any number of editable value fields.
    """
    chunks_changed = Signal()

    def __init__(self, columns: list, make_default_chunk: Callable, parent=None):
        """
        columns: list of (key, label, type, min, max, decimals)
          type: 'int' or 'float'
        make_default_chunk: callable(year_from, year_to) -> dict
        """
        super().__init__(parent)
        self.columns = columns
        self.make_default_chunk = make_default_chunk
        self._data: List[dict] = []

        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Toolbar
        bar = QHBoxLayout()
        self.btn_add = QPushButton("➕ Add Chunk")
        self.btn_del = QPushButton("🗑 Remove Last")
        self.btn_add.clicked.connect(self._add_chunk)
        self.btn_del.clicked.connect(self._remove_last)
        bar.addWidget(self.btn_add)
        bar.addWidget(self.btn_del)
        bar.addStretch()
        layout.addLayout(bar)

        # Table
        col_keys = ["year_from", "year_to"] + [c[0] for c in self.columns]
        col_labels = ["Year From", "Year To"] + [c[1] for c in self.columns]
        self.table = QTableWidget(0, len(col_keys))
        self.table.setHorizontalHeaderLabels(col_labels)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.table)

        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color: #c0392b; font-size: 11px;")
        layout.addWidget(self.lbl_status)

    def set_data(self, chunks: List[dict]):
        self._data = [dict(c) for c in chunks]
        self._refresh_table()

    def get_data(self) -> List[dict]:
        return [dict(c) for c in self._data]

    def _refresh_table(self):
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for row_idx, chunk in enumerate(self._data):
            self._insert_row(row_idx, chunk)
        self.table.blockSignals(False)

    def _insert_row(self, row_idx: int, chunk: dict):
        self.table.insertRow(row_idx)
        all_keys = ["year_from", "year_to"] + [c[0] for c in self.columns]
        for col_idx, key in enumerate(all_keys):
            val = chunk.get(key, 0)
            item = QTableWidgetItem(str(round(val, 6) if isinstance(val, float) else val))
            # Lock year_from (except row 0 year_from) based on continuity
            if key == "year_from" and row_idx > 0:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item.setBackground(QColor("#e8e8e8"))
            elif key == "year_to" and row_idx < len(self._data) - 1:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item.setBackground(QColor("#e8e8e8"))
            self.table.setItem(row_idx, col_idx, item)

    def _add_chunk(self):
        if self._data:
            last = self._data[-1]
            new_from = last["year_to"] + 1
        else:
            new_from = 1
        new_to = min(new_from + 4, 30)
        if new_from > 30:
            QMessageBox.warning(self, "Limit", "Already covers all 30 years.")
            return
        chunk = self.make_default_chunk(new_from, new_to)
        chunk["year_from"] = new_from
        chunk["year_to"] = new_to
        self._data.append(chunk)
        self._refresh_table()
        self._validate()
        self.chunks_changed.emit()

    def _remove_last(self):
        if not self._data:
            return
        self._data.pop()
        self.table.removeRow(self.table.rowCount() - 1)
        self._validate()
        self.chunks_changed.emit()

    def _on_item_changed(self, item):
        row = item.row()
        col = item.column()
        all_keys = ["year_from", "year_to"] + [c[0] for c in self.columns]
        key = all_keys[col]
        try:
            val = float(item.text())
        except ValueError:
            return
        if key in ("year_from", "year_to"):
            val = int(val)

        if row < len(self._data):
            self._data[row][key] = val

            # Enforce continuity: if year_to of row N changes, update year_from of row N+1
            if key == "year_to" and row + 1 < len(self._data):
                self._data[row + 1]["year_from"] = val + 1
                self.table.blockSignals(True)
                idx = all_keys.index("year_from")
                self.table.item(row + 1, idx).setText(str(val + 1))
                self.table.blockSignals(False)

        self._validate()
        self.chunks_changed.emit()

    def _validate(self):
        errors = []
        prev_to = 0
        for i, c in enumerate(self._data):
            yf, yt = c.get("year_from", 0), c.get("year_to", 0)
            if yf < 1 or yt > 30:
                errors.append(f"Row {i+1}: years must be 1-30.")
            if yt < yf:
                errors.append(f"Row {i+1}: Year To < Year From.")
            if i > 0 and yf != prev_to + 1:
                errors.append(f"Row {i+1}: gap/overlap (expected Year From = {prev_to+1}).")
            prev_to = yt

        if errors:
            self.lbl_status.setText(" | ".join(errors))
        else:
            covered = set()
            for c in self._data:
                covered.update(range(c.get("year_from", 0), c.get("year_to", 0) + 1))
            missing = [y for y in range(1, 31) if y not in covered]
            if missing:
                self.lbl_status.setText(f"Uncovered years: {missing[:5]}{'...' if len(missing)>5 else ''}")
            else:
                self.lbl_status.setText("✓ Covers all 30 years")
                self.lbl_status.setStyleSheet("color: #27ae60; font-size: 11px;")
                return
        self.lbl_status.setStyleSheet("color: #c0392b; font-size: 11px;")
