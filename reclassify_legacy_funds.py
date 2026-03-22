# Copyright (c) 2025 Uddipan Bagchi. All rights reserved.
# See LICENSE in the project root for license information.

"""
reclassify_legacy_funds.py
──────────────────────────
Reads the unclassified legacy funds CSV (Income / Growth) produced by
get_amfi_fund_schemes_names.py and attempts to reclassify each fund by
scraping its Groww.in mutual fund page.

Logic per fund:
  1. Build a Groww URL:
       https://groww.in/mutual-funds/{slug}-direct-growth
     where slug = fund_name.lower().replace(" ","-") with "--" collapsed to "-"
     and problematic characters (parentheses, dots, ampersands, etc.) cleaned.

  2. Fetch the page.  If HTTP 404 or connection error → mark as "not_found".

  3. Look for the text "Min. for SIP" in the page body.
     - If the immediately following value (next div/text) is "Not Supported"
       → fund is closed / no longer traded → mark as "closed".
     - If it contains "₹" followed by a number → fund is actively traded.

  4. For actively traded funds, determine the instrument type:
     a. Search for "instrument_name" in the raw HTML / page source JSON.
        The value after the colon will be "Equity" or something else.
     b. Fallback: look for the category label right after the <h1> fund name
        heading — Groww shows "Equity", "Debt", or "Hybrid" there.
     - If Equity → classify as "Equity Scheme - Value Fund" (Growth legacy)
       or "Equity Scheme - Sectoral/ Thematic" as a catch-all.
     - If not Equity → classify as "Debt Scheme - Dynamic Bond" as a
       catch-all for non-equity legacy income funds.

  5. Write three output CSVs:
     - reclassified_funds.csv    : funds successfully reclassified (SEBI type, name)
     - closed_funds.csv          : funds confirmed closed / not traded
     - unresolved_funds.csv      : funds that couldn't be looked up on Groww

Usage:
    python reclassify_legacy_funds.py --input mutual_funds_unclassified.csv
                                      [--output-dir ./output]
                                      [--delay 1.5]
                                      [--timeout 15]
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests

# ─────────────────────────────────────────────────────────────────────────────
# URL construction
# ─────────────────────────────────────────────────────────────────────────────

def _fund_name_to_slug(name: str) -> str:
    """
    Convert a fund name to a Groww URL slug.

    Examples:
        "ICICI Prudential Value Fund"   → "icici-prudential-value-fund"
        "HDFC FMP 1100D April 2019 (1)" → "hdfc-fmp-1100d-april-2019-1"
        "Franklin India Fixed Maturity Plans Series4"
            → "franklin-india-fixed-maturity-plans-series4"
    """
    s = name.lower().strip()
    # Remove characters that Groww drops from slugs
    s = s.replace("&", "and")
    s = re.sub(r"[().'',\"]", "", s)       # drop parens, quotes, dots, commas
    s = re.sub(r"[^a-z0-9\s-]", " ", s)    # anything else non-alphanumeric → space
    s = re.sub(r"\s+", "-", s.strip())      # spaces → hyphens
    s = re.sub(r"-{2,}", "-", s)            # collapse multiple hyphens
    s = s.strip("-")
    return s


def _build_groww_url(fund_name: str) -> str:
    slug = _fund_name_to_slug(fund_name)
    return f"https://groww.in/mutual-funds/{slug}-direct-growth"


# ─────────────────────────────────────────────────────────────────────────────
# Page fetching + parsing
# ─────────────────────────────────────────────────────────────────────────────

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _fetch_page(url: str, timeout: int = 15) -> Optional[str]:
    """Fetch page HTML.  Returns None on 404 or connection errors."""
    try:
        resp = requests.get(url, timeout=timeout, headers=_HEADERS)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.text
    except requests.RequestException:
        return None


def _check_sip_status(html: str) -> str:
    """
    Look for "Min. for SIP" and determine whether fund is traded.

    Returns:
        "traded"       — ₹ followed by a number (SIP is supported)
        "not_supported" — "Not Supported" text found
        "unknown"      — pattern not found in page
    """
    # Strategy: search for "Min. for SIP" then look at the next chunk of text
    # In the raw HTML, the SIP value is typically in a sibling/child div.

    # Pattern 1: Look in rendered text (the text near "Min. for SIP")
    # Groww uses React SSR so the content is in the HTML.
    idx = html.find("Min. for SIP")
    if idx < 0:
        # Try alternate spellings
        idx = html.find("Min for SIP")
    if idx < 0:
        return "unknown"

    # Grab the next 500 chars after "Min. for SIP"
    after = html[idx:idx + 500]

    # Check for "Not Supported" (case-insensitive)
    if re.search(r"not\s+supported", after, re.IGNORECASE):
        return "not_supported"

    # Check for ₹ followed by a number (₹ is U+20B9, or &#x20B9; or "₹")
    # Groww also uses the HTML entity &#8377; or the literal ₹ character
    if re.search(r"(?:₹|&#x20[Bb]9;?|&#8377;?|Rs\.?)\s*[\d,]+", after):
        return "traded"

    return "unknown"


def _detect_instrument_type(html: str) -> Optional[str]:
    """
    Detect whether the fund is Equity or Debt from the Groww page.

    Strategy (in priority order):
    1. Look for "instrument_name" in JSON-LD or inline scripts → value after colon.
    2. Look for the category label pattern near the fund heading:
       Groww renders: <h1>Fund Name</h1> then a label like "Equity" or "Debt".
    3. Look for "Equity Mutual Fund Scheme" or "Debt Mutual Fund Scheme" in
       the meta description / page body.

    Returns "Equity", "Debt", "Hybrid", or None if undetectable.
    """
    # Strategy 1: instrument_name in JSON / script blocks
    # Pattern: "instrument_name":"Equity" or instrument_name: "Equity"
    m = re.search(
        r'["\']?instrument_name["\']?\s*[:=]\s*["\']?(\w+)',
        html, re.IGNORECASE,
    )
    if m:
        return m.group(1).capitalize()

    # Strategy 2: look for "Instruments" column values in the holdings table
    # Count occurrences of Equity vs non-Equity instruments
    instruments = re.findall(
        r'(?:Instruments|instrument)["\s:>]+(\w+)',
        html, re.IGNORECASE,
    )
    if instruments:
        equity_count = sum(1 for i in instruments if i.lower() == "equity")
        if equity_count > len(instruments) * 0.5:
            return "Equity"

    # Strategy 3: meta description or page text
    # Groww includes "is a Equity Mutual Fund Scheme" or "is a Debt Mutual Fund Scheme"
    m2 = re.search(
        r'is\s+(?:a|an)\s+(Equity|Debt|Hybrid)\s+Mutual\s+Fund',
        html, re.IGNORECASE,
    )
    if m2:
        return m2.group(1).capitalize()

    # Strategy 4: the category label right after the h1 heading
    # In the SSR HTML: <h1>Fund Name</h1>...<span/div>Equity</span/div>
    # In rendered text it appears as a line "Equity" or "Debt" right after the heading
    m3 = re.search(
        r'<h1[^>]*>[^<]+</h1>\s*(?:<[^>]*>)*\s*(Equity|Debt|Hybrid)',
        html, re.IGNORECASE,
    )
    if m3:
        return m3.group(1).capitalize()

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Classification logic
# ─────────────────────────────────────────────────────────────────────────────

# Mapping from (legacy_type, instrument_type) → SEBI sub-category.
# These are "catch-all" buckets; more precise classification would require
# reading the scheme's investment objective, which is not reliably parseable.
_SEBI_RECLASSIFICATION: dict[tuple[str, str], str] = {
    # Growth legacy → mostly equity-oriented schemes
    ("Growth", "Equity"):  "Equity Scheme - Sectoral/ Thematic",
    ("Growth", "Debt"):    "Debt Scheme - Dynamic Bond",
    ("Growth", "Hybrid"):  "Hybrid Scheme - Aggressive Hybrid Fund",
    # Income legacy → mostly debt-oriented schemes
    ("Income", "Equity"):  "Equity Scheme - Sectoral/ Thematic",
    ("Income", "Debt"):    "Debt Scheme - Dynamic Bond",
    ("Income", "Hybrid"):  "Hybrid Scheme - Conservative Hybrid Fund",
}

# Default if instrument type can't be determined
_SEBI_DEFAULT: dict[str, str] = {
    "Growth": "Equity Scheme - Sectoral/ Thematic",
    "Income": "Debt Scheme - Dynamic Bond",
}


def _classify(legacy_type: str, instrument_type: Optional[str]) -> str:
    """Map (legacy_type, instrument_type) → SEBI sub-category string."""
    if instrument_type:
        key = (legacy_type, instrument_type)
        if key in _SEBI_RECLASSIFICATION:
            return _SEBI_RECLASSIFICATION[key]
    return _SEBI_DEFAULT.get(legacy_type, "Debt Scheme - Dynamic Bond")


# ─────────────────────────────────────────────────────────────────────────────
# Main orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def reclassify(
    input_csv:  Path,
    output_dir: Path,
    delay:      float = 1.5,
    timeout:    int   = 15,
    progress_cb = None,
) -> dict:
    """
    Read unclassified funds, look up each on Groww, reclassify or drop.

    Returns stats dict.
    """
    def _emit(msg: str):
        print(msg, flush=True)
        if progress_cb:
            progress_cb(msg)

    # ── Read input ────────────────────────────────────────────────────────
    funds: list[tuple[str, str]] = []
    with open(input_csv, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        for row in reader:
            if row and len(row) >= 2:
                funds.append((row[0].strip(), row[1].strip()))

    _emit(f"Read {len(funds)} unclassified funds from {input_csv}")

    # ── Process each fund ─────────────────────────────────────────────────
    reclassified: list[tuple[str, str, str, str]] = []  # (sebi_type, name, legacy, instrument)
    closed:       list[tuple[str, str, str]]      = []  # (legacy_type, name, url)
    unresolved:   list[tuple[str, str, str, str]]  = [] # (legacy_type, name, url, reason)

    for i, (legacy_type, fund_name) in enumerate(funds, 1):
        url = _build_groww_url(fund_name)
        _emit(f"\n  [{i}/{len(funds)}] {fund_name}")
        _emit(f"    URL: {url}")

        html = _fetch_page(url, timeout=timeout)

        if html is None:
            _emit(f"    → NOT FOUND (404 or connection error)")
            unresolved.append((legacy_type, fund_name, url, "page_not_found"))
            time.sleep(delay * 0.5)  # shorter delay for 404s
            continue

        # Check SIP status
        sip_status = _check_sip_status(html)
        _emit(f"    SIP status: {sip_status}")

        if sip_status == "not_supported":
            _emit(f"    → CLOSED (SIP not supported)")
            closed.append((legacy_type, fund_name, url))
            time.sleep(delay * 0.5)
            continue

        if sip_status == "unknown":
            # Page exists but couldn't parse SIP status — try instrument detection anyway
            _emit(f"    ⚠ Could not determine SIP status — checking instrument type")

        # Detect instrument type
        instrument = _detect_instrument_type(html)
        _emit(f"    Instrument type: {instrument or 'unknown'}")

        if instrument is None and sip_status == "unknown":
            _emit(f"    → UNRESOLVED (no SIP status, no instrument type)")
            unresolved.append((legacy_type, fund_name, url, "unparseable"))
            time.sleep(delay)
            continue

        # Classify
        sebi_type = _classify(legacy_type, instrument)
        _emit(f"    → RECLASSIFIED as: {sebi_type}")
        reclassified.append((sebi_type, fund_name, legacy_type, instrument or "unknown"))

        time.sleep(delay)

    # ── Write outputs ─────────────────────────────────────────────────────
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    reclass_csv = output_dir / "reclassified_funds.csv"
    with open(reclass_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Fund Type", "Fund Name", "Legacy Type", "Instrument"])
        w.writerows(reclassified)
    _emit(f"\n  Reclassified: {len(reclassified)} → {reclass_csv}")

    closed_csv = output_dir / "closed_funds.csv"
    with open(closed_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Legacy Type", "Fund Name", "Groww URL"])
        w.writerows(closed)
    _emit(f"  Closed:       {len(closed)} → {closed_csv}")

    unresolved_csv = output_dir / "unresolved_funds.csv"
    with open(unresolved_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Legacy Type", "Fund Name", "Groww URL", "Reason"])
        w.writerows(unresolved)
    _emit(f"  Unresolved:   {len(unresolved)} → {unresolved_csv}")

    stats = {
        "total":        len(funds),
        "reclassified": len(reclassified),
        "closed":       len(closed),
        "unresolved":   len(unresolved),
    }
    _emit(f"\n✓ Done. {stats['reclassified']} reclassified, "
          f"{stats['closed']} closed, {stats['unresolved']} unresolved "
          f"out of {stats['total']} total.")
    return stats


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Reclassify legacy Income/Growth mutual funds using Groww.in data."
    )
    parser.add_argument(
        "--input", "-i", default="/home/uddipan/PycharmProjects/RetirementTaxPlanning/Schemes_and_Funds/mutual_funds_unclassified.csv",
        help="/home/uddipan/PycharmProjects/RetirementTaxPlanning/Schemes_and_Funds/mutual_funds_unclassified.csv",
    )
    parser.add_argument(
        "--output-dir", "-o", default="/home/uddipan/PycharmProjects/RetirementTaxPlanning/Schemes_and_Funds/",
        help="/home/uddipan/PycharmProjects/RetirementTaxPlanning/Schemes_and_Funds/",
    )
    parser.add_argument(
        "--delay", "-d", type=float, default=1.5,
        help="Delay between requests in seconds (default: 1.5)",
    )
    parser.add_argument(
        "--timeout", "-t", type=int, default=15,
        help="HTTP request timeout in seconds (default: 15)",
    )
    args = parser.parse_args()

    reclassify(
        input_csv=Path(args.input),
        output_dir=Path(args.output_dir),
        delay=args.delay,
        timeout=args.timeout,
    )


if __name__ == "__main__":
    main()
