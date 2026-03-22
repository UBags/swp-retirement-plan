# Copyright (c) 2025 Uddipan Bagchi. All rights reserved.
# See LICENSE in the project root for license information.

"""
Debug script: Add to your project directory and run from there.
  python debug_other.py

Loads the saved project and checks "other" category status.
"""
import json
import sys
from pathlib import Path

# Find the project file
candidates = list(Path(".").glob("*_project.swp.json")) + list(Path(".").glob("*.swp.json"))
if not candidates:
    print("No .swp.json project file found in current directory.")
    print("Run this script from your project directory (e.g. Uddipan_Young/)")
    sys.exit(1)

project_file = candidates[0]
print(f"Loading: {project_file}")

with open(project_file, "r", encoding="utf-8") as f:
    raw = json.load(f)

# Check raw JSON for "other" type funds
print("\n=== RAW JSON CHECK ===")
print(f"allocation_chunks in JSON: {len(raw.get('allocation_chunks', []))}")
for i, ac in enumerate(raw.get("allocation_chunks", [])):
    funds = ac.get("funds", [])
    types = {}
    for f in funds:
        ft = f.get("fund_type", "MISSING")
        types[ft] = types.get(ft, 0) + f.get("allocation", 0)
    print(f"  Chunk {i+1} (yr {ac['year_from']}-{ac['year_to']}): {len(funds)} funds")
    for ft, total in sorted(types.items()):
        print(f"    {ft}: {total:.2f}L")

# Check flat funds list
funds_flat = raw.get("funds", [])
types_flat = {}
for f in funds_flat:
    ft = f.get("fund_type", "MISSING")
    types_flat[ft] = types_flat.get(ft, 0) + f.get("allocation", 0)
print(f"\nFlat funds list: {len(funds_flat)} funds")
for ft, total in sorted(types_flat.items()):
    print(f"  {ft}: {total:.2f}L")

# Check other tax chunks
ind_other = raw.get("individual_other_chunks", [])
huf_other = raw.get("huf_other_chunks", [])
print(f"\nindividual_other_chunks: {ind_other}")
print(f"huf_other_chunks: {huf_other}")

# Load via AppState
from models import AppState
state = AppState.from_dict(raw)

print("\n=== APPSTATE CHECK ===")
print(f"total_debt: {state.total_debt_allocation():.2f}L")
print(f"total_equity: {state.total_equity_allocation():.2f}L")
print(f"total_other: {state.total_other_allocation():.2f}L")

init_funds = state.get_funds_for_year(1)
for f in init_funds:
    if f.allocation > 0:
        print(f"  {f.name[:40]:40s} type={f.fund_type:6s} alloc={f.allocation:.2f}L cagr_5={f.cagr_5}")

# Run engine and check month 0
from engine import Engine
engine = Engine(state)
p_rows, yearly, _, _ = engine.run()

print("\n=== ENGINE OUTPUT ===")
for r in p_rows[:3]:
    print(f"Month {r.month_idx}: D={r.corpus_debt_start:.2f} E={r.corpus_equity_start:.2f} O={r.corpus_other_start:.2f}")

print(f"\nFY1: tax_personal={yearly[0].tax_personal:.2f} corpus_other={yearly[0].corpus_other_personal:.2f}")
print(f"FY2: tax_personal={yearly[1].tax_personal:.2f} corpus_other={yearly[1].corpus_other_personal:.2f}")

# Check if there are "other" funds with wrong type in JSON
print("\n=== FUNDS THAT SHOULD BE 'other' ===")
other_keywords = ["gold", "fof", "dynamic asset", "balanced advantage", "multi asset"]
for f in init_funds:
    name_lower = f.name.lower()
    if any(k in name_lower for k in other_keywords):
        print(f"  {f.name[:50]:50s} → type={f.fund_type} (alloc={f.allocation:.2f}L)")
