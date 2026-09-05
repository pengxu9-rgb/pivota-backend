import base64
import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _order_payload(status="processing"):
    return {
        "id": 44,
        "order_key": "wc_order_secretish",
        "status": status,
        "currency": "USD",
        "total": "25.50",
        "total_refunded": "25.50" if status == "refunded" else "0.00",
        "customer_id": 8,
        "transaction_id": "txn-44",
        "payment_method": "stripe",
        "date_created_gmt": "2026-08-27T10:00:00",
        "date_paid_gmt": "2026-08-27T10:01:00" if status == "processing" else None,
        "date_modified_gmt": "2026-08-27T10:02:00",
        "billing": {"email": "buyer@example.com", "phone": "555-0100"},
        "line_items": [
            {
                "id": 1,
                "name": "Private-ish product label",
                "product_id": 10,
                "variation_id": 11,
                "sku": "SKU-11",
                "quantity": 2,
                "total": "25.50",
            }
        ],
        "meta_data": [
            {"key": "_wc_order_attribution_utm_content", "value": "clk_abcdef1234"}
        ],
    }


def test_woocommerce_paid_order_maps_to_idempotent_created_and_paid_events():
    from services.woocommerce_event_adapter import map_woocommerce_webhook

    created = map_woocommerce_webhook(
        _order_payload(),
        topic="order.created",
        delivery_id="delivery-1",
        store_id="store-woo",
    )
    updated = map_woocommerce_webhook(
        _order_payload(),
        topic="order.updated",
        delivery_id="delivery-2",
        store_id="store-woo",
    )

    assert [event.event_type for event in created.events] == ["order.created", "order.paid"]
    assert [event.event_id for event in created.events] == [
        event.event_id for event in updated.events
    ]
    assert created.events[0].order_id == "44"
    assert created.events[0].payment_id == "txn-44"
    assert created.events[0].click_id == "clk_abcdef1234"
    assert created.events[0].amount_cents == 2550
    assert created.events[0].occurred_at.isoformat() == "2026-08-27T10:00:00+00:00"
    assert created.events[1].occurred_at.isoformat() == "2026-08-27T10:01:00+00:00"
    metadata = created.events[0].metadata
    assert "billing" not in metadata
    assert "email" not in json.dumps(metadata)
    assert "name" not in metadata["native_line_items"][0]


@pytest.mark.parametrize(
    ("status", "event_type"),
    [
        ("cancelled", "order.cancelled"),
        ("failed", "payment.failed"),
        ("refunded", "refund.succeeded"),
    ],
)
def test_woocommerce_order_status_maps_lifecycle(status, event_type):
    from services.woocommerce_event_adapter import map_woocommerce_webhook

    batch = map_woocommerce_webhook(
        _order_payload(status),
        topic="order.updated",
        delivery_id=f"delivery-{status}",
        store_id="store-woo",
    )

    assert len(batch.events) == 1
    assert batch.events[0].event_type == event_type
    assert batch.events[0].order_id == "44"
    assert batch.events[0].occurred_at.isoformat() == "2026-08-27T10:02:00+00:00"


def test_woocommerce_terminal_status_wins_over_historical_paid_date():
    from services.woocommerce_event_adapter import map_woocommerce_webhook

    payload = _order_payload("refunded")
    payload["date_paid_gmt"] = "2026-08-27T10:01:00"
    batch = map_woocommerce_webhook(
        payload,
        topic="order.updated",
        delivery_id="delivery-refunded-after-paid",
        store_id="store-woo",
    )

    assert [event.event_type for event in batch.events] == ["refund.succeeded"]


def test_woocommerce_webhook_route_requires_valid_hmac_and_source(monkeypatch):
    from routes import woocommerce_webhooks as route

    ingested = []

    class FakeDB:
        async def fetch_one(self, *args, **kwargs):
            return {
                "store_id": "store-woo",
                "merchant_id": "merchant-1",
                "domain": "https://shop.example",
                "api_key": json.dumps(
                    {
                        "consumer_key": "ck_test",
                        "consumer_secret": "cs_test",
                        "webhook_secret": "hook-secret",
                    }
                ),
            }

    async def fake_ingest(**kwargs):
        ingested.append(kwargs)
        return {"accepted": len(kwargs["batch"].events), "duplicates": 0, "events": []}

    monkeypatch.setattr(route, "database", FakeDB())
    monkeypatch.setattr(route, "ingest_merchant_event_batch", fake_ingest)
    app = FastAPI()
    app.include_router(route.router)
    client = TestClient(app)
    raw = json.dumps(_order_payload(), separators=(",", ":")).encode("utf-8")
    signature = base64.b64encode(
        hmac.new(b"hook-secret", raw, hashlib.sha256).digest()
    ).decode("ascii")
    headers = {
        "Content-Type": "application/json",
        "X-WC-Webhook-Signature": signature,
        "X-WC-Webhook-Topic": "order.updated",
        "X-WC-Webhook-Delivery-ID": "delivery-1",
        "X-WC-Webhook-Source": "https://shop.example/",
    }

    response = client.post("/webhooks/woocommerce/store-woo", content=raw, headers=headers)

    assert response.status_code == 200
    assert response.json()["accepted"] == 2
    assert ingested[0]["merchant_id"] == "merchant-1"
    assert ingested[0]["agent_identity_confidence"] == "platform_asserted"

    invalid = client.post(
        "/webhooks/woocommerce/store-woo",
        content=raw,
        headers={**headers, "X-WC-Webhook-Signature": "invalid"},
    )
    assert invalid.status_code == 401

    wrong_source = client.post(
        "/webhooks/woocommerce/store-woo",
        content=raw,
        headers={**headers, "X-WC-Webhook-Source": "https://attacker.example"},
    )
    assert wrong_source.status_code == 401


def test_woocommerce_legacy_consumer_secret_is_valid_webhook_secret_fallback():
    from routes.woocommerce_webhooks import _credentials

    assert _credentials("ck_test:cs_test") == {
        "consumer_key": "ck_test",
        "consumer_secret": "cs_test",
    }


@pytest.mark.asyncio
async def test_woocommerce_connect_persists_webhook_secret_and_returns_setup_path(monkeypatch):
    from adapters import woocommerce_adapter
    from routes import merchant_store_connections as route

    writes = []

    class FakeAdapter:
        def __init__(self, config):
            self.store_url = "https://shop.example"

        def validate_config(self):
            return True, None

        async def test_connection(self):
            return {"success": True, "store_name": "Example Woo"}

    class FakeDB:
        async def fetch_one(self, *args, **kwargs):
            return None

        async def execute(self, query, values):
            writes.append((query, values))

    monkeypatch.setattr(woocommerce_adapter, "WooCommerceAdapter", FakeAdapter)
    monkeypatch.setattr(route, "database", FakeDB())

    result = await route.merchant_connect_woocommerce(
        route.ConnectWooCommerceRequest(
            merchant_id="merchant-1",
            store_url="shop.example",
            consumer_key="ck_test",
            consumer_secret="cs_test",
            webhook_secret="hook-secret",
        ),
        current_user={"role": "merchant", "merchant_id": "merchant-1"},
    )

    persisted = json.loads(writes[0][1]["api_key"])
    assert persisted["webhook_secret"] == "hook-secret"
    assert result["webhook_path"].startswith("/webhooks/woocommerce/")
    assert result["webhook_subscription_path"].endswith("/webhooks/ensure")
    assert result["required_webhook_topics"] == ["order.created", "order.updated"]
    assert "hook-secret" not in json.dumps(result)


def _refund_entry(refund_id, total, reason="Damaged in transit"):
    """One wc/v3 `refunds[]` entry as WooCommerce reports it on the order."""
    return {"id": refund_id, "reason": reason, "total": total}


def test_woocommerce_partial_refund_emits_a_native_refund_event_alongside_order_events():
    from services.woocommerce_event_adapter import map_woocommerce_webhook

    payload = _order_payload("processing")
    payload["currency_minor_unit"] = 2
    payload["refunds"] = [_refund_entry(901, "-10.50")]

    batch = map_woocommerce_webhook(
        payload,
        topic="order.updated",
        delivery_id="delivery-partial-1",
        store_id="store-woo",
    )

    assert [event.event_type for event in batch.events] == [
        "order.created",
        "order.paid",
        "refund.succeeded",
    ]
    refund = batch.events[-1]
    assert refund.refund_id == "901"
    assert refund.amount_cents == 1050
    assert refund.currency == "USD"
    assert refund.order_id == "44"
    assert refund.payment_id == "txn-44"
    assert refund.click_id == "clk_abcdef1234"
    assert refund.buyer_id == "8"
    assert refund.trace_id == "delivery-partial-1"
    # The order payload gives refund entries no timestamp of their own.
    assert refund.occurred_at.isoformat() == "2026-08-27T10:02:00+00:00"
    assert refund.metadata["native_amount_semantics"] == "native_refund_total"
    # refunds[].reason is merchant free text and must not reach the ledger.
    assert "Damaged in transit" not in json.dumps(refund.metadata)
    assert "native_refund_reason" not in refund.metadata


def test_woocommerce_zero_decimal_currency_refund_uses_declared_minor_units():
    from services.woocommerce_event_adapter import map_woocommerce_webhook

    payload = _order_payload("processing")
    payload["currency"] = "JPY"
    payload["currency_minor_unit"] = 0
    payload["total"] = "3000"
    payload["refunds"] = [_refund_entry(915, "-1200")]

    batch = map_woocommerce_webhook(
        payload,
        topic="order.updated",
        delivery_id="delivery-jpy",
        store_id="store-woo",
    )

    refund = [event for event in batch.events if event.event_type == "refund.succeeded"][0]
    assert refund.currency == "JPY"
    assert refund.amount_cents == 1200


def test_woocommerce_second_partial_refund_is_additive_and_keeps_the_first_event_id():
    from services.woocommerce_event_adapter import map_woocommerce_webhook

    first_payload = _order_payload("processing")
    first_payload["refunds"] = [_refund_entry(901, "-10.50")]
    second_payload = _order_payload("processing")
    second_payload["refunds"] = [
        _refund_entry(901, "-10.50"),
        _refund_entry(902, "-5.00"),
    ]

    first = map_woocommerce_webhook(
        first_payload,
        topic="order.updated",
        delivery_id="delivery-partial-1",
        store_id="store-woo",
    )
    second = map_woocommerce_webhook(
        second_payload,
        topic="order.updated",
        delivery_id="delivery-partial-2",
        store_id="store-woo",
    )

    first_refunds = [e for e in first.events if e.event_type == "refund.succeeded"]
    second_refunds = [e for e in second.events if e.event_type == "refund.succeeded"]
    assert len(second_refunds) == 2
    assert [e.refund_id for e in second_refunds] == ["901", "902"]
    assert [e.amount_cents for e in second_refunds] == [1050, 500]
    # The refund that reappears on every later order.updated dedupes by its own id.
    assert second_refunds[0].event_id == first_refunds[0].event_id
    assert second_refunds[0].event_id != second_refunds[1].event_id


def test_woocommerce_full_refund_with_native_refunds_replaces_the_synthetic_event():
    from services.woocommerce_event_adapter import (
        _entity_event_id,
        map_woocommerce_webhook,
    )

    payload = _order_payload("refunded")
    payload["refunds"] = [
        _refund_entry(901, "-10.50"),
        _refund_entry(902, "-15.00"),
    ]

    batch = map_woocommerce_webhook(
        payload,
        topic="order.updated",
        delivery_id="delivery-full",
        store_id="store-woo",
    )

    assert [event.event_type for event in batch.events] == [
        "refund.succeeded",
        "refund.succeeded",
    ]
    assert [event.refund_id for event in batch.events] == ["901", "902"]
    assert [event.amount_cents for event in batch.events] == [1050, 1500]
    synthetic = _entity_event_id("store-woo", "refund.succeeded", "44:refund")
    assert synthetic not in {event.event_id for event in batch.events}
    # The cumulative total_refunded must not be counted a third time.
    assert 2550 not in {event.amount_cents for event in batch.events}


def test_woocommerce_full_refund_without_native_refunds_keeps_the_legacy_event():
    from services.woocommerce_event_adapter import (
        _entity_event_id,
        map_woocommerce_webhook,
    )

    batch = map_woocommerce_webhook(
        _order_payload("refunded"),
        topic="order.updated",
        delivery_id="delivery-legacy",
        store_id="store-woo",
    )

    assert len(batch.events) == 1
    legacy = batch.events[0]
    assert legacy.event_type == "refund.succeeded"
    assert legacy.event_id == _entity_event_id("store-woo", "refund.succeeded", "44:refund")
    assert legacy.refund_id is None
    assert legacy.amount_cents == 2550
    assert legacy.metadata["native_amount_semantics"] == "cumulative_refund_total"
    assert legacy.metadata["native_cumulative_refund_total"] == "25.50"


def test_woocommerce_malformed_refund_entries_never_suppress_a_valid_sibling():
    from services.woocommerce_event_adapter import map_woocommerce_webhook

    payload = _order_payload("processing")
    payload["refunds"] = [
        {"reason": "no id at all", "total": "-4.00"},
        "not-an-object",
        _refund_entry(903, "not-a-number"),
        _refund_entry(904, "10.25"),
        _refund_entry(905, "-1.00"),
        _refund_entry(905, "-1.00"),
    ]

    batch = map_woocommerce_webhook(
        payload,
        topic="order.updated",
        delivery_id="delivery-malformed",
        store_id="store-woo",
    )

    refunds = [event for event in batch.events if event.event_type == "refund.succeeded"]
    # An entry with no native id cannot be made idempotent, so it is dropped.
    # A duplicated id inside one array is emitted once.
    assert [event.refund_id for event in refunds] == ["903", "904", "905"]
    # An unparseable total records the refund fact with no invented amount.
    assert refunds[0].amount_cents is None
    # WooCommerce writes refund totals negative; a positive one is a magnitude.
    assert refunds[1].amount_cents == 1025
    assert refunds[2].amount_cents == 100


def test_woocommerce_full_refund_with_only_malformed_entries_falls_back_to_legacy():
    from services.woocommerce_event_adapter import (
        _entity_event_id,
        map_woocommerce_webhook,
    )

    payload = _order_payload("refunded")
    payload["refunds"] = [{"reason": "no id", "total": "-25.50"}]

    batch = map_woocommerce_webhook(
        payload,
        topic="order.updated",
        delivery_id="delivery-fallback",
        store_id="store-woo",
    )

    assert len(batch.events) == 1
    assert batch.events[0].event_id == _entity_event_id(
        "store-woo", "refund.succeeded", "44:refund"
    )


def _partially_refunded_order(refunds):
    """A wc/v3 order whose only stitch key across deliveries is its order id."""
    payload = _order_payload("processing")
    # Drop the Order Attribution click so order_id, not click_id, is the key
    # that has to pull both refunds onto one interaction.
    payload.pop("meta_data", None)
    payload["currency_minor_unit"] = 2
    payload["refunds"] = refunds
    return payload


async def _sqlite_ledger(tmp_path, monkeypatch, name):
    """Real commerce ledger tables on SQLite, wired into the write path."""
    import databases
    from sqlalchemy import create_engine

    from db.commerce_interactions import (
        commerce_interaction_events,
        commerce_interactions,
    )
    from db.database import metadata
    from services import commerce_interaction_service as service

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
    monkeypatch.setattr(service, "database", test_database)
    monkeypatch.setattr(service, "IS_POSTGRES", False)
    return test_database


async def _ingest_woocommerce_webhook(payload, *, delivery_id, merchant_id="merchant-woo"):
    from services.merchant_event_ingest_service import ingest_merchant_event_batch
    from services.woocommerce_event_adapter import map_woocommerce_webhook

    batch = map_woocommerce_webhook(
        payload,
        topic="order.updated",
        delivery_id=delivery_id,
        store_id="store-woo",
    )
    # SQLite drops timezone data; keep the whole fixture naive.
    for event in batch.events:
        event.occurred_at = event.occurred_at.replace(tzinfo=None)
    # PR #2051 (ledger trust provenance) makes `write_path` a required kwarg
    # of ingest_merchant_event_batch. This test must pass both before and after
    # that PR lands, so pass the route's write path only once the signature
    # accepts it; the AST ratchet in that PR covers routes/services, not tests.
    import inspect

    provenance = (
        {"write_path": "woocommerce_webhook"}
        if "write_path" in inspect.signature(ingest_merchant_event_batch).parameters
        else {}
    )
    return await ingest_merchant_event_batch(
        merchant_id=merchant_id,
        batch=batch,
        agent_identity_confidence="platform_asserted",
        **provenance,
    )


@pytest.mark.asyncio
async def test_two_partial_refunds_land_on_one_interaction_without_unique_violation(
    tmp_path, monkeypatch
):
    from sqlalchemy import select

    from db.commerce_interactions import (
        commerce_interaction_events,
        commerce_interactions,
    )

    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "woo-partial-refunds")
    try:
        # "no unique violation" is only a claim worth making if the fixture
        # actually built the guard the two refund ids have to pass.
        indexes = await test_database.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
        assert "idx_commerce_interactions_refund_id_unique" in {
            dict(row)["name"] for row in indexes
        }

        first = _partially_refunded_order([_refund_entry(901, "-10.50")])
        second = _partially_refunded_order(
            [_refund_entry(901, "-10.50"), _refund_entry(902, "-5.00")]
        )

        await _ingest_woocommerce_webhook(first, delivery_id="delivery-partial-1")
        await _ingest_woocommerce_webhook(second, delivery_id="delivery-partial-2")

        interactions = await test_database.fetch_all(select(commerce_interactions))
        assert len(interactions) == 1
        interaction = dict(interactions[0])
        assert interaction["order_id"] == "44"
        # The interaction carries the most recent refund id; the per-refund
        # facts live on the events, so the second refund does not fork a row.
        assert interaction["refund_id"] == "902"

        rows = [
            dict(row)
            for row in await test_database.fetch_all(
                select(commerce_interaction_events).where(
                    commerce_interaction_events.c.event_type == "refund.succeeded"
                )
            )
        ]
        assert len(rows) == 2
        assert {row["interaction_id"] for row in rows} == {
            interaction["interaction_id"]
        }
        assert sorted(row["payload"]["refund_id"] for row in rows) == ["901", "902"]
        assert sorted(row["payload"]["amount_cents"] for row in rows) == [500, 1050]

        # A third delivery repeating refunds[] must report both as duplicates.
        replay = await _ingest_woocommerce_webhook(second, delivery_id="delivery-partial-3")
        refund_results = [
            result
            for result in replay["events"]
            if result["event_id"].startswith("woocommerce:refund.succeeded:")
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
async def test_funnel_sums_both_woocommerce_partial_refunds(tmp_path, monkeypatch):
    from services import merchant_commerce_event_funnel_service as funnel_service

    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "woo-refund-funnel")
    monkeypatch.setattr(funnel_service, "database", test_database)
    try:
        await _ingest_woocommerce_webhook(
            _partially_refunded_order([_refund_entry(901, "-10.50")]),
            delivery_id="delivery-partial-1",
        )
        await _ingest_woocommerce_webhook(
            _partially_refunded_order(
                [_refund_entry(901, "-10.50"), _refund_entry(902, "-5.00")]
            ),
            delivery_id="delivery-partial-2",
        )

        result = await funnel_service.get_merchant_commerce_event_funnel(
            merchant_id="merchant-woo",
            group_by="store",
        )
        assert result.payload["available"] is True
        summary = result.payload["summary"]
        assert summary["event_type_breakdown"]["refund.succeeded"] == 2
        # Both refunds carry source=woocommerce_webhook, so _refund_authority
        # files them under the same "store" authority and they sum instead of
        # taking a max as a PSP mirror of the same money would.
        assert summary["refunded_amount_cents_by_currency"] == {"USD": 1550}
        assert result.payload["slices"][0]["key"] == "store-woo"
        assert result.payload["slices"][0]["refunded_amount_cents_by_currency"] == {
            "USD": 1550
        }
    finally:
        await test_database.disconnect()
