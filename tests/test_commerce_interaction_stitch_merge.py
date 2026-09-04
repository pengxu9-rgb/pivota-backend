from datetime import datetime

import databases
import pytest
from sqlalchemy import create_engine, select

from db.commerce_interactions import commerce_interaction_events, commerce_interactions
from db.database import metadata
from services import commerce_interaction_service as service


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sequence",
    [
        [("verified-agent", "verified"), ("browser-claim", "browser_observed")],
        [("browser-claim", "browser_observed"), ("verified-agent", "verified")],
        [("verified-agent", "verified"), (None, "platform_asserted")],
    ],
)
async def test_stitched_interaction_preserves_highest_confidence_agent_identity(
    tmp_path, monkeypatch, sequence
):
    from services.merchant_event_ingest_service import (
        MerchantEventBatch,
        ingest_merchant_event_batch,
    )

    db_path = tmp_path / "commerce-agent-identity-merge.sqlite3"
    sync_engine = create_engine(f"sqlite:///{db_path}")
    metadata.create_all(
        sync_engine,
        tables=[commerce_interactions, commerce_interaction_events],
        checkfirst=True,
    )
    sync_engine.dispose()

    test_database = databases.Database(f"sqlite+aiosqlite:///{db_path}")
    await test_database.connect()
    monkeypatch.setattr(service, "database", test_database)
    monkeypatch.setattr(service, "IS_POSTGRES", False)

    try:
        for index, (agent_id, confidence) in enumerate(sequence):
            identity_context = (
                {
                    "source_channel": "chatgpt",
                    "query_source": "agent_recommendation",
                    "protocol_name": "mcp",
                    "llm_provider": "openai",
                    "llm_model": "gpt-5.4",
                }
                if agent_id == "verified-agent"
                else {}
            )
            batch = MerchantEventBatch.model_validate(
                {
                    "events": [
                        {
                            "event_id": f"identity-event-{index}",
                            "event_type": (
                                "product.viewed" if index == 0 else "checkout.started"
                            ),
                            "occurred_at": f"2026-08-26T12:0{index}:00Z",
                            "platform": "custom",
                            "store_id": "store-a",
                            "session_id": "shared-session",
                            "agent_id": agent_id,
                            **identity_context,
                        }
                    ]
                }
            )
            # SQLite drops timezone data; keep comparisons within this fixture naive.
            batch.events[0].occurred_at = batch.events[0].occurred_at.replace(tzinfo=None)
            await ingest_merchant_event_batch(
                merchant_id="merchant-a",
                batch=batch,
                agent_identity_confidence=confidence,
            )

        rows = await test_database.fetch_all(select(commerce_interactions))
        assert len(rows) == 1
        interaction = dict(rows[0])
        assert interaction["agent_id"] == "verified-agent"
        assert interaction["metadata"]["agent_id"] == "verified-agent"
        assert interaction["metadata"]["agent_identity_confidence"] == "verified"
        assert interaction["metadata"]["traffic"]["agent_id"] == "verified-agent"
        for field, expected in {
            "source_channel": "chatgpt",
            "query_source": "agent_recommendation",
            "protocol_name": "mcp",
            "llm_provider": "openai",
            "llm_model": "gpt-5.4",
        }.items():
            assert interaction[field] == expected
            assert interaction["metadata"][field] == expected
            assert interaction["metadata"]["traffic"][field] == expected

        from services.traffic_taxonomy_service import taxonomy_from_row

        derived = taxonomy_from_row(interaction)
        assert derived["agent_id"] == "verified-agent"
        assert derived["source_channel"] == "chatgpt"
        assert derived["query_source"] == "agent_recommendation"
        assert derived["protocol_name"] == "mcp"
        assert derived["llm_provider"] == "openai"
        assert derived["llm_model"] == "gpt-5.4"
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_merge_rejects_cross_store_candidates_before_mutation():
    with pytest.raises(
        ValueError, match="cannot merge commerce interactions across stores"
    ):
        await service._merge_interactions(
            [
                {
                    "interaction_id": "int_a",
                    "merchant_id": "merchant_a",
                    "store_id": "store_a",
                },
                {
                    "interaction_id": "int_b",
                    "merchant_id": "merchant_a",
                    "store_id": None,
                },
            ],
            {
                "interaction_id": "int_a",
                "merchant_id": "merchant_a",
                "store_id": "store_a",
            },
        )


@pytest.mark.asyncio
async def test_bridge_event_atomically_merges_interactions_and_preserves_events(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "commerce-stitch-merge.sqlite3"
    sync_engine = create_engine(f"sqlite:///{db_path}")
    metadata.create_all(
        sync_engine,
        tables=[commerce_interactions, commerce_interaction_events],
        checkfirst=True,
    )
    sync_engine.dispose()

    test_database = databases.Database(f"sqlite+aiosqlite:///{db_path}")
    await test_database.connect()
    monkeypatch.setattr(service, "database", test_database)
    monkeypatch.setattr(service, "IS_POSTGRES", False)
    monkeypatch.setattr(service, "_now", lambda: datetime(2026, 8, 26, 15, 0))

    try:
        checkout_result = await service.record_commerce_event(
            event_type="checkout.started",
            occurred_at=datetime(2026, 8, 26, 12, 0),
            merchant_id="merchant_a",
            platform="shopify",
            store_id="store_a",
            session_id="session_1",
            checkout_id="checkout_1",
            metadata={"checkout_marker": "preserved"},
            upstream_idempotency_key="delivery_checkout",
        )
        order_result = await service.record_commerce_event(
            event_type="order.created",
            occurred_at=datetime(2026, 8, 26, 13, 0),
            merchant_id="merchant_a",
            platform="shopify",
            store_id="store_a",
            order_id="order_1",
            payment_id="payment_1",
            metadata={"order_marker": "preserved"},
            upstream_idempotency_key="delivery_order",
        )

        assert checkout_result["interaction_id"] != order_result["interaction_id"]

        bridge_result = await service.record_commerce_event(
            event_type="payment.succeeded",
            occurred_at=datetime(2026, 8, 26, 14, 0),
            merchant_id="merchant_a",
            platform="shopify",
            store_id="store_a",
            checkout_id="checkout_1",
            order_id="order_1",
            payment_id="payment_1",
            metadata={"bridge_marker": "preserved"},
            upstream_idempotency_key="delivery_bridge",
        )

        interactions = [
            dict(row)
            for row in await test_database.fetch_all(select(commerce_interactions))
        ]
        events = [
            dict(row)
            for row in await test_database.fetch_all(
                select(commerce_interaction_events).order_by(
                    commerce_interaction_events.c.occurred_at
                )
            )
        ]

        assert len(interactions) == 1
        assert len(events) == 3
        interaction = interactions[0]
        assert bridge_result["interaction_id"] == order_result["interaction_id"]
        assert {event["interaction_id"] for event in events} == {
            bridge_result["interaction_id"]
        }
        assert interaction["session_id"] == "session_1"
        assert interaction["checkout_id"] == "checkout_1"
        assert interaction["order_id"] == "order_1"
        assert interaction["payment_id"] == "payment_1"
        assert interaction["status"] == "paid"
        assert interaction["latest_event_type"] == "payment.succeeded"
        assert interaction["first_occurred_at"] == datetime(2026, 8, 26, 12, 0)
        assert interaction["last_occurred_at"] == datetime(2026, 8, 26, 14, 0)
        assert interaction["metadata"]["checkout_marker"] == "preserved"
        assert interaction["metadata"]["order_marker"] == "preserved"
        assert interaction["metadata"]["bridge_marker"] == "preserved"

        duplicate = await service.record_commerce_event(
            event_type="payment.succeeded",
            occurred_at=datetime(2026, 8, 26, 14, 0),
            merchant_id="merchant_a",
            platform="shopify",
            store_id="store_a",
            checkout_id="checkout_1",
            order_id="order_1",
            payment_id="payment_1",
            upstream_idempotency_key="delivery_bridge",
        )
        assert duplicate == {
            "interaction_id": bridge_result["interaction_id"],
            "event_id": bridge_result["event_id"],
            "duplicate": True,
        }
        assert len(await test_database.fetch_all(select(commerce_interactions))) == 1
        assert len(await test_database.fetch_all(select(commerce_interaction_events))) == 3

        with pytest.raises(Exception) as cross_store_error:
            await service.ensure_interaction(
                interaction_id=bridge_result["interaction_id"],
                merchant_id="merchant_a",
                platform="shopify",
                store_id="store_b",
                latest_event_type="order.paid",
            )
        assert service._is_unique_violation(cross_store_error.value)
        unchanged = dict(
            await test_database.fetch_one(
                select(commerce_interactions).where(
                    commerce_interactions.c.interaction_id
                    == bridge_result["interaction_id"]
                )
            )
        )
        assert unchanged["store_id"] == "store_a"
        assert len(await test_database.fetch_all(select(commerce_interactions))) == 1
    finally:
        await test_database.disconnect()
