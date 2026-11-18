"""Reviewed retailer write guards, on the production dialect.

    TZ=UTC DATABASE_URL=postgresql://postgres@127.0.0.1:5432/pivota_rr_guard_test \\
        .venv/bin/python -m pytest tests/test_reviewed_retailer_guards_postgres.py

WHAT THIS PINS:
  * The cohort C write guard proves an explicit serializable transaction is open. A session
    default of serializable in autocommit reads "serializable" from SHOW but holds no
    transaction; the guard refuses it. Inside BEGIN ... SET TRANSACTION ISOLATION LEVEL
    SERIALIZABLE it passes, and the cohort C advisory lock is then held until the transaction
    ends (transaction scoped, released at commit).

ISOLATION. Every table here is TEMP on one connection, so nothing leaks into sibling gate files.

THIS MODULE MUST NOT IMPORT `main` (the gate runs every test_*_postgres.py in one process).
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL: transactions and advisory locks")

assert "main" not in sys.modules or not _IS_PG, (
    "main is imported: it registers every model on the shared metadata at collection time "
    "and poisons later create_all calls in the Postgres gate"
)


async def _cohort_c_lock_held(conn) -> bool:
    from services.catalog_enrichment_agent.reviewed_retailer_apply import COHORT_C_LOCK

    return bool(await conn.fetch_val(
        """
        SELECT EXISTS (SELECT 1 FROM pg_locks
          WHERE locktype = 'advisory' AND pid = pg_backend_pid()
            AND classid = CAST(:a AS oid) AND objid = CAST(:b AS oid))
        """,
        {"a": COHORT_C_LOCK[0], "b": COHORT_C_LOCK[1]},
    ))


@pytest.mark.asyncio
async def test_cohort_c_guard_refuses_serializable_session_default_without_a_transaction():
    from databases import Database
    from services.catalog_enrichment_agent.reviewed_retailer_apply import require_open_serializable_transaction

    async with Database(DATABASE_URL) as db, db.connection() as conn:
        await conn.execute("SET default_transaction_isolation = 'serializable'")
        try:
            assert await conn.fetch_val("SHOW transaction_isolation") == "serializable"
            with pytest.raises(ValueError, match="open transaction"):
                await require_open_serializable_transaction(conn)
        finally:
            await conn.execute("RESET default_transaction_isolation")


@pytest.mark.asyncio
async def test_cohort_c_guard_refuses_a_read_committed_transaction():
    from databases import Database
    from services.catalog_enrichment_agent.reviewed_retailer_apply import require_open_serializable_transaction

    async with Database(DATABASE_URL) as db, db.connection() as conn:
        async with conn.transaction(force_rollback=True):
            with pytest.raises(ValueError, match="serializable transaction"):
                await require_open_serializable_transaction(conn)


@pytest.mark.asyncio
async def test_cohort_c_guard_passes_inside_serializable_transaction_and_lock_is_transaction_scoped():
    from databases import Database
    from services.catalog_enrichment_agent.reviewed_retailer_apply import (
        COHORT_C_LOCK, require_open_serializable_transaction,
    )

    async with Database(DATABASE_URL) as db, db.connection() as conn:
        async with conn.transaction(force_rollback=True):
            await conn.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
            await require_open_serializable_transaction(conn)
            await conn.execute(f"SELECT pg_advisory_xact_lock({COHORT_C_LOCK[0]}, {COHORT_C_LOCK[1]})")
            assert await _cohort_c_lock_held(conn)
        assert not await _cohort_c_lock_held(conn)

