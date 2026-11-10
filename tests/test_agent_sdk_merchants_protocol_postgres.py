"""The protocol merchant directory on the PRODUCTION dialect.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/<fresh db> \\
        .venv/bin/python -m pytest tests/test_agent_sdk_merchants_protocol_postgres.py

The gates the directory calls compare server-side clocks (`clock_timestamp()` intervals) and
fold domains in SQL, so SQLite passing says little about Postgres. Every case lives in
tests/test_agent_sdk_merchants_protocol.py and is imported here so a case added there runs
on both dialects. This module must not import `main`.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL; see the module docstring",
)

if _IS_PG:
    from tests.test_agent_sdk_merchants_protocol import *  # noqa: F401,F403,E402
    from tests.test_agent_sdk_merchants_protocol import _rail_on, _rows  # noqa: E402,F401  (fixtures by name)
