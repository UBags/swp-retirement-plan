# Copyright (c) 2025 Uddipan Bagchi. All rights reserved.
# See LICENSE in the project root for license information.package com.costheta.cortexa.action

"""
Quick diagnostic: run from your app directory.
  cd C:\PythonProjects\RetirementTaxPlanning
  python check_pycache.py
"""
import os, sys, hashlib, importlib, shutil
from pathlib import Path

app = Path(os.path.dirname(os.path.abspath(__file__)))
pc = app / "__pycache__"

print("=== Step 1: Check __pycache__ ===")
if pc.exists():
    pyc_files = list(pc.glob("*.pyc"))
    print(f"  Found {len(pyc_files)} .pyc files")
    for p in sorted(pyc_files):
        # Find matching .py
        name = p.stem.split(".")[0]  # e.g. "engine" from "engine.cpython-312.pyc"
        py = app / f"{name}.py"
        if py.exists():
            py_mtime = py.stat().st_mtime
            pyc_mtime = p.stat().st_mtime
            stale = "STALE!" if pyc_mtime < py_mtime else "ok"
            print(f"    {p.name}: {stale}  (pyc={pyc_mtime:.0f}, py={py_mtime:.0f})")

    print(f"\n=== Step 2: Delete __pycache__ ===")
    shutil.rmtree(pc)
    print(f"  Deleted __pycache__")
else:
    print("  No __pycache__ found (clean)")

print(f"\n=== Step 3: Verify engine.py version ===")
sys.path.insert(0, str(app))
import engine

print(f"  Loaded from: {engine.__file__}")
has_cat = hasattr(engine.Engine, '_category_monthly_factor')
has_fund = hasattr(engine.Engine, '_fund_monthly_factor')
print(f"  _category_monthly_factor: {has_cat}")
print(f"  _fund_monthly_factor: {has_fund}")

import models

has_init = hasattr(models.AppState, '_init_funds')
print(f"  models._init_funds: {has_init}")

# Quick sanity: create test state and run
from models import FundEntry, AllocationChunk, ReturnChunk, default_state

state = default_state()
funds = [
    FundEntry(name="TestDebt", fund_type="debt", allocation=100, cagr_5=7.0),
    FundEntry(name="TestEquity", fund_type="equity", allocation=100, cagr_5=12.0),
    FundEntry(name="TestOther", fund_type="other", allocation=100, cagr_5=15.0),
]
state.funds = funds
state.allocation_chunks = [AllocationChunk(1, 30, funds)]
state.return_chunks = [ReturnChunk(1, 30, 0.1133)]

e = engine.Engine(state)
rows, yearly, _, _ = e.run()
r = rows[0]
print(f"\n=== Step 4: Engine test (D=100, E=100, O=100) ===")
print(f"  Month 0: D={r.corpus_debt_start:.2f}  E={r.corpus_equity_start:.2f}  O={r.corpus_other_start:.2f}")

if r.corpus_other_start < 0.01:
    print(f"\n  ✗✗✗ BUG: Other corpus = 0! Engine is broken.")
    print(f"  Try: restart Python, delete __pycache__, and re-run the app.")
else:
    print(f"\n  ✓ Engine is working correctly. Other corpus > 0.")
    print(f"  If you still see Other=0 in the GUI, please save your project,")
    print(f"  close the app, delete __pycache__, and re-open.")