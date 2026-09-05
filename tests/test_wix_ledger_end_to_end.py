"""Wix telemetry through the REAL ledger tables on SQLite.

The mapper tests prove the shape of the batch; this file proves what the ledger
and the funnel do with it: two partial refunds of one order must land as two
events on ONE interaction, a replayed delivery must dedupe, and the funnel must
SUM the two refunds under one `order_ref` rather than take a max.

The Wix wrinkle the BigCommerce sibling does not have: an Order Transactions
delivery carries no `currency` at all, so the order read back by
services/wix_order_fetch.py is what makes these refunds countable. The fixture
supplies it the same way the receiver does.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest


# The funnel reads a bounded recent window, so the fixture instants have to be
# recent. The FORMAT stays exactly what Wix documents: ISO 8601 with a Z.
_NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


CREATED_AT = _iso(_NOW - timedelta(days=2))
UPDATED_AT = _iso(_NOW - timedelta(days=1))
REFUND_ONE_AT = _iso(_NOW - timedelta(hours=6))
REFUND_TWO_AT = _iso(_NOW - timedelta(hours=3))

MERCHANT_ID = "merchant-wix"
STORE_ID = "store-wix"
INSTANCE_ID = "d2b4e0a1-1f3c-4a55-9a2e-4c1b7a55e001"
ORDER_ID = "a4738c5d-98d6-45e4-bc88-4e5940acacfd"
PIVOTA_ORDER_ID = "ord_wix_1"


def _order(**overrides):
    order = {
        "id": ORDER_ID,
        "number": 10133,
        "createdDate": CREATED_AT,
        "updatedDate": UPDATED_AT,
        "status": "APPROVED",
        "paymentStatus": "PARTIALLY_REFUNDED",
        "fulfillmentStatus": "FULFILLED",
        "currency": "USD",
        "priceSummary": {"total": {"amount": "40.56", "formattedAmount": "$40.56"}},
        "buyerInfo": {"contactId": "f61f30cd-7474-47b7-95a2-339c0fcacbd3"},
        # What adapters/wix_adapter.py::build_wix_order_payload stamps on an
        # order Pivota wrote back. This is the structured marker; `buyerNote`
        # (which the same writeback also sets) is buyer free text and is never
        # read.
        "channelInfo": {
            "type": "OTHER_PLATFORM",
            "channelName": "Pivota",
            "externalOrderId": PIVOTA_ORDER_ID,
        },
    }
    order.update(overrides)
    return order


def _refund(refund_id, amount, created):
    return {
        "id": refund_id,
        "transactions": [
            {
                "paymentId": "pay-1",
                "amount": {"amount": amount, "formattedAmount": f"${amount}"},
                "refundStatus": "SUCCEEDED",
            }
        ],
        "details": {"items": [], "shippingIncluded": False},
        "createdDate": created,
        "summary": {"requestedRefund": {"amount": amount}, "refunded": {"amount": amount}},
    }


def _refund_completed_event(refunds, *, event_id):
    """The verified `data` claim for a refund_completed delivery."""
    inner = {
        "id": event_id,
        "entityFqdn": "wix.ecom.v1.order_transactions",
        "slug": "refund_completed",
        "entityId": ORDER_ID,
        "eventTime": REFUND_TWO_AT,
        "actionEvent": {
            "body": {
                "orderId": ORDER_ID,
                "refund": refunds[-1],
                "sideEffects": {"sendOrderRefundedEmail": False},
                "orderTransactions": {
                    "orderId": ORDER_ID,
                    "payments": [],
                    "refunds": refunds,
                },
            }
        },
        "triggeredByAnonymizeRequest": False,
    }
    return {
        "eventType": "wix.ecom.v1.order_transactions_refund_completed",
        "instanceId": INSTANCE_ID,
        "data": json.dumps(inner),
        "identity": json.dumps({"identityType": "APP", "appId": "app-1"}),
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


async def _ingest(refunds, *, event_id):
    from services.merchant_event_ingest_service import ingest_merchant_event_batch
    from services.wix_event_adapter import map_wix_event

    batch = map_wix_event(
        _refund_completed_event(refunds, event_id=event_id),
        store_id=STORE_ID,
        # What the receiver's fetch returns: a transactions delivery has no
        # currency of its own.
        order=_order(),
    )
    # SQLite's DATETIME binding refuses tz-aware values; strip after the real
    # adapter has run so the mapper's own normalization is still exercised.
    for event in batch.events:
        event.occurred_at = event.occurred_at.replace(tzinfo=None)
    return await ingest_merchant_event_batch(
        merchant_id=MERCHANT_ID,
        batch=batch,
        agent_identity_confidence="platform_asserted",
        write_path="wix_webhook",
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

    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "wix-partial-refunds")
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

        first = [_refund("refund-901", "10.50", REFUND_ONE_AT)]
        second = first + [_refund("refund-902", "5.00", REFUND_TWO_AT)]

        await _ingest(first, event_id="delivery-1")
        await _ingest(second, event_id="delivery-2")

        interactions = [
            dict(row) for row in await test_database.fetch_all(select(commerce_interactions))
        ]
        assert len(interactions) == 1
        interaction = interactions[0]
        assert interaction["order_id"] == ORDER_ID
        # Recovered from `channelInfo.externalOrderId`, so this Wix order and
        # its Stripe/agent siblings aggregate as ONE purchase.
        assert interaction["order_ref"] == f"pivota:{PIVOTA_ORDER_ID}"

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
        assert sorted(row["payload"]["refund_id"] for row in refund_rows) == [
            "refund-901",
            "refund-902",
        ]
        assert sorted(row["payload"]["amount_cents"] for row in refund_rows) == [500, 1050]
        # The ingress stamped the provenance, not the payload.
        assert {row["write_path"] for row in refund_rows} == {"wix_webhook"}
        assert {row["authority"] for row in refund_rows} == {"platform"}

        # A third delivery repeating the same refunds must report both as
        # duplicates, even though its envelope event id is new.
        replay = await _ingest(second, event_id="delivery-3")
        refund_results = [
            result
            for result in replay["events"]
            if result["event_id"].startswith("wix:refund.succeeded:")
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
async def test_funnel_sums_both_wix_partial_refunds(tmp_path, monkeypatch):
    from services import merchant_commerce_event_funnel_service as funnel_service

    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "wix-refund-funnel")
    try:
        await _ingest([_refund("refund-901", "10.50", REFUND_ONE_AT)], event_id="delivery-1")
        await _ingest(
            [
                _refund("refund-901", "10.50", REFUND_ONE_AT),
                _refund("refund-902", "5.00", REFUND_TWO_AT),
            ],
            event_id="delivery-2",
        )

        result = await funnel_service.get_merchant_commerce_event_funnel(
            merchant_id=MERCHANT_ID,
            group_by="store",
        )
        assert result.payload["available"] is True
        summary = result.payload["summary"]
        assert summary["event_type_breakdown"]["refund.succeeded"] == 2
        # Both refunds carry source=wix_webhook, so they are filed under one
        # authority and SUM instead of taking the max a PSP mirror would.
        assert summary["refunded_amount_cents_by_currency"] == {"USD": 1550}
        assert result.payload["slices"][0]["key"] == STORE_ID
    finally:
        await test_database.disconnect()
