#!/usr/bin/env python3
"""Compatibility wrapper for the search chain inventory probe.

This script keeps a stable entrypoint (`search_chain_probe.py`) while reusing
`search_chain_inventory_probe.py` as the source of truth.
"""

from __future__ import annotations

import runpy
from pathlib import Path


if __name__ == "__main__":
    target = Path(__file__).with_name("search_chain_inventory_probe.py")
    runpy.run_path(str(target), run_name="__main__")
