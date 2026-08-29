import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _event(event_type="order.created"):
    return {
        "event_id": "native-event-1",
        "type": event_type,
        "occurred_at": "2026-08-28T10:00:00Z",
        "site_id": "RefArchGlobal",
        "basket_id": "basket-1",
        "checkout_id": "checkout-1",
        "order_id": "order-44",
        "payment_id": "payment-44",
        "refund_id": "refund-44",
        "customer_id": "customer-8",
        "session_id": "session-8",
        "click_id": "clk_abcdef1234",
        "amount": "25.50",
        "currency": "USD",
        "status": "created",
        "items": [
            {
                "id": "item-1",
                "product_id": "product-10",
                "variant_id": "variant-11",
                "sku": "SKU-11",
                "quantity": 2,
                "price": "12.75",
                "total": "25.50",
                "product_name": "Do not retain",
            }
        ],
        "billing_address": {"email": "buyer@example.com"},
    }


def test_sfcc_event_maps_to_platform_neutral_ledger_without_pii():
    from services.sfcc_event_adapter import map_sfcc_integration_event

    event = map_sfcc_integration_event(
        _event(),
        store_id="store-sfcc",
        delivery_id="delivery-1",
    )
    retry = map_sfcc_integration_event(
        _event(),
        store_id="store-sfcc",
        delivery_id="delivery-2",
    )

    assert event.event_type == "order.created"
    assert event.platform == "salesforce_commerce_cloud"
    assert event.source == "sfcc_cartridge_outbox"
    assert event.order_id == "order-44"
    assert event.click_id == "clk_abcdef1234"
    assert event.amount_cents == 2550
    assert event.event_id == retry.event_id
    assert event.metadata["native_site_id"] == "RefArchGlobal"
    assert event.metadata["native_line_items"] == [
        {
            "id": "item-1",
            "product_id": "product-10",
            "variant_id": "variant-11",
            "sku": "SKU-11",
            "quantity": 2,
            "price": "12.75",
            "total": "25.50",
        }
    ]
    serialized = event.model_dump_json()
    assert "buyer@example.com" not in serialized
    assert "Do not retain" not in serialized


@pytest.mark.parametrize(
    ("native_type", "canonical_type"),
    [
        ("basket.created", "cart.created"),
        ("basket.item_added", "cart.item_added"),
        ("checkout.submitted", "checkout.submitted"),
        ("payment.authorized", "payment.authorized"),
        ("payment.declined", "payment.declined"),
        ("order.cancelled", "order.cancelled"),
        ("refund.succeeded", "refund.succeeded"),
    ],
)
def test_sfcc_lifecycle_mapping(native_type, canonical_type):
    from services.sfcc_event_adapter import map_sfcc_integration_event

    event = map_sfcc_integration_event(
        _event(native_type),
        store_id="store-sfcc",
    )
    assert event.event_type == canonical_type


def test_sfcc_event_requires_native_id_and_entity_stitch_key():
    from services.sfcc_event_adapter import map_sfcc_integration_event

    missing_id = _event()
    missing_id.pop("event_id")
    with pytest.raises(ValueError, match="event_id"):
        map_sfcc_integration_event(missing_id, store_id="store-sfcc")

    missing_order = _event()
    missing_order.pop("order_id")
    with pytest.raises(ValueError, match="order_id"):
        map_sfcc_integration_event(missing_order, store_id="store-sfcc")


def test_sfcc_webhook_requires_fresh_body_signature_and_exact_site(monkeypatch):
    from routes import sfcc_events as route

    ingested = []

    class FakeDB:
        async def fetch_one(self, *args, **kwargs):
            return {
                "store_id": "store-sfcc",
                "merchant_id": "merchant-1",
                "api_key": json.dumps(
                    {
                        "site_id": "RefArchGlobal",
                        "telemetry_signing_secret": "hook-secret",
                    }
                ),
            }

    async def fake_ingest(**kwargs):
        ingested.append(kwargs)
        return {"accepted": len(kwargs["batch"].events), "duplicates": 0, "events": []}

    monkeypatch.setattr(route, "database", FakeDB())
    monkeypatch.setattr(route, "ingest_merchant_event_batch", fake_ingest)
    monkeypatch.setattr(route.time, "time", lambda: 2_000_000_000)
    app = FastAPI()
    app.include_router(route.router)
    client = TestClient(app)
    raw = json.dumps({"events": [_event()]}, separators=(",", ":")).encode("utf-8")
    timestamp = "2000000000"
    digest = hmac.new(
        b"hook-secret",
        timestamp.encode("ascii") + b"." + raw,
        hashlib.sha256,
    ).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Pivota-SFCC-Signature": f"sha256={digest}",
        "X-Pivota-SFCC-Timestamp": timestamp,
        "X-Pivota-SFCC-Delivery-Id": "delivery-1",
        "X-Pivota-SFCC-Site-Id": "RefArchGlobal",
    }

    response = client.post(
        "/webhooks/salesforce-commerce-cloud/store-sfcc",
        content=raw,
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["accepted"] == 1
    assert ingested[0]["merchant_id"] == "merchant-1"

    tampered = client.post(
        "/webhooks/salesforce-commerce-cloud/store-sfcc",
        content=raw + b" ",
        headers=headers,
    )
    assert tampered.status_code == 401

    wrong_site = client.post(
        "/webhooks/salesforce-commerce-cloud/store-sfcc",
        content=raw,
        headers={**headers, "X-Pivota-SFCC-Site-Id": "OtherSite"},
    )
    assert wrong_site.status_code == 401

    for body_site_id in ("OtherSite", None):
        body_event = _event()
        body_event["site_id"] = body_site_id
        body_raw = json.dumps(
            {"events": [body_event]}, separators=(",", ":")
        ).encode("utf-8")
        body_digest = hmac.new(
            b"hook-secret",
            timestamp.encode("ascii") + b"." + body_raw,
            hashlib.sha256,
        ).hexdigest()
        body_site_response = client.post(
            "/webhooks/salesforce-commerce-cloud/store-sfcc",
            content=body_raw,
            headers={
                **headers,
                "X-Pivota-SFCC-Signature": f"sha256={body_digest}",
            },
        )
        assert body_site_response.status_code == 401

    expired = client.post(
        "/webhooks/salesforce-commerce-cloud/store-sfcc",
        content=raw,
        headers={**headers, "X-Pivota-SFCC-Timestamp": "1999999000"},
    )
    assert expired.status_code == 401


def test_sfcc_webhook_ignores_future_event_without_dropping_supported_batch_items(monkeypatch):
    from routes import sfcc_events as route

    ingested = []

    class FakeDB:
        async def fetch_one(self, *args, **kwargs):
            return {
                "store_id": "store-sfcc",
                "merchant_id": "merchant-1",
                "api_key": json.dumps(
                    {
                        "site_id": "RefArchGlobal",
                        "telemetry_signing_secret": "hook-secret",
                    }
                ),
            }

    async def fake_ingest(**kwargs):
        ingested.extend(kwargs["batch"].events)
        return {"accepted": len(kwargs["batch"].events), "duplicates": 0, "events": []}

    monkeypatch.setattr(route, "database", FakeDB())
    monkeypatch.setattr(route, "ingest_merchant_event_batch", fake_ingest)
    monkeypatch.setattr(route.time, "time", lambda: 2_000_000_000)
    app = FastAPI()
    app.include_router(route.router)
    client = TestClient(app)
    future = _event()
    future["event_id"] = "future-1"
    future["type"] = "shipment.dispatched"
    raw = json.dumps({"events": [future, _event()]}, separators=(",", ":")).encode()
    timestamp = "2000000000"
    digest = hmac.new(
        b"hook-secret", timestamp.encode() + b"." + raw, hashlib.sha256
    ).hexdigest()
    response = client.post(
        "/webhooks/salesforce-commerce-cloud/store-sfcc",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Pivota-SFCC-Signature": f"sha256={digest}",
            "X-Pivota-SFCC-Timestamp": timestamp,
            "X-Pivota-SFCC-Site-Id": "RefArchGlobal",
        },
    )

    assert response.status_code == 200
    assert response.json()["accepted"] == 1
    assert response.json()["ignored"] == 1
    assert [event.event_type for event in ingested] == ["order.created"]


def test_sfcc_webhook_drops_malformed_supported_event_without_poisoning_valid_sibling(
    monkeypatch,
):
    from routes import sfcc_events as route

    ingested = []

    class FakeDB:
        async def fetch_one(self, *args, **kwargs):
            return {
                "store_id": "store-sfcc",
                "merchant_id": "merchant-1",
                "api_key": json.dumps(
                    {
                        "site_id": "RefArchGlobal",
                        "telemetry_signing_secret": "hook-secret",
                    }
                ),
            }

    async def fake_ingest(**kwargs):
        ingested.extend(kwargs["batch"].events)
        return {"accepted": len(kwargs["batch"].events), "duplicates": 0, "events": []}

    monkeypatch.setattr(route, "database", FakeDB())
    monkeypatch.setattr(route, "ingest_merchant_event_batch", fake_ingest)
    monkeypatch.setattr(route.time, "time", lambda: 2_000_000_000)
    app = FastAPI()
    app.include_router(route.router)
    client = TestClient(app)
    malformed = _event()
    malformed["event_id"] = "malformed-1"
    malformed.pop("order_id")
    valid = _event()
    valid["event_id"] = "valid-1"
    raw = json.dumps({"events": [malformed, valid]}, separators=(",", ":")).encode()
    timestamp = "2000000000"
    digest = hmac.new(
        b"hook-secret", timestamp.encode() + b"." + raw, hashlib.sha256
    ).hexdigest()

    response = client.post(
        "/webhooks/salesforce-commerce-cloud/store-sfcc",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Pivota-SFCC-Signature": f"sha256={digest}",
            "X-Pivota-SFCC-Timestamp": timestamp,
            "X-Pivota-SFCC-Site-Id": "RefArchGlobal",
        },
    )

    assert response.status_code == 200
    assert response.json()["accepted"] == 1
    assert response.json()["rejected"] == 1
    assert len(ingested) == 1
    assert ingested[0].order_id == "order-44"


@pytest.mark.asyncio
async def test_sfcc_telemetry_provision_returns_secret_once_and_supports_rotation(monkeypatch):
    from routes import sfcc_integration as route

    writes = []
    credentials = {
        "site_id": "RefArchGlobal",
        "client_id": "client-123",
        "client_secret": "slas-secret",
    }

    class FakeDB:
        async def fetch_one(self, query, values):
            if query.lstrip().startswith("UPDATE"):
                assert values["expected_api_key"] == json.dumps(credentials)
                writes.append(values)
                credentials.clear()
                credentials.update(json.loads(values["api_key"]))
                return {"store_id": "store-sfcc"}
            return {
                "store_id": "store-sfcc",
                "merchant_id": "merchant-1",
                "api_key": json.dumps(credentials),
            }

    generated = iter(["first-signing-secret", "rotated-signing-secret"])
    monkeypatch.setattr(route, "database", FakeDB())
    monkeypatch.setattr(route.secrets, "token_urlsafe", lambda _: next(generated))
    user = {"role": "merchant", "merchant_id": "merchant-1"}

    first = await route.provision_salesforce_commerce_cloud_telemetry(
        "store-sfcc",
        route.SalesforceCommerceCloudTelemetryProvisionRequest(),
        current_user=user,
    )
    assert first["status"] == "provisioned"
    assert first["signing_secret"] == "first-signing-secret"
    assert credentials["client_secret"] == "slas-secret"

    existing = await route.provision_salesforce_commerce_cloud_telemetry(
        "store-sfcc",
        route.SalesforceCommerceCloudTelemetryProvisionRequest(),
        current_user=user,
    )
    assert existing["status"] == "already_configured"
    assert "signing_secret" not in existing

    rotated = await route.provision_salesforce_commerce_cloud_telemetry(
        "store-sfcc",
        route.SalesforceCommerceCloudTelemetryProvisionRequest(rotate=True),
        current_user=user,
    )
    assert rotated["status"] == "rotated"
    assert rotated["signing_secret"] == "rotated-signing-secret"
    assert len(writes) == 2


@pytest.mark.asyncio
async def test_sfcc_first_provision_cas_loser_does_not_return_a_stale_secret(monkeypatch):
    from routes import sfcc_integration as route

    base = {
        "site_id": "RefArchGlobal",
        "client_id": "client-123",
        "client_secret": "slas-secret",
    }
    concurrent = {**base, "telemetry_signing_secret": "concurrent-winner"}
    selects = 0

    class FakeDB:
        async def fetch_one(self, query, values):
            nonlocal selects
            if query.lstrip().startswith("UPDATE"):
                return None
            selects += 1
            credentials = base if selects == 1 else concurrent
            return {
                "store_id": "store-sfcc",
                "merchant_id": "merchant-1",
                "api_key": json.dumps(credentials),
            }

    monkeypatch.setattr(route, "database", FakeDB())
    monkeypatch.setattr(route.secrets, "token_urlsafe", lambda _: "cas-loser-secret")
    result = await route.provision_salesforce_commerce_cloud_telemetry(
        "store-sfcc",
        route.SalesforceCommerceCloudTelemetryProvisionRequest(),
        current_user={"role": "merchant", "merchant_id": "merchant-1"},
    )

    assert result["status"] == "already_configured"
    assert "signing_secret" not in result
    assert "cas-loser-secret" not in json.dumps(result)
