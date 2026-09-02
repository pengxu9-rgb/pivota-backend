"""Real-Postgres concurrency coverage for interaction convergence locking."""

import asyncio
import os
import uuid

import pytest


DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith(
    "postgres://"
)
pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs the Postgres DATABASE_URL supplied by postgres-dialect-gate",
)

MERCHANT_ID = f"merch_stitch_gate_{uuid.uuid4().hex[:10]}"


@pytest.fixture(autouse=True)
async def _database():
    from sqlalchemy import create_engine

    from db.commerce_interactions import (
        commerce_interaction_events,
        commerce_interactions,
    )
    from db.database import database, metadata

    sync_url = DATABASE_URL.replace(
        "postgresql://", "postgresql+psycopg2://", 1
    ).replace("postgres://", "postgresql+psycopg2://", 1)
    engine = create_engine(sync_url)
    metadata.create_all(
        engine,
        tables=[commerce_interactions, commerce_interaction_events],
        checkfirst=True,
    )
    engine.dispose()

    await database.connect()
    try:
        yield database
    finally:
        await database.execute(
            commerce_interaction_events.delete().where(
                commerce_interaction_events.c.merchant_id == MERCHANT_ID
            )
        )
        await database.execute(
            commerce_interactions.delete().where(
                commerce_interactions.c.merchant_id == MERCHANT_ID
            )
        )
        await database.disconnect()


async def test_waiter_on_loser_resolves_again_after_bridge_merge():
    from sqlalchemy import select

    from db.commerce_interactions import commerce_interactions
    from db.database import database
    from services import commerce_interaction_service as service

    checkout = await service.ensure_interaction(
        merchant_id=MERCHANT_ID,
        platform="shopify",
        store_id="store_a",
        checkout_id="checkout_1",
    )
    order = await service.ensure_interaction(
        merchant_id=MERCHANT_ID,
        platform="shopify",
        store_id="store_a",
        order_id="order_1",
    )
    assert checkout["interaction_id"] != order["interaction_id"]

    holder_entered = asyncio.Event()
    merge_finished = asyncio.Event()
    release_holder = asyncio.Event()
    waiter_entered = asyncio.Event()

    bridge_refs = {
        "merchant_id": MERCHANT_ID,
        "platform": "shopify",
        "store_id": "store_a",
        "checkout_id": "checkout_1",
        "order_id": "order_1",
    }
    loser_refs = {
        "merchant_id": MERCHANT_ID,
        "platform": "shopify",
        "store_id": "store_a",
        "checkout_id": "checkout_1",
    }

    async def merge_holder():
        async with service._event_write_lock(
            MERCHANT_ID, "payment.succeeded", "bridge_delivery", bridge_refs
        ):
            holder_entered.set()
            await service.ensure_interaction(
                latest_event_type="payment.succeeded", **bridge_refs
            )
            merge_finished.set()
            await release_holder.wait()

    async def loser_waiter():
        async with service._event_write_lock(
            MERCHANT_ID, "checkout.started", "waiter_delivery", loser_refs
        ):
            waiter_entered.set()
            await service.ensure_interaction(
                latest_event_type="checkout.started", **loser_refs
            )

    holder_task = asyncio.create_task(merge_holder())
    await asyncio.wait_for(holder_entered.wait(), timeout=2)
    waiter_task = asyncio.create_task(loser_waiter())
    await asyncio.wait_for(merge_finished.wait(), timeout=2)
    await asyncio.sleep(0.1)
    assert not waiter_entered.is_set(), "waiter bypassed the bridge stitch lock"

    release_holder.set()
    await asyncio.wait_for(asyncio.gather(holder_task, waiter_task), timeout=5)
    assert waiter_entered.is_set()

    rows = await database.fetch_all(
        select(commerce_interactions).where(
            commerce_interactions.c.merchant_id == MERCHANT_ID
        )
    )
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["interaction_id"] == order["interaction_id"]
    assert row["checkout_id"] == "checkout_1"
    assert row["order_id"] == "order_1"
