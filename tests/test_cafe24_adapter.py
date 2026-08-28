import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def _data_bridge_payload(event_name="VIEW_CONTENT"):
    data = {
        "mall_id": "sample_mall",
        "shop_no": 1,
        "product_list": {
            "product_no": 10001,
            "variant_code": "P0000QNB000A",
            "product_name": "Sample Product",
            "quantity": 1,
            "product_price": "15000.00",
        },
    }
    if event_name.upper() == "CREATE_ORDER":
        data.update({"order_id": "20260826-00001", "member_id": "member-1", "currency": "KRW"})
    return {
        "event_name": event_name,
        "event_time": "2026-08-26T10:49:11+09:00",
        "event_data": data,
        "analytics_data": {
            "event_source_url": "https://chatgpt.com/c/abc?email=buyer@example.com&token=secret",
            "client_user_agent": "Mozilla/5.0",
            "CVID": "CVID.session-1",
            "CVID_Y": "CVID_Y.visitor-1",
            "CVID_AD": "advertising-visitor-1",
        },
    }


def _order_webhook(event_no=90023, **overrides):
    resource = {
        "mall_id": "sample_mall",
        "event_shop_no": "1",
        "event_code": "create_order",
        "order_id": "20260826-00001",
        "currency": "KRW",
        "order_date": "2026-08-26T15:28:14+09:00",
        "payment_date": "2026-08-26T15:29:14+09:00",
        "paid": "T",
        "actual_payment_amount": "24680.00",
        "member_id": "member-1",
        # These fields must never enter telemetry metadata.
        "buyer_name": "Sensitive Name",
        "buyer_email": "buyer@example.com",
        "buyer_cellphone": "010-0000-0000",
        "bank_account_no": "123-456-789",
    }
    resource.update(overrides)
    return {"event_no": event_no, "resource": resource}


def test_data_bridge_view_maps_to_product_view_and_cvid_session():
    from services.cafe24_event_adapter import map_cafe24_webhook

    batch = map_cafe24_webhook(
        _data_bridge_payload(),
        trace_id="trace-view-1",
        store_id="store-c24",
    )

    assert len(batch.events) == 1
    event = batch.events[0]
    assert event.event_type == "product.viewed"
    assert event.platform == "cafe24"
    assert event.store_id == "store-c24"
    assert event.session_id == "CVID.session-1"
    assert event.visitor_id == "CVID_Y.visitor-1"
    assert event.query_source == "chatgpt.com"
    assert event.metadata["native_product_no"] == "10001"
    serialized = event.model_dump_json()
    assert "buyer@example.com" not in serialized
    assert "token=secret" not in serialized
    assert "Mozilla/5.0" not in serialized
    assert "advertising-visitor-1" not in serialized


def test_order_created_paid_emits_two_correlated_events_and_krw_minor_units():
    from services.cafe24_event_adapter import map_cafe24_webhook

    batch = map_cafe24_webhook(
        _order_webhook(),
        trace_id="trace-order-1",
        store_id="store-c24",
    )

    assert [event.event_type for event in batch.events] == ["order.created", "order.paid"]
    assert {event.order_id for event in batch.events} == {"20260826-00001"}
    assert {event.amount_cents for event in batch.events} == {24680}
    serialized = json.dumps([event.metadata for event in batch.events])
    assert "buyer@example.com" not in serialized
    assert "Sensitive Name" not in serialized
    assert "010-0000-0000" not in serialized
    assert "123-456-789" not in serialized


def test_data_bridge_and_store_webhook_share_order_created_idempotency_key():
    from services.cafe24_event_adapter import map_cafe24_webhook

    bridge = map_cafe24_webhook(
        _data_bridge_payload("CREATE_ORDER"),
        trace_id="trace-data-bridge-order",
        store_id="store-c24",
    )
    webhook = map_cafe24_webhook(
        _order_webhook(),
        trace_id="trace-store-order",
        store_id="store-c24",
    )

    bridge_created = next(event for event in bridge.events if event.event_type == "order.created")
    webhook_created = next(event for event in webhook.events if event.event_type == "order.created")
    assert bridge_created.event_id == webhook_created.event_id
    assert bridge_created.trace_id != webhook_created.trace_id


def test_payment_status_and_refund_status_map_to_canonical_events():
    from services.cafe24_event_adapter import map_cafe24_webhook

    created = map_cafe24_webhook(
        _order_webhook(event_no=90023), trace_id="trace-created", store_id="store-c24"
    )
    paid = map_cafe24_webhook(
        _order_webhook(event_no=90025), trace_id="trace-paid", store_id="store-c24"
    )
    refund = map_cafe24_webhook(
        _order_webhook(
            event_no=90029,
            refund_no="refund-1",
            refunded_date="2026-08-27T11:00:00+09:00",
        ),
        trace_id="trace-refund",
        store_id="store-c24",
    )

    assert [event.event_type for event in paid.events] == ["order.paid"]
    created_paid = next(event for event in created.events if event.event_type == "order.paid")
    assert created_paid.event_id == paid.events[0].event_id
    assert [event.event_type for event in refund.events] == ["refund.succeeded"]
    assert refund.events[0].refund_id == "refund-1"


def test_native_cart_webhook_maps_product_variant_and_quantity_without_scripttag():
    from services.cafe24_event_adapter import map_cafe24_webhook

    batch = map_cafe24_webhook(
        {
            "event_no": 90084,
            "resource": {
                "mall_id": "sample_mall",
                "event_shop_no": "1",
                "member_id": "member-1",
                "shipping_type": "A",
                "product_no": 781,
                "variant_code": "P0000BEB000A",
                "quantity": 2,
                "product_bundle": "F",
            },
        },
        trace_id="trace-cart-add",
        store_id="store-c24",
    )

    assert len(batch.events) == 1
    event = batch.events[0]
    assert event.event_type == "cart.item_added"
    assert event.buyer_id == "member-1"
    assert event.metadata["native_product_no"] == "781"
    assert event.metadata["native_variant_code"] == "P0000BEB000A"
    assert event.metadata["native_quantity"] == 2
    assert event.metadata["native_products"][0]["quantity"] == 2


@pytest.mark.parametrize(
    ("event_no", "event_type"),
    [
        (90072, "order.cancelled"),
        (90073, "refund.created"),
        (90074, "return.created"),
    ],
)
def test_bulk_order_webhooks_expand_comma_separated_order_ids(event_no, event_type):
    from services.cafe24_event_adapter import map_cafe24_webhook

    batch = map_cafe24_webhook(
        {
            "event_no": event_no,
            "resource": {
                "mall_id": "sample_mall",
                "event_shop_no": "1",
                "order_id": "order-1,order-2",
            },
        },
        trace_id=f"trace-bulk-{event_no}",
        store_id="store-c24",
    )

    assert [event.event_type for event in batch.events] == [event_type, event_type]
    assert [event.order_id for event in batch.events] == ["order-1", "order-2"]
    assert len({event.event_id for event in batch.events}) == 2


def test_product_adapter_converts_catalog_and_variants():
    from adapters.cafe24_adapter import Cafe24ProductAdapter

    product = Cafe24ProductAdapter.convert_product(
        {
            "product_no": 128,
            "product_code": "P0000128",
            "product_name": "Cafe24 Serum",
            "description": "Hydrating serum",
            "price": "15000.00",
            "retail_price": "18000.00",
            "currency": "KRW",
            "display": "T",
            "selling": "T",
            "detail_image": "https://example.com/p.jpg",
            "brand_name": "Example Brand",
            "variants": [
                {
                    "variant_code": "P000000Q000A",
                    "custom_variant_code": "SERUM-30",
                    "additional_amount": "1000.00",
                    "quantity": 7,
                    "options": [{"name": "Size", "value": "30ml"}],
                }
            ],
        },
        merchant_id="merchant-1",
        mall_id="sample_mall",
    )

    assert product.platform == "cafe24"
    assert product.id == "128"
    assert product.vendor == "Example Brand"
    assert product.price == 15000
    assert product.inventory_quantity == 7
    assert product.variants[0].id == "P000000Q000A"
    assert product.variants[0].price == 16000
    assert product.variants[0].title == "30ml"
    assert product.orderable is True


def test_cafe24_is_registered_in_shared_product_adapter_factory():
    from adapters.product_adapters import PLATFORM_ADAPTERS
    from adapters.cafe24_adapter import Cafe24ProductAdapter

    assert PLATFORM_ADAPTERS["cafe24"] is Cafe24ProductAdapter


def test_oauth_state_is_signed_scoped_and_expires(monkeypatch):
    from services import cafe24_integration_service as service

    monkeypatch.setattr(service.time, "time", lambda: 1_000)
    state = service.create_cafe24_oauth_state(
        merchant_id="merchant-1",
        mall_id="Sample_Mall.cafe24api.com",
        secret="state-secret",
        ttl_seconds=60,
    )
    payload = service.verify_cafe24_oauth_state(state, secret="state-secret")
    assert payload["merchant_id"] == "merchant-1"
    assert payload["mall_id"] == "sample_mall"

    with pytest.raises(ValueError, match="signature"):
        service.verify_cafe24_oauth_state(state + "x", secret="state-secret")
    monkeypatch.setattr(service.time, "time", lambda: 1_061)
    with pytest.raises(ValueError, match="expired"):
        service.verify_cafe24_oauth_state(state, secret="state-secret")


@pytest.mark.asyncio
async def test_expired_access_token_rotates_and_persists_refresh_token(monkeypatch):
    from services import cafe24_integration_service as service

    writes = []

    class FakeDB:
        async def fetch_one(self, query, values=None):
            return {
                "api_key": json.dumps(
                    {
                        "mall_id": "sample_mall",
                        "access_token": "old-access",
                        "refresh_token": "old-refresh",
                        "webhook_api_key": "keep-this-webhook-key",
                    }
                )
            }

        async def execute(self, query, values=None):
            writes.append(dict(values or {}))

    async def fake_exchange(**kwargs):
        assert kwargs["refresh_token"] == "old-refresh"
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_at": "2099-01-01T00:00:00+00:00",
        }

    monkeypatch.setattr(service, "database", FakeDB())
    monkeypatch.setattr(service, "exchange_cafe24_token", fake_exchange)
    token = await service.resolve_cafe24_access_token(
        {
            "store_id": "store-c24",
            "mall_id": "sample_mall",
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_at": "2020-01-01T00:00:00+00:00",
        }
    )

    assert token == "new-access"
    persisted = json.loads(writes[0]["api_key"])
    assert persisted["access_token"] == "new-access"
    assert persisted["refresh_token"] == "new-refresh"
    assert persisted["webhook_api_key"] == "keep-this-webhook-key"


@pytest.mark.asyncio
async def test_reconnect_preserves_reconciliation_cursor(monkeypatch):
    from services import cafe24_integration_service as service

    writes = []

    class FakeDB:
        async def fetch_one(self, query, values=None):
            return {
                "store_id": "store-c24",
                "api_key": json.dumps(
                    {
                        "access_token": "old-access",
                        "reconciliation": {"webhooks_cursor": "88"},
                    }
                ),
            }

        async def execute(self, query, values=None):
            writes.append(dict(values or {}))

    monkeypatch.setattr(service, "database", FakeDB())
    store_id = await service.upsert_cafe24_store(
        merchant_id="merchant-1",
        mall_id="sample_mall",
        access_token="new-access",
        refresh_token="new-refresh",
        expires_at="2099-01-01T00:00:00+00:00",
        refresh_token_expires_at="2099-01-14T00:00:00+00:00",
        webhook_api_key="webhook-secret",
        api_version="2025-12-01",
    )

    persisted = json.loads(writes[0]["api_key"])
    assert store_id == "store-c24"
    assert persisted["access_token"] == "new-access"
    assert persisted["reconciliation"]["webhooks_cursor"] == "88"


@pytest.mark.asyncio
async def test_webhook_reception_activation_uses_cafe24_setting_api(monkeypatch):
    from services import cafe24_integration_service as service

    requests = []
    persisted = []

    class FakeResponse:
        status_code = 200

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def put(self, url, **kwargs):
            requests.append((url, kwargs))
            return FakeResponse()

    async def fake_resolve(credentials):
        return "live-access"

    async def fake_merge(**kwargs):
        persisted.append(kwargs)
        return kwargs["updates"]

    monkeypatch.setattr(service.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(service, "resolve_cafe24_access_token", fake_resolve)
    monkeypatch.setattr(service, "merge_cafe24_store_credentials", fake_merge)

    result = await service.enable_cafe24_webhook_reception(
        {
            "store_id": "store-c24",
            "mall_id": "sample_mall",
            "api_version": "2025-12-01",
        }
    )

    assert requests[0][0].endswith("/admin/webhooks/setting")
    assert requests[0][1]["json"] == {"request": {"reception_status": "T"}}
    assert result["reception_status"] == "T"
    assert result["event_subscription_configuration"] == "developer_center_required"
    assert persisted[0]["store_id"] == "store-c24"


@pytest.mark.asyncio
async def test_reconciliation_replays_both_log_streams_and_persists_cursors(monkeypatch):
    from services import cafe24_reconciliation_service as service

    fetched = []
    ingested = []
    persisted = []

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            fetched.append((url, kwargs["params"]))
            if "/webhooks/logs" in url:
                return FakeResponse(
                    {
                        "webhooklogs": [
                            {
                                "log_id": 11,
                                "trace_id": "trace-order-log",
                                "request_body": json.dumps(_order_webhook()),
                            }
                        ]
                    }
                )
            return FakeResponse(
                {
                    "databridgelogs": [
                        {
                            "log_id": 23,
                            "trace_id": "trace-view-log",
                            "request_body": _data_bridge_payload(),
                        }
                    ]
                }
            )

    async def fake_find(store_id):
        return {
            "store_id": store_id,
            "merchant_id": "merchant-1",
            "domain": "sample_mall.cafe24api.com",
            "credentials": {
                "mall_id": "sample_mall",
                "access_token": "access-1",
                "api_version": "2025-12-01",
            },
        }

    async def fake_resolve(credentials):
        return "access-1"

    async def fake_ingest(**kwargs):
        ingested.append([event.event_type for event in kwargs["batch"].events])
        return {
            "accepted": len(kwargs["batch"].events),
            "duplicates": 0,
            "events": [],
        }

    async def fake_merge(**kwargs):
        persisted.append(kwargs)
        return kwargs["updates"]

    monkeypatch.setattr(service.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(service, "find_cafe24_store_by_id", fake_find)
    monkeypatch.setattr(service, "resolve_cafe24_access_token", fake_resolve)
    monkeypatch.setattr(service, "ingest_merchant_event_batch", fake_ingest)
    monkeypatch.setattr(service, "merge_cafe24_store_credentials", fake_merge)

    result = await service.reconcile_cafe24_store(store_id="store-c24")

    assert result["accepted"] == 3
    assert ingested == [["order.created", "order.paid"], ["product.viewed"]]
    assert fetched[0][1]["requested_start_date"]
    assert "since_log_id" not in fetched[0][1]
    state = persisted[0]["updates"]["reconciliation"]
    assert state["webhooks_cursor"] == "11"
    assert state["databridge_cursor"] == "23"


@pytest.mark.asyncio
async def test_reconciliation_uses_saved_cursors_and_rejects_cross_mall_log(monkeypatch):
    from services import cafe24_reconciliation_service as service

    fetched_params = []
    ingested = []

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            fetched_params.append(dict(kwargs["params"]))
            key = "webhooklogs" if "/webhooks/logs" in url else "databridgelogs"
            return FakeResponse(
                {
                    key: [
                        {
                            "log_id": 31 if key == "webhooklogs" else 42,
                            "trace_id": "wrong-mall",
                            "request_body": _order_webhook(mall_id="another_mall"),
                        }
                    ]
                }
            )

    async def fake_find(store_id):
        return {
            "store_id": store_id,
            "merchant_id": "merchant-1",
            "domain": "sample_mall.cafe24api.com",
            "credentials": {
                "mall_id": "sample_mall",
                "access_token": "access-1",
                "reconciliation": {
                    "webhooks_cursor": "30",
                    "databridge_cursor": "40",
                },
            },
        }

    async def fake_ingest(**kwargs):
        ingested.append(kwargs)
        return {"accepted": 0, "duplicates": 0, "events": []}

    async def fake_merge(**kwargs):
        return kwargs["updates"]

    monkeypatch.setattr(service.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(service, "find_cafe24_store_by_id", fake_find)

    async def fake_resolve(credentials):
        return "access-1"

    monkeypatch.setattr(service, "resolve_cafe24_access_token", fake_resolve)
    monkeypatch.setattr(service, "ingest_merchant_event_batch", fake_ingest)
    monkeypatch.setattr(service, "merge_cafe24_store_credentials", fake_merge)

    result = await service.reconcile_cafe24_store(store_id="store-c24")

    assert fetched_params == [
        {"limit": 500, "since_log_id": "30"},
        {"limit": 500, "since_log_id": "40"},
    ]
    assert result["invalid"] == 2
    assert ingested == []


def test_universal_sync_prepares_cafe24_credentials_without_losing_store_id():
    from routes.universal_product_sync import prepare_platform_credentials

    credentials = prepare_platform_credentials(
        "cafe24",
        {
            "store_id": "store-c24",
            "domain": "sample_mall.cafe24api.com",
            "api_key": "access-token-only-view",
            "api_key_raw": json.dumps(
                {
                    "mall_id": "sample_mall",
                    "access_token": "access-1",
                    "refresh_token": "refresh-1",
                }
            ),
        },
    )

    assert credentials["mall_id"] == "sample_mall"
    assert credentials["access_token"] == "access-1"
    assert credentials["refresh_token"] == "refresh-1"
    assert credentials["store_id"] == "store-c24"


def test_cafe24_catalog_source_is_enabled_without_claiming_checkout_writeback():
    from services.commerce_source_registry import catalog_sync_blocker, get_commerce_source
    from services.merchant_commerce_readiness_service import _SUPPORTED_COMMERCE_PLATFORMS

    assert catalog_sync_blocker("cafe24") is None
    assert get_commerce_source("cafe24").capabilities.catalog_pull is True
    assert "cafe24" not in _SUPPORTED_COMMERCE_PLATFORMS


def _webhook_client():
    from routes.cafe24_webhooks import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def patched_webhook(monkeypatch):
    calls = []

    async def fake_store(mall_id):
        if mall_id != "sample_mall":
            return None
        return {
            "store_id": "store-c24",
            "merchant_id": "merchant-1",
            "credentials": {"webhook_api_key": "cafe24-secret-key"},
        }

    async def fake_ingest(**kwargs):
        calls.append(kwargs)
        return {"accepted": len(kwargs["batch"].events), "duplicates": 0, "events": []}

    monkeypatch.setattr("routes.cafe24_webhooks.find_cafe24_store", fake_store)
    monkeypatch.setattr("routes.cafe24_webhooks.ingest_merchant_event_batch", fake_ingest)
    return calls


def _post_webhook(payload, key="cafe24-secret-key", trace="trace-1"):
    return _webhook_client().post(
        "/webhooks/cafe24",
        json=payload,
        headers={"X-API-Key": key, "X-Trace-ID": trace},
    )


def test_verified_cafe24_webhook_reaches_canonical_ingest(patched_webhook):
    response = _post_webhook(_order_webhook())

    assert response.status_code == 200, response.text
    assert response.json()["accepted"] == 2
    assert len(patched_webhook) == 1
    assert patched_webhook[0]["merchant_id"] == "merchant-1"


def test_bad_cafe24_webhook_key_is_rejected_before_ingest(patched_webhook):
    response = _post_webhook(_order_webhook(), key="wrong-key")

    assert response.status_code == 401
    assert patched_webhook == []


def test_unrelated_verified_cafe24_event_is_acknowledged_and_ignored(patched_webhook):
    response = _post_webhook(
        {"event_no": 90001, "resource": {"mall_id": "sample_mall", "product_no": 1}}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert patched_webhook == []
