"""
get_amfi_fund_schemes_names.py
──────────────────────────────
Downloads the AMFI NAVAll.txt feed and extracts a de-duplicated list of
(Fund Type, Fund Name) pairs.

Optionally filters to funds above a minimum AUM threshold by cross-referencing
fetch_amfi_aum.py, which queries the AMFI fund-performance API.

Public API
----------
fetch_scheme_fund_names(output_dir, ...)
    → (csv_path, stats)
    Writes Schemes_and_Funds/mutual_funds.csv  (all funds, no AUM column).

fetch_scheme_fund_names_filtered(output_dir, min_aum_cr, report_date, ...)
    → (all_csv, filtered_csv, stats)
    Downloads NAVAll.txt (all funds), fetches AUM via fund-performance API,
    and writes mutual_funds_min<N>cr.csv with only funds ≥ min_aum_cr.

CLI:
    python get_amfi_fund_schemes_names.py --output-dir /path [--min-aum 1000] [--date DD-Mon-YYYY]
"""

from __future__ import annotations

import csv
import difflib
import logging
import re
import sys
import time
from pathlib import Path
from typing import Callable, Optional

import requests

AMFI_URL        = "https://portal.amfiindia.com/spages/NAVAll.txt"
OUTPUT_FILE     = "mutual_funds.csv"
OUTPUT_SUBDIR   = "Schemes_and_Funds"


def _norm_name(s: str) -> str:
    """Collapse multiple whitespace into single space and strip."""
    return re.sub(r"\s+", " ", s).strip()
REQUEST_TIMEOUT = 30
MAX_RETRIES     = 3
RETRY_DELAY     = 5

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# NAVAll.txt download + parse
# ─────────────────────────────────────────────────────────────────────────────

def _download_nav_text(
    url: str = AMFI_URL,
    timeout: int = REQUEST_TIMEOUT,
    max_retries: int = MAX_RETRIES,
    retry_delay: int = RETRY_DELAY,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> str:
    def _emit(msg):
        log.info(msg)
        if progress_cb:
            progress_cb(msg)

    for attempt in range(1, max_retries + 1):
        try:
            _emit(f"Downloading AMFI NAVAll.txt (attempt {attempt}/{max_retries}) …")
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            if len(resp.text) < 1000:
                raise ValueError(f"Response suspiciously short ({len(resp.text)} bytes).")
            _emit(f"  Downloaded {len(resp.text):,} bytes.")
            return resp.text
        except (requests.RequestException, ValueError) as exc:
            _emit(f"  ⚠  Attempt {attempt} failed: {exc}")
            if attempt < max_retries:
                _emit(f"  Retrying in {retry_delay}s …")
                time.sleep(retry_delay)
            else:
                raise


# Trailing suffixes to strip iteratively from NAVAll scheme names.
# These are plan/option/variant suffixes that the AMFI fund-performance API
# does not include in its schemeName — we strip them so names match.
_SUFFIX_PATTERNS = [re.compile(p, re.IGNORECASE) for p in [
    r"\s+(?:direct|regular)\s+plan$",          # "... Direct Plan" / "... Regular Plan"
    r"\s+\(direct\)$",                          # "... (Direct)"
    r"\s+\(regular\)$",                         # "... (Regular)"
    r"\s+growth\s*(?:plan|option)?$",            # "... Growth" / "... Growth Option"
    r"\s+idcw\s*(?:plan|option)?$",              # "... IDCW"
    r"\s+bonus\s*(?:plan|option)?$",             # "... Bonus"
    r"\s+(?:daily|weekly|fortnightly|fort\s+nightly|"
     r"monthly|quarterly|half[\s-]yearly|annual)"
     r"\s*(?:idcw|dividend|reinvestment|payout)?$",  # frequency suffixes
    r"\s+plan\s+[a-z](?:\s+redemption.*)?$",    # "... Plan C Redemption ..."
    r"\s+unclaimed\s+(?:idcw|redemption).*$", # "... Unclaimed Redemption ..."
    r"\s+\(unclaimed\)$",
    r"\s+institutional(?:\s+plus|\s+premium)?(?:\s+fund|\s+plan)?$",
    r"\s+retail(?:\s+plan|\s+plus)?$",
    r"\s+\(\d+/\d+/\d+\)$",                   # maturity date "(28/2/25)"
]]

def _strip_plan_suffixes(name: str) -> str:
    """
    Iteratively strip trailing plan/option/variant suffixes from a fund name
    until no more patterns match.  Stops if result would be fewer than 3 words
    (prevents over-stripping fund names that contain the suffix words as core).
    """
    s = _norm_name(name)
    prev = None
    while s != prev:
        prev = s
        for pat in _SUFFIX_PATTERNS:
            candidate = pat.sub("", s).strip()
            # Guard: never strip down to fewer than 3 words
            if candidate and len(candidate.split()) >= 3:
                s = candidate
            elif candidate and len(candidate.split()) >= len(s.split()):
                # No change or grew — safe
                s = candidate
    return s


# ─────────────────────────────────────────────────────────────────────────────
# Regular plan filter — exclude Regular (non-Direct) plan variants
# ─────────────────────────────────────────────────────────────────────────────

# Patterns that identify a raw NAVAll scheme name as a Regular plan variant.
# Applied to the ORIGINAL scheme name (parts[3]) BEFORE suffix stripping,
# so we can reliably detect " - Regular Plan" / " - Regular" / "(Regular)".
#
# Important: funds whose *product name* contains "Regular" (e.g.
# "ICICI Prudential Regular Savings Fund") are NOT filtered — the patterns
# only match when "Regular" appears as a trailing plan/variant indicator.
_REGULAR_PLAN_RE = re.compile(
    r"""(?ix)                        # case-insensitive, verbose
    (?:
        \bRegular\s+Plan\b           # "Regular Plan" anywhere
      | -\s*Regular\s*(?:Plan\s*)?$  # "- Regular" or "- Regular Plan" at end
      | \(Regular(?:\s+Plan)?\)      # "(Regular)" or "(Regular Plan)"
      | \bRegular\s*$                # bare trailing "Regular"
    )
    """,
)

# Whitelist phrases where "Regular" is a product name, not a plan variant.
_REGULAR_NAME_WHITELIST_PHRASES = [
    "regular savings",
]

def _is_regular_plan(raw_scheme_name: str) -> bool:
    """
    Return True if the raw NAVAll scheme name is a Regular plan variant.

    Distinguishes:
      ✗ "Mirae Asset Money Market Fund - Regular Plan - Growth"   → filter
      ✗ "Mirae Asset Money Market Fund Regular"                   → filter
      ✗ "Motilal Oswal Gold and Silver FoF(Regular Plan) - Growth"→ filter
      ✓ "ICICI Prudential Regular Savings Fund - Direct - Growth" → keep
      ✓ "Aditya Birla Sun Life Regular Savings Fund"              → keep
    """
    lower = raw_scheme_name.lower()
    for phrase in _REGULAR_NAME_WHITELIST_PHRASES:
        if phrase in lower:
            return False
    return bool(_REGULAR_PLAN_RE.search(raw_scheme_name))


def _parse_nav_text(
    text: str,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> list[tuple[str, str]]:
    """
    Parse NAVAll.txt → de-duplicated (scheme_type, base_fund_name) list.

    Base name extraction (two-step):
      1. Split on first ' - ' to drop plan suffixes encoded after a dash.
      2. Apply _strip_plan_suffixes() to remove trailing Direct/Regular/Growth/
         IDCW/Bonus/frequency/Retail/Institutional suffixes that appear without
         a dash separator in some older or renamed scheme names.
    """
    def _emit(msg):
        log.info(msg)
        if progress_cb:
            progress_cb(msg)

    fund_list: list[tuple[str, str]] = []
    seen: set[tuple[str, str]]       = set()
    current_scheme: Optional[str]    = None
    line_count = data_rows = skipped = 0
    regular_filtered = 0

    for line in text.splitlines():
        ls = line.strip()
        line_count += 1
        if not ls or ls.startswith("Scheme Code;"):
            continue
        m = re.search(r'Schemes?\((.+?)\)', ls)
        if m:
            current_scheme = m.group(1).strip()
            continue
        if current_scheme and ";" in ls:
            parts = [p.strip() for p in ls.split(";")]
            if len(parts) < 4 or not parts[3]:
                skipped += 1
                continue
            # ── Skip Regular plan variants (Direct-only universe) ─────
            if _is_regular_plan(parts[3]):
                regular_filtered += 1
                data_rows += 1
                continue
            # Step 1: split on first ' - '
            after_dash = re.split(r"\s*-\s*", parts[3], maxsplit=1)[0].strip()
            # Step 2: strip trailing plan/option suffixes
            base_name = _norm_name(_strip_plan_suffixes(after_dash))
            if not base_name:
                skipped += 1
                continue
            # Case-insensitive dedup: NAVAll has multiple plan variants that
            # collapse to the same base name but with different casing (e.g.
            # "Axis Flexi Cap fund" vs "Axis Flexi Cap Fund").  We keep the
            # first occurrence's casing.
            key = (current_scheme.lower(), base_name.lower())
            if key not in seen:
                seen.add(key)
                fund_list.append((current_scheme, base_name))
            data_rows += 1

    _emit(
        f"  Parsed {line_count:,} lines → "
        f"{data_rows:,} data rows, "
        f"{len(fund_list):,} unique (scheme, fund) pairs, "
        f"{regular_filtered:,} Regular plan variants filtered, "
        f"{skipped} skipped."
    )
    return fund_list


# ─────────────────────────────────────────────────────────────────────────────
# Post-parse cleanup: SEBI type remapping + encoding fixes
# ─────────────────────────────────────────────────────────────────────────────

# NAVAll.txt contains legacy Schemes(...) headers that predate the October 2017
# SEBI Categorization & Rationalization circular.  Funds listed under these
# headers are either:
#   (a) Re-classifiable to a proper SEBI sub-category (remap), or
#   (b) Defunct closed-end / interval schemes no longer accepting investment (drop).
#
# Direct remaps — legacy type → standard SEBI sub-category:
_LEGACY_TYPE_MAP: dict[str, str] = {
    "ELSS":         "Equity Scheme - ELSS",
    "Gilt":         "Debt Scheme - Gilt Fund",
    "Money Market": "Debt Scheme - Money Market Fund",
}

# Legacy types with no clear single SEBI target.  Funds under these headers
# are mostly closed-end FMPs, interval plans, or pre-2017 relics.  They are
# separated out: those matching closed-end patterns are dropped outright;
# the remainder go to an unclassified CSV for Groww-based reclassification.
_LEGACY_TYPES_TO_DROP: set[str] = {
    "Income",
    "Growth",
}

# ── Closed-end / defunct fund name patterns ──────────────────────────────────
# Funds under legacy types ("Income", "Growth") whose names match any of these
# patterns are almost certainly closed-end, matured, or defunct schemes that
# cannot be invested in.  They are dropped outright rather than sent to the
# Groww reclassification step.
#
# Patterns were derived empirically from the full 412-fund legacy list:
#   210 FMPs, 45 interval, 54 fixed-term/fixed-income, 33 fixed-horizon,
#   17 capital-protection, 14 dual-advantage, 7 IL&FS, 6 Reliance (pre-Nippon),
#   plus smaller groups.  Only ~12 funds genuinely need a Groww lookup.
_CLOSED_END_PATTERNS = [re.compile(p, re.IGNORECASE) for p in [
    r'\bFMP\b|Fixed Maturity|Fixed Maturit',         # FMPs (incl. typos)
    r'\bInterval\b',                                  # Interval funds
    r'Fixed Horizon',                                 # Fixed Horizon funds
    r'Fixed Term|Fixed Income|\bFIIF\b|\bFTIF\b'
        r'|F\s+I\s+I\s+F|\bFTS\b',                   # Fixed Term / Income series
    r'Capital Protection',                             # Capital Protection Oriented
    r'Dual Advantage',                                 # Dual Advantage Fixed Tenure
    r'\bIL&FS\b|\bIL.FS\b',                           # IL&FS (defunct company)
    r'^Reliance\b',                                    # Reliance → renamed to Nippon
    r'Capital Builder',                                # Closed-end capital builder series
    r'\bMIP\b',                                        # Monthly Income Plan (legacy)
    r'Charity\b',                                      # Charity funds (HDFC Cancer Cure etc.)
    r'Multiple Yield',                                 # Closed-end hybrid
    r'Child Care Plan',                                # Legacy child care schemes
]]


def _is_closed_end_legacy(fund_name: str) -> bool:
    """Return True if fund_name matches a known closed-end / defunct pattern."""
    for pat in _CLOSED_END_PATTERNS:
        if pat.search(fund_name):
            return True
    # Stub names (≤ 2 words) under legacy types are parse artifacts
    if len(fund_name.split()) <= 2:
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Groww-based reclassification for genuinely ambiguous legacy funds
# ─────────────────────────────────────────────────────────────────────────────

_GROWW_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _fund_name_to_slug(name: str) -> str:
    """Convert a fund name to a Groww URL slug."""
    s = name.lower().strip()
    s = s.replace("&", "and")
    s = re.sub(r"[().'',\"]", "", s)       # drop parens, quotes, dots, commas
    s = re.sub(r"[^a-z0-9\s-]", " ", s)    # anything else non-alphanumeric → space
    s = re.sub(r"\s+", "-", s.strip())      # spaces → hyphens
    s = re.sub(r"-{2,}", "-", s)            # collapse multiple hyphens
    return s.strip("-")


def _build_groww_url(fund_name: str) -> str:
    return f"https://groww.in/mutual-funds/{_fund_name_to_slug(fund_name)}-direct-growth"


def _fetch_groww_page(url: str, timeout: int = 15) -> Optional[str]:
    """Fetch Groww page HTML.  Returns None on 404 / connection errors."""
    try:
        resp = requests.get(url, timeout=timeout, headers=_GROWW_HEADERS)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.text
    except requests.RequestException:
        return None


def _check_sip_status(html: str) -> str:
    """
    Returns "traded", "not_supported", or "unknown" based on the
    "Min. for SIP" value on a Groww fund page.
    """
    idx = html.find("Min. for SIP")
    if idx < 0:
        idx = html.find("Min for SIP")
    if idx < 0:
        return "unknown"
    after = html[idx:idx + 500]
    if re.search(r"not\s+supported", after, re.IGNORECASE):
        return "not_supported"
    if re.search(r"(?:₹|&#x20[Bb]9;?|&#8377;?|Rs\.?)\s*[\d,]+", after):
        return "traded"
    return "unknown"


def _detect_instrument_type(html: str) -> Optional[str]:
    """Detect Equity / Debt / Hybrid from a Groww fund page."""
    # Strategy 1: instrument_name in JSON / script blocks
    m = re.search(
        r'["\']?instrument_name["\']?\s*[:=]\s*["\']?(\w+)',
        html, re.IGNORECASE,
    )
    if m:
        return m.group(1).capitalize()

    # Strategy 2: "Instruments" column majority in holdings table
    instruments = re.findall(
        r'(?:Instruments|instrument)["\s:>]+(\w+)',
        html, re.IGNORECASE,
    )
    if instruments:
        equity_count = sum(1 for i in instruments if i.lower() == "equity")
        if equity_count > len(instruments) * 0.5:
            return "Equity"

    # Strategy 3: "is a Equity/Debt/Hybrid Mutual Fund Scheme" in page text
    m2 = re.search(
        r'is\s+(?:a|an)\s+(Equity|Debt|Hybrid)\s+Mutual\s+Fund',
        html, re.IGNORECASE,
    )
    if m2:
        return m2.group(1).capitalize()

    # Strategy 4: category label right after <h1> heading
    m3 = re.search(
        r'<h1[^>]*>[^<]+</h1>\s*(?:<[^>]*>)*\s*(Equity|Debt|Hybrid)',
        html, re.IGNORECASE,
    )
    if m3:
        return m3.group(1).capitalize()

    return None


# Mapping from (legacy_type, instrument_type) → SEBI sub-category.
_GROWW_SEBI_MAP: dict[tuple[str, str], str] = {
    ("Growth", "Equity"):  "Equity Scheme - Sectoral/ Thematic",
    ("Growth", "Debt"):    "Debt Scheme - Dynamic Bond",
    ("Growth", "Hybrid"):  "Hybrid Scheme - Aggressive Hybrid Fund",
    ("Income", "Equity"):  "Equity Scheme - Sectoral/ Thematic",
    ("Income", "Debt"):    "Debt Scheme - Dynamic Bond",
    ("Income", "Hybrid"):  "Hybrid Scheme - Conservative Hybrid Fund",
}
_GROWW_SEBI_DEFAULT: dict[str, str] = {
    "Growth": "Equity Scheme - Sectoral/ Thematic",
    "Income": "Debt Scheme - Dynamic Bond",
}


def _classify_instrument(legacy_type: str, instrument: Optional[str]) -> str:
    """Map (legacy_type, instrument_type) → SEBI sub-category string."""
    if instrument:
        key = (legacy_type, instrument)
        if key in _GROWW_SEBI_MAP:
            return _GROWW_SEBI_MAP[key]
    return _GROWW_SEBI_DEFAULT.get(legacy_type, "Debt Scheme - Dynamic Bond")


def _reclassify_via_groww(
    unclassified: list[tuple[str, str]],
    delay:        float = 1.0,
    timeout:      int   = 15,
    progress_cb:  Optional[Callable[[str], None]] = None,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[tuple[str, str]]]:
    """
    Look up each unclassified fund on Groww.in and reclassify.

    Returns (reclassified, closed, unresolved):
        reclassified : (SEBI_type, fund_name) — traded funds with new type
        closed       : (legacy_type, fund_name) — SIP not supported
        unresolved   : (legacy_type, fund_name) — 404 or unparseable
    """
    def _emit(msg: str):
        log.info(msg)
        if progress_cb:
            progress_cb(msg)

    if not unclassified:
        return [], [], []

    _emit(f"\n  Groww reclassification: {len(unclassified)} funds to look up ...")

    reclassified: list[tuple[str, str]] = []
    closed:       list[tuple[str, str]] = []
    unresolved:   list[tuple[str, str]] = []

    for i, (legacy_type, fund_name) in enumerate(unclassified, 1):
        url  = _build_groww_url(fund_name)
        html = _fetch_groww_page(url, timeout=timeout)

        if html is None:
            _emit(f"    [{i}/{len(unclassified)}] {fund_name} → not found")
            unresolved.append((legacy_type, fund_name))
            time.sleep(delay * 0.3)
            continue

        sip = _check_sip_status(html)

        if sip == "not_supported":
            _emit(f"    [{i}/{len(unclassified)}] {fund_name} → closed")
            closed.append((legacy_type, fund_name))
            time.sleep(delay * 0.3)
            continue

        instrument = _detect_instrument_type(html)
        sebi_type  = _classify_instrument(legacy_type, instrument)
        _emit(f"    [{i}/{len(unclassified)}] {fund_name} → {sebi_type}")
        reclassified.append((sebi_type, fund_name))
        time.sleep(delay)

    _emit(f"  Groww results: {len(reclassified)} reclassified, "
          f"{len(closed)} closed, {len(unresolved)} unresolved.")
    return reclassified, closed, unresolved

# Encoding fix: NAVAll.txt sometimes serves the right single-quote (U+2019 ')
# encoded as UTF-8 bytes 0xE2 0x80 0x99, which when decoded as latin-1 produce
# the sequence "â€™" or its raw-byte equivalents.  We normalise these to ASCII
# apostrophe so that downstream matching works.
_ENCODING_REPLACEMENTS: list[tuple[str, str]] = [
    ("\u2019",              "'"),   # U+2019 RIGHT SINGLE QUOTATION MARK
    ("\u2018",              "'"),   # U+2018 LEFT SINGLE QUOTATION MARK
    ("â\x80\x99",          "'"),   # UTF-8 bytes of U+2019 mis-decoded as latin-1
    ("â\x80\x98",          "'"),   # UTF-8 bytes of U+2018 mis-decoded as latin-1
    ("\u00e2\u0080\u0099", "'"),   # same, but as Unicode codepoints
    ("\u00e2\u0080\u0098", "'"),   # same, left quote variant
    ("â€™",                "'"),   # rendered mojibake (CP1252 re-interpretation)
    ("â€˜",                "'"),   # rendered mojibake, left quote
]


def _fix_encoding(text: str) -> str:
    """Fix common mojibake/encoding issues in scheme type strings."""
    for old, new in _ENCODING_REPLACEMENTS:
        text = text.replace(old, new)
    return text


def _cleanup_fund_types(
    fund_list:   list[tuple[str, str]],
    progress_cb: Optional[Callable[[str], None]] = None,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[tuple[str, str]]]:
    """
    Post-parse cleanup of the (fund_type, fund_name) list:

    1. Fix encoding issues in fund_type strings (mojibake apostrophes).
    2. Remap legacy NAVAll scheme-category headers to proper SEBI sub-categories.
    3. Separate out funds under legacy types:
       a. Those matching closed-end patterns → dropped (third return list, for logging).
       b. The remainder → unclassified (second return list, for Groww reclassification).
    4. Re-deduplicate after remapping (a fund may now collide with an existing
       entry under the correct SEBI type).

    Returns (cleaned_list, unclassified_list, closed_end_list):
        cleaned_list      : funds with proper SEBI types
        unclassified_list : funds under legacy types needing Groww reclassification
        closed_end_list   : funds identified as closed-end/defunct by name pattern
    """
    def _emit(msg: str):
        log.info(msg)
        if progress_cb:
            progress_cb(msg)

    cleaned       = []
    unclassified  = []
    closed_end    = []
    remapped      = 0
    enc_fixed     = 0

    for fund_type, fund_name in fund_list:
        # Step 1: fix encoding in the type string
        fixed_type = _fix_encoding(fund_type)
        if fixed_type != fund_type:
            enc_fixed += 1
            fund_type = fixed_type

        # Step 2: separate legacy types that have no SEBI mapping
        if fund_type in _LEGACY_TYPES_TO_DROP:
            if _is_closed_end_legacy(fund_name):
                closed_end.append((fund_type, fund_name))
            else:
                unclassified.append((fund_type, fund_name))
            continue

        # Step 3: remap legacy types to SEBI equivalents
        if fund_type in _LEGACY_TYPE_MAP:
            fund_type = _LEGACY_TYPE_MAP[fund_type]
            remapped += 1

        cleaned.append((fund_type, fund_name))

    # Step 4: re-deduplicate (case-insensitive on both type and name)
    seen: set[tuple[str, str]] = set()
    deduped: list[tuple[str, str]] = []
    dupes = 0
    for fund_type, fund_name in cleaned:
        fund_name = _norm_name(fund_name)
        key = (fund_type.lower(), fund_name.lower())
        if key not in seen:
            seen.add(key)
            deduped.append((fund_type, fund_name))
        else:
            dupes += 1

    parts = []
    if remapped:
        parts.append(f"{remapped} legacy types remapped to SEBI categories")
    if closed_end:
        parts.append(f"{len(closed_end)} closed-end/defunct legacy funds dropped")
    if unclassified:
        parts.append(f"{len(unclassified)} legacy funds set aside for reclassification")
    if enc_fixed:
        parts.append(f"{enc_fixed} encoding fixes applied")
    if dupes:
        parts.append(f"{dupes} post-remap duplicates removed")
    if parts:
        _emit(f"  Cleanup: {'; '.join(parts)}.")
    else:
        _emit(f"  Cleanup: no changes needed.")

    return deduped, unclassified, closed_end


def _write_csv(
    rows: list[tuple],
    headers: list[str],
    output_path: Path,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)
    msg = f"  Written {len(rows):,} rows → {output_path}"
    log.info(msg)
    if progress_cb:
        progress_cb(msg)


# ─────────────────────────────────────────────────────────────────────────────
# AUM cross-reference
# ─────────────────────────────────────────────────────────────────────────────

def _build_aum_lookup(
    aum_map: dict[str, float],
) -> Callable[[str], Optional[float]]:
    """
    Return a lookup function that maps a NAVAll base fund name to an AUM value
    from the fund-performance API.

    The API uses full scheme names (e.g. "Axis Liquid Fund") while NAVAll base
    names may differ slightly.  We try:
      1. Exact match.
      2. Normalised case-insensitive match.
      3. difflib best-match with similarity ≥ 0.82.
    """
    # Normalised API names for fuzzy fallback
    norm_api: dict[str, str] = {n.lower().strip(): n for n in aum_map}
    api_names = list(norm_api.keys())

    def lookup(base_name: str) -> Optional[float]:
        # 1. Exact
        if base_name in aum_map:
            return aum_map[base_name]
        # 2. Case-insensitive
        low = base_name.lower().strip()
        if low in norm_api:
            return aum_map[norm_api[low]]
        # 3. Fuzzy
        matches = difflib.get_close_matches(low, api_names, n=1, cutoff=0.82)
        if matches:
            return aum_map[norm_api[matches[0]]]
        return None

    return lookup


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def fetch_scheme_fund_names(
    output_dir:        Optional[str | Path] = None,
    url:               str   = AMFI_URL,
    timeout:           int   = REQUEST_TIMEOUT,
    max_retries:       int   = MAX_RETRIES,
    retry_delay:       int   = RETRY_DELAY,
    reclassify_groww:  bool  = True,
    groww_delay:       float = 1.0,
    progress_cb:       Optional[Callable[[str], None]] = None,
) -> tuple[Path, dict]:
    """
    Download AMFI NAVAll.txt, parse it, reclassify legacy funds via Groww,
    and write mutual_funds.csv (all classified funds).

    Parameters
    ----------
    reclassify_groww : If True (default), look up the small number of
                       genuinely ambiguous legacy funds on Groww.in and
                       merge reclassified ones into the main list.
    groww_delay      : Seconds between Groww requests (default 1.0).

    CSV columns: Fund Type, Fund Name.
    Returns (csv_path, stats).
    """
    if output_dir is None:
        output_dir = Path(__file__).parent
    output_dir = Path(output_dir)
    subdir     = output_dir / OUTPUT_SUBDIR
    csv_path   = subdir / OUTPUT_FILE

    def _emit(msg):
        log.info(msg)
        if progress_cb:
            progress_cb(msg)

    _emit(f"Output: {csv_path}")
    raw_text  = _download_nav_text(url=url, timeout=timeout,
                                   max_retries=max_retries,
                                   retry_delay=retry_delay,
                                   progress_cb=progress_cb)
    fund_list = _parse_nav_text(raw_text, progress_cb=progress_cb)
    if not fund_list:
        raise ValueError("No fund records extracted — AMFI format may have changed.")
    fund_list, unclassified, closed_end = _cleanup_fund_types(fund_list, progress_cb=progress_cb)

    # ── Groww reclassification of ambiguous legacy funds ──────────────────
    groww_reclassified = 0
    groww_closed       = 0
    groww_unresolved   = 0

    if reclassify_groww and unclassified:
        reclass, g_closed, g_unresolved = _reclassify_via_groww(
            unclassified, delay=groww_delay, timeout=timeout,
            progress_cb=progress_cb,
        )
        # Merge reclassified funds into the main list
        fund_list.extend(reclass)
        groww_reclassified = len(reclass)
        # Closed and unresolved join the closed_end pile
        closed_end.extend(g_closed)
        closed_end.extend(g_unresolved)
        groww_closed     = len(g_closed)
        groww_unresolved = len(g_unresolved)
        # Clear unclassified — everything is now resolved
        unclassified = []

    # ── Write outputs ─────────────────────────────────────────────────────
    _write_csv(fund_list, ["Fund Type", "Fund Name"], csv_path, progress_cb)

    # Write unclassified legacy funds if any remain (only when Groww is off)
    unclassified_csv = subdir / "mutual_funds_unclassified.csv"
    if unclassified:
        _write_csv(unclassified, ["Legacy Type", "Fund Name"],
                   unclassified_csv, progress_cb)
        _emit(f"  {len(unclassified):,} unclassified legacy funds → {unclassified_csv}")

    # Write closed-end / dropped funds for reference
    closed_csv = subdir / "mutual_funds_closed_end.csv"
    if closed_end:
        _write_csv(closed_end, ["Legacy Type", "Fund Name"],
                   closed_csv, progress_cb)
        _emit(f"  {len(closed_end):,} closed-end/defunct funds dropped → {closed_csv}")

    fund_types = sorted({ft for ft, _ in fund_list})
    _emit(f"\n✓ Done.  {len(fund_list):,} unique funds across {len(fund_types)} scheme types.")
    return csv_path, {
        "unique_funds":         len(fund_list),
        "fund_types":           fund_types,
        "csv_path":             str(csv_path),
        "unclassified":         len(unclassified),
        "unclassified_csv":     str(unclassified_csv) if unclassified else None,
        "closed_end":           len(closed_end),
        "closed_end_csv":       str(closed_csv) if closed_end else None,
        "groww_reclassified":   groww_reclassified,
        "groww_closed":         groww_closed,
        "groww_unresolved":     groww_unresolved,
    }


def fetch_scheme_fund_names_filtered(
    output_dir:        Optional[str | Path] = None,
    min_aum_cr:        float  = 1000.0,
    report_date:       Optional[str] = None,
    url:               str   = AMFI_URL,
    timeout:           int   = REQUEST_TIMEOUT,
    max_retries:       int   = MAX_RETRIES,
    retry_delay:       int   = RETRY_DELAY,
    reclassify_groww:  bool  = True,
    groww_delay:       float = 1.0,
    progress_cb:       Optional[Callable[[str], None]] = None,
    stop_flag:         Optional[Callable[[], bool]]    = None,
) -> tuple[Path, Path, dict]:
    """
    Download + parse NAVAll.txt, reclassify legacy funds via Groww,
    fetch AUM from AMFI fund-performance API, and write a filtered CSV
    containing only funds with AUM ≥ min_aum_cr.

    Parameters
    ----------
    min_aum_cr       : Minimum AUM in crores.
    report_date      : "DD-Mon-YYYY" — past business day for AUM data.
                       Defaults to 7 days before today.
    reclassify_groww : If True (default), look up ambiguous legacy funds
                       on Groww.in and merge reclassified ones back.
    groww_delay      : Seconds between Groww requests (default 1.0).
    stop_flag        : callable() → bool; return True to abort AUM fetch.

    Returns (all_csv, filtered_csv, stats).
    """
    if output_dir is None:
        output_dir = Path(__file__).parent
    output_dir   = Path(output_dir)
    subdir       = output_dir / OUTPUT_SUBDIR
    all_csv      = subdir / OUTPUT_FILE
    n_label      = str(int(min_aum_cr)) if min_aum_cr == int(min_aum_cr) else str(min_aum_cr)
    filtered_csv = subdir / f"mutual_funds_min{n_label}cr.csv"

    def _emit(msg):
        log.info(msg)
        if progress_cb:
            progress_cb(msg)

    n_steps = 2   # download + AUM; Groww step added dynamically if needed
    step    = 0

    # ── Step 1: download NAVAll.txt and get the full fund list ────────────────
    step += 1
    _emit("═" * 55)
    _emit(f"Step {step} — Download AMFI NAVAll.txt (fund list)")
    _emit("═" * 55)
    raw_text  = _download_nav_text(url=url, timeout=timeout,
                                   max_retries=max_retries,
                                   retry_delay=retry_delay,
                                   progress_cb=progress_cb)
    fund_list = _parse_nav_text(raw_text, progress_cb=progress_cb)
    if not fund_list:
        raise ValueError("No fund records extracted — AMFI format may have changed.")
    fund_list, unclassified, closed_end = _cleanup_fund_types(fund_list, progress_cb=progress_cb)

    # ── Step 2 (optional): Groww reclassification ─────────────────────────────
    if reclassify_groww and unclassified:
        step += 1
        _emit("")
        _emit("═" * 55)
        _emit(f"Step {step} — Reclassify {len(unclassified)} legacy funds via Groww.in")
        _emit("═" * 55)
        reclass, g_closed, g_unresolved = _reclassify_via_groww(
            unclassified, delay=groww_delay, timeout=timeout,
            progress_cb=progress_cb,
        )
        fund_list.extend(reclass)
        closed_end.extend(g_closed)
        closed_end.extend(g_unresolved)
        unclassified = []

    _write_csv(fund_list, ["Fund Type", "Fund Name"], all_csv, progress_cb)

    # Write unclassified legacy funds if any remain (only when Groww is off)
    unclassified_csv = subdir / "mutual_funds_unclassified.csv"
    if unclassified:
        _write_csv(unclassified, ["Legacy Type", "Fund Name"],
                   unclassified_csv, progress_cb)
        _emit(f"  {len(unclassified):,} unclassified legacy funds → {unclassified_csv}")

    # Write closed-end / dropped funds for reference
    closed_csv = subdir / "mutual_funds_closed_end.csv"
    if closed_end:
        _write_csv(closed_end, ["Legacy Type", "Fund Name"],
                   closed_csv, progress_cb)
        _emit(f"  {len(closed_end):,} closed-end/defunct funds dropped → {closed_csv}")

    # ── Next step: fetch AUM from fund-performance API ──────────────────────
    step += 1
    _emit("")
    _emit("═" * 55)
    _emit(f"Step {step} — Fetch AUM from AMFI fund-performance API")
    _emit("═" * 55)

    # Import here so the module works even if fetch_amfi_aum.py is absent
    # (fetch_scheme_fund_names without filtering still works)
    try:
        from fetch_amfi_aum import fetch_amfi_aum, _default_report_date
    except ImportError as e:
        raise ImportError(
            "fetch_amfi_aum.py must be in the same directory as "
            "get_amfi_fund_schemes_names.py to use AUM filtering."
        ) from e

    if report_date is None:
        report_date = _default_report_date()

    aum_csv, aum_stats = fetch_amfi_aum(
        output_dir  = output_dir,
        report_date = report_date,
        min_aum_cr  = 0,            # fetch all; we filter below by matching to fund_list
        progress_cb = progress_cb,
        stop_flag   = stop_flag,
    )

    # Build name → AUM dict from the AUM CSV
    aum_map: dict[str, float] = {}
    with open(aum_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = row["Fund Name"].strip()
            try:
                aum_map[name] = float(row["AUM (Rs Cr)"])
            except (ValueError, KeyError):
                pass

    # ── Step 3: match and filter ──────────────────────────────────────────────
    _emit(f"\n── Matching {len(fund_list):,} NAVAll funds to {len(aum_map):,} AUM records ──")
    lookup       = _build_aum_lookup(aum_map)
    kept         = []
    unmatched    = []   # (fund_type, fund_name) — in NAVAll but no AUM record found
    below        = 0

    for fund_type, fund_name in fund_list:
        aum = lookup(fund_name)
        if aum is None:
            unmatched.append((fund_type, fund_name))
        elif aum >= min_aum_cr:
            kept.append((fund_type, fund_name, f"{aum:.2f}"))
        else:
            below += 1

    # ── Dedup kept list on fund name (case-insensitive) ───────────────────
    # Cross-type duplicates arise when NAVAll lists the same fund under both
    # a current SEBI category and a legacy pre-2018 category.  Keep the row
    # with the standard SEBI type; for same-priority ties, keep first seen.
    _STANDARD_PREFIXES = (
        "Equity Scheme", "Debt Scheme", "Hybrid Scheme",
        "Solution Oriented", "Other Scheme",
    )
    def _type_priority(fund_type: str) -> int:
        """0 = standard SEBI type (preferred), 1 = legacy/other."""
        return 0 if fund_type.startswith(_STANDARD_PREFIXES) else 1

    seen_names: dict[str, int] = {}   # lowered name → index in kept
    deduped: list[tuple] = []
    for row in kept:
        fund_type, fund_name = row[0], row[1]
        key = _norm_name(fund_name).lower()
        if key not in seen_names:
            seen_names[key] = len(deduped)
            deduped.append(row)
        else:
            # Replace if new row has a better (lower) type priority
            existing_idx = seen_names[key]
            if _type_priority(fund_type) < _type_priority(deduped[existing_idx][0]):
                deduped[existing_idx] = row

    n_cross_dupes = len(kept) - len(deduped)
    if n_cross_dupes > 0:
        _emit(f"  Removed {n_cross_dupes} cross-type duplicate(s) by fund name")
    kept = deduped

    _write_csv(
        kept,
        ["Fund Type", "Fund Name", "AUM (Rs Cr)"],
        filtered_csv,
        progress_cb,
    )

    # Write unmatched fund names to a separate CSV for inspection
    unmatched_csv = subdir / "amfi_aum_unmatched.csv"
    _write_csv(
        unmatched,
        ["Fund Type", "Fund Name"],
        unmatched_csv,
        progress_cb,
    )

    stats = {
        "total_funds":     len(fund_list),
        "kept_funds":      len(kept),
        "below_threshold": below,
        "no_aum_match":    len(unmatched),
        "unmatched_csv":   str(unmatched_csv),
        "min_aum_cr":      min_aum_cr,
        "report_date":     report_date,
        "filtered_csv":    str(filtered_csv),
    }
    _emit(
        f"\n✓ Done.\n"
        f"  {len(kept):,} funds kept (AUM ≥ ₹{min_aum_cr:,.0f} Cr)\n"
        f"  {below:,} below threshold\n"
        f"  {len(unmatched):,} could not be matched to AUM data → {unmatched_csv.name}\n"
        f"  All funds  : {all_csv}\n"
        f"  Filtered   : {filtered_csv}\n"
        f"  Unmatched  : {unmatched_csv}"
    )
    return all_csv, filtered_csv, stats


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _cli():
    import argparse
    parser = argparse.ArgumentParser(
        description=(
            "Download AMFI NAVAll.txt and save mutual_funds.csv.\n"
            "With --min-aum, also fetches AUM from AMFI fund-performance API\n"
            "and writes mutual_funds_min<N>cr.csv."
        )
    )
    parser.add_argument("--output-dir", "-o", default=None)
    parser.add_argument("--min-aum", type=float, default=0,
                        help="Min AUM in crores (0 = no filter).")
    parser.add_argument("--date", default=None, metavar="DD-Mon-YYYY",
                        help="AUM report date (default: 7 days before today).")
    parser.add_argument("--no-groww", action="store_true",
                        help="Skip Groww.in reclassification of legacy funds.")
    parser.add_argument("--groww-delay", type=float, default=1.0,
                        help="Seconds between Groww requests (default: 1.0).")
    parser.add_argument("--url",         default=AMFI_URL)
    parser.add_argument("--timeout",     type=int, default=REQUEST_TIMEOUT)
    parser.add_argument("--retries",     type=int, default=MAX_RETRIES)
    parser.add_argument("--retry-delay", type=int, default=RETRY_DELAY)
    args = parser.parse_args()

    # Only log to stdout if we're NOT using print as progress_cb
    # (otherwise every message appears twice).
    logging.basicConfig(level=logging.WARNING, stream=sys.stdout)

    try:
        if args.min_aum > 0:
            fetch_scheme_fund_names_filtered(
                output_dir       = args.output_dir,
                min_aum_cr       = args.min_aum,
                report_date      = args.date,
                reclassify_groww = not args.no_groww,
                groww_delay      = args.groww_delay,
                url=args.url, timeout=args.timeout,
                max_retries=args.retries, retry_delay=args.retry_delay,
                progress_cb = print,
            )
        else:
            fetch_scheme_fund_names(
                output_dir       = args.output_dir,
                reclassify_groww = not args.no_groww,
                groww_delay      = args.groww_delay,
                url=args.url, timeout=args.timeout,
                max_retries=args.retries, retry_delay=args.retry_delay,
                progress_cb = print,
            )
        sys.exit(0)
    except Exception as exc:
        print(f"\n✗  Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _cli()