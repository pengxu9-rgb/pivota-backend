import asyncio
import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _envelope(event_code, value, *, event_id="event-1", client_id="client-1"):
    return {
        "specversion": "1.0",
        "id": f"cloud-{event_id}",
        "eventid": event_id,
        "recipientclientid": client_id,
        "source": "urn:uuid:12345678-1234-1234-1234-123456789abc",
        "type": f"com.adobe.commerce.{event_code}",
        "time": "2026-08-28T10:00:00Z",
        "data": {"key": event_id, "source": "commerce.Stage", "value": value},
    }


def _order():
    return {
        "entity_id": 44,
        "increment_id": "000000044",
        "state": "processing",
        "status": "processing",
        "grand_total": "25.50",
        "total_paid": "25.50",
        "total_due": "0.00",
        "order_currency_code": "USD",
        "customer_id": 8,
        "customer_email": "buyer@example.com",
        "billing_address": {"firstname": "Private", "telephone": "555-0100"},
        "payment": {
            "last_trans_id": "txn-44",
            "cc_number_enc": "encrypted-card-data",
            "cc_last4": "4242",
        },
        "items": [
            {
                "item_id": 1,
                "product_id": 10,
                "sku": "SKU-10",
                "qty_ordered": 2,
                "price": "12.75",
                "row_total": "25.50",
                "name": "Do not retain",
                "product_option": {"private": "value"},
            }
        ],
    }


def test_adobe_commerce_paid_order_maps_to_stable_created_and_paid_facts_without_pii():
    from services.adobe_commerce_event_adapter import map_adobe_commerce_io_event

    first = map_adobe_commerce_io_event(
        _envelope("observer.sales_order_save_after", _order()),
        store_id="store-magento",
    )
    retry = map_adobe_commerce_io_event(
        _envelope("observer.sales_order_save_after", _order(), event_id="retry-delivery"),
        store_id="store-magento",
    )

    assert [event.event_type for event in first.events] == ["order.created", "order.paid"]
    assert [event.event_id for event in first.events] == [event.event_id for event in retry.events]
    assert first.events[0].order_id == "44"
    assert first.events[0].payment_id == "txn-44"
    assert first.events[0].amount_cents == 2550
    assert first.events[0].metadata["native_line_items"] == [
        {
            "id": 1,
            "product_id": 10,
            "sku": "SKU-10",
            "quantity": 2,
            "price": "12.75",
            "subtotal": "25.50",
        }
    ]
    serialized = first.model_dump_json()
    for private_value in (
        "buyer@example.com",
        "555-0100",
        "Private",
        "encrypted-card-data",
        "4242",
        "Do not retain",
    ):
        assert private_value not in serialized


def test_adobe_commerce_does_not_overclaim_partial_payment_when_total_due_is_missing():
    from services.adobe_commerce_event_adapter import map_adobe_commerce_io_event

    order = _order()
    order["grand_total"] = "25.00"
    order["total_paid"] = "5.00"
    order.pop("total_due")
    batch = map_adobe_commerce_io_event(
        _envelope("observer.sales_order_save_after", order),
        store_id="store-magento",
    )
    assert [event.event_type for event in batch.events] == ["order.created"]


def test_adobe_commerce_rejects_non_scalar_ids_and_non_finite_amounts_from_safe_fields():
    from services.adobe_commerce_event_adapter import map_adobe_commerce_io_event

    order = _order()
    order["customer_id"] = {"email": "nested-private@example.com"}
    order["grand_total"] = "NaN"
    order["total_paid"] = "0"
    order["total_due"] = "NaN"
    event = map_adobe_commerce_io_event(
        _envelope("observer.sales_order_save_after", order),
        store_id="store-magento",
    ).events[0]
    assert event.buyer_id is None
    assert event.amount_cents is None
    assert "nested-private@example.com" not in event.model_dump_json()


def test_adobe_commerce_tolerates_scalar_items_schema_drift():
    from services.adobe_commerce_event_adapter import map_adobe_commerce_io_event

    order = _order()
    order["items"] = 7
    event = map_adobe_commerce_io_event(
        _envelope("observer.sales_order_save_after", order),
        store_id="store-magento",
    ).events[0]
    assert "native_line_items" not in event.metadata


def test_adobe_commerce_requires_exact_commerce_event_type_prefix():
    from services.adobe_commerce_event_adapter import (
        UnsupportedAdobeCommerceEvent,
        map_adobe_commerce_io_event,
    )

    envelope = _envelope("observer.sales_order_save_after", _order())
    envelope["type"] = "com.adobe.other.observer.sales_order_save_after"
    with pytest.raises(UnsupportedAdobeCommerceEvent):
        map_adobe_commerce_io_event(envelope, store_id="store-magento")


def test_adobe_commerce_invoice_and_creditmemo_require_terminal_state():
    from services.adobe_commerce_event_adapter import (
        UnsupportedAdobeCommerceEvent,
        map_adobe_commerce_io_event,
    )

    invoice = {
        "entity_id": 90,
        "order_id": 44,
        "state": 2,
        "transaction_id": "capture-90",
        "grand_total": "25.50",
        "order_currency_code": "USD",
    }
    payment = map_adobe_commerce_io_event(
        _envelope("observer.sales_order_invoice_save_after", invoice),
        store_id="store-magento",
    ).events[0]
    assert payment.event_type == "payment.succeeded"
    assert payment.order_id == "44"
    assert payment.payment_id == "capture-90"

    invoice["state"] = 1
    with pytest.raises(UnsupportedAdobeCommerceEvent, match="not paid"):
        map_adobe_commerce_io_event(
            _envelope("observer.sales_order_invoice_save_after", invoice),
            store_id="store-magento",
        )

    creditmemo = {
        "entity_id": 91,
        "increment_id": "100000091",
        "order_id": 44,
        "state": 2,
        "transaction_id": "refund-91",
        "grand_total": "5.00",
        "order_currency_code": "USD",
    }
    refund = map_adobe_commerce_io_event(
        _envelope("observer.sales_order_creditmemo_save_after", creditmemo),
        store_id="store-magento",
    ).events[0]
    assert refund.event_type == "refund.succeeded"
    assert refund.refund_id == "91"
    assert refund.order_id == "44"
    assert refund.amount_cents == 500


@pytest.mark.asyncio
async def test_adobe_io_signature_accepts_either_rotating_adobe_key(monkeypatch):
    from services import adobe_io_webhook_auth as auth

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    raw = b'{"event":"signed"}'
    signature = base64.b64encode(
        private_key.sign(raw, padding.PKCS1v15(), hashes.SHA256())
    ).decode("ascii")

    async def fake_fetch(path):
        assert path == "/prod/keys/pub-key-12345678-1234-1234-1234-123456789abc.pem"
        return private_key.public_key()

    monkeypatch.setattr(auth, "_fetch_public_key", fake_fetch)
    assert await auth.verify_adobe_io_signature(
        raw,
        signature_1="invalid",
        signature_2=signature,
        public_key_path_1="https://attacker.example/key.pem",
        public_key_path_2="/prod/keys/pub-key-12345678-1234-1234-1234-123456789abc.pem",
    )
    assert not await auth.verify_adobe_io_signature(
        raw + b"tampered",
        signature_1=None,
        signature_2=signature,
        public_key_path_1=None,
        public_key_path_2="/prod/keys/pub-key-12345678-1234-1234-1234-123456789abc.pem",
    )


@pytest.mark.asyncio
async def test_adobe_io_signature_tries_second_key_after_first_fetch_is_unavailable(monkeypatch):
    from services import adobe_io_webhook_auth as auth

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    raw = b'{"event":"signed"}'
    signature = base64.b64encode(
        private_key.sign(raw, padding.PKCS1v15(), hashes.SHA256())
    ).decode("ascii")

    async def fake_fetch(path):
        if path.endswith("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.pem"):
            raise auth.AdobeIOPublicKeyUnavailable("temporary")
        return private_key.public_key()

    monkeypatch.setattr(auth, "_fetch_public_key", fake_fetch)
    assert await auth.verify_adobe_io_signature(
        raw,
        signature_1=signature,
        signature_2=signature,
        public_key_path_1="/prod/keys/pub-key-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.pem",
        public_key_path_2="/prod/keys/pub-key-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb.pem",
    )


@pytest.mark.asyncio
async def test_adobe_io_public_key_fetch_is_single_flight_and_negative_cached(monkeypatch):
    from services import adobe_io_webhook_auth as auth

    good_path = "/prod/keys/pub-key-cccccccc-cccc-cccc-cccc-cccccccccccc.pem"
    bad_path = "/prod/keys/pub-key-dddddddd-dddd-dddd-dddd-dddddddddddd.pem"
    auth._key_cache.pop(good_path, None)
    auth._negative_key_cache.pop(bad_path, None)
    calls = []
    public_key = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()

    async def fake_download(path):
        calls.append(path)
        await asyncio.sleep(0)
        if path == bad_path:
            raise auth.AdobeIOWebhookAuthError("missing")
        return public_key

    monkeypatch.setattr(auth, "_download_public_key", fake_download)
    first, second = await asyncio.gather(
        auth._fetch_public_key(good_path),
        auth._fetch_public_key(good_path),
    )
    assert first is public_key and second is public_key
    assert calls.count(good_path) == 1

    with pytest.raises(auth.AdobeIOWebhookAuthError):
        await auth._fetch_public_key(bad_path)
    with pytest.raises(auth.AdobeIOWebhookAuthError):
        await auth._fetch_public_key(bad_path)
    assert calls.count(bad_path) == 1
    auth._key_cache.pop(good_path, None)
    auth._negative_key_cache.pop(bad_path, None)


@pytest.mark.asyncio
async def test_adobe_io_key_fetch_survives_one_cancelled_waiter(monkeypatch):
    from services import adobe_io_webhook_auth as auth

    path = "/prod/keys/pub-key-eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee.pem"
    auth._key_cache.pop(path, None)


@pytest.mark.asyncio
async def test_adobe_io_key_fetch_registry_cleans_up_after_only_waiter_cancels(monkeypatch):
    from services import adobe_io_webhook_auth as auth

    path = "/prod/keys/pub-key-ffffffff-ffff-ffff-ffff-ffffffffffff.pem"
    auth._key_cache.pop(path, None)
    auth._negative_key_cache.pop(path, None)
    auth._key_fetch_tasks.pop(path, None)
    gate = asyncio.Event()
    public_key = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()

    async def fake_download(candidate):
        assert candidate == path
        await gate.wait()
        return public_key

    monkeypatch.setattr(auth, "_download_public_key", fake_download)
    only_waiter = asyncio.create_task(auth._fetch_public_key(path))
    await asyncio.sleep(0)
    only_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await only_waiter
    assert path in auth._key_fetch_tasks
    gate.set()
    for _ in range(5):
        await asyncio.sleep(0)
        if path not in auth._key_fetch_tasks:
            break
    assert path not in auth._key_fetch_tasks
    auth._negative_key_cache.pop(path, None)
    gate = asyncio.Event()
    calls = []
    public_key = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()

    async def fake_download(candidate):
        calls.append(candidate)
        await gate.wait()
        return public_key

    monkeypatch.setattr(auth, "_download_public_key", fake_download)
    cancelled_waiter = asyncio.create_task(auth._fetch_public_key(path))
    surviving_waiter = asyncio.create_task(auth._fetch_public_key(path))
    await asyncio.sleep(0)
    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter
    gate.set()
    assert await surviving_waiter is public_key
    assert calls == [path]
    auth._key_cache.pop(path, None)


def test_adobe_commerce_route_validates_challenge_recipient_signature_and_batch(monkeypatch):
    from routes import adobe_commerce_events as route

    ingested = []
    verification_calls = []

    class FakeDB:
        async def fetch_one(self, *args, **kwargs):
            return {
                "store_id": "store-magento",
                "merchant_id": "merchant-1",
                "api_key": json.dumps(
                    {
                        "access_token": "secret",
                        "adobe_io_client_id": "client-1",
                        "adobe_io_provider_source": "urn:uuid:12345678-1234-1234-1234-123456789abc",
                    }
                ),
            }

    async def fake_verify(raw, **kwargs):
        verification_calls.append((raw, kwargs))
        return kwargs["signature_2"] == "valid-signature"

    async def fake_ingest(**kwargs):
        ingested.append(kwargs)
        return {"accepted": len(kwargs["batch"].events), "duplicates": 0, "events": []}

    monkeypatch.setattr(route, "database", FakeDB())
    monkeypatch.setattr(route, "verify_adobe_io_signature", fake_verify)
    monkeypatch.setattr(route, "ingest_merchant_event_batch", fake_ingest)
    app = FastAPI()
    app.include_router(route.router)
    client = TestClient(app)

    challenge = client.get(
        "/webhooks/adobe-commerce/store-magento?challenge=challenge-value"
    )
    assert challenge.status_code == 200
    assert challenge.text == "challenge-value"

    payload = [
        _envelope("observer.checkout_submit_all_after", {"order": _order()}),
        _envelope("observer.sales_order_invoice_save_after", {
            "entity_id": 90,
            "order_id": 44,
            "state": 2,
            "transaction_id": "capture-90",
            "grand_total": "25.50",
            "order_currency_code": "USD",
        }, event_id="event-2"),
    ]
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    response = client.post(
        "/webhooks/adobe-commerce/store-magento",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Adobe-Digital-Signature-2": "valid-signature",
            "X-Adobe-Public-Key2-Path": "/prod/keys/pub-key-12345678-1234-1234-1234-123456789abc.pem",
            "X-Adobe-Delivery-Id": "delivery-1",
        },
    )
    assert response.status_code == 200
    assert response.json()["accepted"] == 2
    assert ingested[0]["merchant_id"] == "merchant-1"
    assert ingested[0]["agent_identity_confidence"] == "platform_asserted"
    assert verification_calls[0][0] == raw

    wrong_recipient = [dict(payload[0], recipientclientid="another-client")]
    invalid = client.post(
        "/webhooks/adobe-commerce/store-magento",
        json=wrong_recipient,
        headers={"X-Adobe-Digital-Signature-2": "valid-signature"},
    )
    assert invalid.status_code == 401
    assert len(verification_calls) == 1

    wrong_provider = [dict(payload[0], source="urn:uuid:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")]
    invalid_provider = client.post(
        "/webhooks/adobe-commerce/store-magento",
        json=wrong_provider,
        headers={"X-Adobe-Digital-Signature-2": "valid-signature"},
    )
    assert invalid_provider.status_code == 401
    assert len(verification_calls) == 1

    oversized = client.post(
        "/webhooks/adobe-commerce/store-magento",
        content=b"x" * (route.MAX_ADOBE_IO_WEBHOOK_BYTES + 1),
    )
    assert oversized.status_code == 413


def test_adobe_commerce_route_chunks_expanded_canonical_batch(monkeypatch):
    from routes import adobe_commerce_events as route

    ingested_sizes = []

    class FakeDB:
        async def fetch_one(self, *args, **kwargs):
            return {
                "store_id": "store-magento-batch",
                "merchant_id": "merchant-1",
                "api_key": json.dumps(
                    {
                        "adobe_io_client_id": "client-1",
                        "adobe_io_provider_source": "urn:uuid:12345678-1234-1234-1234-123456789abc",
                    }
                ),
            }

    async def fake_verify(*args, **kwargs):
        return True

    async def fake_ingest(**kwargs):
        size = len(kwargs["batch"].events)
        ingested_sizes.append(size)
        return {"accepted": size, "duplicates": 0, "events": []}

    monkeypatch.setattr(route, "database", FakeDB())
    monkeypatch.setattr(route, "verify_adobe_io_signature", fake_verify)
    monkeypatch.setattr(route, "ingest_merchant_event_batch", fake_ingest)
    app = FastAPI()
    app.include_router(route.router)
    client = TestClient(app)
    payload = [
        _envelope(
            "observer.sales_order_save_after",
            dict(_order(), entity_id=index, increment_id=str(index)),
            event_id=f"event-{index}",
        )
        for index in range(51)
    ]
    response = client.post(
        "/webhooks/adobe-commerce/store-magento-batch",
        json=payload,
        headers={
            "X-Adobe-Digital-Signature-1": "valid",
            "X-Adobe-Public-Key1-Path": "/prod/keys/pub-key-12345678-1234-1234-1234-123456789abc.pem",
        },
    )
    assert response.status_code == 200
    assert response.json()["accepted"] == 102
    assert ingested_sizes == [100, 2]


@pytest.mark.asyncio
async def test_magento_reconnect_preserves_adobe_io_client_id(monkeypatch):
    from routes import magento_integration as route

    writes = []

    class FakeDB:
        async def fetch_one(self, *args, **kwargs):
            return {
                "store_id": "store-existing",
                "api_key": json.dumps(
                    {
                        "access_token": "old",
                        "adobe_io_client_id": "keep-client",
                        "adobe_io_provider_source": "urn:uuid:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    }
                ),
            }

        async def execute(self, query, values):
            writes.append(values)

    class FakeAdapter:
        def __init__(self, config):
            self.store_url = "https://shop.example"

        def validate_config(self):
            return True, None

        async def test_connection(self):
            return {"success": True, "store_name": "Example", "product_count": 8}

    async def fake_sync(*args, **kwargs):
        return None

    monkeypatch.setattr(route, "database", FakeDB())
    monkeypatch.setattr(route, "MagentoAdapter", FakeAdapter)
    monkeypatch.setattr(route, "sync_catalog_merchant_status", fake_sync)
    result = await route.connect_magento(
        route.MagentoConnectRequest(
            merchant_id="merchant-1",
            store_url="shop.example",
            access_token="new-token",
        ),
        current_user={"role": "merchant", "merchant_id": "merchant-1"},
    )
    persisted = json.loads(writes[0]["api_key"])
    assert persisted["adobe_io_client_id"] == "keep-client"
    assert persisted["adobe_io_provider_source"] == "urn:uuid:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert result["native_eventing"] == "adobe_io_events_configured"


@pytest.mark.parametrize("value", ["", " ", "\t"])
def test_magento_connect_rejects_blank_adobe_io_identifiers(value):
    from pydantic import ValidationError
    from routes.magento_integration import MagentoConnectRequest

    with pytest.raises(ValidationError):
        MagentoConnectRequest(
            merchant_id="merchant-1",
            store_url="https://shop.example",
            access_token="token",
            adobe_io_client_id=value,
            adobe_io_provider_id="12345678-1234-1234-1234-123456789abc",
        )
