# Copyright (c) 2025 Uddipan Bagchi. All rights reserved.
# See LICENSE in the project root for license information.

"""
fetch_amfi_aum.py
─────────────────
Fetches fund-level AUM for ALL open-ended mutual funds from the AMFI
fund-performance API.

  POST https://www.amfiindia.com/gateway/pollingsebi/api/amfi/fundperformance

  Payload: {maturityType, category, subCategory, mfid, reportDate}
    maturityType : 1 = Open Ended  (only type with fund-level AUM data)
    category     : 1=Equity 2=Debt 3=Hybrid 4=Solution-Oriented 5=Other
    subCategory  : see SUBCATEGORIES dict below
    mfid         : 0 = All AMCs
    reportDate   : "DD-Mon-YYYY"  — must be a past business day

  Response: { "data": [ { "schemeName": "...", "dailyAUM": 39014.95, … } ] }

Public API
----------
fetch_amfi_aum(output_dir, report_date, min_aum_cr, progress_cb, stop_flag)
    → (csv_path, stats_dict)

CLI:
    python fetch_amfi_aum.py --output-dir /path [--date DD-Mon-YYYY] [--min-aum 0]
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Optional

import requests

# ── Constants ─────────────────────────────────────────────────────────────────
from configuration import config

PERF_URL        = "https://www.amfiindia.com/gateway/pollingsebi/api/amfi/fundperformance"
REQUEST_TIMEOUT = 30
SLEEP_BETWEEN   = config.amfi_sleep_between_calls
OUTPUT_SUBDIR   = "Schemes_and_Funds"
OUTPUT_FILE     = "amfi_aum.csv"

HEADERS = {
    "Content-Type": "application/json",
    "Origin":       "https://www.amfiindia.com",
    "Referer":      "https://www.amfiindia.com/polling/amfi/fund-performance",
    "User-Agent":   (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/133.0.0.0 Safari/537.36"
    ),
}

# ── Complete AMFI Open-Ended taxonomy (maturityType=1) ────────────────────────
# Verified from live getsubcategory API responses, Feb 2026.
# Structure: (category_id, category_name) → [(subcat_id, subcat_name), ...]
SUBCATEGORIES: dict[tuple[int, str], list[tuple[int, str]]] = {
    (1, "Equity"): [
        (1,  "Large Cap"),
        (2,  "Large & Mid Cap"),
        (3,  "Flexi Cap"),
        (4,  "Multi Cap"),
        (5,  "Mid Cap"),
        (6,  "Small Cap"),
        (7,  "Value"),
        (8,  "ELSS"),
        (9,  "Contra"),
        (10, "Dividend Yield"),
        (11, "Focused"),
        (12, "Sectoral / Thematic"),
    ],
    (2, "Debt"): [
        (13, "Long Duration"),
        (14, "Medium to Long Duration"),
        (15, "Short Duration"),
        (16, "Medium Duration"),
        (17, "Money Market"),
        (18, "Low Duration"),
        (19, "Ultra Short Duration"),
        (20, "Liquid"),
        (21, "Overnight"),
        (22, "Dynamic Bond"),
        (23, "Corporate Bond"),
        (24, "Credit Risk"),
        (25, "Banking and PSU"),
        (26, "Floater"),
        (27, "FMP"),
        (28, "Gilt"),
        (29, "Gilt with 10 year constant duration"),
    ],
    (3, "Hybrid"): [
        (30, "Aggressive Hybrid"),
        (31, "Conservative Hybrid"),
        (32, "Equity Savings"),
        (33, "Arbitrage"),
        (34, "Multi Asset Allocation"),
        (35, "Dynamic Asset Allocation or Balanced Advantage"),
        (40, "Balanced Hybrid"),
    ],
    (4, "Solution-Oriented"): [
        (36, "Children's"),
        (37, "Retirement"),
    ],
    (5, "Other"): [
        (38, "Index Funds ETFs"),
        (39, "FoFs (Overseas/Domestic)"),
    ],
}

# Flat list of all (category_id, cat_name, subcat_id, subcat_name) combos
ALL_COMBOS: list[tuple[int, str, int, str]] = [
    (cat_id, cat_name, sc_id, sc_name)
    for (cat_id, cat_name), subcats in SUBCATEGORIES.items()
    for sc_id, sc_name in subcats
]

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Core fetch
# ─────────────────────────────────────────────────────────────────────────────

def _default_report_date() -> str:
    """One week before today as DD-Mon-YYYY (e.g. '17-Feb-2026')."""
    return (date.today() - timedelta(days=7)).strftime("%d-%b-%Y")


def _fetch_one(
    category:    int,
    subCategory: int,
    report_date: str,
    timeout:     int = REQUEST_TIMEOUT,
) -> list[dict]:
    """
    POST fundperformance for one (category, subCategory) with mfid=0 (all AMCs).
    Returns the raw list of fund records, or [] on any error.
    """
    payload = {
        "maturityType": 1,          # Open Ended
        "category":     category,
        "subCategory":  subCategory,
        "mfid":         0,          # 0 = all AMCs
        "reportDate":   report_date,
    }
    r = requests.post(PERF_URL, json=payload, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if data.get("validationStatus") != "SUCCESS":
        raise ValueError(
            f"API returned non-success: {data.get('validationMsg')}"
        )
    return data.get("data") or []


def _fetch_all_aum(
    report_date: str,
    progress_cb: Optional[Callable[[str], None]] = None,
    stop_flag:   Optional[Callable[[], bool]]    = None,
) -> dict[str, float]:
    """
    Iterate every (category, subCategory) combination and collect
    schemeName → max(dailyAUM) across all records.

    Returns dict: schemeName (str) → AUM in crores (float).
    """
    def _emit(msg: str):
        log.info(msg)
        if progress_cb:
            progress_cb(msg)

    aum_map: dict[str, float] = {}
    total = len(ALL_COMBOS)

    for idx, (cat_id, cat_name, sc_id, sc_name) in enumerate(ALL_COMBOS, 1):
        if stop_flag and stop_flag():
            _emit("⏹  Aborted by user.")
            break

        label = f"{cat_name} / {sc_name}"
        _emit(f"[{idx}/{total}] {label}")

        try:
            records = _fetch_one(cat_id, sc_id, report_date)
            added = 0
            for rec in records:
                name = (rec.get("schemeName") or "").strip()
                aum  = rec.get("dailyAUM")
                if not name or aum is None:
                    continue
                aum = float(aum)
                # Keep max AUM if same fund appears in multiple subcategories
                if name not in aum_map or aum > aum_map[name]:
                    aum_map[name] = aum
                    added += 1
            _emit(f"  → {len(records)} funds, {added} new/updated")
        except Exception as exc:
            _emit(f"  ⚠  Failed: {exc}")

        time.sleep(SLEEP_BETWEEN)

    _emit(f"\n  Total unique funds with AUM data: {len(aum_map):,}")
    return aum_map


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def fetch_amfi_aum(
    output_dir:  Optional[str | Path]            = None,
    report_date: Optional[str]                   = None,
    min_aum_cr:  float                           = 0.0,
    progress_cb: Optional[Callable[[str], None]] = None,
    stop_flag:   Optional[Callable[[], bool]]    = None,
) -> tuple[Path, dict]:
    """
    Fetch AUM for all open-ended mutual funds from the AMFI fund-performance API.

    Parameters
    ----------
    output_dir  : Parent directory; CSV goes to <output_dir>/Schemes_and_Funds/amfi_aum.csv
    report_date : "DD-Mon-YYYY" — must be a past business day (not today, not a holiday).
                  Defaults to 7 days before today.
    min_aum_cr  : If > 0, only write rows with AUM ≥ this value.
    progress_cb : callable(str) for live log streaming to a GUI.
    stop_flag   : callable() → bool; return True to abort.

    Returns
    -------
    (csv_path, stats)
        stats keys: total_funds (int), kept_funds (int),
                    report_date (str), csv_path (str).

    Raises
    ------
    requests.RequestException  on network failure.
    ValueError                 if API returns no data (wrong date, holiday, etc.).
    """
    if output_dir is None:
        output_dir = Path(__file__).parent
    output_dir = Path(output_dir)
    subdir     = output_dir / OUTPUT_SUBDIR
    csv_path   = subdir / OUTPUT_FILE

    if report_date is None:
        report_date = _default_report_date()

    def _emit(msg: str):
        log.info(msg)
        if progress_cb:
            progress_cb(msg)

    _emit(f"AMFI AUM fetch  —  report date: {report_date}")
    _emit(f"Output: {csv_path}")
    if min_aum_cr > 0:
        _emit(f"Filter: AUM ≥ ₹{min_aum_cr:,.0f} Cr")
    _emit(f"Calls to make: {len(ALL_COMBOS)} subcategories × mfid=0 (all AMCs)\n")

    # Fetch
    aum_map = _fetch_all_aum(
        report_date, progress_cb=progress_cb, stop_flag=stop_flag
    )

    if not aum_map:
        raise ValueError(
            f"No AUM data returned for report date '{report_date}'.\n"
            "Possible causes: market holiday, future date, or network issue.\n"
            "Try a different date (must be a past business day)."
        )

    # Filter + sort descending by AUM
    rows = sorted(aum_map.items(), key=lambda x: -x[1])
    if min_aum_cr > 0:
        rows = [(n, a) for n, a in rows if a >= min_aum_cr]

    # Write CSV
    subdir.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Fund Name", "AUM (Rs Cr)", "Report Date"])
        for name, aum in rows:
            w.writerow([name, f"{aum:.2f}", report_date])

    stats = {
        "total_funds": len(aum_map),
        "kept_funds":  len(rows),
        "report_date": report_date,
        "csv_path":    str(csv_path),
    }
    _emit(
        f"\n✓ Done.  {len(rows):,} funds written"
        + (f" (≥ ₹{min_aum_cr:,.0f} Cr)" if min_aum_cr > 0 else " (all funds)")
        + f"\n  Output: {csv_path}"
    )
    return csv_path, stats


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _cli():
    parser = argparse.ArgumentParser(
        description=(
            "Fetch fund-level AUM from AMFI fund-performance API.\n"
            "Output: <output-dir>/Schemes_and_Funds/amfi_aum.csv\n\n"
            "reportDate must be a past business day (not today, not a holiday)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--output-dir", "-o", default=None,
                        help="Output directory.")
    parser.add_argument("--date", default=None,
                        metavar="DD-Mon-YYYY",
                        help="Report date (default: 7 days before today).")
    parser.add_argument("--min-aum", type=float, default=0,
                        help="Min AUM in crores (0 = write all funds).")
    args = parser.parse_args()

    # Only log to stdout if we're NOT using print as progress_cb
    # (otherwise every message appears twice).
    logging.basicConfig(level=logging.WARNING, stream=sys.stdout)

    try:
        fetch_amfi_aum(
            output_dir  = args.output_dir,
            report_date = args.date,
            min_aum_cr  = args.min_aum,
            progress_cb = print,
        )
        sys.exit(0)
    except Exception as exc:
        print(f"\n✗  Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _cli()
