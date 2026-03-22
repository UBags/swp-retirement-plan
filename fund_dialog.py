"""
Fund selection and allocation dialog.
"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QHeaderView, QComboBox, QMessageBox,
    QDialogButtonBox, QAbstractItemView, QDoubleSpinBox, QLineEdit,
    QGroupBox, QSplitter, QWidget, QSizePolicy
)
from PySide6.QtCore import Qt, QSortFilterProxyModel
from PySide6.QtGui import QColor, QFont
from typing import List
import copy

from models import AppState, FundEntry, DEFAULT_FUNDS, SCORE_COLUMNS, _first_available


SORT_OPTIONS = [c[1] for c in SCORE_COLUMNS]
SORT_KEYS    = [c[0] for c in SCORE_COLUMNS]
SORT_DIR     = [c[2] for c in SCORE_COLUMNS]


class FundAllocationDialog(QDialog):
    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.setWindowTitle("View Fund Selection & Allocation")
        self.resize(1200, 700)
        self.state = state

        # Build per-chunk fund lists (or single list if no chunks)
        if state.allocation_chunks:
            self._chunks = state.allocation_chunks          # List[AllocationChunk]
            self._chunk_idx = 0                             # currently displayed
            # Work on deep copies so cancel works
            self._chunk_funds = [
                [copy.deepcopy(f) for f in ac.funds]
                for ac in self._chunks
            ]
            self.funds = self._chunk_funds[0]
        else:
            self._chunks = []
            self._chunk_idx = 0
            self.funds: List[FundEntry] = [copy.deepcopy(f) for f in state.funds]
            self._chunk_funds = [self.funds]

        self._build_ui()
        self._populate()

    def _build_ui(self):
        main = QVBoxLayout(self)

        # ── Chunk selector (only shown when allocation_chunks exist) ──────────
        if self._chunks:
            chunk_bar = QHBoxLayout()
            chunk_bar.addWidget(QLabel("<b>Viewing chunk:</b>"))
            self.combo_chunk = QComboBox()
            for ac in self._chunks:
                self.combo_chunk.addItem(
                    f"Chunk {self._chunks.index(ac)+1}  "
                    f"(Years {ac.year_from}–{ac.year_to})")
            self.combo_chunk.currentIndexChanged.connect(self._on_chunk_changed)
            chunk_bar.addWidget(self.combo_chunk)
            self.lbl_chunk_yield = QLabel("")
            self.lbl_chunk_yield.setStyleSheet("color:#2980b9; font-weight:bold;")
            chunk_bar.addWidget(self.lbl_chunk_yield)
            chunk_bar.addStretch()
            main.addLayout(chunk_bar)
            self._update_chunk_yield_label()

        # Sort controls
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("Sort by:"))
        self.combo_sort = QComboBox()
        self.combo_sort.addItems(SORT_OPTIONS)
        self.combo_sort.currentIndexChanged.connect(self._sort)
        ctrl.addWidget(self.combo_sort)

        ctrl.addWidget(QLabel("Filter type:"))
        self.combo_type = QComboBox()
        self.combo_type.addItems(["All", "Debt", "Equity", "Other"])
        self.combo_type.currentIndexChanged.connect(self._filter)
        ctrl.addWidget(self.combo_type)

        ctrl.addStretch()

        lbl_total = QLabel("Total allocated (L):")
        self.lbl_total_val = QLabel("0")
        self.lbl_total_val.setFont(QFont("", 12, QFont.Weight.Bold))
        ctrl.addWidget(lbl_total)
        ctrl.addWidget(self.lbl_total_val)

        main.addLayout(ctrl)

        # Table
        all_cols = ["Name", "Type", "Sub-Category", "Allocation (L)", "Weight %",
                    "Comb Ratio", "Std Dev", "Sharpe", "Sortino",
                    "Calmar", "Alpha", "Treynor", "Max DD", "Beta",
                    "1Y%", "3Y%", "5Y%", "10Y%"]
        self.table = QTableWidget(0, len(all_cols))
        self.table.setHorizontalHeaderLabels(all_cols)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in range(1, len(all_cols)):
            self.table.horizontalHeader().setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.itemChanged.connect(self._on_alloc_changed)
        main.addWidget(self.table)

        # Note
        note = QLabel("💡 Edit the 'Allocation (L)' column directly. "
                      "'Weight %' = fund allocation as % of chunk total.  "
                      "Row colours: 🟢 in all chunks | 🟠 some chunks | 🔴 one chunk only.")
        note.setStyleSheet("color: #555; font-size: 11px;")
        main.addWidget(note)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._save_and_accept)
        btns.rejected.connect(self.reject)
        main.addWidget(btns)

        self._row_fund_map: List[int] = []   # row -> index in self.funds

    def _populate(self):
        self._filter()

    def _filter(self):
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self._row_fund_map = []
        type_filter = self.combo_type.currentText().lower()

        sort_key = SORT_KEYS[self.combo_sort.currentIndex()]
        sort_dir = SORT_DIR[self.combo_sort.currentIndex()]

        # Filter
        candidates = [(i, f) for i, f in enumerate(self.funds)
                      if type_filter == "all" or f.fund_type == type_filter]
        # Sort
        def sortval(pair):
            val = getattr(pair[1], sort_key, None)
            return val if val is not None else -1e9
        candidates.sort(key=sortval, reverse=(sort_dir == "higher"))

        # ── Cross-chunk fund presence map (for color coding) ─────────────
        # For each fund name, count how many chunks it appears in (alloc > 0)
        num_chunks = len(self._chunk_funds) if self._chunks else 1
        fund_chunk_count = {}
        if num_chunks > 1:
            for chunk_funds in self._chunk_funds:
                for f in chunk_funds:
                    if f.allocation > 0:
                        fund_chunk_count[f.name] = fund_chunk_count.get(f.name, 0) + 1

        # Total allocation for this chunk (for weight % calculation)
        total_alloc = sum(f.allocation for f in self.funds if f.allocation > 0)

        for orig_idx, fund in candidates:
            r = self.table.rowCount()
            self.table.insertRow(r)
            self._row_fund_map.append(orig_idx)

            # Opt. Weight % = fund's allocation as a percentage of chunk total
            opt_weight_pct = ""
            if fund.allocation > 0 and total_alloc > 0:
                opt_weight_pct = f"{fund.allocation / total_alloc * 100:.2f}%"

            # AMFI sub-category: show a short label (strip "Debt Scheme - " etc.)
            amfi_sub = fund.amfi_fund_type or ""
            if " - " in amfi_sub:
                amfi_sub = amfi_sub.split(" - ", 1)[1]

            cols = [
                fund.name, fund.fund_type, amfi_sub,
                str(fund.allocation),
                opt_weight_pct,                          # col 4: Opt. Weight %
                f"{fund.combined_ratio:.3f}",
                f"{fund.std_dev:.2f}", f"{fund.sharpe:.2f}", f"{fund.sortino:.2f}",
                f"{fund.calmar:.2f}", f"{fund.alpha:.2f}", f"{fund.treynor:.2f}",
                f"{fund.max_dd:.3f}", f"{fund.beta:.2f}",
                f"{fund.cagr_1:.2f}%" if fund.cagr_1 else "–",
                f"{fund.cagr_3:.2f}%" if fund.cagr_3 else "–",
                f"{fund.cagr_5:.2f}%" if fund.cagr_5 else "–",
                f"{fund.cagr_10:.2f}%" if fund.cagr_10 else "–",
            ]

            # ── Determine row background color ───────────────────────────
            # Default: type-based coloring (equity=light blue, other=light orange)
            # Override if multi-chunk: green=all chunks, orange=some, pink=one only
            row_bg = None
            if num_chunks > 1 and fund.allocation > 0:
                n = fund_chunk_count.get(fund.name, 0)
                if n >= num_chunks:
                    row_bg = QColor("#d5f5e3")    # light green — in ALL chunks
                elif n > 1:
                    row_bg = QColor("#fdebd0")    # light orange — in SOME chunks
                else:
                    row_bg = QColor("#fadbd8")    # light pink — in ONE chunk only

            for col_idx, val in enumerate(cols):
                item = QTableWidgetItem(val)
                if col_idx != 3:   # only allocation (col 3) is editable
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if col_idx == 4 and opt_weight_pct:
                    # highlight optimized weight column
                    item.setForeground(QColor("#2980b9"))
                # Apply row background
                if row_bg is not None:
                    item.setBackground(row_bg)
                elif fund.fund_type == "equity":
                    item.setBackground(QColor("#e8f4fd"))
                elif fund.fund_type == "other":
                    item.setBackground(QColor("#fdf5e6"))
                if fund.allocation > 0:
                    item.setFont(QFont("", -1, QFont.Weight.Bold))
                self.table.setItem(r, col_idx, item)

        self.table.blockSignals(False)
        self._update_total()

    def _sort(self):
        self._filter()

    def _on_alloc_changed(self, item):
        if item.column() != 3:
            return
        row = item.row()
        if row >= len(self._row_fund_map):
            return
        orig_idx = self._row_fund_map[row]
        try:
            val = float(item.text())
            self.funds[orig_idx].allocation = max(0.0, val)
        except ValueError:
            pass
        self._update_total()

    def _update_total(self):
        total = sum(f.allocation for f in self.funds)
        self.lbl_total_val.setText(f"₹ {total:.1f} L")
        debt = sum(f.allocation for f in self.funds if f.fund_type == "debt")
        eq = sum(f.allocation for f in self.funds if f.fund_type == "equity")
        oth = sum(f.allocation for f in self.funds if f.fund_type == "other")
        self.lbl_total_val.setToolTip(f"Debt: {debt:.1f}L | Equity: {eq:.1f}L | Other: {oth:.1f}L")
        if self._chunks:
            self._update_chunk_yield_label()

    def _on_chunk_changed(self, idx: int):
        """Switch displayed fund list to the selected chunk."""
        # Save current edits back into _chunk_funds before switching
        self._chunk_funds[self._chunk_idx] = [
            copy.deepcopy(f) for f in self.funds
        ]
        self._chunk_idx = idx
        self.funds = self._chunk_funds[idx]
        self._update_chunk_yield_label()
        self._filter()

    def _update_chunk_yield_label(self):
        if not self._chunks:
            return
        ac    = self._chunks[self._chunk_idx]
        funds = self._chunk_funds[self._chunk_idx]

        # Compute portfolio yield from current edits
        total = weight = 0.0
        wtd_std = wtd_dd = 0.0
        for f in funds:
            if f.allocation > 0:
                cagr = _first_available(f.cagr_5, f.cagr_3, f.cagr_1, default=7.0)
                total  += cagr * f.allocation
                weight += f.allocation
                # Weighted std_dev and max_dd
                wtd_std += (f.std_dev or 0.0) * f.allocation
                wtd_dd  += abs(f.max_dd or 0.0) * f.allocation
        yld   = (total / weight) if weight > 0 else 0.0
        port_std = (wtd_std / weight) if weight > 0 else 0.0
        port_dd  = (wtd_dd / weight) if weight > 0 else 0.0
        alloc = sum(f.allocation for f in funds if f.allocation > 0)

        # ── Aim-pass type ratios (_type_ratios set by run_aim_pass) ──────────
        tr     = getattr(ac, "_type_ratios", {})
        eq_pct = tr.get("equity", 0.0) * 100
        dt_pct = tr.get("debt",   0.0) * 100
        ot_pct = tr.get("other",  0.0) * 100
        if tr:
            ratio_str = (
                f"  │  D:{dt_pct:.0f}% E:{eq_pct:.0f}%"
                + (f" O:{ot_pct:.0f}%" if ot_pct > 0.5 else "")
            )
        else:
            ratio_str = ""

        # ── Portfolio risk metrics ───────────────────────────────────────────
        # std_dev is already in % (e.g. 1.91); max_dd is decimal fraction
        # (e.g. 0.021 = 2.1%), so multiply by 100 for display.
        risk_str = f"  │  Std:{port_std:.2f}%  |DD|:{port_dd * 100:.2f}%"

        # ── Constraint slack (constraint_slack_used set by run_track_pass) ───
        sl = getattr(ac, "constraint_slack_used", {})
        slack_parts = []
        if sl.get("return",  0.0) > 1e-5:
            slack_parts.append(f"ret↓{sl['return']*100:.2f}pp")
        if sl.get("std_dev", 0.0) > 1e-5:
            slack_parts.append(f"std↑{sl['std_dev']*100:.2f}pp")
        if sl.get("max_dd",  0.0) > 1e-5:
            slack_parts.append(f"dd↑{sl['max_dd']*100:.2f}pp")
        slack_str = ("  ⚠ Slack: " + ", ".join(slack_parts)) if slack_parts else ""

        self.lbl_chunk_yield.setText(
            f"  ₹{alloc:.1f} L  |  Yield: {yld:.2f}%{ratio_str}{risk_str}{slack_str}"
        )

        # Colour: orange if slack consumed, blue if optimised, grey if not yet run
        if slack_parts:
            self.lbl_chunk_yield.setStyleSheet(
                "color:#e67e22; font-weight:bold; font-size:11px;")
        elif tr:
            self.lbl_chunk_yield.setStyleSheet(
                "color:#2980b9; font-weight:bold; font-size:11px;")
        else:
            self.lbl_chunk_yield.setStyleSheet(
                "color:#2980b9; font-weight:bold;")

    def _save_and_accept(self):
        # Save current chunk edits
        self._chunk_funds[self._chunk_idx] = [
            copy.deepcopy(f) for f in self.funds
        ]

        if self._chunks:
            # Update allocation_chunks in state
            for i, ac in enumerate(self.state.allocation_chunks):
                ac.funds = [copy.deepcopy(f) for f in self._chunk_funds[i]]
            # Also update flat funds list to match the last (most recent) chunk
            last_funds = self._chunk_funds[-1]
            self.state.funds = [copy.deepcopy(f) for f in last_funds]
        else:
            self.state.funds = [copy.deepcopy(f) for f in self.funds]

        # Update FD rate from portfolio yield (legacy scalar, for backward compat)
        self.state.fd_rate = self.state.portfolio_yield()
        # Note: FD rate chunks are now user-managed separately
        self.accept()