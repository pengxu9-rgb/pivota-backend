"""AgenPas shipping-address capture on Postgres: the SAME tests as tests/test_agenpas_shipping_address.py (one
source, dialect-aware), collected here so the Postgres dialect gate runs them. A minimal app, never `main`.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_dialect_check \\
        pytest tests/test_agenpas_shipping_address_postgres.py
"""

from __future__ import annotations

import os
import sys

import pytest

from tests.test_agenpas_shipping_address import *  # noqa: F401,F403 - the tests
from tests.test_agenpas_shipping_address import _auth, _db, _env, _no_network  # noqa: F401 - autouse fixtures

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_DBNAME = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]

# Replaces the imported SQLite-arm mark: run only on a throwaway Postgres database.
pytestmark = pytest.mark.skipif(
    not _IS_PG or not any(m in _DBNAME or m in DATABASE_URL for m in _SAFE_DB_MARKERS),
    reason="needs a throwaway Postgres DATABASE_URL",
)


#: Taken at IMPORT (collection) time, as tests/test_agent_internal_commerce_purchases_postgres.py does: a later
#: gate file may load main inside one of its own tests, which says nothing about this module.
_MAIN_AT_IMPORT = "main" in sys.modules


def test_the_twin_never_loads_main():
    # main's model registrations poison every gate file after it (be#2841's CI): the twin must not load it.
    assert not _MAIN_AT_IMPORT
