"""Real-Postgres coverage for state-independent convergence locks."""

import os

import asyncpg
import pytest


DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith(
    "postgres://"
)
pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs the Postgres DATABASE_URL supplied by postgres-dialect-gate",
)


async def test_bridge_and_loser_waiter_contend_on_same_real_postgres_lock():
    from services.commerce_interaction_service import _stitch_advisory_lock_keys

    merchant_id = "merch_stitch_lock_gate"
    bridge_refs = {
        "merchant_id": merchant_id,
        "store_id": "store_a",
        "checkout_id": "checkout_1",
        "order_id": "order_1",
    }
    loser_refs = {
        "merchant_id": merchant_id,
        "store_id": "store_a",
        "checkout_id": "checkout_1",
    }
    bridge_keys = _stitch_advisory_lock_keys(merchant_id, bridge_refs)
    loser_keys = _stitch_advisory_lock_keys(merchant_id, loser_refs)
    shared_keys = set(bridge_keys) & set(loser_keys)
    assert shared_keys == {
        "stitch|merch_stitch_lock_gate|store_a|checkout_id|checkout_1"
    }
    shared_key = next(iter(shared_keys))

    holder = await asyncpg.connect(DATABASE_URL)
    waiter = await asyncpg.connect(DATABASE_URL)
    holder_tx = holder.transaction()
    waiter_tx = waiter.transaction()
    try:
        await holder_tx.start()
        for lock_key in bridge_keys:
            await holder.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", lock_key
            )

        await waiter_tx.start()
        acquired_while_held = await waiter.fetchval(
            "SELECT pg_try_advisory_xact_lock(hashtext($1))", shared_key
        )
        assert acquired_while_held is False

        await holder_tx.rollback()
        acquired_after_release = await waiter.fetchval(
            "SELECT pg_try_advisory_xact_lock(hashtext($1))", shared_key
        )
        assert acquired_after_release is True
        await waiter_tx.rollback()
    finally:
        if not holder.is_closed():
            await holder.close()
        if not waiter.is_closed():
            await waiter.close()
