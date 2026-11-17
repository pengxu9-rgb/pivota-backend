"""The AgenPas internal purchase read's tests on Postgres: the SAME tests as
tests/test_agent_internal_commerce_purchases.py (one source, dialect-aware), collected here so the Postgres
dialect gate runs the agent-scoped ledger reads (db/agent_purchase_ledger.get_for_agent and
get_by_rail_id_for_agent) on the production dialect.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_dialect_check \\
        pytest tests/test_agent_internal_commerce_purchases_postgres.py
"""

from __future__ import annotations

import os

import pytest

from tests.test_agent_internal_commerce_purchases import *  # noqa: F401,F403 - the tests
# The autouse fixtures (`import *` skips `_` names).
from tests.test_agent_internal_commerce_purchases import _db, _env, _mounted, _no_network  # noqa: F401

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_DBNAME = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]

# Replaces the imported SQLite-arm mark: run only on a throwaway Postgres database.
pytestmark = pytest.mark.skipif(
    not _IS_PG or not any(m in _DBNAME or m in DATABASE_URL for m in _SAFE_DB_MARKERS),
    reason="needs a throwaway Postgres DATABASE_URL",
)
