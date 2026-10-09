"""The Reap price witness (mig 258) against REAL Postgres.

Picked up by .github/workflows/postgres-dialect-gate.yml via the `tests/test_*_postgres.py` glob.
Every case in tests/reap_price_witness_cases.py, collected again on the production engine (the
schema built from the migrations, 258 included), plus what only Postgres can show: every new
module-level statement PREPAREs against that schema.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_price_witness_test \\
        .venv/bin/python -m pytest tests/test_reap_price_witness_postgres.py
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from reap_price_witness_cases import *  # noqa: E402,F401,F403

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG, reason="needs a Postgres DATABASE_URL -- this is the production-dialect gate"
)


@pytest.mark.parametrize("name", [
    "_BEGIN_PREFLIGHT_SQL", "_WITHDRAW_PREFLIGHT_SQL", "_RECORD_PREFLIGHT_SQL",
    "_RECORD_LIVE_PRICE_SQL", "_ENRICHMENT_PROOFS_FOR_VARIANT_SQL",
])
async def test_the_witness_statements_prepare_on_postgres(name):
    """PREPARE, not execute: a statement on a dark path that nothing has planned is how #1588
    reached production."""
    import re

    import db.enrichment_cart_variant_proofs as proofs
    import db.reap_price_witness as witness
    from db.database import database

    assert await proofs.ensure_table()
    sql = getattr(witness, name)
    order = []
    def _bind(match):
        if match.group(1) not in order:
            order.append(match.group(1))
        return f"${order.index(match.group(1)) + 1}"
    positional = re.sub(r"(?<!:):([a-zA-Z_][a-zA-Z0-9_]*)", _bind, sql)
    async with database.connection() as connection:
        raw = connection.raw_connection
        await raw.prepare(positional)
