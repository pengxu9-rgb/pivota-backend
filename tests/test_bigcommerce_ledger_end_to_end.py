"""BigCommerce telemetry through the REAL ledger tables on SQLite.

The mapper tests prove the shape of the batch; this file proves what the
ledger and the funnel do with it: two partial refunds of one order must land as
two events on ONE interaction, a replayed delivery must dedupe, and the funnel
must SUM the two refunds under `bigcommerce:<order id>` rather than take a max.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest


# The funnel reads a bounded recent window, so the fixture instants have to be
# recent. The FORMATS stay exactly what BigCommerce documents: RFC-2822 for
# order dates, ISO 8601 for a refund's `created`.
_NOW = datetime.now(timezone.utc).replace(microsecond=0)
CREATED_AT = format_datetime(_NOW - timedelta(days=2))
MODIFIED_AT = format_datetime(_NOW - timedelta(days=1))
REFUND_ONE_AT = (_NOW - timedelta(hours=6)).isoformat().replace("+00:00", "Z")
REFUND_TWO_AT = (_NOW - timedelta(hours=3)).isoformat().replace("+00:00", "Z")

MERCHANT_ID = "merchant-bc"
STORE_ID = "store-bc"
ORDER_ID = 250


def _order(**overrides):
    order = {
        "id": ORDER_ID,
        "customer_id": 8,
        "date_created": CREATED_AT,
        "date_modified": MODIFIED_AT,
        "status_id": 14,
        "status": "Partially Refunded",
        "payment_status": "partially refunded",
        "payment_method": "Credit Card",
        "payment_provider_id": "txn-bc-250",
        "total_inc_tax": "49.99",
        "currency_code": "USD",
        # Documented as "Always returns 0" — pinned here so nothing in this
        # bridge is ever tempted to read it as the refunded magnitude.
        "refunded_amount": "0.0000",
    }
    order.update(overrides)
    return order


def _refund(refund_id, total_amount, created):
    return {
        "id": refund_id,
        "order_id": ORDER_ID,
        "user_id": 1,
        "created": created,
        "reason": "partial",
        "total_amount": total_amount,
        "total_tax": "0.00",
        "items": [],
    }


async def _sqlite_ledger(tmp_path, monkeypatch, name: str):
    """Real commerce ledger tables on SQLite, wired into the write path."""
    import databases
    from sqlalchemy import create_engine

    from db.commerce_interactions import (
        commerce_interaction_events,
        commerce_interactions,
    )
    from db.database import metadata
    from services import commerce_interaction_service as interaction_service
    import services.merchant_commerce_event_funnel_service as funnel_module

    db_path = tmp_path / f"{name}.sqlite3"
    sync_engine = create_engine(f"sqlite:///{db_path}")
    metadata.create_all(
        sync_engine,
        tables=[commerce_interactions, commerce_interaction_events],
        checkfirst=True,
    )
    sync_engine.dispose()

    test_database = databases.Database(f"sqlite+aiosqlite:///{db_path}")
    await test_database.connect()
    monkeypatch.setattr(interaction_service, "database", test_database)
    monkeypatch.setattr(interaction_service, "IS_POSTGRES", False)
    monkeypatch.setattr(funnel_module, "database", test_database)
    return test_database


async def _ingest(order, refunds, *, delivery_hash, scope="store/order/refund/created"):
    from services.bigcommerce_event_adapter import map_bigcommerce_order
    from services.merchant_event_ingest_service import ingest_merchant_event_batch

    batch = map_bigcommerce_order(
        order,
        refunds,
        scope=scope,
        delivery_hash=delivery_hash,
        store_id=STORE_ID,
    )
    # SQLite's DATETIME binding refuses tz-aware values; strip after the real
    # adapter has run so the mapper's own normalization is still exercised.
    for event in batch.events:
        event.occurred_at = event.occurred_at.replace(tzinfo=None)
    return await ingest_merchant_event_batch(
        merchant_id=MERCHANT_ID,
        batch=batch,
        agent_identity_confidence="platform_asserted",
        write_path="bigcommerce_webhook",
    )


@pytest.mark.asyncio
async def test_two_partial_refunds_land_on_one_interaction_and_replay_dedupes(
    tmp_path, monkeypatch
):
    from sqlalchemy import select

    from db.commerce_interactions import (
        commerce_interaction_events,
        commerce_interactions,
    )

    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "bc-partial-refunds")
    try:
        # "one interaction, no unique violation" is only worth claiming if the
        # fixture actually built the guard the two refund ids have to pass.
        indexes = {
            dict(row)["name"]
            for row in await test_database.fetch_all(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert "idx_commerce_interactions_refund_id_unique" in indexes

        first = [_refund(901, "10.50", REFUND_ONE_AT)]
        second = first + [_refund(902, "5.00", REFUND_TWO_AT)]

        await _ingest(_order(), first, delivery_hash="hash-1")
        await _ingest(_order(), second, delivery_hash="hash-2")

        interactions = [
            dict(row) for row in await test_database.fetch_all(select(commerce_interactions))
        ]
        assert len(interactions) == 1
        interaction = interactions[0]
        assert interaction["order_id"] == "250"
        assert interaction["order_ref"] == "bigcommerce:250"

        refund_rows = [
            dict(row)
            for row in await test_database.fetch_all(
                select(commerce_interaction_events).where(
                    commerce_interaction_events.c.event_type == "refund.succeeded"
                )
            )
        ]
        assert len(refund_rows) == 2
        assert {row["interaction_id"] for row in refund_rows} == {
            interaction["interaction_id"]
        }
        assert sorted(row["payload"]["refund_id"] for row in refund_rows) == ["901", "902"]
        assert sorted(row["payload"]["amount_cents"] for row in refund_rows) == [500, 1050]
        # The ingress stamped the provenance, not the payload.
        assert {row["write_path"] for row in refund_rows} == {"bigcommerce_webhook"}
        assert {row["authority"] for row in refund_rows} == {"platform"}

        # A third delivery repeating the same refunds must report both as duplicates.
        replay = await _ingest(_order(), second, delivery_hash="hash-3")
        refund_results = [
            result
            for result in replay["events"]
            if result["event_id"].startswith("bigcommerce:refund.succeeded:")
        ]
        assert len(refund_results) == 2
        assert all(result["duplicate"] for result in refund_results)
        assert replay["accepted"] == 0

        after_replay = await test_database.fetch_all(
            select(commerce_interaction_events).where(
                commerce_interaction_events.c.event_type == "refund.succeeded"
            )
        )
        assert len(after_replay) == 2
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_funnel_sums_both_bigcommerce_partial_refunds(tmp_path, monkeypatch):
    from services import merchant_commerce_event_funnel_service as funnel_service

    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "bc-refund-funnel")
    try:
        await _ingest(
            _order(),
            [_refund(901, "10.50", REFUND_ONE_AT)],
            delivery_hash="hash-1",
        )
        await _ingest(
            _order(),
            [
                _refund(901, "10.50", REFUND_ONE_AT),
                _refund(902, "5.00", REFUND_TWO_AT),
            ],
            delivery_hash="hash-2",
        )

        result = await funnel_service.get_merchant_commerce_event_funnel(
            merchant_id=MERCHANT_ID,
            group_by="store",
        )
        assert result.payload["available"] is True
        summary = result.payload["summary"]
        assert summary["event_type_breakdown"]["refund.succeeded"] == 2
        # Both refunds carry source=bigcommerce_webhook, so they are filed
        # under one authority and SUM instead of taking the max a PSP mirror
        # of the same money would.
        assert summary["refunded_amount_cents_by_currency"] == {"USD": 1550}
        assert result.payload["slices"][0]["key"] == STORE_ID
    finally:
        await test_database.disconnect()
