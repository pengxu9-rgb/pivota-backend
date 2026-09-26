from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest


def _fresh_updated_at() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.mark.asyncio
async def test_compute_merchant_commerce_readiness_state_marks_supported_platform_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.merchant_commerce_readiness_service as module

    async def fake_get_merchant_onboarding(_merchant_id: str):
        return {"merchant_id": "merch_1", "mcp_platform": "shopify", "mcp_connected_at": "2026-03-25T00:00:00Z"}

    async def fake_get_primary_store(_merchant_id: str):
        return {"platform": "shopify", "connected_at": "2026-03-25T00:00:00Z"}

    async def fake_fetch_listing_rows(_merchant_id: str):
        return [{"status": "indexed", "canonical_variant_id": "cv_1", "updated_at": _fresh_updated_at()}]

    async def fake_fetch_click_rows(_merchant_id: str):
        return [{"click_id": "clk_1", "impression_count": 3, "click_count": 1}]

    async def fake_fetch_edge_rows(_merchant_id: str):
        return [{"order_id": "ord_1", "click_id": "clk_1"}]

    async def fake_fetch_order_rows(_merchant_id: str, order_ids: list[str]):
        assert order_ids == ["ord_1"]
        return [{"order_id": "ord_1", "payment_status": "paid", "status": "pending"}]

    async def fake_fetch_active_psps(_merchant_id: str):
        return [
            {
                "provider": "stripe",
                "status": "active",
                "api_key": "sk_live_123",
                "account_id": "acct_123",
                "provider_config": {
                    "mode": "payment_intent",
                    "public_key": "pk_live_123",
                    "webhook_endpoint_id": "we_123",
                    "webhook_endpoint_secret": "sec_123",
                },
                "environment": "live",
                "validation_status": "valid",
                "validation_error": None,
            }
        ]

    monkeypatch.setattr(module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(module, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(module, "_fetch_listing_rows", fake_fetch_listing_rows)
    monkeypatch.setattr(module, "_fetch_click_rows", fake_fetch_click_rows)
    monkeypatch.setattr(module, "_fetch_edge_rows", fake_fetch_edge_rows)
    monkeypatch.setattr(module, "_fetch_order_rows", fake_fetch_order_rows)
    monkeypatch.setattr(module, "_fetch_active_psps", fake_fetch_active_psps)

    readiness = await module.compute_merchant_commerce_readiness_state("merch_1")

    assert readiness["foundation_status"] == "ready"
    assert readiness["discover_status"] == "ready"
    assert readiness["signals_status"] == "ready"
    assert readiness["execute_status"] == "ready"
    assert readiness["surfaced_exposure_supported"] is True
    assert readiness["metadata"]["paid_conversion"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["woocommerce", "bigcommerce"])
async def test_compute_merchant_commerce_readiness_state_marks_woocommerce_and_bigcommerce_ready(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    import services.merchant_commerce_readiness_service as module

    async def fake_get_merchant_onboarding(_merchant_id: str):
        return {"merchant_id": "merch_1", "mcp_platform": platform, "mcp_connected_at": "2026-03-25T00:00:00Z"}

    async def fake_get_primary_store(_merchant_id: str):
        return {"platform": platform, "connected_at": "2026-03-25T00:00:00Z"}

    async def fake_fetch_listing_rows(_merchant_id: str):
        return [{"status": "indexed", "canonical_variant_id": "cv_1", "updated_at": _fresh_updated_at()}]

    async def fake_fetch_click_rows(_merchant_id: str):
        return [{"click_id": "clk_1", "impression_count": 2, "click_count": 1}]

    async def fake_fetch_edge_rows(_merchant_id: str):
        return [{"order_id": "ord_1", "click_id": "clk_1"}]

    async def fake_fetch_order_rows(_merchant_id: str, order_ids: list[str]):
        assert order_ids == ["ord_1"]
        return [{"order_id": "ord_1", "payment_status": "paid", "status": "pending"}]

    async def fake_fetch_active_psps(_merchant_id: str):
        return [
            {
                "provider": "stripe",
                "status": "active",
                "api_key": "sk_live_123",
                "account_id": "acct_123",
                "provider_config": {
                    "mode": "payment_intent",
                    "public_key": "pk_live_123",
                    "webhook_endpoint_id": "we_123",
                    "webhook_endpoint_secret": "sec_123",
                },
                "environment": "live",
                "validation_status": "valid",
                "validation_error": None,
            }
        ]

    monkeypatch.setattr(module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(module, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(module, "_fetch_listing_rows", fake_fetch_listing_rows)
    monkeypatch.setattr(module, "_fetch_click_rows", fake_fetch_click_rows)
    monkeypatch.setattr(module, "_fetch_edge_rows", fake_fetch_edge_rows)
    monkeypatch.setattr(module, "_fetch_order_rows", fake_fetch_order_rows)
    monkeypatch.setattr(module, "_fetch_active_psps", fake_fetch_active_psps)

    readiness = await module.compute_merchant_commerce_readiness_state("merch_1")

    assert readiness["foundation_status"] == "ready"
    assert readiness["discover_status"] == "ready"
    assert readiness["signals_status"] == "ready"
    assert readiness["execute_status"] == "ready"
    assert readiness["surfaced_exposure_supported"] is True
    assert readiness["metadata"]["paid_conversion"] == 1


@pytest.mark.asyncio
async def test_compute_merchant_commerce_readiness_state_uses_catalog_fallback_for_discover(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.merchant_commerce_readiness_service as module

    async def fake_get_merchant_onboarding(_merchant_id: str):
        return {"merchant_id": "merch_1", "mcp_platform": "wix", "mcp_connected_at": "2026-03-25T00:00:00Z"}

    async def fake_get_primary_store(_merchant_id: str):
        return {"platform": "wix", "connected_at": "2026-03-25T00:00:00Z"}

    async def fake_fetch_listing_rows(_merchant_id: str):
        return [
            {
                "status": "indexed",
                "canonical_product_id": "prod::merch_1::wix::prod_1",
                "canonical_variant_id": "sku::prod::merch_1::wix::var_1",
                "surface": "default",
                "updated_at": _fresh_updated_at(),
            }
        ]

    async def fake_fetch_click_rows(_merchant_id: str):
        return []

    async def fake_fetch_edge_rows(_merchant_id: str):
        return []

    async def fake_fetch_order_rows(_merchant_id: str, order_ids: list[str]):
        assert order_ids == []
        return []

    async def fake_fetch_active_psps(_merchant_id: str):
        return [
            {
                "provider": "stripe",
                "status": "active",
                "api_key": "sk_live_123",
                "account_id": "acct_123",
                "provider_config": {
                    "mode": "payment_intent",
                    "public_key": "pk_live_123",
                    "webhook_endpoint_id": "we_123",
                    "webhook_endpoint_secret": "sec_123",
                },
                "environment": "live",
                "validation_status": "valid",
                "validation_error": None,
            }
        ]

    monkeypatch.setattr(module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(module, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(module, "_fetch_listing_rows", fake_fetch_listing_rows)
    monkeypatch.setattr(module, "_fetch_click_rows", fake_fetch_click_rows)
    monkeypatch.setattr(module, "_fetch_edge_rows", fake_fetch_edge_rows)
    monkeypatch.setattr(module, "_fetch_order_rows", fake_fetch_order_rows)
    monkeypatch.setattr(module, "_fetch_active_psps", fake_fetch_active_psps)

    readiness = await module.compute_merchant_commerce_readiness_state("merch_1")

    assert readiness["foundation_status"] == "ready"
    assert readiness["discover_status"] == "ready"
    assert readiness["signals_status"] == "blocked"
    assert readiness["signals_blockers"] == ["missing_surface_impressions"]
    assert readiness["execute_status"] == "ready"


@pytest.mark.asyncio
async def test_compute_merchant_commerce_readiness_state_blocks_execute_without_live_psp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.merchant_commerce_readiness_service as module

    async def fake_get_merchant_onboarding(_merchant_id: str):
        return {"merchant_id": "merch_1", "mcp_platform": "wix", "mcp_connected_at": "2026-03-25T00:00:00Z"}

    async def fake_get_primary_store(_merchant_id: str):
        return {"platform": "wix", "connected_at": "2026-03-25T00:00:00Z"}

    async def fake_fetch_listing_rows(_merchant_id: str):
        return [{"status": "indexed", "canonical_variant_id": "cv_1", "updated_at": _fresh_updated_at()}]

    async def fake_fetch_click_rows(_merchant_id: str):
        return [{"click_id": "clk_1", "impression_count": 1, "click_count": 0}]

    async def fake_fetch_edge_rows(_merchant_id: str):
        return []

    async def fake_fetch_order_rows(_merchant_id: str, order_ids: list[str]):
        assert order_ids == []
        return []

    async def fake_fetch_active_psps(_merchant_id: str):
        return [
            {
                "provider": "stripe",
                "status": "active",
                "api_key": "sk_test_123",
                "account_id": "acct_123",
                "provider_config": {"mode": "payment_intent"},
                "environment": "test",
                "validation_status": "invalid",
                "validation_error": "test credential",
            }
        ]

    monkeypatch.setattr(module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(module, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(module, "_fetch_listing_rows", fake_fetch_listing_rows)
    monkeypatch.setattr(module, "_fetch_click_rows", fake_fetch_click_rows)
    monkeypatch.setattr(module, "_fetch_edge_rows", fake_fetch_edge_rows)
    monkeypatch.setattr(module, "_fetch_order_rows", fake_fetch_order_rows)
    monkeypatch.setattr(module, "_fetch_active_psps", fake_fetch_active_psps)

    readiness = await module.compute_merchant_commerce_readiness_state("merch_1")

    assert readiness["foundation_status"] == "ready"
    assert readiness["discover_status"] == "ready"
    assert readiness["signals_status"] == "ready"
    assert readiness["execute_status"] == "blocked"
    assert readiness["execute_blockers"] == ["missing_live_psp"]
    assert readiness["active_psp"] is None


async def test_fetch_click_rows_supports_row_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.merchant_commerce_readiness_service as module

    class FakeRow:
        def __init__(self, payload):
            self._mapping = payload

    class FakeDB:
        async def fetch_all(self, query):
            return [FakeRow({"click_id": "clk_1", "impression_count": 1})]

    monkeypatch.setattr(module, "database", FakeDB())

    rows = await module._fetch_click_rows("merch_1")

    assert rows == [{"click_id": "clk_1", "impression_count": 1}]


def test_normalize_readiness_state_payload_parses_json_strings() -> None:
    import services.merchant_commerce_readiness_service as module

    payload = module._normalize_readiness_state_payload(
        {
            "foundation_blockers": '["missing_store_connection"]',
            "discover_blockers": "[]",
            "signals_blockers": '["missing_surface_impressions"]',
            "execute_blockers": "[]",
            "metadata": '{"clicked_exposure":2,"paid_conversion":1}',
        }
    )

    assert payload["foundation_blockers"] == ["missing_store_connection"]
    assert payload["discover_blockers"] == []
    assert payload["signals_blockers"] == ["missing_surface_impressions"]
    assert payload["execute_blockers"] == []
    assert payload["metadata"] == {"clicked_exposure": 2, "paid_conversion": 1}


def test_merchant_analytics_routes_expose_readiness_issues_and_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.merchant_analytics_routes as module

    app = FastAPI()
    app.include_router(module.router)

    async def fake_principal():
        return {"merchant_id": "merch_1", "role": "merchant"}

    async def fake_issues(**kwargs):
        assert kwargs["merchant_id"] == "merch_1"
        return {"issues": [{"code": "TRACE_BROKEN", "count": 2}]}

    async def fake_trace(interaction_id: str):
        assert interaction_id == "int_1"
        return {"interaction": {"interaction_id": "int_1", "merchant_id": "merch_1"}, "events": []}

    async def fake_readiness(merchant_id: str):
        assert merchant_id == "merch_1"
        return {"merchant_id": "merch_1", "execute_status": "ready"}

    warmups = []

    def fake_warmup(merchant_id: str, *, channel: str = "ucp"):
        warmups.append((merchant_id, channel))
        return True

    app.dependency_overrides[module._get_principal] = fake_principal
    monkeypatch.setattr(module, "build_merchant_commerce_funnel_issues", fake_issues)
    monkeypatch.setattr(module, "trace_interaction", fake_trace)
    monkeypatch.setattr(module, "upsert_merchant_commerce_readiness_state", fake_readiness)
    monkeypatch.setattr(module, "schedule_readiness_optimization_warmup", fake_warmup)

    client = TestClient(app)

    issues_response = client.get("/merchant/analytics/commerce-funnel/issues")
    trace_response = client.get("/merchant/analytics/commerce-interactions/int_1")
    readiness_response = client.get("/merchant/analytics/readiness-state")

    assert issues_response.status_code == 200
    assert issues_response.json()["issues"][0]["code"] == "TRACE_BROKEN"
    assert trace_response.status_code == 200
    assert trace_response.json()["interaction"]["interaction_id"] == "int_1"
    assert readiness_response.status_code == 200
    assert readiness_response.json()["execute_status"] == "ready"
    assert warmups == [("merch_1", "ucp")]


def test_agent_commerce_checkout_route_returns_public_checkout_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.agent_commerce as module

    app = FastAPI()
    app.include_router(module.router)

    class FakeContext:
        agent_id = "agent_1"

        def can_access_merchant(self, merchant_id: str) -> bool:
            return merchant_id == "merch_1"

    async def fake_context():
        return FakeContext()

    async def fake_agent_user():
        return None

    async def fake_readiness(_merchant_id: str):
        return {"execute_status": "ready", "discover_status": "ready", "signals_status": "ready", "primary_platform": "shopify"}

    async def fake_store(_merchant_id: str):
        return {"platform": "shopify"}

    async def fake_create_order(**kwargs):
        req = kwargs["order_request"]
        assert req.merchant_id == "merch_1"
        assert req.metadata["interaction_id"] == "int_checkout_1"
        return {
            "order_id": "ord_1",
            "merchant_id": "merch_1",
            "status": "pending",
            "payment_status": "awaiting_payment",
            "client_secret": "https://checkout.example.com/ord_1",
        }

    recorded = []

    async def fake_record_event(**kwargs):
        recorded.append(kwargs)
        return {"interaction_id": "int_checkout_1", "event_id": f"evt_{len(recorded)}"}

    app.dependency_overrides[module.get_agent_context] = fake_context
    app.dependency_overrides[module.get_agent_user_context] = fake_agent_user
    monkeypatch.setattr(module, "upsert_merchant_commerce_readiness_state", fake_readiness)
    monkeypatch.setattr(module, "get_primary_store", fake_store)
    monkeypatch.setattr(module, "agent_create_order", fake_create_order)
    monkeypatch.setattr(module, "record_commerce_event", fake_record_event)

    client = TestClient(app)
    response = client.post(
        "/agent/v2/commerce/checkouts",
        json={
            "merchant_id": "merch_1",
            "interaction_id": "int_checkout_1",
            "customer_email": "buyer@example.com",
            "shipping_address": {
                "name": "Buyer One",
                "address_line1": "1 Market St",
                "city": "San Francisco",
                "postal_code": "94105",
                "country": "US",
            },
            "items": [
                {
                    "product_id": "prod_1",
                    "variant_id": "var_1",
                    "quantity": 1,
                    "title": "Cleanser",
                    "unit_price": 24.0,
                }
            ],
        },
    )

    assert response.status_code == 200
    # Every agent-commerce ledger write is stamped as the first-party verified
    # issuer: get_agent_context authenticated the agent's own credential.
    assert recorded, "the checkout must write to the ledger"
    assert {call["write_path"] for call in recorded} == {"agent_commerce_api"}
    assert {call["authority"] for call in recorded} == {"pivota"}
    assert {call["agent_identity_confidence"] for call in recorded} == {"verified"}
    assert all(call["metadata"]["agent_identity_confidence"] == "verified" for call in recorded)
    assert all(call["metadata"]["agent_id"] == call["actor_id"] for call in recorded)
    payload = response.json()
    assert payload["checkout_id"] == "ord_1"
    assert payload["payment_url"] == "https://checkout.example.com/ord_1"
    assert len(recorded) == 2


def test_agent_commerce_payment_intent_route_reads_existing_order_action(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.agent_commerce as module

    app = FastAPI()
    app.include_router(module.router)

    class FakeContext:
        agent_id = "agent_1"

        def can_access_merchant(self, merchant_id: str) -> bool:
            return merchant_id == "merch_1"

    async def fake_context():
        return FakeContext()

    async def fake_get_order(order_id: str):
        assert order_id == "ord_1"
        return {"order_id": "ord_1", "merchant_id": "merch_1", "client_secret": "pi_secret_123", "payment_status": "awaiting_payment"}

    async def fake_find_interaction(_order_id: str, *, merchant_id: str):
        assert merchant_id == "merch_1"
        return {"interaction_id": "int_1", "surface": "agent_v2_commerce"}

    async def fake_store(_merchant_id: str):
        return {"platform": "shopify"}

    async def fake_record_event(**_kwargs):
        return {"interaction_id": "int_1", "event_id": "evt_1"}

    app.dependency_overrides[module.get_agent_context] = fake_context
    monkeypatch.setattr(module, "get_order", fake_get_order)
    monkeypatch.setattr(module, "find_interaction_by_order_id", fake_find_interaction)
    monkeypatch.setattr(module, "get_primary_store", fake_store)
    monkeypatch.setattr(module, "record_commerce_event", fake_record_event)

    client = TestClient(app)
    response = client.post("/agent/v2/commerce/checkouts/ord_1/payment-intent", json={})

    assert response.status_code == 200
    assert response.json()["client_secret"] == "pi_secret_123"


def test_agent_commerce_returns_route_reports_pending_for_wix(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.agent_commerce as module

    app = FastAPI()
    app.include_router(module.router)

    class FakeContext:
        agent_id = "agent_1"

        def can_access_merchant(self, merchant_id: str) -> bool:
            return merchant_id == "merch_1"

    async def fake_context():
        return FakeContext()

    async def fake_get_order(_order_id: str):
        return {"order_id": "ord_1", "merchant_id": "merch_1"}

    async def fake_store(_merchant_id: str):
        return {"platform": "wix"}

    async def fake_find_interaction(_order_id: str, *, merchant_id: str):
        assert merchant_id == "merch_1"
        return {"interaction_id": "int_1", "surface": "agent_v2_commerce"}

    async def fake_record_event(**_kwargs):
        return {"interaction_id": "int_1", "event_id": "evt_1"}

    app.dependency_overrides[module.get_agent_context] = fake_context
    monkeypatch.setattr(module, "get_order", fake_get_order)
    monkeypatch.setattr(module, "get_primary_store", fake_store)
    monkeypatch.setattr(module, "find_interaction_by_order_id", fake_find_interaction)
    monkeypatch.setattr(module, "record_commerce_event", fake_record_event)

    client = TestClient(app)
    response = client.post("/agent/v2/commerce/checkouts/ord_1/returns", json={})

    assert response.status_code == 200
    assert response.json()["status"] == "pending_external_platform"
