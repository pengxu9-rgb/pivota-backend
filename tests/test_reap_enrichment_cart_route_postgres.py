"""The cart-link lane's ENRICHMENT-row branch (option 2, PR C) against REAL Postgres.

Picked up by .github/workflows/postgres-dialect-gate.yml via the `tests/test_*_postgres.py` glob.
Every case in tests/reap_enrichment_cart_route_cases.py, collected again on the production engine:
the new SQL (a `substr` key-prefix count, the listing-offer read with its `CAST(... AS TEXT)` price,
the proof read with its TIMESTAMPTZ / BOOLEAN / BIGINT columns) PREPAREs and answers here exactly
as on SQLite. The rail's tables come from the migrations, the proof table from its own
`ensure_table()`, the catalog from the repo's `metadata`.

THIS MODULE MUST NOT IMPORT `main` (see tests/test_agent_commerce_reap_routes_postgres.py). It
leaves every shared table empty and drops the proof table and external_product_seeds at teardown.

    DATABASE_URL=postgresql://<user>@localhost:5432/pivota_<name>_dialect_check \\
        .venv/bin/python -m pytest tests/test_reap_enrichment_cart_route_postgres.py
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from reap_enrichment_cart_route_cases import *  # noqa: E402,F401,F403

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

# Defined AFTER the star import, so this module's skip is the one that applies.
pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL -- this is the production-dialect gate",
)

assert "main" not in sys.modules, (
    "tests/test_reap_enrichment_cart_route_postgres.py imported `main`; the Postgres gate shares "
    "one process and one database -- keep this module's import graph to the router."
)
