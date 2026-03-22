# Copyright (c) 2025 Uddipan Bagchi. All rights reserved.
# See LICENSE in the project root for license information.package com.costheta.cortexa.action

"""
glide_path.py
=============
Builds the year-by-year portfolio weight schedule (GlidePath) from the
per-chunk target_weights produced by the sticky-portfolio optimizer.

Public API
----------
build_glide_path(chunks, spread_years)  → GlidePath
    Constructs a GlidePath that:
      • Holds chunk.target_weights flat within the stable interior of each chunk.
      • Linearly interpolates weights across a transition window centred on each
        chunk boundary, spreading the rebalance over ``spread_years`` years to
        minimise tax incidence and exit loads.
      • Handles edge cases: first year is a clean buy at Chunk 1 weights (no
        ramp-in); last year holds Chunk N weights until end-of-life (no ramp-out).
      • Truncates overlapping windows symmetrically when chunks are short.

build_flat_glide_path(weights)  → GlidePath
    Returns a GlidePath where every year (1–30) maps to the same weight dict.
    Used for Mode A (buy-and-hold) and single-chunk plans.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models import AllocationChunk, GlidePath


# ─────────────────────────────────────────────────────────────────────────────
# Public helpers
# ─────────────────────────────────────────────────────────────────────────────

def build_flat_glide_path(weights: dict) -> "GlidePath":
    """
    Return a GlidePath where every plan year (1–30) carries the same
    weight dict.  Used for Mode A (singular buy-and-hold).
    """
    from models import GlidePath
    w = dict(weights)   # defensive copy
    return GlidePath(schedule={y: w for y in range(1, 31)})


def _weights_from_funds(chunk) -> dict:
    """
    Derive normalised target_weights from chunk.funds[].allocation.
    Used as a fallback when the Two-Pass optimizer has not been run yet
    (target_weights is empty).  Returns {} if no funds have allocation > 0.
    """
    total = sum(f.allocation for f in (chunk.funds or []) if f.allocation > 0)
    if total < 1e-9:
        return {}
    return {
        f.name: f.allocation / total
        for f in chunk.funds
        if f.allocation > 0
    }


def _effective_weights(chunk) -> dict:
    """
    Return target_weights if populated, otherwise fall back to weights
    derived from chunk.funds[].allocation.
    Guarantees a non-empty dict if the chunk has any funded assets.
    """
    tw = chunk.target_weights or {}
    if tw:
        return dict(tw)
    return _weights_from_funds(chunk)


def build_glide_path(
    chunks:       list,   # list[AllocationChunk]
    spread_years: int,    # total years spanning each boundary transition
) -> "GlidePath":
    """
    Build a year-by-year WITHDRAWAL-WEIGHT schedule from per-chunk target_weights.

    Conceptual model
    ----------------
    The glide path defines the target portfolio state for each year.
    By LINEARLY INTERPOLATING the weights over the `spread_years` window, the
    engine's `_rebalance_portfolio` function calculates a small delta each year.

    Because the SWP engine natively prioritises selling "over-weight" funds,
    the natural monthly withdrawals do the heavy lifting of transitioning the
    portfolio from the old chunk to the new chunk. This completely avoids
    massive one-time Capital Gains Tax events.

    Parameters
    ----------
    chunks : list[AllocationChunk]
        Sorted by year_from ascending (as stored in AppState).
    spread_years : int
        Total width of each transition window.  Must be >= 2.

    Returns
    -------
    GlidePath
    """
    from models import GlidePath

    if not chunks:
        return GlidePath(schedule={y: {} for y in range(1, 31)})

    if len(chunks) == 1:
        w = _effective_weights(chunks[0])
        return GlidePath(schedule={y: w for y in range(1, 31)})

    spread = max(2, spread_years)

    # ── Pre-compute transition windows for each boundary ──────────────────────
    boundaries: list[tuple[int, int, int, dict, dict]] = []

    for k in range(len(chunks) - 1):
        left_chunk  = chunks[k]
        right_chunk = chunks[k + 1]

        w_left_eff  = _effective_weights(left_chunk)
        w_right_eff = _effective_weights(right_chunk)
        if not w_left_eff or not w_right_eff:
            continue

        B = left_chunk.year_to  # last year of left chunk

        # ── FIXED window formula ───────────────────────────────────────────
        # FIX 1: Correct math for both even and odd spreads
        # spread=4: half=2 → left_start=B-1, right_end=B+2  (4 years)
        # spread=5: half=2 → left_start=B-1, right_end=B+3  (5 years)
        left_start = B - (spread // 2) + 1
        right_end  = left_start + spread - 1

        # Clip rules
        left_start = max(left_start, 2)       # year 1 is clean buy
        right_end  = min(right_end, 29)       # year 30 is hold-to-end
        if boundaries:
            prev_right = boundaries[-1][1]
            left_start = max(left_start, prev_right + 1)
        right_end = max(right_end, left_start)

        boundaries.append((left_start, right_end, B, w_left_eff, w_right_eff))

    # ── Build schedule year by year ───────────────────────────────────────────
    year_to_boundary: dict[int, tuple] = {}
    for bnd in boundaries:
        l_start, r_end, _B, _wl, _wr = bnd
        for y in range(l_start, r_end + 1):
            year_to_boundary[y] = bnd

    def _owning_weights(year: int) -> dict:
        for c in chunks:
            if c.year_from <= year <= c.year_to:
                return _effective_weights(c)
        return _effective_weights(chunks[-1])

    schedule: dict[int, dict[str, float]] = {}

    for y in range(1, 31):
        if y == 1:
            schedule[y] = _owning_weights(1)
            continue
        if y == 30:
            schedule[y] = _owning_weights(30)
            continue

        bnd = year_to_boundary.get(y)
        if bnd is None:
            schedule[y] = _owning_weights(y)
        else:
            l_start, r_end, B, w_l, w_r = bnd

            # ── LINEAR INTERPOLATION (The Core Glide Path Fix) ─────────────
            # We smoothly shift from old weights to new weights over the
            # transition window. This is required for SWP tax-alpha to work.
            actual_spread = r_end - l_start + 1
            if actual_spread <= 1:
                t = 1.0 if y > B else 0.0
            else:
                # Step 1 is the first year of the window
                step = y - l_start + 1
                t = step / actual_spread

            # Blend the weights
            interp = {}
            all_funds = set(w_l.keys()) | set(w_r.keys())
            for f in all_funds:
                val = w_l.get(f, 0.0) * (1 - t) + w_r.get(f, 0.0) * t
                if val > 1e-6:
                    interp[f] = val

            schedule[y] = interp

    return GlidePath(schedule=schedule)