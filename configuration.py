# Copyright (c) 2025 Uddipan Bagchi. All rights reserved.
# See LICENSE in the project root for license information.

"""
Configuration.py
================
Singleton configuration reader for the SWP Financial Planner.

Reads key-value pairs from ``RetirementTaxPlanning.configuration`` located
alongside this file (project root).  Values are auto-parsed into int, float,
or str.

Usage
-----
    from configuration import config, get_project_root

    cess = config.cess_rate                   # float 0.04
    path = get_project_root() / "some_file"   # Path
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional


# ─── Project root ─────────────────────────────────────────────────────────────

def get_project_root() -> Path:
    """Return the project root directory (the directory containing this file)."""
    return Path(os.path.dirname(os.path.abspath(__file__)))


# ─── Configuration singleton ──────────────────────────────────────────────────

_CONFIG_FILENAME = "RetirementTaxPlanning.configuration"


class _Configuration:
    """
    Singleton that lazily loads the configuration properties file.

    Access any property as an attribute::

        config.cess_rate          # 0.04
        config.stcg_holding_months  # 12
        config.allocator_default_input  # "Fund_Metrics_Output.csv"

    If a key is not found, ``AttributeError`` is raised.
    """

    _instance: Optional["_Configuration"] = None
    _loaded: bool = False
    _data: Dict[str, Any] = {}

    def __new__(cls) -> "_Configuration":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    # ── Loading ───────────────────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        config_path = get_project_root() / _CONFIG_FILENAME
        if not config_path.exists():
            import warnings
            warnings.warn(
                f"Configuration file not found: {config_path}. "
                "Using built-in defaults.",
                stacklevel=2,
            )
            return
        self._data = _parse_properties(config_path)

    # ── Attribute access ──────────────────────────────────────────────────────

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        self._ensure_loaded()
        if name in self._data:
            return self._data[name]
        raise AttributeError(
            f"Configuration key '{name}' not found in {_CONFIG_FILENAME}"
        )

    def get(self, name: str, default: Any = None) -> Any:
        """Return a configuration value, or *default* if not found."""
        self._ensure_loaded()
        return self._data.get(name, default)

    def reload(self) -> None:
        """Force re-read of the configuration file (useful for testing)."""
        self._loaded = False
        self._data = {}
        self._ensure_loaded()


def _parse_properties(path: Path) -> Dict[str, Any]:
    """
    Parse a simple ``key = value`` properties file.

    * Lines starting with ``#`` are comments.
    * Blank lines are ignored.
    * Values are auto-parsed: int → float → bool → str.
    """
    data: Dict[str, Any] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, raw_val = line.partition("=")
            key = key.strip()
            raw_val = raw_val.strip()
            data[key] = _auto_cast(raw_val)
    return data


def _auto_cast(val: str) -> Any:
    """Attempt int, then float, then bool, then return as str."""
    # Boolean
    if val.lower() in ("true", "false"):
        return val.lower() == "true"
    # Integer (only if no decimal point)
    if "." not in val:
        try:
            return int(val)
        except ValueError:
            pass
    # Float
    try:
        return float(val)
    except ValueError:
        pass
    return val


# ── Module-level singleton ────────────────────────────────────────────────────
config = _Configuration()
