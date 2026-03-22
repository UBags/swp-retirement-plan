# Copyright (c) 2025 Uddipan Bagchi. All rights reserved.
# See LICENSE in the project root for license information.package com.costheta.cortexa.action

"""
chart_dialog.py – Non-modal chart pop-ups for SWP Planner tables.

Each chart window is independent (non-modal) and stays open alongside the main app.
Uses matplotlib embedded in a PySide6 widget.
"""
from __future__ import annotations
from typing import List, TYPE_CHECKING

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QComboBox,
    QLabel, QWidget, QSizePolicy, QToolBar
)
from PySide6.QtCore import Qt

import matplotlib
matplotlib.use("QtAgg")
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

# Colour palette
COLOURS = [
    "#2980b9", "#27ae60", "#c0392b", "#8e44ad",
    "#e67e22", "#16a085", "#d35400", "#2c3e50",
    "#f39c12", "#1abc9c",
]


class ChartWindow(QDialog):
    """
    Non-modal chart window.  Instantiate and call .show() – it will not block.
    Supply `chart_fn(ax, data)` to draw into a pre-created axes, or use the
    named constructors below.
    """
    def __init__(self, title: str, parent=None):
        super().__init__(parent, Qt.WindowType.Window)   # Window flag = non-modal, own taskbar
        self.setWindowTitle(title)
        self.resize(1000, 600)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # Chart selector row (populated by subclasses)
        self.ctrl_bar = QHBoxLayout()
        layout.addLayout(self.ctrl_bar)

        # Matplotlib figure
        self.fig = Figure(figsize=(12, 6), tight_layout=True)
        self.canvas = FigureCanvas(self.fig)
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.toolbar = NavigationToolbar(self.canvas, self)
        layout.addWidget(self.toolbar)
        layout.addWidget(self.canvas)

    def _draw(self):
        self.canvas.draw()

    def _clear(self):
        self.fig.clear()


# ── Helper ────────────────────────────────────────────────────────────────────

def _bar_line(ax, x, bar_ys, bar_labels, line_ys, line_labels, title, ylabel):
    """Draw grouped bars + optional line series on a shared axes."""
    import numpy as np
    n = len(x)
    width = 0.8 / max(len(bar_ys), 1)
    offsets = [(i - (len(bar_ys) - 1) / 2) * width for i in range(len(bar_ys))]

    for i, (y, lbl) in enumerate(zip(bar_ys, bar_labels)):
        ax.bar([xi + offsets[i] for xi in range(n)], y, width,
               label=lbl, color=COLOURS[i % len(COLOURS)], alpha=0.82)

    ax2 = ax.twinx() if line_ys else None
    for i, (y, lbl) in enumerate(zip(line_ys, line_labels)):
        ax2.plot(range(n), y, marker="o", markersize=3,
                 label=lbl, color=COLOURS[(len(bar_ys) + i) % len(COLOURS)],
                 linewidth=1.8, zorder=5)
        ax2.set_ylabel("Rs Lakhs (lines)", fontsize=9)
        ax2.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:.0f}"))

    ax.set_xticks(range(n))
    ax.set_xticklabels([str(xi) for xi in x], fontsize=8)
    ax.set_title(title, fontsize=12, fontweight="bold")
    ax.set_ylabel(ylabel, fontsize=9)
    ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:.0f}"))
    ax.grid(axis="y", alpha=0.3)

    # Combine legends
    handles, labels = ax.get_legend_handles_labels()
    if ax2:
        h2, l2 = ax2.get_legend_handles_labels()
        handles += h2; labels += l2
    ax.legend(handles, labels, fontsize=8, loc="upper left")


# ── Personal Monthly Chart ────────────────────────────────────────────────────

class PersonalMonthlyChart(ChartWindow):
    """Chart window for personal monthly SWP data."""

    VIEWS = [
        "Corpus Over Time",
        "Monthly Withdrawals (Debt vs Equity)",
        "Principal vs Gain per Month",
        "Tax & HUF Transfer Events",
    ]

    def __init__(self, rows, parent=None):
        super().__init__("Personal Monthly – Chart View", parent)
        self.rows = rows

        lbl = QLabel("View:")
        self.combo = QComboBox()
        self.combo.addItems(self.VIEWS)
        self.combo.currentIndexChanged.connect(self._refresh)
        self.ctrl_bar.addWidget(lbl)
        self.ctrl_bar.addWidget(self.combo)
        self.ctrl_bar.addStretch()

        self._refresh()

    def _refresh(self):
        self._clear()
        ax = self.fig.add_subplot(111)
        v = self.combo.currentIndex()
        rows = self.rows
        months = [r.month_idx + 1 for r in rows]

        if v == 0:
            ax.plot(months, [r.corpus_debt_end for r in rows],
                    label="Corpus Debt", color=COLOURS[0], linewidth=1.5)
            ax.plot(months, [r.corpus_equity_end for r in rows],
                    label="Corpus Equity", color=COLOURS[1], linewidth=1.5)
            other_vals = [r.corpus_other_end for r in rows]
            if any(v > 0.01 for v in other_vals):
                ax.plot(months, other_vals,
                        label="Corpus Other", color=COLOURS[3] if len(COLOURS) > 3 else "#e67e22", linewidth=1.5)
            total = [r.corpus_debt_end + r.corpus_equity_end + r.corpus_other_end for r in rows]
            ax.plot(months, total, label="Total Corpus", color=COLOURS[2],
                    linewidth=2, linestyle="--")
            ax.set_title("Personal Corpus Over Time", fontweight="bold")
            ax.set_xlabel("Month #")
            ax.set_ylabel("Rs Lakhs")
            ax.legend(fontsize=9)
            ax.grid(alpha=0.3)

        elif v == 1:
            ax.bar(months, [r.wd_debt for r in rows],
                   label="WD Debt", color=COLOURS[0], alpha=0.8, width=0.8)
            ax.bar(months, [r.wd_equity for r in rows],
                   bottom=[r.wd_debt for r in rows],
                   label="WD Equity", color=COLOURS[1], alpha=0.8, width=0.8)
            ax.set_title("Monthly Withdrawals – Debt vs Equity", fontweight="bold")
            ax.set_xlabel("Month #")
            ax.set_ylabel("Rs Lakhs")
            ax.legend(fontsize=9)
            ax.grid(axis="y", alpha=0.3)

        elif v == 2:
            ax.fill_between(months, [r.principal_debt + r.principal_equity for r in rows],
                            label="Principal", alpha=0.6, color=COLOURS[0])
            ax.fill_between(months, [r.gain_debt + r.gain_equity for r in rows],
                            label="Gain", alpha=0.6, color=COLOURS[2])
            ax.set_title("Principal vs Gain in Monthly Withdrawals", fontweight="bold")
            ax.set_xlabel("Month #")
            ax.set_ylabel("Rs Lakhs")
            ax.legend(fontsize=9)
            ax.grid(alpha=0.3)

        elif v == 3:
            # Only April rows with events
            april_rows = [r for r in rows if r.calendar_month == 4 and r.fy_year >= 2]
            fy_labels = [f"FY{r.fy_year}" for r in april_rows]
            tax_vals   = [r.ind_tax_paid   for r in april_rows]
            xfer_vals  = [r.huf_transfer_in for r in april_rows]
            x = range(len(april_rows))
            w = 0.35
            ax.bar([xi - w/2 for xi in x], tax_vals,  w, label="Ind Tax Paid",   color=COLOURS[2], alpha=0.85)
            ax.bar([xi + w/2 for xi in x], xfer_vals, w, label="HUF Transfer",   color=COLOURS[1], alpha=0.85)
            ax.set_xticks(list(x))
            ax.set_xticklabels(fy_labels, rotation=45, fontsize=8)
            ax.set_title("April Events: Tax Paid & HUF Transfer (by FY)", fontweight="bold")
            ax.set_ylabel("Rs Lakhs")
            ax.legend(fontsize=9)
            ax.grid(axis="y", alpha=0.3)

        ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:.1f}"))
        self._draw()


# ── Personal Annual Chart ─────────────────────────────────────────────────────

class PersonalAnnualChart(ChartWindow):
    VIEWS = [
        "Corpus Growth (Debt + Equity)",
        "Net Cash Available per Year",
        "Tax: Personal vs FD Benchmark",
        "Tax Saved to HUF per Year",
        "Corpus + Net Cash Overview",
    ]

    def __init__(self, rows, parent=None):
        super().__init__("Personal Annual Summary – Chart View", parent)
        self.rows = rows

        self.combo = QComboBox()
        self.combo.addItems(self.VIEWS)
        self.combo.currentIndexChanged.connect(self._refresh)
        self.ctrl_bar.addWidget(QLabel("View:"))
        self.ctrl_bar.addWidget(self.combo)
        self.ctrl_bar.addStretch()
        self._refresh()

    def _refresh(self):
        self._clear()
        ax = self.fig.add_subplot(111)
        v = self.combo.currentIndex()
        rows = self.rows
        yrs = [r.year for r in rows]

        if v == 0:
            stack_data = [[r.corpus_debt_personal  for r in rows],
                          [r.corpus_equity_personal for r in rows]]
            stack_labels = ["Corpus Debt", "Corpus Equity"]
            stack_colors = [COLOURS[0], COLOURS[1]]
            other_vals = [r.corpus_other_personal for r in rows]
            if any(v > 0.01 for v in other_vals):
                stack_data.append(other_vals)
                stack_labels.append("Corpus Other")
                stack_colors.append("#e67e22")
            ax.stackplot(yrs, *stack_data,
                         labels=stack_labels,
                         colors=stack_colors, alpha=0.8)
            ax.set_title("Personal Corpus Growth", fontweight="bold")
            ax.legend(fontsize=9, loc="upper left")

        elif v == 1:
            ax.bar(yrs, [r.net_cash_personal for r in rows],
                   color=COLOURS[1], alpha=0.85, label="Personal Net Cash")
            ax.plot(yrs, [r.net_cash_total for r in rows],
                    color=COLOURS[2], linewidth=2, marker="o", markersize=4,
                    label="Total Net Cash (incl HUF)")
            ax.set_title("Net Cash Available per Year", fontweight="bold")
            ax.legend(fontsize=9)

        elif v == 2:
            ax.plot(yrs, [r.fd_tax_benchmark for r in rows],
                    color=COLOURS[2], linewidth=2, marker="s", markersize=4,
                    label="FD Benchmark Tax")
            ax.plot(yrs, [r.tax_personal for r in rows],
                    color=COLOURS[0], linewidth=2, marker="o", markersize=4,
                    label="Actual Tax (SWP)")
            ax.fill_between(yrs,
                            [r.fd_tax_benchmark for r in rows],
                            [r.tax_personal for r in rows],
                            alpha=0.25, color=COLOURS[1], label="Tax Saved")
            ax.set_title("Tax: Personal SWP vs FD Benchmark", fontweight="bold")
            ax.legend(fontsize=9)

        elif v == 3:
            ax.bar(yrs, [r.tax_saved for r in rows],
                   color=COLOURS[1], alpha=0.85)
            ax.plot(yrs, [sum(r2.tax_saved for r2 in rows[:i+1]) for i, _ in enumerate(rows)],
                    color=COLOURS[2], linewidth=2, marker="o", markersize=3,
                    label="Cumulative Saved")
            ax.set_title("Annual Tax Saved (diverted to HUF)", fontweight="bold")
            ax2 = ax.twinx()
            ax2.plot(yrs, [sum(r2.tax_saved for r2 in rows[:i+1]) for i, _ in enumerate(rows)],
                     color=COLOURS[2], linewidth=2)
            ax2.set_ylabel("Cumulative (Rs Lakhs)", fontsize=9)
            ax.legend(fontsize=9)

        elif v == 4:
            # 2-panel overview
            self._clear()
            ax1 = self.fig.add_subplot(121)
            ax2 = self.fig.add_subplot(122)
            stack_data = [[r.corpus_debt_personal for r in rows],
                          [r.corpus_equity_personal for r in rows]]
            stack_labels = ["Debt", "Equity"]
            stack_colors = [COLOURS[0], COLOURS[1]]
            other_vals = [r.corpus_other_personal for r in rows]
            if any(v > 0.01 for v in other_vals):
                stack_data.append(other_vals)
                stack_labels.append("Other")
                stack_colors.append("#e67e22")
            ax1.stackplot(yrs, *stack_data,
                          labels=stack_labels,
                          colors=stack_colors, alpha=0.8)
            ax1.set_title("Corpus Growth", fontweight="bold")
            ax1.legend(fontsize=8)
            ax1.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:.0f}"))
            ax1.set_xlabel("FY Year")
            ax1.grid(alpha=0.3)

            ax2.bar(yrs, [r.net_cash_personal for r in rows],
                    color=COLOURS[1], alpha=0.8, label="Net Cash")
            ax2.plot(yrs, [r.tax_saved for r in rows],
                     color=COLOURS[2], linewidth=1.8, marker="o", markersize=3,
                     label="Tax Saved")
            ax2.set_title("Cash & Tax Savings", fontweight="bold")
            ax2.legend(fontsize=8)
            ax2.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:.0f}"))
            ax2.set_xlabel("FY Year")
            ax2.grid(alpha=0.3)
            self._draw()
            return

        ax.set_xlabel("FY Year")
        ax.set_ylabel("Rs Lakhs")
        ax.grid(alpha=0.3)
        ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:.1f}"))
        self._draw()


# ── HUF Monthly Chart ─────────────────────────────────────────────────────────

class HUFMonthlyChart(ChartWindow):
    VIEWS = [
        "HUF Corpus Over Time",
        "HUF Monthly Withdrawals",
        "HUF Principal vs Gain",
        "HUF Transfer Inflows",
    ]

    def __init__(self, rows, parent=None):
        super().__init__("HUF Monthly – Chart View", parent)
        self.rows = rows
        self.combo = QComboBox()
        self.combo.addItems(self.VIEWS)
        self.combo.currentIndexChanged.connect(self._refresh)
        self.ctrl_bar.addWidget(QLabel("View:"))
        self.ctrl_bar.addWidget(self.combo)
        self.ctrl_bar.addStretch()
        self._refresh()

    def _refresh(self):
        self._clear()
        ax = self.fig.add_subplot(111)
        v = self.combo.currentIndex()
        rows = self.rows
        months = [r.month_idx + 1 for r in rows]

        if v == 0:
            ax.plot(months, [r.corpus_debt_end for r in rows],
                    label="HUF Debt Corpus", color=COLOURS[3], linewidth=1.5)
            ax.plot(months, [r.corpus_equity_end for r in rows],
                    label="HUF Equity Corpus", color=COLOURS[4], linewidth=1.5)
            huf_other = [r.corpus_other_end for r in rows]
            if any(v > 0.01 for v in huf_other):
                ax.plot(months, huf_other,
                        label="HUF Other Corpus", color="#e67e22", linewidth=1.5)
            ax.plot(months, [r.corpus_debt_end + r.corpus_equity_end + r.corpus_other_end for r in rows],
                    label="HUF Total", color=COLOURS[2], linewidth=2, linestyle="--")
            ax.set_title("HUF Corpus Over Time", fontweight="bold")
            ax.legend(fontsize=9)

        elif v == 1:
            ax.bar(months, [r.wd_debt for r in rows],
                   label="WD Debt", color=COLOURS[3], alpha=0.8, width=0.8)
            ax.bar(months, [r.wd_equity for r in rows],
                   bottom=[r.wd_debt for r in rows],
                   label="WD Equity", color=COLOURS[4], alpha=0.8, width=0.8)
            ax.set_title("HUF Monthly Withdrawals", fontweight="bold")
            ax.legend(fontsize=9)

        elif v == 2:
            ax.fill_between(months, [r.principal_debt + r.principal_equity for r in rows],
                            alpha=0.6, color=COLOURS[3], label="Principal")
            ax.fill_between(months, [r.gain_debt + r.gain_equity for r in rows],
                            alpha=0.6, color=COLOURS[2], label="Gain")
            ax.set_title("HUF Principal vs Gain", fontweight="bold")
            ax.legend(fontsize=9)

        elif v == 3:
            inflow_rows = [r for r in rows if r.huf_transfer_in > 0 or r.windfall_huf > 0]
            fy_labels = [f"FY{r.fy_year}\n{r.calendar_year}-{r.calendar_month:02d}"
                         for r in inflow_rows]
            x = range(len(inflow_rows))
            w = 0.35
            ax.bar([xi - w/2 for xi in x], [r.huf_transfer_in for r in inflow_rows],
                   w, label="Tax Saving Transfer", color=COLOURS[1], alpha=0.85)
            ax.bar([xi + w/2 for xi in x], [r.windfall_huf for r in inflow_rows],
                   w, label="Windfall", color=COLOURS[4], alpha=0.85)
            ax.set_xticks(list(x))
            ax.set_xticklabels(fy_labels, rotation=45, fontsize=7)
            ax.set_title("HUF Inflows: Transfers & Windfalls", fontweight="bold")
            ax.legend(fontsize=9)

        ax.set_xlabel("Month #" if v != 3 else "Event")
        ax.set_ylabel("Rs Lakhs")
        ax.grid(alpha=0.3)
        ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:.2f}"))
        self._draw()


# ── Annual Combined Chart ─────────────────────────────────────────────────────

class AnnualSummaryChart(ChartWindow):
    VIEWS = [
        "All Corpus (Personal + HUF)",
        "Net Cash Breakdown",
        "Tax Comparison (Personal vs HUF vs FD Benchmark)",
        "Full 5-Panel Dashboard",
    ]

    def __init__(self, rows, parent=None):
        super().__init__("Annual Combined Summary – Chart View", parent)
        self.rows = rows
        self.combo = QComboBox()
        self.combo.addItems(self.VIEWS)
        self.combo.currentIndexChanged.connect(self._refresh)
        self.ctrl_bar.addWidget(QLabel("View:"))
        self.ctrl_bar.addWidget(self.combo)
        self.ctrl_bar.addStretch()
        self._refresh()

    def _refresh(self):
        self._clear()
        v = self.combo.currentIndex()
        rows = self.rows
        yrs = [r.year for r in rows]

        if v == 0:
            ax = self.fig.add_subplot(111)
            stack_data = [[r.corpus_debt_personal  for r in rows],
                          [r.corpus_equity_personal for r in rows]]
            stack_labels = ["P-Debt", "P-Equity"]
            stack_colors = list(COLOURS[:2])
            p_other = [r.corpus_other_personal for r in rows]
            if any(v > 0.01 for v in p_other):
                stack_data.append(p_other)
                stack_labels.append("P-Other")
                stack_colors.append("#e67e22")
            stack_data.extend([
                [r.corpus_debt_huf for r in rows],
                [r.corpus_equity_huf for r in rows],
            ])
            stack_labels.extend(["HUF-Debt", "HUF-Equity"])
            stack_colors.extend([COLOURS[2], COLOURS[3]])
            h_other = [r.corpus_other_huf for r in rows]
            if any(v > 0.01 for v in h_other):
                stack_data.append(h_other)
                stack_labels.append("HUF-Other")
                stack_colors.append("#d35400")
            ax.stackplot(yrs, *stack_data,
                         labels=stack_labels,
                         colors=stack_colors, alpha=0.82)
            ax.set_title("Total Corpus: Personal + HUF", fontweight="bold")
            ax.legend(fontsize=9, loc="upper left")
            ax.set_xlabel("FY Year"); ax.set_ylabel("Rs Lakhs")
            ax.grid(alpha=0.3)
            ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:.0f}"))

        elif v == 1:
            ax = self.fig.add_subplot(111)
            ax.bar(yrs, [r.net_cash_personal for r in rows],
                   label="Personal Net Cash", color=COLOURS[0], alpha=0.82)
            ax.bar(yrs, [r.net_cash_huf for r in rows],
                   bottom=[r.net_cash_personal for r in rows],
                   label="HUF Net Cash", color=COLOURS[3], alpha=0.82)
            ax.plot(yrs, [r.net_cash_total for r in rows],
                    color=COLOURS[2], linewidth=2, marker="o", markersize=4,
                    label="Total Net Cash")
            ax.set_title("Net Cash Breakdown per Year", fontweight="bold")
            ax.legend(fontsize=9); ax.set_xlabel("FY Year"); ax.set_ylabel("Rs Lakhs")
            ax.grid(axis="y", alpha=0.3)
            ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:.1f}"))

        elif v == 2:
            ax = self.fig.add_subplot(111)
            ax.plot(yrs, [r.fd_tax_benchmark for r in rows],
                    color=COLOURS[2], linewidth=2, marker="s", markersize=4,
                    label="FD Tax Benchmark")
            ax.plot(yrs, [r.tax_personal for r in rows],
                    color=COLOURS[0], linewidth=2, marker="o", markersize=4,
                    label="Personal Tax")
            ax.plot(yrs, [r.tax_huf for r in rows],
                    color=COLOURS[3], linewidth=2, marker="^", markersize=4,
                    label="HUF Tax")
            ax.fill_between(yrs,
                            [r.fd_tax_benchmark for r in rows],
                            [r.tax_personal + r.tax_huf for r in rows],
                            alpha=0.2, color=COLOURS[1], label="Net Tax Saving")
            ax.set_title("Tax Comparison: FD Benchmark vs SWP (Personal + HUF)", fontweight="bold")
            ax.legend(fontsize=9); ax.set_xlabel("FY Year"); ax.set_ylabel("Rs Lakhs")
            ax.grid(alpha=0.3)
            ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:.1f}"))

        elif v == 3:
            # 2x3 dashboard
            axes = self.fig.subplots(2, 3)
            self.fig.suptitle("30-Year SWP Dashboard", fontsize=13, fontweight="bold")

            # Panel 1: Personal corpus
            a = axes[0][0]
            pd_data = [[r.corpus_debt_personal for r in rows],
                       [r.corpus_equity_personal for r in rows]]
            pd_labels = ["Debt", "Equity"]
            pd_colors = [COLOURS[0], COLOURS[1]]
            p_other = [r.corpus_other_personal for r in rows]
            if any(v > 0.01 for v in p_other):
                pd_data.append(p_other)
                pd_labels.append("Other")
                pd_colors.append("#e67e22")
            a.stackplot(yrs, *pd_data,
                        labels=pd_labels, colors=pd_colors, alpha=0.8)
            a.set_title("Personal Corpus", fontsize=10)
            a.legend(fontsize=7); a.grid(alpha=0.3)
            a.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:.0f}"))

            # Panel 2: HUF corpus
            a = axes[0][1]
            hd_data = [[r.corpus_debt_huf for r in rows],
                       [r.corpus_equity_huf for r in rows]]
            hd_labels = ["Debt", "Equity"]
            hd_colors = [COLOURS[3], COLOURS[4]]
            h_other = [r.corpus_other_huf for r in rows]
            if any(v > 0.01 for v in h_other):
                hd_data.append(h_other)
                hd_labels.append("Other")
                hd_colors.append("#d35400")
            a.stackplot(yrs, *hd_data,
                        labels=hd_labels, colors=hd_colors, alpha=0.8)
            a.set_title("HUF Corpus", fontsize=10)
            a.legend(fontsize=7); a.grid(alpha=0.3)
            a.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:.0f}"))

            # Panel 3: Net cash
            a = axes[0][2]
            a.bar(yrs, [r.net_cash_total for r in rows], color=COLOURS[1], alpha=0.85)
            a.set_title("Total Net Cash", fontsize=10)
            a.grid(axis="y", alpha=0.3)
            a.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:.0f}"))

            # Panel 4: Tax saved
            a = axes[1][0]
            a.bar(yrs, [r.tax_saved for r in rows], color=COLOURS[1], alpha=0.85, label="Annual")
            cum = [sum(r2.tax_saved for r2 in rows[:i+1]) for i, _ in enumerate(rows)]
            a2 = a.twinx()
            a2.plot(yrs, cum, color=COLOURS[2], linewidth=1.8, label="Cumulative")
            a2.set_ylabel("Cumul.", fontsize=8)
            a.set_title("Tax Saved -> HUF", fontsize=10)
            a.grid(axis="y", alpha=0.3)

            # Panel 5: Tax comparison
            a = axes[1][1]
            a.plot(yrs, [r.fd_tax_benchmark for r in rows],
                   color=COLOURS[2], linewidth=1.8, label="FD Tax")
            a.plot(yrs, [r.tax_personal for r in rows],
                   color=COLOURS[0], linewidth=1.8, label="SWP Tax")
            a.set_title("Tax: FD vs SWP", fontsize=10)
            a.legend(fontsize=7); a.grid(alpha=0.3)
            a.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:.1f}"))

            # Panel 6: Combined net cash breakdown
            a = axes[1][2]
            a.bar(yrs, [r.net_cash_personal for r in rows],
                  color=COLOURS[0], alpha=0.82, label="Personal")
            a.bar(yrs, [max(0, r.net_cash_huf) for r in rows],
                  bottom=[r.net_cash_personal for r in rows],
                  color=COLOURS[3], alpha=0.82, label="HUF")
            a.set_title("Net Cash Split", fontsize=10)
            a.legend(fontsize=7); a.grid(axis="y", alpha=0.3)
            a.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:.0f}"))

            self.fig.tight_layout()

        self._draw()


# ── Sensitivity Chart ─────────────────────────────────────────────────────────

class SensitivityChart(ChartWindow):
    VIEWS = ["Net Cash Comparison", "Total Corpus Comparison"]

    def __init__(self, results: dict, parent=None):
        """results = {scenario_name: [YearSummary, ...]}"""
        super().__init__("Sensitivity Analysis – Chart View", parent)
        self.results = results
        self.combo = QComboBox()
        self.combo.addItems(self.VIEWS)
        self.combo.currentIndexChanged.connect(self._refresh)
        self.ctrl_bar.addWidget(QLabel("View:"))
        self.ctrl_bar.addWidget(self.combo)
        self.ctrl_bar.addStretch()
        self._refresh()

    def _refresh(self):
        self._clear()
        ax = self.fig.add_subplot(111)
        v = self.combo.currentIndex()
        yrs = list(range(1, 31))

        for i, (name, rows) in enumerate(self.results.items()):
            if v == 0:
                vals = [r.net_cash_total for r in rows]
            else:
                vals = [r.corpus_debt_personal + r.corpus_equity_personal +
                        r.corpus_other_personal +
                        r.corpus_debt_huf + r.corpus_equity_huf +
                        r.corpus_other_huf for r in rows]
            lw = 2.5 if name == "Base Case" else 1.5
            ls = "-" if name == "Base Case" else "--"
            ax.plot(yrs, vals, label=name, color=COLOURS[i % len(COLOURS)],
                    linewidth=lw, linestyle=ls, marker="o", markersize=3)

        title = "Net Cash Comparison" if v == 0 else "Total Corpus Comparison"
        ax.set_title(title, fontweight="bold")
        ax.set_xlabel("FY Year")
        ax.set_ylabel("Rs Lakhs")
        ax.legend(fontsize=9)
        ax.grid(alpha=0.3)
        ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:.1f}"))
        self._draw()


# ── Monte Carlo Chart ─────────────────────────────────────────────────────────

class MonteCarloChart(ChartWindow):
    """
    Five-view chart window for Monte Carlo results:
      0. Corpus Fan Chart         — shaded percentile bands + deterministic line
      1. Net Cash Fan Chart       — same treatment for annual spendable cash
      2. Ruin Probability Curve   — cumulative % of sims depleted by FY N
      3. Sequence-of-Returns      — individual best / worst / median paths + 20 random paths
      4. Summary Table            — key statistics in a text/table layout
    """

    VIEWS = [
        "Corpus Fan Chart (P5–P95)",
        "Net Cash Fan Chart (P5–P95)",
        "Ruin Probability by Year",
        "Sequence-of-Returns Paths",
        "Summary Statistics",
    ]

    def __init__(self, results, parent=None):
        """results: MCResults dataclass from monte_carlo.run_monte_carlo"""
        super().__init__("Monte Carlo – Sequence-of-Returns Risk", parent)
        self.results = results
        self.resize(1100, 650)

        self.combo = QComboBox()
        self.combo.addItems(self.VIEWS)
        self.combo.currentIndexChanged.connect(self._refresh)
        self.ctrl_bar.addWidget(QLabel("View:"))
        self.ctrl_bar.addWidget(self.combo)
        self.ctrl_bar.addStretch()

        # Stats label
        res = results
        stats_text = (
            f"  {res.n_sims:,} simulations  |  σ = {res.sigma_used*100:.2f}%  |  "
            f"Floor FY1–10: {res.floor_fy1_10*100:.2f}%  |  "
            f"Floor FY11–30: {res.floor_fy11_30*100:.2f}%  |  "
            f"30-yr Ruin Prob: {res.ruin_probability*100:.1f}%  |  "
            f"Median final corpus: Rs {res.median_final_corpus:.0f}L  |  "
            f"P5 final corpus: Rs {res.p5_final_corpus:.0f}L"
        )
        lbl = QLabel(stats_text)
        lbl.setStyleSheet(
            "background:#1a252f;color:white;padding:5px 8px;"
            "font-size:11px;font-family:monospace;border-radius:3px;"
        )
        self.ctrl_bar.addWidget(lbl)

        self._refresh()

    # ── View dispatcher ───────────────────────────────────────────────────────

    def _refresh(self):
        self._clear()
        v = self.combo.currentIndex()
        if   v == 0: self._draw_corpus_fan()
        elif v == 1: self._draw_cash_fan()
        elif v == 2: self._draw_ruin()
        elif v == 3: self._draw_paths()
        elif v == 4: self._draw_summary()
        self._draw()

    # ── View 0: Corpus fan chart ──────────────────────────────────────────────

    def _draw_corpus_fan(self):
        import numpy as np
        ax = self.fig.add_subplot(111)
        res = self.results
        yrs = res.fy_labels

        # Shaded bands
        ax.fill_between(yrs, res.corpus_p5,  res.corpus_p95,
                         alpha=0.15, color="#2980b9", label="P5–P95 range")
        ax.fill_between(yrs, res.corpus_p25, res.corpus_p75,
                         alpha=0.30, color="#2980b9", label="P25–P75 range")

        # Percentile lines
        ax.plot(yrs, res.corpus_p5,  "--", color="#c0392b", linewidth=1.2,
                label="P5  (tail risk)")
        ax.plot(yrs, res.corpus_p25, "-",  color="#e67e22", linewidth=1.2,
                label="P25")
        ax.plot(yrs, res.corpus_p50, "-",  color="#2980b9", linewidth=2.2,
                label="Median (P50)")
        ax.plot(yrs, res.corpus_p75, "-",  color="#27ae60", linewidth=1.2,
                label="P75")
        ax.plot(yrs, res.corpus_p95, "--", color="#27ae60", linewidth=1.2,
                label="P95 (upside)")

        # Deterministic reference
        ax.plot(yrs, res.corpus_det, "k--", linewidth=2.0,
                label="Deterministic (base case)", zorder=6)

        # Zero line
        ax.axhline(0, color="red", linewidth=0.8, linestyle=":", alpha=0.7)

        ax.set_title(
            f"Total Corpus (Personal + HUF) — {res.n_sims:,} Monte Carlo Paths  |  σ = {res.sigma_used*100:.2f}%",
            fontweight="bold", fontsize=11
        )
        ax.set_xlabel("Financial Year")
        ax.set_ylabel("Rs Lakhs")
        ax.legend(fontsize=9, loc="upper left")
        ax.grid(alpha=0.25)
        ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
        self._annotate_ruin(ax, res)

    # ── View 1: Net cash fan chart ────────────────────────────────────────────

    def _draw_cash_fan(self):
        ax = self.fig.add_subplot(111)
        res = self.results
        yrs = res.fy_labels

        ax.fill_between(yrs, res.cash_p5,  res.cash_p95,
                         alpha=0.15, color="#27ae60", label="P5–P95 range")
        ax.fill_between(yrs, res.cash_p25, res.cash_p75,
                         alpha=0.30, color="#27ae60", label="P25–P75 range")

        ax.plot(yrs, res.cash_p5,  "--", color="#c0392b", linewidth=1.2,
                label="P5  (tail risk)")
        ax.plot(yrs, res.cash_p25, "-",  color="#e67e22", linewidth=1.2, label="P25")
        ax.plot(yrs, res.cash_p50, "-",  color="#27ae60", linewidth=2.2, label="Median (P50)")
        ax.plot(yrs, res.cash_p75, "-",  color="#2980b9", linewidth=1.2, label="P75")
        ax.plot(yrs, res.cash_p95, "--", color="#2980b9", linewidth=1.2,
                label="P95 (upside)")

        ax.plot(yrs, res.cash_det, "k--", linewidth=2.0,
                label="Deterministic (base case)", zorder=6)

        ax.axhline(0, color="red", linewidth=0.8, linestyle=":", alpha=0.7)

        ax.set_title(
            f"Annual Household Net Cash — {res.n_sims:,} Monte Carlo Paths  |  σ = {res.sigma_used*100:.2f}%",
            fontweight="bold", fontsize=11
        )
        ax.set_xlabel("Financial Year")
        ax.set_ylabel("Rs Lakhs per year")
        ax.legend(fontsize=9, loc="upper left")
        ax.grid(alpha=0.25)
        ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:,.1f}"))

    # ── View 2: Ruin probability ──────────────────────────────────────────────

    def _draw_ruin(self):
        import numpy as np
        ax  = self.fig.add_subplot(111)
        ax2 = ax.twinx()
        res = self.results
        yrs = res.fy_labels

        # Ruin probability curve (left axis, %)
        ruin_pct = res.ruin_by_fy * 100.0
        ax.fill_between(yrs, 0, ruin_pct, alpha=0.30, color="#c0392b")
        ax.plot(yrs, ruin_pct, color="#c0392b", linewidth=2.2, marker="o",
                markersize=4, label="Cumulative ruin probability (%)")

        # Reference lines at 5%, 10%, 25%
        for pct, ls in [(5, ":"), (10, "--"), (25, "-.")]:
            ax.axhline(pct, color="#7f8c8d", linewidth=0.9, linestyle=ls,
                       label=f"{pct}% threshold")

        # Annotate first FY where ruin > 5%
        threshold_fy = next((fy for fy, rp in zip(yrs, ruin_pct) if rp >= 5.0), None)
        if threshold_fy:
            idx = threshold_fy - 1
            ax.annotate(
                f"FY{threshold_fy}: {ruin_pct[idx]:.1f}%\nfirst exceeds 5%",
                xy=(threshold_fy, ruin_pct[idx]),
                xytext=(threshold_fy + 1.5, ruin_pct[idx] + 3),
                arrowprops=dict(arrowstyle="->", color="#c0392b"),
                fontsize=9, color="#c0392b"
            )

        # P5 corpus (right axis — what the unluckiest 5% end up with)
        ax2.plot(yrs, res.corpus_p5, color="#8e44ad", linewidth=1.8,
                 linestyle="--", label="P5 corpus (right axis)")
        ax2.set_ylabel("P5 corpus (Rs Lakhs)", fontsize=9, color="#8e44ad")
        ax2.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
        ax2.tick_params(axis="y", labelcolor="#8e44ad")

        ax.set_title(
            f"Ruin Probability by Year — {res.n_sims:,} simulations  |  "
            f"30-yr overall: {res.ruin_probability*100:.1f}%",
            fontweight="bold", fontsize=11
        )
        ax.set_xlabel("Financial Year")
        ax.set_ylabel("Cumulative ruin probability (%)", fontsize=9)
        ax.set_ylim(0, max(ruin_pct.max() * 1.3 + 1, 15))

        # Merge legends
        h1, l1 = ax.get_legend_handles_labels()
        h2, l2 = ax2.get_legend_handles_labels()
        ax.legend(h1 + h2, l1 + l2, fontsize=9, loc="upper left")
        ax.grid(alpha=0.25)

    # ── View 3: Individual paths ──────────────────────────────────────────────

    def _draw_paths(self):
        import numpy as np
        ax  = self.fig.add_subplot(111)
        res = self.results
        yrs = res.fy_labels
        raw = res.corpus_raw   # (n_sims, 30)

        # 30 random background paths (thin, grey)
        rng = np.random.default_rng(7)
        sample_idx = rng.choice(res.n_sims, size=min(40, res.n_sims), replace=False)
        for i in sample_idx:
            ax.plot(yrs, raw[i], color="#bdc3c7", linewidth=0.5, alpha=0.6, zorder=1)

        # Worst, best, median by final corpus
        final = raw[:, -1]
        worst_i  = int(np.argmin(final))
        best_i   = int(np.argmax(final))
        median_i = int(np.argsort(final)[res.n_sims // 2])

        ax.plot(yrs, raw[worst_i],  color="#c0392b", linewidth=2.0,
                label=f"Worst path  (final: {final[worst_i]:.0f}L)", zorder=5)
        ax.plot(yrs, raw[best_i],   color="#27ae60", linewidth=2.0,
                label=f"Best path   (final: {final[best_i]:.0f}L)",  zorder=5)
        ax.plot(yrs, raw[median_i], color="#2980b9", linewidth=2.0,
                label=f"Median path (final: {final[median_i]:.0f}L)", zorder=5)

        # Deterministic reference
        ax.plot(yrs, res.corpus_det, "k--", linewidth=2.0,
                label="Deterministic (base case)", zorder=6)

        # Shade the sequence-of-returns gap annotation
        worst_early = raw[worst_i, :5].mean()
        best_early  = raw[best_i,  :5].mean()
        ax.annotate(
            "Early returns drive\nlong-term outcomes\n(sequence-of-returns effect)",
            xy=(5, worst_early), xytext=(10, worst_early - 50),
            arrowprops=dict(arrowstyle="->", color="#c0392b"),
            fontsize=9, color="#c0392b",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="#fdecea", alpha=0.8)
        )

        ax.axhline(0, color="red", linewidth=0.8, linestyle=":", alpha=0.7)
        ax.set_title(
            f"Sequence-of-Returns Paths (40 sample paths + best / worst / median)  |  σ = {res.sigma_used*100:.2f}%",
            fontweight="bold", fontsize=11
        )
        ax.set_xlabel("Financial Year")
        ax.set_ylabel("Total Corpus (Rs Lakhs)")
        ax.legend(fontsize=9, loc="upper left")
        ax.grid(alpha=0.25)
        ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))

    # ── View 4: Summary table ─────────────────────────────────────────────────

    def _draw_summary(self):
        import numpy as np
        res = self.results
        ax = self.fig.add_subplot(111)
        ax.axis("off")

        # ── Build table data ──────────────────────────────────────────────────
        col_labels = ["FY", "P5 Corpus", "P25", "Median", "P75", "P95",
                      "Det. Corpus", "P5 Cash", "Med. Cash", "Det. Cash", "Ruin %"]
        rows_data = []
        highlight_rows = []   # rows where ruin first crosses 1%, 5%, 10%
        thresholds = {0.01: None, 0.05: None, 0.10: None}
        for i, fy in enumerate(res.fy_labels):
            rp = res.ruin_by_fy[i]
            for t in thresholds:
                if thresholds[t] is None and rp >= t:
                    thresholds[t] = i
                    highlight_rows.append(i)
            rows_data.append([
                str(fy),
                f"{res.corpus_p5[i]:,.0f}",
                f"{res.corpus_p25[i]:,.0f}",
                f"{res.corpus_p50[i]:,.0f}",
                f"{res.corpus_p75[i]:,.0f}",
                f"{res.corpus_p95[i]:,.0f}",
                f"{res.corpus_det[i]:,.0f}",
                f"{res.cash_p5[i]:,.1f}",
                f"{res.cash_p50[i]:,.1f}",
                f"{res.cash_det[i]:,.1f}",
                f"{rp*100:.1f}%",
            ])

        # Draw matplotlib table
        tbl = ax.table(
            cellText=rows_data,
            colLabels=col_labels,
            loc="center",
            cellLoc="right",
        )
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(8.5)
        tbl.scale(1.0, 1.35)

        # Style header
        for j in range(len(col_labels)):
            tbl[0, j].set_facecolor("#2c3e50")
            tbl[0, j].set_text_props(color="white", fontweight="bold")

        # Alternating rows + highlight ruin milestones
        for i in range(30):
            bg = "#f9f9f9" if i % 2 == 0 else "white"
            if i in highlight_rows:
                bg = "#fdecea"
            for j in range(len(col_labels)):
                tbl[i + 1, j].set_facecolor(bg)
                # P5 corpus column in red tint
                if j == 1:
                    tbl[i + 1, j].set_text_props(color="#c0392b")
                # Median in blue
                if j == 3:
                    tbl[i + 1, j].set_text_props(color="#2980b9", fontweight="bold")
                # Ruin% in red when >0
                if j == 10 and res.ruin_by_fy[i] > 0:
                    tbl[i + 1, j].set_text_props(color="#c0392b", fontweight="bold")

        # ── Headline stats above table ────────────────────────────────────────
        thresh_1pct  = f"FY{res.fy_labels[thresholds[0.01]]}"  if thresholds[0.01]  is not None else "Never"
        thresh_5pct  = f"FY{res.fy_labels[thresholds[0.05]]}"  if thresholds[0.05]  is not None else "Never"
        thresh_10pct = f"FY{res.fy_labels[thresholds[0.10]]}"  if thresholds[0.10]  is not None else "Never"

        summary = (
            f"{res.n_sims:,} simulations  |  σ = {res.sigma_used*100:.2f}%  |  "
            f"30-yr ruin: {res.ruin_probability*100:.1f}%  |  "
            f"Ruin first >1%: {thresh_1pct}  |  >5%: {thresh_5pct}  |  >10%: {thresh_10pct}  |  "
            f"Median final corpus: Rs {res.median_final_corpus:,.0f}L  |  "
            f"P5 final corpus: Rs {res.p5_final_corpus:,.0f}L"
        )
        ax.set_title(summary, fontsize=9, pad=12, color="#2c3e50")

    # ── Helper ────────────────────────────────────────────────────────────────

    def _annotate_ruin(self, ax, res):
        """Add a small ruin-probability annotation box to a corpus chart."""
        rp = res.ruin_probability * 100
        colour = "#c0392b" if rp > 5 else "#e67e22" if rp > 1 else "#27ae60"
        ax.text(
            0.02, 0.04,
            f"30-yr ruin probability: {rp:.1f}%",
            transform=ax.transAxes, fontsize=10, fontweight="bold",
            color=colour,
            bbox=dict(boxstyle="round,pad=0.4", facecolor="white",
                      edgecolor=colour, alpha=0.9)
        )

# ── Allocation Drift / Glide-Path Chart ───────────────────────────────────────

class AllocationDriftChart(ChartWindow):
    """
    Portfolio drift chart — stacked area showing fund weights year-by-year
    across the 30-year horizon.  Requires a GlidePath object.

    Views:
      0  Stacked Area (fund weights)
      1  Top-10 Fund Weight Lines
      2  Debt / Equity / Other Composition
    """

    VIEWS = [
        "Stacked Area — Fund Weights",
        "Top-10 Fund Weight Lines",
        "Debt / Equity / Other Bands",
    ]

    def __init__(self, glide_path, state, parent=None):
        """
        Parameters
        ----------
        glide_path : GlidePath
        state      : AppState (used for fund_type lookup)
        """
        super().__init__("Allocation Glide Path — Portfolio Drift", parent)
        self.gp    = glide_path
        self.state = state

        self.combo = QComboBox()
        self.combo.addItems(self.VIEWS)
        self.combo.currentIndexChanged.connect(self._refresh)
        self.ctrl_bar.addWidget(QLabel("View:"))
        self.ctrl_bar.addWidget(self.combo)
        self.ctrl_bar.addStretch()
        self._refresh()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _weight_matrix(self):
        """
        Returns (years, fund_names, matrix).
        years      : list[int] 1–30
        fund_names : sorted list of all fund names that appear in the glide path
        matrix     : dict{fund_name: list[float]} — weight per year (0 if absent)
        """
        years = list(range(1, 31))
        all_names: set = set()
        yearly_weights = {}
        for y in years:
            w = self.gp.weights_for_year(y)
            yearly_weights[y] = w
            all_names.update(w.keys())

        fund_names = sorted(all_names)
        matrix = {fn: [yearly_weights[y].get(fn, 0.0) for y in years]
                  for fn in fund_names}
        return years, fund_names, matrix

    def _fund_type(self, fund_name: str) -> str:
        for ac in self.state.allocation_chunks:
            for f in ac.funds:
                if f.name == fund_name:
                    return f.fund_type
        return "other"

    # ── chunk boundary helper ─────────────────────────────────────────────────

    def _draw_chunk_boundaries(self, ax):
        """Draw vertical dashed lines at each chunk boundary."""
        boundaries = set()
        for ac in self.state.allocation_chunks:
            if ac.year_to < 30:
                boundaries.add(ac.year_to + 0.5)
        for x in boundaries:
            ax.axvline(x, color="#888", linewidth=1.0, linestyle="--", alpha=0.6)

    def _draw_transition_bands(self, ax, y_top=1.0):
        """Draw shaded bands for glide-path transition years."""
        trans = self.gp.transition_years()
        if not trans:
            return
        # Group consecutive years into bands
        import numpy as np
        if not trans:
            return
        trans = sorted(trans)
        bands = []
        start = trans[0]
        prev  = trans[0]
        for yr in trans[1:]:
            if yr == prev + 1:
                prev = yr
            else:
                bands.append((start, prev))
                start = prev = yr
        bands.append((start, prev))
        for band_start, band_end in bands:
            ax.axvspan(band_start - 0.5, band_end + 0.5,
                       color="#f0e68c", alpha=0.22, zorder=0)

    # ── views ─────────────────────────────────────────────────────────────────

    def _refresh(self):
        self._clear()
        v = self.combo.currentIndex()
        years, fund_names, matrix = self._weight_matrix()

        if v == 0:
            self._draw_stacked(years, fund_names, matrix)
        elif v == 1:
            self._draw_lines(years, fund_names, matrix)
        elif v == 2:
            self._draw_type_bands(years, fund_names, matrix)
        self._draw()

    def _draw_stacked(self, years, fund_names, matrix):
        import numpy as np
        ax = self.fig.add_subplot(111)

        # Top 9 funds by mean weight; rest → "Other Funds"
        mean_w = {fn: float(np.mean(matrix[fn])) for fn in fund_names}
        top9 = sorted(fund_names, key=lambda fn: mean_w[fn], reverse=True)[:9]
        rest = [fn for fn in fund_names if fn not in top9]

        stack_labels = [fn[:40] for fn in top9]
        stack_data   = [matrix[fn] for fn in top9]
        stack_colors = [COLOURS[i % len(COLOURS)] for i in range(len(top9))]

        if rest:
            other_vals = [sum(matrix[fn][yi] for fn in rest) for yi in range(len(years))]
            stack_data.append(other_vals)
            stack_labels.append("Other Funds")
            stack_colors.append("#bdc3c7")

        ax.stackplot(years, *stack_data,
                     labels=stack_labels,
                     colors=stack_colors, alpha=0.82)

        self._draw_chunk_boundaries(ax)
        self._draw_transition_bands(ax)

        ax.set_title("Portfolio Allocation Glide Path — Fund Weight Stack",
                     fontweight="bold", fontsize=12)
        ax.set_xlabel("Financial Year")
        ax.set_ylabel("Portfolio Weight (fraction)")
        ax.set_xlim(1, 30)
        ax.set_ylim(0, 1.02)
        ax.yaxis.set_major_formatter(
            ticker.FuncFormatter(lambda v, _: f"{v*100:.0f}%"))
        ax.legend(fontsize=7, loc="upper right",
                  ncol=2, framealpha=0.7)
        ax.grid(axis="y", alpha=0.2)

        # Annotation: transition bands explanation
        trans = self.gp.transition_years()
        if trans:
            ax.text(0.01, 0.97,
                    f"Yellow bands = glide-path transition years ({len(trans)} yrs)\n"
                    "Dashed lines = chunk boundaries",
                    transform=ax.transAxes, fontsize=7.5, va="top",
                    color="#555",
                    bbox=dict(boxstyle="round,pad=0.3",
                              facecolor="white", alpha=0.7))

    def _draw_lines(self, years, fund_names, matrix):
        import numpy as np
        ax = self.fig.add_subplot(111)

        mean_w = {fn: float(np.mean(matrix[fn])) for fn in fund_names}
        top10 = sorted(fund_names, key=lambda fn: mean_w[fn], reverse=True)[:10]

        for i, fn in enumerate(top10):
            ax.plot(years, [v * 100 for v in matrix[fn]],
                    label=fn[:40],
                    color=COLOURS[i % len(COLOURS)],
                    linewidth=1.8, marker=".", markersize=3)

        self._draw_chunk_boundaries(ax)
        self._draw_transition_bands(ax)

        ax.set_title("Top-10 Fund Weights Over 30 Years",
                     fontweight="bold", fontsize=12)
        ax.set_xlabel("Financial Year")
        ax.set_ylabel("Weight (%)")
        ax.set_xlim(1, 30)
        ax.legend(fontsize=7.5, loc="upper right", ncol=2, framealpha=0.7)
        ax.grid(alpha=0.25)
        ax.yaxis.set_major_formatter(
            ticker.FuncFormatter(lambda v, _: f"{v:.1f}%"))

    def _draw_type_bands(self, years, fund_names, matrix):
        ax = self.fig.add_subplot(111)

        debt_w   = []
        equity_w = []
        other_w  = []
        for yi in range(len(years)):
            d = e = o = 0.0
            for fn in fund_names:
                w = matrix[fn][yi]
                ft = self._fund_type(fn)
                if ft == "debt":
                    d += w
                elif ft == "equity":
                    e += w
                else:
                    o += w
            debt_w.append(d * 100)
            equity_w.append(e * 100)
            other_w.append(o * 100)

        ax.stackplot(years,
                     debt_w, equity_w, other_w,
                     labels=["Debt", "Equity", "Other"],
                     colors=[COLOURS[0], COLOURS[1], "#e67e22"],
                     alpha=0.85)

        self._draw_chunk_boundaries(ax)
        self._draw_transition_bands(ax)

        ax.set_title("Debt / Equity / Other Composition Over 30 Years",
                     fontweight="bold", fontsize=12)
        ax.set_xlabel("Financial Year")
        ax.set_ylabel("Weight (%)")
        ax.set_xlim(1, 30)
        ax.set_ylim(0, 105)
        ax.legend(fontsize=9, loc="upper right")
        ax.grid(axis="y", alpha=0.25)
        ax.yaxis.set_major_formatter(
            ticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))


# ── Rebalancing Cost Chart ────────────────────────────────────────────────────

class RebalancingCostChart(ChartWindow):
    """
    Two-panel chart showing glide-path rebalancing costs year-by-year.

    Panel 1 — Stacked bar per year: STCG tax + LTCG tax + exit loads.
    Panel 2 — Cumulative rebalancing cost vs cumulative tax saved vs FD.

    Views:
      0  Annual rebalancing cost breakdown (stacked bar)
      1  Cumulative cost & savings overview
      2  Turnover (weight change %) per year
    """

    VIEWS = [
        "Annual Rebalancing Cost Breakdown",
        "Cumulative Cost & Savings",
        "Year-by-Year Portfolio Turnover",
    ]

    def __init__(self, yearly_rows, glide_path, state, parent=None):
        """
        Parameters
        ----------
        yearly_rows : list[YearSummary]
        glide_path  : GlidePath
        state       : AppState
        """
        super().__init__("Rebalancing Cost Analysis", parent)
        self.rows = yearly_rows
        self.gp   = glide_path
        self.state = state

        self.combo = QComboBox()
        self.combo.addItems(self.VIEWS)
        self.combo.currentIndexChanged.connect(self._refresh)
        self.ctrl_bar.addWidget(QLabel("View:"))
        self.ctrl_bar.addWidget(self.combo)
        self.ctrl_bar.addStretch()
        self._refresh()

    def _refresh(self):
        self._clear()
        v = self.combo.currentIndex()
        if v == 0:
            self._draw_annual_breakdown()
        elif v == 1:
            self._draw_cumulative()
        elif v == 2:
            self._draw_turnover()
        self._draw()

    def _draw_annual_breakdown(self):
        import numpy as np
        ax = self.fig.add_subplot(111)

        years = [r.year for r in self.rows]
        rebal_tax   = [r.rebalance_tax_paid   for r in self.rows]
        exit_loads  = [getattr(r, 'rebalance_exit_loads', 0.0) for r in self.rows]
        normal_tax  = [r.tax_personal - r.rebalance_tax_paid for r in self.rows]

        # Filter to years where something happened
        # But still show all 30 years for context — just make non-event years transparent
        x = np.arange(len(years))
        w = 0.6

        ax.bar(x, normal_tax,  w, label="Regular SWP Tax",      color=COLOURS[2], alpha=0.70)
        ax.bar(x, rebal_tax,   w, bottom=normal_tax,
               label="Rebalancing Tax (subset)",                  color=COLOURS[6], alpha=0.85)
        ax.bar(x, exit_loads,  w,
               bottom=[normal_tax[i] + rebal_tax[i] for i in range(len(years))],
               label="Exit Loads (glide-path)",                   color="#8e44ad", alpha=0.80)

        ax.set_xticks(x[::2])
        ax.set_xticklabels([str(y) for y in years[::2]], fontsize=8)
        ax.set_title("Annual Tax & Exit Loads — SWP vs Rebalancing",
                     fontweight="bold", fontsize=12)
        ax.set_xlabel("Financial Year")
        ax.set_ylabel("Rs Lakhs")
        ax.legend(fontsize=9)
        ax.grid(axis="y", alpha=0.25)
        ax.yaxis.set_major_formatter(
            ticker.FuncFormatter(lambda v, _: f"{v:.2f}"))

        # Annotate total rebalancing burden
        total_rebal = sum(rebal_tax) + sum(exit_loads)
        total_tax   = sum(r.tax_personal for r in self.rows)
        if total_rebal > 0:
            pct = 100 * total_rebal / max(total_tax, 1e-9)
            ax.text(0.98, 0.97,
                    f"Total rebalancing cost: ₹{total_rebal:.2f}L\n"
                    f"({pct:.1f}% of total 30-yr tax)",
                    transform=ax.transAxes, fontsize=9, va="top", ha="right",
                    color=COLOURS[6],
                    bbox=dict(boxstyle="round,pad=0.3", facecolor="white",
                              edgecolor=COLOURS[6], alpha=0.85))

    def _draw_cumulative(self):
        import numpy as np
        ax  = self.fig.add_subplot(111)
        ax2 = ax.twinx()

        years = [r.year for r in self.rows]

        # Cumulative rebalancing cost
        cum_rebal = np.cumsum([
            r.rebalance_tax_paid + getattr(r, 'rebalance_exit_loads', 0.0)
            for r in self.rows
        ])
        # Cumulative tax saved vs FD
        cum_saved = np.cumsum([r.tax_saved for r in self.rows])
        # Net benefit = saved - rebal_cost
        cum_net   = cum_saved - cum_rebal

        ax.fill_between(years, cum_saved, color=COLOURS[1], alpha=0.35,
                        label="Cumulative tax saved vs FD")
        ax.plot(years, cum_saved, color=COLOURS[1], linewidth=2)
        ax.fill_between(years, cum_rebal, color=COLOURS[6], alpha=0.45,
                        label="Cumulative rebalancing cost")
        ax.plot(years, cum_rebal, color=COLOURS[6], linewidth=2)

        ax2.plot(years, cum_net, color=COLOURS[0], linewidth=2.5,
                 linestyle="-", marker=".", markersize=3,
                 label="Cumulative net benefit (right axis)")
        ax2.axhline(0, color="#aaa", linewidth=0.8, linestyle=":")
        ax2.set_ylabel("Net benefit (Rs Lakhs)", fontsize=9, color=COLOURS[0])
        ax2.yaxis.set_major_formatter(
            ticker.FuncFormatter(lambda v, _: f"{v:.1f}"))
        ax2.tick_params(axis="y", labelcolor=COLOURS[0])

        ax.set_title("Cumulative Tax Savings vs Rebalancing Cost",
                     fontweight="bold", fontsize=12)
        ax.set_xlabel("Financial Year")
        ax.set_ylabel("Rs Lakhs")
        ax.yaxis.set_major_formatter(
            ticker.FuncFormatter(lambda v, _: f"{v:.1f}"))
        ax.grid(alpha=0.25)

        h1, l1 = ax.get_legend_handles_labels()
        h2, l2 = ax2.get_legend_handles_labels()
        ax.legend(h1 + h2, l1 + l2, fontsize=9, loc="upper left")

    def _draw_turnover(self):
        """Show % weight change year-over-year derived from the glide path."""
        import numpy as np
        ax = self.fig.add_subplot(111)

        years = list(range(1, 31))
        turnovers = []
        for y in years:
            curr = self.gp.weights_for_year(y)
            prev = self.gp.weights_for_year(y - 1) if y > 1 else curr
            all_keys = set(curr.keys()) | set(prev.keys())
            tv = sum(abs(curr.get(k, 0.0) - prev.get(k, 0.0)) for k in all_keys)
            turnovers.append(tv * 100)  # as percentage

        colors = [COLOURS[2] if t > 5 else (COLOURS[4] if t > 1 else COLOURS[1])
                  for t in turnovers]
        ax.bar(years, turnovers, color=colors, alpha=0.85)

        # Overlay chunk boundaries
        for ac in self.state.allocation_chunks:
            if ac.year_to < 30:
                ax.axvline(ac.year_to + 0.5, color="#888",
                           linewidth=1.0, linestyle="--", alpha=0.6)

        ax.set_title("Year-by-Year Portfolio Turnover (Sum of |Δweight|)",
                     fontweight="bold", fontsize=12)
        ax.set_xlabel("Financial Year")
        ax.set_ylabel("Turnover (%)")
        ax.set_xlim(0.5, 30.5)
        ax.yaxis.set_major_formatter(
            ticker.FuncFormatter(lambda v, _: f"{v:.1f}%"))
        ax.grid(axis="y", alpha=0.25)

        total_tv = sum(turnovers)
        ax.text(0.98, 0.97,
                f"Total 30-yr turnover: {total_tv:.1f}%\n"
                f"Avg per year: {total_tv/30:.1f}%",
                transform=ax.transAxes, fontsize=9, va="top", ha="right",
                bbox=dict(boxstyle="round,pad=0.3", facecolor="white",
                          edgecolor="#aaa", alpha=0.85))

        # Legend for colours
        from matplotlib.patches import Patch
        legend_elements = [
            Patch(facecolor=COLOURS[2], label=">5% turnover (high)"),
            Patch(facecolor=COLOURS[4], label="1–5% turnover (moderate)"),
            Patch(facecolor=COLOURS[1], label="<1% turnover (low)"),
        ]
        ax.legend(handles=legend_elements, fontsize=8, loc="upper left")