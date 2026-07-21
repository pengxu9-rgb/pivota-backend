from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import AsyncMock

import httpx
import pytest


from main import app


class _TestAgentContext:
    agent_id = "agent_contract"
    agent_name = "Contract Agent"
    allowed_merchants = ["m_contract"]
    session_id = "session_contract"

    def can_access_merchant(self, merchant_id: str) -> bool:
        return True


async def _override_get_agent_context() -> _TestAgentContext:
    return _TestAgentContext()


@pytest.mark.asyncio
async def test_contract_agent_products_search_response_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_api as agent_api
    from models.standard_product import ProductStatus, StandardProduct
    from routes.agent_auth import get_agent_context

    async def noop_log_agent_request(*args: Any, **kwargs: Any) -> None:
        return None

    async def fake_verify_merchant_active(_merchant_id: str) -> Dict[str, Any]:
        return {"merchant_id": "m_contract", "business_name": "Contract Merchant"}

    async def fake_get_products_hybrid(**kwargs: Any):
        product = StandardProduct(
            id="prod_contract_1",
            product_id="prod_contract_1",
            platform="shopify",
            merchant_id="m_contract",
            title="Contract Product",
            description="contract-search",
            price=19.9,
            currency="USD",
            inventory_quantity=5,
            orderable=True,
            status=ProductStatus.ACTIVE,
        )
        return [product], "cache", None

    app.dependency_overrides[get_agent_context] = _override_get_agent_context
    monkeypatch.setattr(agent_api, "log_agent_request", noop_log_agent_request)
    monkeypatch.setattr(agent_api, "verify_merchant_active", fake_verify_merchant_active)
    monkeypatch.setattr(agent_api, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_api, "hydrate_quality_and_enrichment", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_api, "passes_agent_gating", lambda *_a, **_k: True)
    monkeypatch.setattr(agent_api, "compute_agent_ranking_score", lambda *_a, **_k: 1.0)
    monkeypatch.setattr(agent_api, "serialize_features_for_log", lambda *_a, **_k: {})
    if hasattr(agent_api, "log_ranking_batch"):
        monkeypatch.setattr(agent_api, "log_ranking_batch", AsyncMock(return_value=None))
    if hasattr(agent_api, "log_product_events"):
        monkeypatch.setattr(agent_api, "log_product_events", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_api, "_load_external_seed_products_for_search", AsyncMock(return_value=[]))

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/agent/v1/products/search",
                params={
                    "merchant_id": "m_contract",
                    "query": "contract",
                    "limit": 10,
                    "offset": 0,
                    "in_stock_only": "false",
                },
            )
    finally:
        app.dependency_overrides.pop(get_agent_context, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body.get("status") == "success"
    assert isinstance(body.get("products"), list)
    assert isinstance(body.get("pagination"), dict)
    assert isinstance(body.get("metadata"), dict)

    first = body["products"][0]
    assert isinstance(first.get("id"), str)
    assert isinstance(first.get("title"), str)
    assert isinstance(first.get("merchant_id"), str)
    route_health = (body.get("metadata") or {}).get("route_health") or {}
    for key in (
        "orchestrator_path",
        "decision_node",
        "domain_filter_dropped_external",
        "external_fill_gate_reason",
        "semantic_retry_applied",
        "semantic_retry_query",
        "semantic_retry_hits",
        "external_seed_brand_strict_rows",
        "external_seed_brand_relevant_rows",
        "external_seed_broad_fallback_used",
        "external_seed_broad_scope_rows",
        "pivot_shadow_scheduled",
        "pivot_shadow_mode",
        "pivot_rollout_mode",
        "pivot_rollout_guard_passed",
    ):
        assert key in route_health


@pytest.mark.asyncio
async def test_contract_agent_shop_invoke_find_products_multi_response_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as gateway

    async def fake_handler(payload: Any, metadata: Dict[str, Any], background_tasks: Any) -> Dict[str, Any]:
        return {
            "products": [
                {
                    "id": "p_shop_contract_1",
                    "product_id": "p_shop_contract_1",
                    "title": "Shop Contract Product",
                }
            ],
            "total": 1,
            "page": 1,
            "page_size": 1,
            "reply": "ok",
            "metadata": {"query_source": "contract_test"},
        }

    monkeypatch.setattr(gateway, "_handle_find_products_multi", fake_handler)
    monkeypatch.setattr(gateway, "INVOKE_MULTI_BYPASS_QUEUE_SHOPPING", True)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/agent/shop/v1/invoke",
            json={
                "operation": "find_products_multi",
                "payload": {
                    "search": {
                        "query": "contract",
                        "page": 1,
                        "limit": 10,
                        "in_stock_only": False,
                    }
                },
                "metadata": {"source": "shopping_agent"},
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body.get("products"), list)
    assert isinstance(body.get("total"), int)
    assert isinstance(body.get("page"), int)
    assert isinstance(body.get("page_size"), int)
    assert isinstance(body.get("metadata"), dict)
    route_health = (body.get("metadata") or {}).get("route_health") or {}
    for key in (
        "orchestrator_path",
        "decision_node",
        "domain_filter_dropped_external",
        "external_fill_gate_reason",
        "semantic_retry_applied",
        "semantic_retry_query",
        "semantic_retry_hits",
        "external_seed_brand_strict_rows",
        "external_seed_brand_relevant_rows",
        "external_seed_broad_fallback_used",
        "external_seed_broad_scope_rows",
        "pivot_shadow_scheduled",
        "pivot_shadow_mode",
        "pivot_rollout_mode",
        "pivot_rollout_guard_passed",
    ):
        assert key in route_health


@pytest.mark.asyncio
async def test_contract_agent_payments_response_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mvp.events as mvp_events
    import mvp.governance as mvp_governance
    import routes.agent_payment_sdk as payment_module
    from db.database import database as database_obj
    from routes.agent_auth import get_agent_context

    app.dependency_overrides[get_agent_context] = _override_get_agent_context

    async def noop_log_agent_request(*args: Any, **kwargs: Any) -> None:
        return None

    async def fake_get_order(order_id: str) -> Dict[str, Any]:
        return {
            "order_id": order_id,
            "merchant_id": "m_contract",
            "payment_status": "unpaid",
            "total": 10.0,
            "currency": "USD",
            "shipping_address": {
                "country": "US",
                "postal_code": "94107",
                "city": "SF",
                "state": "CA",
            },
            # Quote-first contract: /agent/v1/payments 409s unless the order
            # was created from a live-validated quote that hasn't expired.
            "metadata": {
                "pricing_quote": {
                    "quote_id": "quote_contract_1",
                    "live_validation": {"status": "validated"},
                }
            },
        }

    async def fake_update_payment_info(**kwargs: Any) -> None:
        return None

    async def fake_get_merchant_onboarding(merchant_id: str) -> Dict[str, Any]:
        return {"merchant_id": merchant_id, "psp_connected": True}

    async def fake_get_primary_store(merchant_id: str) -> Dict[str, Any]:
        # Only the shopify platform resolves to the pivota_direct_quote_first
        # commerce path that allows public agent PSP creation.
        return {"merchant_id": merchant_id, "platform": "shopify"}

    async def fake_select_psp(self, *, agent_id: str, merchant_id: str, amount: float, currency: str):
        return "stripe", {
            "route_id": "route_contract",
            "psp_priority": [{"psp": "stripe", "priority": 1}],
        }

    async def fake_fetch_one(query: Any, values: Dict[str, Any] | None = None):
        return None

    async def fake_execute(query: Any, values: Dict[str, Any] | None = None):
        return None

    async def fake_create_payment_with_failover(*args: Any, **kwargs: Any):
        payment_intent = SimpleNamespace(
            id="pi_contract_1",
            status="succeeded",
            client_secret="cs_contract_1",
        )
        return True, payment_intent, None, "stripe"

    class _Decision:
        decision = "allow"
        reason_codes = []
        required_scopes = []
        risk_tier = "low"

    monkeypatch.setattr(payment_module, "log_agent_request", noop_log_agent_request)
    monkeypatch.setattr(payment_module, "get_order", fake_get_order)
    monkeypatch.setattr(payment_module, "update_payment_info", fake_update_payment_info)
    monkeypatch.setattr(payment_module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(payment_module, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(payment_module.PaymentRoutingService, "select_psp", fake_select_psp)
    monkeypatch.setattr(payment_module, "create_payment_with_failover", fake_create_payment_with_failover)
    monkeypatch.setattr(database_obj, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(database_obj, "execute", fake_execute)
    monkeypatch.setattr(mvp_events, "emit_best_effort", lambda **_: None)
    monkeypatch.setattr(mvp_governance.governance, "evaluate", lambda *_a, **_k: _Decision())
    monkeypatch.setattr(mvp_governance.governance, "record_audit_event", lambda **_: None)

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/agent/v1/payments",
                json={
                    "order_id": "ord_contract_1",
                    "payment_method": {"type": "card", "token": "tok_contract"},
                    "idempotency_key": "idem_contract_1",
                },
            )
    finally:
        app.dependency_overrides.pop(get_agent_context, None)

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body.get("status"), str)
    assert isinstance(body.get("payment_id"), str)
    assert isinstance(body.get("payment_intent_id"), str)
    assert isinstance(body.get("amount"), float)
    assert isinstance(body.get("currency"), str)
    assert isinstance(body.get("psp_used"), str)
    assert body.get("payment_action", {}).get("type") == "stripe_client_secret"
    # 兼容性：保留已有顶层字段语义
    assert isinstance(body.get("created_at"), str)
    datetime.fromisoformat(body["created_at"])


@pytest.mark.asyncio
async def test_contract_agent_payments_times_out_with_structured_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mvp.events as mvp_events
    import mvp.governance as mvp_governance
    import routes.agent_payment_sdk as payment_module
    from db.database import database as database_obj
    from routes.agent_auth import get_agent_context

    app.dependency_overrides[get_agent_context] = _override_get_agent_context

    async def noop_log_agent_request(*args: Any, **kwargs: Any) -> None:
        return None

    async def fake_get_order(order_id: str) -> Dict[str, Any]:
        return {
            "order_id": order_id,
            "merchant_id": "m_contract",
            "payment_status": "unpaid",
            "total": 10.0,
            "currency": "USD",
            "shipping_address": {
                "country": "US",
                "postal_code": "94107",
                "city": "SF",
                "state": "CA",
            },
            # Quote-first contract: /agent/v1/payments 409s unless the order
            # was created from a live-validated quote that hasn't expired.
            "metadata": {
                "pricing_quote": {
                    "quote_id": "quote_contract_1",
                    "live_validation": {"status": "validated"},
                }
            },
        }

    async def fake_get_merchant_onboarding(merchant_id: str) -> Dict[str, Any]:
        return {"merchant_id": merchant_id, "psp_connected": True}

    async def fake_get_primary_store(merchant_id: str) -> Dict[str, Any]:
        # Only the shopify platform resolves to the pivota_direct_quote_first
        # commerce path that allows public agent PSP creation.
        return {"merchant_id": merchant_id, "platform": "shopify"}

    async def fake_select_psp(self, *, agent_id: str, merchant_id: str, amount: float, currency: str):
        return "stripe", {
            "route_id": "route_contract",
            "psp_priority": [{"psp": "stripe", "priority": 1}],
        }

    async def fake_fetch_one(query: Any, values: Dict[str, Any] | None = None):
        return None

    async def fake_execute(query: Any, values: Dict[str, Any] | None = None):
        return None

    async def fake_create_payment_with_failover(*args: Any, **kwargs: Any):
        await asyncio.sleep(0.05)
        return True, None, None, "stripe"

    class _Decision:
        decision = "allow"
        reason_codes = []
        required_scopes = []
        risk_tier = "low"

    monkeypatch.setattr(payment_module, "log_agent_request", noop_log_agent_request)
    monkeypatch.setattr(payment_module, "get_order", fake_get_order)
    monkeypatch.setattr(payment_module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(payment_module, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(payment_module.PaymentRoutingService, "select_psp", fake_select_psp)
    monkeypatch.setattr(payment_module, "create_payment_with_failover", fake_create_payment_with_failover)
    monkeypatch.setattr(payment_module, "AGENT_PAYMENT_INITIATION_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(database_obj, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(database_obj, "execute", fake_execute)
    monkeypatch.setattr(mvp_events, "emit_best_effort", lambda **_: None)
    monkeypatch.setattr(mvp_governance.governance, "evaluate", lambda *_a, **_k: _Decision())
    monkeypatch.setattr(mvp_governance.governance, "record_audit_event", lambda **_: None)

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/agent/v1/payments",
                json={
                    "order_id": "ord_contract_timeout",
                    "payment_method": {"type": "card", "token": "tok_contract"},
                    "idempotency_key": "idem_contract_timeout",
                },
            )
    finally:
        app.dependency_overrides.pop(get_agent_context, None)

    assert resp.status_code == 504
    assert resp.json()["detail"]["error"] == "UPSTREAM_TIMEOUT"
