from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

import httpx
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres")
os.chdir(REPO_ROOT)

from main import app
from routes.agent_docs import _is_documented_agent_path


class _TestAgentContext:
    agent_id = "agent_v2_contract"
    agent_name = "Agent V2 Contract"
    allowed_merchants = ["m_contract"]
    session_id = "session_v2_contract"

    def can_access_merchant(self, merchant_id: str) -> bool:
        return merchant_id in self.allowed_merchants


async def _override_get_agent_context() -> _TestAgentContext:
    return _TestAgentContext()


@pytest.mark.asyncio
async def test_agent_public_route_uniqueness() -> None:
    scoped_counts: Dict[tuple[str, str], int] = {}
    tracked_paths = {
        "/agent/v1/products/search",
        "/agent/v1/beauty/products/search",
        "/agent/v2/products/search",
        "/agent/v2/quotes/preview",
        "/agent/v2/quotes/{quote_id}",
        "/agent/v2/orders",
        "/agent/v2/orders/{order_id}",
        "/agent/v2/payments/checkout-sessions",
        "/agent/v2/merchants/capabilities",
        "/agent/v2/orders/{order_id}/tracking",
        "/agent/v2/orders/{order_id}/refunds",
    }

    for route in app.routes:
        path = getattr(route, "path", "") or ""
        methods = getattr(route, "methods", set()) or set()
        if path not in tracked_paths:
            continue
        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            key = (method, path)
            scoped_counts[key] = scoped_counts.get(key, 0) + 1

    expected_unique = {
        ("GET", "/agent/v1/products/search"),
        ("GET", "/agent/v1/beauty/products/search"),
        ("POST", "/agent/v2/products/search"),
        ("POST", "/agent/v2/quotes/preview"),
        ("GET", "/agent/v2/quotes/{quote_id}"),
        ("POST", "/agent/v2/orders"),
        ("GET", "/agent/v2/orders/{order_id}"),
        ("POST", "/agent/v2/payments/checkout-sessions"),
        ("GET", "/agent/v2/merchants/capabilities"),
        ("GET", "/agent/v2/orders/{order_id}/tracking"),
        ("POST", "/agent/v2/orders/{order_id}/refunds"),
    }
    for key in expected_unique:
        assert scoped_counts.get(key) == 1, f"duplicate or missing route ownership for {key}"


@pytest.mark.asyncio
async def test_agent_docs_openapi_matches_runtime_agent_paths() -> None:
    runtime_paths = {
        path
        for path in app.openapi()["paths"].keys()
        if _is_documented_agent_path(path)
    }

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        openapi_resp = await client.get("/agent/docs/openapi.json")
        endpoints_resp = await client.get("/agent/docs/endpoints")
        sdk_resp = await client.get("/agent/v1/openapi.json")

    assert openapi_resp.status_code == 200
    assert endpoints_resp.status_code == 200
    assert sdk_resp.status_code == 200

    openapi_paths = set(openapi_resp.json()["paths"].keys())
    assert openapi_paths == runtime_paths
    assert "/agent/v2/products/search" in openapi_paths
    assert "/agent/v2/orders" in openapi_paths
    assert "/merchant/dashboard/stats" not in openapi_paths

    sdk_paths = set(sdk_resp.json()["paths"].keys())
    assert sdk_paths == openapi_paths

    endpoint_paths = {item["path"] for item in endpoints_resp.json()["endpoints"]}
    assert "/agent/v2/products/search" in endpoint_paths
    assert "/agent/v2/payments/checkout-sessions" in endpoint_paths


@pytest.mark.asyncio
async def test_agent_v2_products_search_response_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_v2 as agent_v2
    from routes.agent_auth import get_agent_context

    async def fake_v1_search(**kwargs: Any) -> Dict[str, Any]:
        return {
            "status": "success",
            "products": [
                {
                    "id": "prod_1",
                    "product_id": "prod_1",
                    "merchant_id": "m_contract",
                    "merchant_name": "Contract Merchant",
                    "title": "Contract Serum",
                    "category": "beauty",
                    "brand": "Pivota",
                    "price": "42.00",
                    "currency": "USD",
                    "platform": "shopify",
                    "source": "products_cache",
                    "cached_at": "2026-03-19T00:00:00+00:00",
                    "variant_id": "var_1",
                    "score": 0.92,
                }
            ],
            "pagination": {"total": 1, "limit": 10, "offset": 0, "has_more": False},
            "metadata": {"reason_code": "ok"},
        }

    app.dependency_overrides[get_agent_context] = _override_get_agent_context
    monkeypatch.setattr(agent_v2, "agent_v1_search_products", fake_v1_search)

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/agent/v2/products/search",
                json={"query": "serum", "merchant_id": "m_contract", "limit": 10},
            )
    finally:
        app.dependency_overrides.pop(get_agent_context, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["pagination"]["total"] == 1

    first = body["products"][0]
    assert first["product_id"] == "prod_1"
    assert first["canonical_title"] == "Contract Serum"
    assert first["offers"][0]["offer_id"].startswith("offer::m_contract::")
    assert first["offers"][0]["capability_flags"] == [
        "catalog_search",
        "quote_preview",
        "hosted_checkout",
        "order_create",
    ]
    assert first["provenance"]["merchant_id"] == "m_contract"


@pytest.mark.asyncio
async def test_agent_v2_merchant_capabilities_exposes_access_scope_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_v2 as agent_v2
    from routes.agent_auth import get_agent_context

    async def fake_fetch_all(_query: str, _values: Dict[str, Any]) -> list[Dict[str, Any]]:
        checked_at = datetime.now(timezone.utc)
        return [
            {
                "merchant_id": "m_contract",
                "business_name": "Contract Merchant",
                "status": "active",
                "mcp_connected": True,
                "mcp_platform": "shopify",
                "psp_connected": True,
                "psp_type": "stripe",
                "shopify_api_version": "2025-10",
                "scopes_json": {
                    "access_scopes": ["read_products", "read_discounts", "write_discounts", "read_customers"],
                    "missing_required_scopes": [],
                    "missing_optional_scopes": ["read_returns"],
                },
                "has_shopify_payments": True,
                "has_returns_api": False,
                "last_checked_at": checked_at,
            }
        ]

    async def fake_get_merchant_pcs_tier(*, merchant_id: str) -> str:
        assert merchant_id == "m_contract"
        return "tier_1"

    app.dependency_overrides[get_agent_context] = _override_get_agent_context
    monkeypatch.setattr(agent_v2.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_v2, "get_merchant_pcs_tier", fake_get_merchant_pcs_tier)

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/agent/v2/merchants/capabilities", params={"merchant_id": "m_contract"})
    finally:
        app.dependency_overrides.pop(get_agent_context, None)

    assert resp.status_code == 200
    body = resp.json()
    merchant = body["merchants"][0]
    assert merchant["merchant_id"] == "m_contract"
    assert merchant["policy_flags"]["access_scopes"] == [
        "read_customers",
        "read_discounts",
        "read_products",
        "write_discounts",
    ]
    assert merchant["policy_flags"]["has_read_discounts"] is True
    assert merchant["policy_flags"]["has_write_discounts"] is True
    assert merchant["policy_flags"]["has_read_customers"] is True
    assert merchant["policy_flags"]["missing_optional_scopes"] == ["read_returns"]
    assert merchant["commerce_capabilities"]["supports_live_quote"] is True
    assert merchant["commerce_capabilities"]["supports_platform_order_writeback"] is True
    assert merchant["psp_capabilities"]["provider"] == "stripe"
    assert merchant["psp_capabilities"]["supports_auto_refund"] is True
    assert merchant["psp_capabilities"]["order_flow_auth_first_enabled"] is False
    assert merchant["supported_flows"]["payment_refunds"] is True
    assert merchant["supported_flows"]["pivota_direct_checkout"] is True
    assert merchant["supported_flows"]["external_platform_checkout"] is True


@pytest.mark.asyncio
async def test_agent_v2_merchant_capabilities_distinguishes_external_checkout_from_direct_purchase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_v2 as agent_v2
    from routes.agent_auth import get_agent_context

    async def fake_fetch_all(_query: str, _values: Dict[str, Any]) -> list[Dict[str, Any]]:
        return [
            {
                "merchant_id": "m_contract",
                "business_name": "Woo Merchant",
                "status": "active",
                "mcp_connected": True,
                "mcp_platform": "woocommerce",
                "psp_connected": False,
                "psp_type": None,
                "shopify_api_version": None,
                "scopes_json": {"access_scopes": [], "missing_required_scopes": [], "missing_optional_scopes": []},
                "has_shopify_payments": False,
                "has_returns_api": False,
                "last_checked_at": datetime.now(timezone.utc),
            }
        ]

    async def fake_get_merchant_pcs_tier(*, merchant_id: str) -> str:
        assert merchant_id == "m_contract"
        return "tier_0"

    app.dependency_overrides[get_agent_context] = _override_get_agent_context
    monkeypatch.setattr(agent_v2.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_v2, "get_merchant_pcs_tier", fake_get_merchant_pcs_tier)

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/agent/v2/merchants/capabilities", params={"merchant_id": "m_contract"})
    finally:
        app.dependency_overrides.pop(get_agent_context, None)

    assert resp.status_code == 200
    merchant = resp.json()["merchants"][0]
    assert merchant["connector"] == "woocommerce"
    assert merchant["supported_flows"]["hosted_checkout"] is True
    assert merchant["supported_flows"]["external_platform_checkout"] is True
    assert merchant["supported_flows"]["pivota_direct_checkout"] is False
    assert merchant["supported_flows"]["quote_refresh"] is False
    assert merchant["commerce_capabilities"]["supports_live_quote"] is False
    assert merchant["commerce_capabilities"]["supports_platform_checkout"] is True
    assert merchant["commerce_capabilities"]["purchase_status"] == "requires_external_platform_checkout_validation"


@pytest.mark.asyncio
async def test_agent_v2_order_and_checkout_session_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_v2 as agent_v2
    from routes.agent_auth import get_agent_context
    import services.agent_governance as governance_module
    from services.quote_service import QuoteSnapshot

    async def fake_load_active_quote_or_raise(self: Any, *, quote_id: str) -> QuoteSnapshot:
        now = datetime.now(timezone.utc)
        return QuoteSnapshot(
            quote_id=quote_id,
            merchant_id="m_contract",
            agent_id="agent_v2_contract",
            expires_at=now + timedelta(minutes=10),
            status="active",
            engine="shopify_storefront_cart",
            engine_ref="cart_contract",
            request_fingerprint="fp_contract",
            request_json={
                "items": [
                    {"product_id": "prod_1", "variant_id": "var_1", "quantity": 2}
                ],
                "discount_codes": ["SAVE10"],
                "selected_delivery_option": {"handle": "standard"},
            },
            snapshot_json={"currency": "USD"},
            quote_hash_sha256="quote_hash_contract",
            debug_id="debug_contract",
        )

    captured_order_request: Dict[str, Any] = {}

    async def fake_create_order(**kwargs: Any) -> Dict[str, Any]:
        captured_order_request["metadata"] = kwargs["order_request"].metadata
        return {
            "status": "success",
            "order_id": "ORD_V2_CONTRACT",
            "payment": {
                "psp": "stripe",
                "payment_intent_id": "pi_contract",
                "client_secret": "cs_contract",
                "payment_action": {"type": "stripe_client_secret", "client_secret": "cs_contract"},
                "instructions": "Use client_secret for Stripe payment confirmation",
            },
        }

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        if order_id != "ORD_V2_CONTRACT":
            return None
        now = datetime.now(timezone.utc)
        return {
            "order_id": order_id,
            "merchant_id": "m_contract",
            "agent_id": "agent_v2_contract",
            "customer_email": "buyer@example.com",
            "customer_name": "Buyer Example",
            "items": [
                {
                    "product_id": "prod_1",
                    "variant_id": "var_1",
                    "quantity": 2,
                    "unit_price": "21.00",
                }
            ],
            "shipping_address": {
                "name": "Buyer Example",
                "address_line1": "123 Market St",
                "city": "San Francisco",
                "state": "CA",
                "postal_code": "94105",
                "country": "US",
            },
            "subtotal": "42.00",
            "shipping_fee": "0.00",
            "tax": "3.20",
            "total": "45.20",
            "currency": "USD",
            "status": "pending",
            "payment_status": "awaiting_payment",
            "payment_intent_id": "pi_contract",
            "client_secret": "cs_contract",
            "psp_used": "stripe",
            "metadata": {"pricing_quote": {"quote_id": "q_v2_contract"}},
            "created_at": now,
            "updated_at": now,
            "tracking_number": None,
            "carrier": None,
            "fulfillment_status": None,
        }

    async def fake_create_checkout_intent_route(**kwargs: Any) -> Dict[str, Any]:
        req = kwargs["req"]
        return {
            "intent_id": "ci_contract",
            "checkout_session_id": "ci_contract",
            "checkout_token": "tok_contract",
            "checkout_url": "https://checkout.pivota.test/order?checkout_token=tok_contract",
            "expires_at": 1_900_000_000,
            "order_id": req.order_id,
        }

    async def noop_validate_request(agent_id: str, *, fail_closed: bool = False) -> None:
        return None

    app.dependency_overrides[get_agent_context] = _override_get_agent_context
    monkeypatch.setattr(agent_v2.QuoteService, "load_active_quote_or_raise", fake_load_active_quote_or_raise)
    monkeypatch.setattr(agent_v2, "agent_v1_create_order", fake_create_order)
    monkeypatch.setattr(agent_v2, "get_order", fake_get_order)
    monkeypatch.setattr(agent_v2, "create_checkout_intent_route", fake_create_checkout_intent_route)
    monkeypatch.setattr(governance_module.agent_governance, "validate_request", noop_validate_request)

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            create_order_resp = await client.post(
                "/agent/v2/orders",
                json={
                    "quote_id": "q_v2_contract",
                    "buyer_context": {
                        "customer_email": "buyer@example.com",
                        "customer_name": "Buyer Example",
                        "shipping_address": {
                            "name": "Buyer Example",
                            "address_line1": "123 Market St",
                            "city": "San Francisco",
                            "state": "CA",
                            "postal_code": "94105",
                            "country": "US",
                        },
                        "buyer_ref": "buyer_ref_contract",
                    },
                    "request_context": {
                        "tenant_id": "tenant_contract",
                        "request_id": "req_contract",
                        "channel": "agent",
                        "currency": "USD",
                    },
                    "metadata": {
                        "agent_v2": {
                            "checkout_provider": "pivota_hosted_checkout",
                            "hosted_checkout": True,
                        },
                    },
                },
            )
            checkout_resp = await client.post(
                "/agent/v2/payments/checkout-sessions",
                json={
                    "order_id": "ORD_V2_CONTRACT",
                    "return_url": "https://app.pivota.test/return",
                    "request_context": {
                        "tenant_id": "tenant_contract",
                        "channel": "agent",
                        "locale": "en-US",
                    },
                },
            )
    finally:
        app.dependency_overrides.pop(get_agent_context, None)

    assert create_order_resp.status_code == 200
    order_body = create_order_resp.json()
    assert order_body["status"] == "success"
    assert order_body["order"]["order_id"] == "ORD_V2_CONTRACT"
    assert order_body["order"]["state"] == "awaiting_checkout"
    assert order_body["order"]["quote_id"] == "q_v2_contract"
    assert order_body["payment"]["payment_action"]["type"] == "stripe_client_secret"
    assert order_body["events"][0]["type"] == "order.created"
    assert captured_order_request["metadata"]["agent_v2"]["checkout_provider"] == "pivota_hosted_checkout"
    assert captured_order_request["metadata"]["agent_v2"]["hosted_checkout"] is True
    assert captured_order_request["metadata"]["agent_v2"]["contract_version"] == "merchant-network-middleware-v1"

    assert checkout_resp.status_code == 200
    checkout_body = checkout_resp.json()
    assert checkout_body["status"] == "success"
    assert checkout_body["checkout_session"]["checkout_session_id"] == "ci_contract"
    assert checkout_body["checkout_session"]["order_id"] == "ORD_V2_CONTRACT"
    assert checkout_body["checkout_session"]["state"] == "created"
    assert checkout_body["events"][0]["type"] == "checkout.session.created"


@pytest.mark.asyncio
async def test_agent_v2_create_order_returns_conflict_for_expired_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_v2 as agent_v2
    from routes.agent_auth import get_agent_context
    from services.quote_service import QuoteError

    async def fake_load_active_quote_or_raise(self: Any, *, quote_id: str):
        raise QuoteError(
            "QUOTE_EXPIRED",
            "Quote expired before order creation",
            details={"quote_id": quote_id},
        )

    app.dependency_overrides[get_agent_context] = _override_get_agent_context
    monkeypatch.setattr(agent_v2.QuoteService, "load_active_quote_or_raise", fake_load_active_quote_or_raise)

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/agent/v2/orders",
                json={
                    "quote_id": "q_expired_contract",
                    "buyer_context": {},
                },
            )
    finally:
        app.dependency_overrides.pop(get_agent_context, None)

    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"]["error"] == "QUOTE_EXPIRED"
    assert body["detail"]["message"] == "Quote expired before order creation"
    assert body["detail"]["details"]["quote_id"] == "q_expired_contract"


@pytest.mark.asyncio
async def test_agent_v2_checkout_session_rejects_non_checkoutable_order_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_v2 as agent_v2
    from routes.agent_auth import get_agent_context
    import services.agent_governance as governance_module

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        return {
            "order_id": order_id,
            "merchant_id": "m_contract",
            "agent_id": "agent_v2_contract",
            "customer_email": "buyer@example.com",
            "items": [],
            "shipping_address": {
                "name": "Buyer Example",
                "address_line1": "123 Market St",
                "city": "San Francisco",
                "state": "CA",
                "postal_code": "94105",
                "country": "US",
            },
            "subtotal": "42.00",
            "shipping_fee": "0.00",
            "tax": "3.20",
            "total": "45.20",
            "currency": "USD",
            "status": "processing",
            "payment_status": "paid",
            "created_at": now,
            "updated_at": now,
        }

    async def noop_validate_request(agent_id: str, *, fail_closed: bool = False) -> None:
        return None

    app.dependency_overrides[get_agent_context] = _override_get_agent_context
    monkeypatch.setattr(agent_v2, "get_order", fake_get_order)
    monkeypatch.setattr(governance_module.agent_governance, "validate_request", noop_validate_request)

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/agent/v2/payments/checkout-sessions",
                json={"order_id": "ORD_ALREADY_PAID"},
            )
    finally:
        app.dependency_overrides.pop(get_agent_context, None)

    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"]["error"] == "CHECKOUT_SESSION_NOT_ALLOWED"
    assert body["detail"]["order_state"] == "confirmed"
    assert "awaiting checkout" in body["detail"]["message"]


@pytest.mark.asyncio
async def test_agent_v2_order_and_tracking_include_pricing_breakdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_v2 as agent_v2
    from routes.agent_auth import get_agent_context

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        return {
            "order_id": order_id,
            "merchant_id": "m_contract",
            "agent_id": "agent_v2_contract",
            "customer_email": "buyer@example.com",
            "items": [
                {
                    "product_id": "prod_1",
                    "variant_id": "var_1",
                    "quantity": 1,
                    "unit_price": "1.69",
                    "subtotal": "1.69",
                }
            ],
            "shipping_address": {
                "name": "Buyer Example",
                "address_line1": "123 Market St",
                "city": "San Francisco",
                "state": "CA",
                "postal_code": "94105",
                "country": "US",
            },
            "subtotal": "1.69",
            "discount_total": "0.16",
            "shipping_fee": "8.00",
            "tax": "0.00",
            "total": "9.53",
            "currency": "USD",
            "status": "processing",
            "payment_status": "paid",
            "created_at": now,
            "updated_at": now,
            "tracking_number": "TRACK123",
            "carrier": "ups",
            "fulfillment_status": "shipped",
        }

    async def fake_track_order(**_: Any) -> Dict[str, Any]:
        return {
            "tracking": {
                "status": "shipped",
                "tracking_number": "TRACK123",
                "carrier": "ups",
            }
        }

    app.dependency_overrides[get_agent_context] = _override_get_agent_context
    monkeypatch.setattr(agent_v2, "get_order", fake_get_order)
    monkeypatch.setattr(agent_v2, "agent_v1_track_order", fake_track_order)

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            order_resp = await client.get("/agent/v2/orders/ORD_AMOUNT_CONTRACT")
            tracking_resp = await client.get("/agent/v2/orders/ORD_AMOUNT_CONTRACT/tracking")
    finally:
        app.dependency_overrides.pop(get_agent_context, None)

    assert order_resp.status_code == 200
    order_body = order_resp.json()
    assert order_body["order"]["amounts"] == {
        "subtotal": "1.69",
        "discount_total": "0.16",
        "shipping_fee": "8.00",
        "tax": "0",
        "total": "9.53",
        "currency": "USD",
    }

    assert tracking_resp.status_code == 200
    tracking_body = tracking_resp.json()
    assert tracking_body["tracking"]["pricing"] == {
        "subtotal": "1.69",
        "discount_total": "0.16",
        "shipping_fee": "8.00",
        "tax": "0",
        "total": "9.53",
        "currency": "USD",
    }
    assert tracking_body["tracking"]["total"] == "9.53"
