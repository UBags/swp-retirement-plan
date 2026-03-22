"""
optimization_report.py — Post-optimization summary dialog for SWP Planner.

Shows a rich multi-tab report after optimize_sticky_portfolio() runs:
  Tab 1 — Glide Path Summary
           • Per-chunk asset-class ratios (from _type_ratios set by Aim pass)
           • Constraint slack consumed (from constraint_slack_used set by Track pass)
           • Year-by-year equity/debt/other weights as a stacked chart
  Tab 2 — Fund Selection Per Chunk
           • Which funds were chosen in each chunk
           • Their weights, type, CAGRs, and whether they carry over from prior chunk
           • Turnover (weight change) between consecutive chunks
  Tab 3 — Tax Attribution Breakdown (populated after Run Calculations)
           • Per-year: regular SWP tax, rebalancing tax, exit loads
           • Lifetime totals and percentages
  Tab 4 — Robustness Indicators
           • Constraint slack consumed per chunk (visual traffic-light)
           • Drift tolerance events (years where rebalance was skipped)
"""

from __future__ import annotations
from typing import List, Optional, Dict, TYPE_CHECKING

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QWidget,
    QTableWidget, QTableWidgetItem, QHeaderView, QLabel,
    QPushButton, QScrollArea, QGroupBox, QSizePolicy,
    QAbstractItemView, QFrame, QGridLayout
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from models import _first_available

if TYPE_CHECKING:
    from models import AppState, GlidePath
    from engine import YearSummary


# ─── Palette ──────────────────────────────────────────────────────────────────
CLR_DEBT     = QColor("#ddeeff")
CLR_EQUITY   = QColor("#dff0d8")
CLR_OTHER    = QColor("#fef3e2")
CLR_WARN     = QColor("#fff3cd")
CLR_DANGER   = QColor("#fdecea")
CLR_OK       = QColor("#eafaf1")
CLR_NEW_FUND = QColor("#f5eaff")   # purple tint: fund newly introduced in this chunk
CLR_CARRY    = QColor("#eafaf1")   # green tint:  fund carried over from prior chunk


def _ro(text: str, bg: QColor = None, bold: bool = False,
        fg: QColor = None) -> QTableWidgetItem:
    item = QTableWidgetItem(str(text))
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    if bg:
        item.setBackground(bg)
    if bold:
        f = QFont(); f.setBold(True); item.setFont(f)
    if fg:
        item.setForeground(fg)
    return item


def _hdr(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet("font-weight:bold; font-size:13px; color:#2c3e50; "
                      "border-bottom:2px solid #bdc3c7; padding-bottom:4px;")
    return lbl


def _kpi_box(title: str, value: str, subtitle: str = "",
             color: str = "#2980b9") -> QFrame:
    """Small KPI tile: colored border, bold value."""
    frame = QFrame()
    frame.setFrameShape(QFrame.Shape.StyledPanel)
    frame.setStyleSheet(f"QFrame {{ border:2px solid {color}; border-radius:6px; "
                        f"background:#fff; padding:6px; }}")
    lay = QVBoxLayout(frame)
    lay.setSpacing(2)
    t = QLabel(title)
    t.setStyleSheet("font-size:10px; color:#7f8c8d; font-weight:bold;")
    v = QLabel(value)
    v.setStyleSheet(f"font-size:18px; font-weight:bold; color:{color};")
    lay.addWidget(t)
    lay.addWidget(v)
    if subtitle:
        s = QLabel(subtitle)
        s.setStyleSheet("font-size:10px; color:#95a5a6;")
        lay.addWidget(s)
    return frame


class OptimizationReportDialog(QDialog):
    """
    Rich multi-tab post-optimization report.

    Parameters
    ----------
    state       : AppState after optimize_sticky_portfolio() has run
    yearly_rows : Optional list[YearSummary] from the most recent engine run
                  (needed to populate Tax Attribution tab).  Pass None to
                  show the tab with a "Run Calculations first" placeholder.
    parent      : parent widget
    """

    def __init__(self, state: 'AppState', yearly_rows=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Optimization Report")
        self.resize(1100, 740)
        self.state = state
        self.yearly_rows: Optional[List] = yearly_rows
        self.gp = getattr(state, "glide_path", None)

        self._build_ui()

    # ──────────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        main = QVBoxLayout(self)

        # Header KPI strip
        main.addWidget(_hdr("Optimization Report — Aim & Track Two-Pass Portfolio"))
        kpi_row = QHBoxLayout()
        chunks  = self.state.allocation_chunks or []
        n_trans = len(self.gp.transition_years()) if self.gp else 0
        mode    = getattr(self.state, "allocation_mode", "singular")
        spread  = getattr(self.state, "rebalance_spread_years", 4)

        kpi_row.addWidget(_kpi_box("Chunks", str(len(chunks)), "allocation periods", "#2980b9"))
        kpi_row.addWidget(_kpi_box("Mode",
                                   "Singular" if mode == "singular" else "Sticky",
                                   "optimization mode", "#8e44ad"))
        kpi_row.addWidget(_kpi_box("Transition Years", str(n_trans),
                                   f"glide spread = {spread}yr", "#27ae60"))
        flat = self.gp.is_flat() if self.gp else True
        kpi_row.addWidget(_kpi_box("Glide Path",
                                   "Flat" if flat else "Dynamic",
                                   "weight schedule", "#e67e22"))
        kpi_row.addStretch()
        main.addLayout(kpi_row)

        # Tab widget
        tabs = QTabWidget()
        tabs.addTab(self._build_glide_summary_tab(),   "🎯  Glide Path Summary")
        tabs.addTab(self._build_fund_selection_tab(),  "📋  Fund Selection")
        tabs.addTab(self._build_tax_attribution_tab(), "💸  Tax Attribution")
        tabs.addTab(self._build_robustness_tab(),      "🛡  Robustness")
        main.addWidget(tabs)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        close_btn.setFixedWidth(100)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        main.addLayout(btn_row)

    # ── Tab 1: Glide Path Summary ─────────────────────────────────────────────

    def _build_glide_summary_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        chunks = self.state.allocation_chunks or []
        if not chunks:
            lay.addWidget(QLabel("No allocation chunks defined."))
            return w

        # Per-chunk summary table
        lay.addWidget(_hdr("Per-Chunk Asset Allocation & Optimizer Results"))
        note = QLabel(
            "  Aim pass sets the sacred equity/debt/other ratios (Pass 1).  "
            "Track pass preserves them while minimising fund turnover (Pass 2).  "
            "Slack = how much a constraint was relaxed to find a solution."
        )
        note.setStyleSheet("color:#555; font-size:11px;")
        note.setWordWrap(True)
        lay.addWidget(note)

        cols = ["Chunk", "Years", "Equity %", "Debt %", "Other %",
                "Return Target", "Std Dev Limit", "Max DD Limit",
                "Ret Slack Used", "Std Slack Used", "DD Slack Used",
                "Funds Selected"]
        tbl = QTableWidget(len(chunks), len(cols))
        tbl.setHorizontalHeaderLabels(cols)
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        tbl.setAlternatingRowColors(True)
        tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

        for i, c in enumerate(chunks):
            tr = getattr(c, "_type_ratios", {})
            sl = getattr(c, "constraint_slack_used", {})
            eq_pct  = tr.get("equity", 0.0) * 100
            dt_pct  = tr.get("debt",   0.0) * 100
            ot_pct  = tr.get("other",  0.0) * 100
            r_slack = sl.get("return",  0.0) * 100
            s_slack = sl.get("std_dev", 0.0) * 100
            d_slack = sl.get("max_dd",  0.0) * 100
            n_funds = len([v for v in (c.target_weights or {}).values() if v > 1e-5])

            def _slack_item(v: float) -> QTableWidgetItem:
                txt = f"{v:.3f}pp" if v > 1e-5 else "—"
                bg  = CLR_DANGER if v > 0.3 else (CLR_WARN if v > 0.1 else CLR_OK)
                return _ro(txt, bg)

            tbl.setItem(i, 0,  _ro(f"Chunk {i+1}", bold=True))
            tbl.setItem(i, 1,  _ro(f"Yr {c.year_from}–{c.year_to}"))

            eq_item = _ro(f"{eq_pct:.1f}%", CLR_EQUITY if eq_pct >= 40 else None)
            dt_item = _ro(f"{dt_pct:.1f}%", CLR_DEBT   if dt_pct >= 40 else None)
            ot_item = _ro(f"{ot_pct:.1f}%", CLR_OTHER  if ot_pct > 5  else None)
            tbl.setItem(i, 2, eq_item)
            tbl.setItem(i, 3, dt_item)
            tbl.setItem(i, 4, ot_item)

            r_tgt = getattr(c, "min_return",  0.0) * 100
            s_lim = getattr(c, "max_std_dev", 0.0) * 100
            d_lim = getattr(c, "max_dd",      0.0) * 100
            tbl.setItem(i, 5, _ro(f"{r_tgt:.2f}%"))
            tbl.setItem(i, 6, _ro(f"{s_lim:.2f}%"))
            tbl.setItem(i, 7, _ro(f"{d_lim:.2f}%"))
            tbl.setItem(i, 8,  _slack_item(r_slack))
            tbl.setItem(i, 9,  _slack_item(s_slack))
            tbl.setItem(i, 10, _slack_item(d_slack))
            tbl.setItem(i, 11, _ro(str(n_funds)))

        lay.addWidget(tbl)

        # Glide path year-by-year chart (matplotlib embedded)
        if self.gp and not self.gp.is_flat():
            lay.addSpacing(12)
            lay.addWidget(_hdr("Year-by-Year Weight Schedule"))
            try:
                lay.addWidget(self._build_glide_chart())
            except Exception as e:
                lay.addWidget(QLabel(f"(Chart unavailable: {e})"))
        else:
            lay.addSpacing(8)
            lay.addWidget(QLabel("  Flat glide path — same weights every year."))

        return w

    def _build_glide_chart(self) -> QWidget:
        """Stacked area chart of equity/debt/other % per year."""
        import matplotlib
        matplotlib.use("QtAgg")
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure

        gp     = self.gp
        chunks = self.state.allocation_chunks or []
        years  = list(range(1, 31))

        # Build fund_type lookup
        fund_types: Dict[str, str] = {}
        for c in chunks:
            for f in c.funds:
                fund_types[f.name] = f.fund_type

        eq_pct = []; dt_pct = []; ot_pct = []
        for y in years:
            w = gp.weights_for_year(y)
            eq = sum(v for k, v in w.items() if fund_types.get(k) == "equity") * 100
            dt = sum(v for k, v in w.items() if fund_types.get(k) == "debt")   * 100
            ot = sum(v for k, v in w.items() if fund_types.get(k) == "other")  * 100
            eq_pct.append(eq); dt_pct.append(dt); ot_pct.append(ot)

        fig = Figure(figsize=(11, 3.2), tight_layout=True)
        ax  = fig.add_subplot(111)
        ax.stackplot(years,
                     [eq_pct, dt_pct, ot_pct],
                     labels=["Equity", "Debt", "Other"],
                     colors=["#27ae60", "#2980b9", "#e67e22"],
                     alpha=0.80)
        ax.set_xlim(1, 30)
        ax.set_ylim(0, 100)
        ax.set_xlabel("FY Year", fontsize=9)
        ax.set_ylabel("Portfolio Weight %", fontsize=9)
        ax.set_title("Glide Path — Asset Class Weights Over Time", fontsize=11)
        ax.legend(loc="upper right", fontsize=8)
        ax.grid(axis="y", alpha=0.3)

        # Mark chunk boundaries
        for c in chunks[1:]:
            ax.axvline(x=c.year_from, color="#e74c3c", linestyle="--",
                       linewidth=0.8, alpha=0.6)

        canvas = FigureCanvasQTAgg(fig)
        canvas.setMinimumHeight(240)
        return canvas

    # ── Tab 2: Fund Selection ─────────────────────────────────────────────────

    def _build_fund_selection_tab(self) -> QWidget:
        w   = QWidget()
        lay = QVBoxLayout(w)
        chunks = self.state.allocation_chunks or []

        if not chunks:
            lay.addWidget(QLabel("No allocation chunks defined."))
            return w

        lay.addWidget(_hdr("Fund Selection Per Chunk"))
        note = QLabel(
            "  🟢 = fund carried over from previous chunk (zero turnover).  "
            "  🟣 = fund newly introduced in this chunk.  "
            "  Δ Weight = change vs prior chunk (for fund in both)."
        )
        note.setStyleSheet("color:#555; font-size:11px;")
        note.setWordWrap(True)
        lay.addWidget(note)

        # Build set of fund weights per chunk for turnover calculation
        chunk_weights: List[Dict[str, float]] = [
            dict(getattr(c, "target_weights", {})) for c in chunks
        ]

        for ci, c in enumerate(chunks):
            prior_w = chunk_weights[ci - 1] if ci > 0 else {}
            curr_w  = chunk_weights[ci]

            grp = QGroupBox(
                f"Chunk {ci+1}  (Years {c.year_from}–{c.year_to})  "
                f"│  {len(curr_w)} funds selected"
            )
            grp.setStyleSheet(
                "QGroupBox { font-weight:bold; border:1px solid #bdc3c7; "
                "border-radius:4px; margin-top:8px; padding:6px; }"
            )
            g_lay = QVBoxLayout(grp)

            # Turnover summary for this chunk vs prior
            if ci > 0:
                all_names = set(curr_w) | set(prior_w)
                turnover  = sum(abs(curr_w.get(n, 0.0) - prior_w.get(n, 0.0))
                                for n in all_names) * 100
                new_funds = len([n for n in curr_w if n not in prior_w and curr_w[n] > 1e-5])
                exit_funds= len([n for n in prior_w if n not in curr_w and prior_w[n] > 1e-5])
                t_lbl = QLabel(
                    f"  Turnover vs Chunk {ci}: {turnover:.1f}pp  │  "
                    f"{new_funds} new fund(s)  │  {exit_funds} exited"
                )
                t_lbl.setStyleSheet(
                    "color:#c0392b; font-weight:bold;" if turnover > 20
                    else "color:#27ae60; font-weight:bold;"
                )
                g_lay.addWidget(t_lbl)

            # Build fund table for this chunk
            cols = ["Fund Name", "Type", "Weight %", "Δ vs Prior",
                    "CAGR 5Y", "Std Dev", "Sharpe", "Status"]
            tbl = QTableWidget(len(curr_w), len(cols))
            tbl.setHorizontalHeaderLabels(cols)
            tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            for ci2 in range(1, len(cols)):
                tbl.horizontalHeader().setSectionResizeMode(
                    ci2, QHeaderView.ResizeMode.ResizeToContents)
            tbl.setAlternatingRowColors(True)
            tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            tbl.setMaximumHeight(220)

            # Build fund metadata lookup
            fund_meta: Dict[str, object] = {}
            for chunk in chunks:
                for f in chunk.funds:
                    fund_meta[f.name] = f

            for ri, (fn, wt) in enumerate(
                    sorted(curr_w.items(), key=lambda x: -x[1])):
                f    = fund_meta.get(fn)
                prev = prior_w.get(fn, 0.0)
                delta= (wt - prev) * 100
                is_new   = fn not in prior_w or prior_w[fn] < 1e-5
                bg = CLR_NEW_FUND if is_new else CLR_CARRY

                tbl.setItem(ri, 0, _ro(fn, bg))
                ftype = f.fund_type if f else "—"
                ftype_bg = (CLR_EQUITY if ftype == "equity"
                            else CLR_DEBT if ftype == "debt" else CLR_OTHER)
                tbl.setItem(ri, 1, _ro(ftype, ftype_bg))
                tbl.setItem(ri, 2, _ro(f"{wt*100:.2f}%", bg, bold=True))

                if ci == 0:
                    tbl.setItem(ri, 3, _ro("—"))
                else:
                    delta_item = _ro(f"{delta:+.2f}pp",
                                     fg=QColor("#27ae60") if delta >= 0
                                     else QColor("#c0392b"))
                    tbl.setItem(ri, 3, delta_item)

                cagr = _first_available(f.cagr_5, f.cagr_3, f.cagr_1, default=None) if f else None
                tbl.setItem(ri, 4, _ro(f"{cagr:.1f}%" if cagr else "—"))
                tbl.setItem(ri, 5, _ro(f"{f.std_dev:.3f}" if f else "—"))
                tbl.setItem(ri, 6, _ro(f"{f.sharpe:.2f}" if f else "—"))
                tbl.setItem(ri, 7, _ro("🆕 New" if is_new else "✅ Carry",
                                       CLR_NEW_FUND if is_new else CLR_CARRY))

            g_lay.addWidget(tbl)
            lay.addWidget(grp)

        # Wrap in scroll area
        scroll_content = QWidget()
        scroll_content.setLayout(lay)
        scroll = QScrollArea()
        scroll.setWidget(scroll_content)
        scroll.setWidgetResizable(True)

        outer = QWidget()
        QVBoxLayout(outer).addWidget(scroll)
        return outer

    # ── Tab 3: Tax Attribution ────────────────────────────────────────────────

    def _build_tax_attribution_tab(self) -> QWidget:
        w   = QWidget()
        lay = QVBoxLayout(w)

        if not self.yearly_rows:
            lay.addWidget(_hdr("Tax Attribution — Run Calculations First"))
            msg = QLabel(
                "This tab shows the per-year tax breakdown between:\n"
                "  • Regular SWP tax  (paid by user, deducted from net income)\n"
                "  • Rebalancing tax  (self-funded by portfolio — never hits your income)\n"
                "  • Exit loads       (also portfolio-internal)\n\n"
                "Click Run Calculations on the main window, then re-open this report."
            )
            msg.setStyleSheet("color:#555; padding:20px;")
            msg.setWordWrap(True)
            lay.addWidget(msg)
            return w

        lay.addWidget(_hdr("Tax Attribution — SWP Tax vs Rebalancing Tax"))

        note = QLabel(
            "  Rebalancing tax is self-funded by the portfolio (v7 Bug #1 fix) — "
            "it never reduces your spendable income.  Only SWP tax does."
        )
        note.setStyleSheet("color:#555; font-size:11px;")
        note.setWordWrap(True)
        lay.addWidget(note)

        # KPI strip
        total_swp_tax  = sum(max(0, r.tax_personal - r.rebalance_tax_paid)
                             for r in self.yearly_rows)
        total_rebal    = sum(r.rebalance_tax_paid for r in self.yearly_rows)
        total_loads    = sum(getattr(r, "rebalance_exit_loads", 0.0)
                             for r in self.yearly_rows)
        total_saved    = sum(r.tax_saved for r in self.yearly_rows)

        kpi = QHBoxLayout()
        kpi.addWidget(_kpi_box("SWP Tax (30yr)", f"₹{total_swp_tax:.1f}L",
                               "deducted from income", "#c0392b"))
        kpi.addWidget(_kpi_box("Rebalance Tax (30yr)", f"₹{total_rebal:.1f}L",
                               "portfolio-self-funded", "#e67e22"))
        kpi.addWidget(_kpi_box("Exit Loads (30yr)", f"₹{total_loads:.2f}L",
                               "portfolio-internal", "#8e44ad"))
        kpi.addWidget(_kpi_box("Tax Saved vs FD (30yr)", f"₹{total_saved:.1f}L",
                               "vs full FD benchmark", "#27ae60"))
        kpi.addStretch()
        lay.addLayout(kpi)

        # Per-year table
        cols = ["FY", "SWP Tax (₹L)", "Rebal Tax (₹L)",
                "Exit Loads (₹L)", "Total Tax (₹L)", "Tax Saved (₹L)",
                "Rebal % of Total Tax", "Net Cash (₹L)"]
        tbl = QTableWidget(len(self.yearly_rows), len(cols))
        tbl.setHorizontalHeaderLabels(cols)
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        tbl.setAlternatingRowColors(True)
        tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

        for i, r in enumerate(self.yearly_rows):
            swp_tax   = max(0.0, r.tax_personal - r.rebalance_tax_paid)
            rebal_tax = r.rebalance_tax_paid
            loads     = getattr(r, "rebalance_exit_loads", 0.0)
            total_tax = r.tax_personal
            saved     = r.tax_saved
            rebal_pct = 100 * rebal_tax / max(total_tax, 1e-9) if total_tax > 0 else 0.0
            net_cash  = r.net_cash_personal

            tbl.setItem(i, 0, _ro(str(r.year)))
            tbl.setItem(i, 1, _ro(f"{swp_tax:.3f}",
                                  CLR_WARN if swp_tax > 2.0 else None))
            tbl.setItem(i, 2, _ro(f"{rebal_tax:.3f}" if rebal_tax > 1e-5 else "—",
                                  CLR_WARN if rebal_tax > 0 else None))
            tbl.setItem(i, 3, _ro(f"{loads:.3f}" if loads > 1e-5 else "—"))
            tbl.setItem(i, 4, _ro(f"{total_tax:.3f}"))
            tbl.setItem(i, 5, _ro(f"{saved:.3f}", CLR_OK if saved > 0 else None,
                                  fg=QColor("#27ae60") if saved > 0 else None))
            rp_item = _ro(f"{rebal_pct:.1f}%",
                          CLR_DANGER if rebal_pct > 30 else
                          (CLR_WARN if rebal_pct > 10 else None))
            tbl.setItem(i, 6, rp_item)
            tbl.setItem(i, 7, _ro(f"{net_cash:.2f}",
                                  CLR_DANGER if net_cash < 0 else None))

        lay.addWidget(tbl)
        return w

    # ── Tab 4: Robustness ─────────────────────────────────────────────────────

    def _build_robustness_tab(self) -> QWidget:
        w   = QWidget()
        lay = QVBoxLayout(w)

        lay.addWidget(_hdr("Robustness Indicators"))

        chunks = self.state.allocation_chunks or []

        # ── Constraint slack traffic light ────────────────────────────────────
        lay.addWidget(QLabel("<b>Constraint Slack per Chunk</b>  "
                             "(green = no slack needed, yellow = minor, red = significant)"))
        slack_note = QLabel(
            "  Slack consumed during Track pass.  If a chunk shows red slack, "
            "the optimizer had to relax its risk/return constraints to achieve "
            "fund stickiness.  Consider loosening that chunk's constraints manually."
        )
        slack_note.setStyleSheet("color:#555; font-size:11px;")
        slack_note.setWordWrap(True)
        lay.addWidget(slack_note)

        grid = QGridLayout()
        grid.addWidget(QLabel("<b>Chunk</b>"),       0, 0)
        grid.addWidget(QLabel("<b>Return Slack</b>"), 0, 1)
        grid.addWidget(QLabel("<b>Std Slack</b>"),    0, 2)
        grid.addWidget(QLabel("<b>DD Slack</b>"),     0, 3)
        grid.addWidget(QLabel("<b>Status</b>"),       0, 4)

        for i, c in enumerate(chunks):
            sl = getattr(c, "constraint_slack_used", {})
            rs = sl.get("return",  0.0) * 100
            ss = sl.get("std_dev", 0.0) * 100
            ds = sl.get("max_dd",  0.0) * 100
            max_slack = max(rs, ss, ds)
            status = ("🔴 Significant" if max_slack > 0.3
                      else "🟡 Minor" if max_slack > 0.1
                      else "🟢 Clean")

            def _sl_lbl(v: float) -> QLabel:
                col = ("#c0392b" if v > 0.3 else "#e67e22" if v > 0.1 else "#27ae60")
                txt = f"{v:.3f}pp" if v > 1e-5 else "—"
                l = QLabel(txt)
                l.setStyleSheet(f"color:{col}; font-weight:bold; padding:4px;")
                return l

            row = i + 1
            grid.addWidget(QLabel(f"Chunk {i+1}  (Yr {c.year_from}–{c.year_to})"), row, 0)
            grid.addWidget(_sl_lbl(rs), row, 1)
            grid.addWidget(_sl_lbl(ss), row, 2)
            grid.addWidget(_sl_lbl(ds), row, 3)
            grid.addWidget(QLabel(status), row, 4)

        lay.addLayout(grid)
        lay.addSpacing(16)

        # ── Fund continuity matrix ────────────────────────────────────────────
        if len(chunks) > 1:
            lay.addWidget(QLabel("<b>Fund Continuity Matrix</b>  "
                                 "(% of chunk's weight carried over from prior chunk)"))

            continuity_note = QLabel(
                "  Goal: maximise green (high continuity = low real-world turnover).  "
                "100% = every fund in this chunk was already in the prior chunk."
            )
            continuity_note.setStyleSheet("color:#555; font-size:11px;")
            continuity_note.setWordWrap(True)
            lay.addWidget(continuity_note)

            cont_cols = ["Chunk", "Funds Carried Over", "New Funds",
                         "Weight Continuity %", "Rating"]
            cont_tbl  = QTableWidget(len(chunks), len(cont_cols))
            cont_tbl.setHorizontalHeaderLabels(cont_cols)
            cont_tbl.horizontalHeader().setSectionResizeMode(
                QHeaderView.ResizeMode.ResizeToContents)
            cont_tbl.setAlternatingRowColors(True)
            cont_tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            cont_tbl.setMaximumHeight(200)

            for i, c in enumerate(chunks):
                curr = getattr(c, "target_weights", {})
                prev = getattr(chunks[i-1], "target_weights", {}) if i > 0 else {}
                carried     = sum(v for n, v in curr.items()
                                  if n in prev and prev[n] > 1e-5) * 100
                new_w       = sum(v for n, v in curr.items()
                                  if n not in prev or prev[n] < 1e-5) * 100
                n_carried   = len([n for n in curr if n in prev and prev[n] > 1e-5])
                n_new       = len([n for n in curr if n not in prev or prev[n] < 1e-5])
                continuity  = carried

                rating = ("🟢 Excellent" if continuity >= 80
                          else "🟡 Good" if continuity >= 50
                          else "🔴 High Turnover" if i > 0 else "—")
                bg = (CLR_OK if continuity >= 80
                      else CLR_WARN if continuity >= 50
                      else CLR_DANGER if i > 0 else None)

                cont_tbl.setItem(i, 0, _ro(f"Chunk {i+1}"))
                cont_tbl.setItem(i, 1, _ro(str(n_carried)))
                cont_tbl.setItem(i, 2, _ro(str(n_new)))
                cont_tbl.setItem(i, 3, _ro(f"{continuity:.1f}%", bg, bold=True))
                cont_tbl.setItem(i, 4, _ro(rating, bg))

            lay.addWidget(cont_tbl)

        lay.addStretch()
        return w


# ─── Convenience launcher ──────────────────────────────────────────────────────

def show_optimization_report(state: 'AppState',
                             yearly_rows=None,
                             parent=None) -> OptimizationReportDialog:
    """Create and show (non-blocking) the optimization report dialog."""
    dlg = OptimizationReportDialog(state, yearly_rows=yearly_rows, parent=parent)
    dlg.show()
    return dlg