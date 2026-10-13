"""The purchase create route's tests on Postgres: the SAME tests as tests/test_agent_commerce_purchase_create.py
(one source, dialect-aware), collected here so the Postgres dialect gate runs them: the purchase and its
key in one unit, rolled back together when another request wins the key.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_dialect_check \\
        pytest tests/test_agent_commerce_purchase_create_postgres.py
"""

from __future__ import annotations

import os

import pytest

from tests.test_agent_commerce_purchase_create import *  # noqa: F401,F403 - the tests
# The autouse fixtures (`import *` skips `_` names).
from tests.test_agent_commerce_purchase_create import _db, _env, _no_network  # noqa: F401

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_DBNAME = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]

# Replaces the imported SQLite-arm mark: run only on a throwaway Postgres database.
pytestmark = pytest.mark.skipif(
    not _IS_PG or not any(m in _DBNAME or m in DATABASE_URL for m in _SAFE_DB_MARKERS),
    reason="needs a throwaway Postgres DATABASE_URL",
)
