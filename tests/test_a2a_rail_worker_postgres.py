"""The A2A rail worker's tests on Postgres: the SAME tests as tests/test_a2a_rail_worker.py (one source,
dialect-aware), collected here so the Postgres dialect gate runs them.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_dialect_check \\
        pytest tests/test_a2a_rail_worker_postgres.py
"""

from __future__ import annotations

import os

import pytest

from tests.test_a2a_rail_worker import *  # noqa: F401,F403 - the tests
from tests.test_a2a_rail_worker import _db  # noqa: F401 - the autouse fixture (`import *` skips `_` names)

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_DBNAME = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]

# Replaces the imported SQLite-arm mark: run only on a throwaway Postgres database.
pytestmark = pytest.mark.skipif(
    not _IS_PG or not any(m in _DBNAME or m in DATABASE_URL for m in _SAFE_DB_MARKERS),
    reason="needs a throwaway Postgres DATABASE_URL",
)
