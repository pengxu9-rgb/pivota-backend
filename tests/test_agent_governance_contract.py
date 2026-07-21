from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import httpx
import pytest
from fastapi import HTTPException


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.chdir(REPO_ROOT)

from main import app


class _TestAgentContext:
    agent_id = "agent_governance_contract"
    agent_name = "Agent Governance Contract"
    allowed_merchants = ["m_governance"]
    session_id = "session_governance_contract"

    def can_access_merchant(self, merchant_id: str) -> bool:
        return merchant_id in self.allowed_merchants


async def _override_get_agent_context() -> _TestAgentContext:
    return _TestAgentContext()


def _pending_order(order_id: str) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "order_id": order_id,
        "merchant_id": "m_governance",
        "agent_id": "agent_governance_contract",
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
        "status": "pending",
        "payment_status": "awaiting_payment",
        "created_at": now,
        "updated_at": now,
    }


@pytest.mark.asyncio
async def test_agent_governance_validate_request_compat_supports_legacy_signature() -> None:
    from services.agent_governance import validate_request_compat

    calls: list[str] = []

    class _LegacyGovernance:
        async def validate_request(self, agent_id: str) -> None:
            calls.append(agent_id)

    await validate_request_compat(_LegacyGovernance(), "agent_governance_contract", fail_closed=True)

    assert calls == ["agent_governance_contract"]


@pytest.mark.asyncio
async def test_agent_v2_checkout_session_denies_when_governance_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_v2 as agent_v2
    import services.agent_governance as governance_module
    from routes.agent_auth import get_agent_context

    calls: list[tuple[str, bool]] = []

    async def fake_validate_request(agent_id: str, *, fail_closed: bool = False) -> None:
        calls.append((agent_id, fail_closed))
        raise HTTPException(
            status_code=503,
            detail={
                "error": "GOVERNANCE_UNAVAILABLE",
                "message": "Agent governance unavailable for mutating request.",
            },
        )

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        return _pending_order(order_id)

    app.dependency_overrides[get_agent_context] = _override_get_agent_context
    monkeypatch.setattr(agent_v2, "get_order", fake_get_order)
    monkeypatch.setattr(governance_module.agent_governance, "validate_request", fake_validate_request)

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/agent/v2/payments/checkout-sessions",
                json={"order_id": "ORD_GOVERNANCE_BLOCKED"},
            )
    finally:
        app.dependency_overrides.pop(get_agent_context, None)

    assert resp.status_code == 503
    body = resp.json()
    assert body["detail"]["error"] == "GOVERNANCE_UNAVAILABLE"
    assert calls == [("agent_governance_contract", True)]


@pytest.mark.asyncio
async def test_agent_v1_confirm_payment_denies_when_governance_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as order_routes_module
    import services.agent_governance as governance_module
    from routes.agent_auth import get_agent_context

    calls: list[tuple[str, bool]] = []

    async def fake_validate_request(agent_id: str, *, fail_closed: bool = False) -> None:
        calls.append((agent_id, fail_closed))
        raise HTTPException(
            status_code=503,
            detail={
                "error": "GOVERNANCE_UNAVAILABLE",
                "message": "Agent governance unavailable for mutating request.",
            },
        )

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        return _pending_order(order_id)

    app.dependency_overrides[get_agent_context] = _override_get_agent_context
    monkeypatch.setattr(order_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(governance_module.agent_governance, "validate_request", fake_validate_request)

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/agent/v1/orders/ORD_CONFIRM_BLOCKED/confirm-payment")
    finally:
        app.dependency_overrides.pop(get_agent_context, None)

    assert resp.status_code == 503
    body = resp.json()
    assert body["detail"]["error"] == "GOVERNANCE_UNAVAILABLE"
    assert calls == [("agent_governance_contract", True)]


@pytest.mark.asyncio
async def test_agent_v1_confirm_payment_refuses_when_psp_not_succeeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as order_routes_module
    import services.agent_governance as governance_module
    import services.merchant_store_service as merchant_store_service
    from routes.agent_auth import get_agent_context

    order = {
        **_pending_order("ORD_CONFIRM_UNPAID"),
        "agent_id": None,
        "payment_intent_id": "pi_requires_payment_method",
    }
    marked_paid = {"called": False}
    shopify_called = {"called": False}
    events: list[Dict[str, Any]] = []

    async def fake_validate_request(agent_id: str, *, fail_closed: bool = False) -> None:
        return None

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        return order

    async def fake_get_primary_store(merchant_id: str) -> None:
        return None

    async def fake_verify_order_payment_succeeded(order_row: Dict[str, Any]):
        return False, "requires_payment_method", None

    async def fail_mark_order_paid(order_id: str) -> bool:
        marked_paid["called"] = True
        raise AssertionError("mark_order_paid must not be called before PSP success")

    async def fail_create_shopify_order(order_id: str) -> bool:
        shopify_called["called"] = True
        raise AssertionError("create_shopify_order must not run before PSP success")

    async def fake_log_order_event(**kwargs: Any) -> None:
        events.append(kwargs)

    app.dependency_overrides[get_agent_context] = _override_get_agent_context
    monkeypatch.setattr(governance_module.agent_governance, "validate_request", fake_validate_request)
    monkeypatch.setattr(order_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(merchant_store_service, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(order_routes_module, "verify_order_payment_succeeded", fake_verify_order_payment_succeeded)
    monkeypatch.setattr(order_routes_module, "mark_order_paid", fail_mark_order_paid)
    monkeypatch.setattr(order_routes_module, "create_shopify_order", fail_create_shopify_order)
    monkeypatch.setattr(order_routes_module, "log_order_event", fake_log_order_event)

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/agent/v1/orders/ORD_CONFIRM_UNPAID/confirm-payment")
    finally:
        app.dependency_overrides.pop(get_agent_context, None)

    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "PAYMENT_NOT_SUCCEEDED"
    assert body["detail"]["error"] == "PAYMENT_NOT_SUCCEEDED"
    assert body["detail"]["psp_status"] == "requires_payment_method"
    assert marked_paid["called"] is False
    assert shopify_called["called"] is False
    assert [event["event_type"] for event in events] == ["payment_confirm_rejected"]


@pytest.mark.asyncio
async def test_agent_v1_confirm_payment_marks_paid_only_after_psp_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_api as agent_api_module
    import routes.order_routes as order_routes_module
    import services.agent_governance as governance_module
    import services.agent_webhook_service as agent_webhook_service
    import services.merchant_store_service as merchant_store_service
    import services.pcs_evidence_pack_service as evidence_pack_service
    from routes.agent_auth import get_agent_context

    order = {
        **_pending_order("ORD_CONFIRM_SUCCEEDED"),
        "agent_id": None,
        "payment_intent_id": "pi_succeeded",
    }
    marked_paid: list[str] = []
    events: list[str] = []

    async def fake_validate_request(agent_id: str, *, fail_closed: bool = False) -> None:
        return None

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        return order

    async def fake_get_primary_store(merchant_id: str) -> None:
        return None

    async def fake_verify_order_payment_succeeded(order_row: Dict[str, Any]):
        return True, "succeeded", None

    async def fake_mark_order_paid(order_id: str) -> bool:
        marked_paid.append(order_id)
        return True

    async def fake_log_order_event(**kwargs: Any) -> None:
        events.append(kwargs["event_type"])

    async def noop_create_order_snapshot_evidence_pack(*_: Any, **__: Any) -> None:
        return None

    async def noop_emit_agent_webhook_event(*_: Any, **__: Any) -> None:
        return None

    async def noop_log_agent_request(*_: Any, **__: Any) -> None:
        return None

    app.dependency_overrides[get_agent_context] = _override_get_agent_context
    monkeypatch.setattr(governance_module.agent_governance, "validate_request", fake_validate_request)
    monkeypatch.setattr(order_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(merchant_store_service, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(order_routes_module, "verify_order_payment_succeeded", fake_verify_order_payment_succeeded)
    monkeypatch.setattr(order_routes_module, "mark_order_paid", fake_mark_order_paid)
    monkeypatch.setattr(order_routes_module, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(
        evidence_pack_service,
        "create_order_snapshot_evidence_pack",
        noop_create_order_snapshot_evidence_pack,
    )
    monkeypatch.setattr(agent_webhook_service, "emit_agent_webhook_event", noop_emit_agent_webhook_event)
    monkeypatch.setattr(agent_api_module, "log_agent_request", noop_log_agent_request)

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/agent/v1/orders/ORD_CONFIRM_SUCCEEDED/confirm-payment")
    finally:
        app.dependency_overrides.pop(get_agent_context, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["shopify_sync"] == "not_configured"
    assert marked_paid == ["ORD_CONFIRM_SUCCEEDED"]
    assert "payment_succeeded" in events


@pytest.mark.asyncio
async def test_agent_v2_products_search_remains_read_only_when_governance_is_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_v2 as agent_v2
    import services.agent_governance as governance_module
    from routes.agent_auth import get_agent_context

    async def fake_v1_search(**kwargs: Any) -> Dict[str, Any]:
        return {
            "status": "success",
            "products": [
                {
                    "id": "prod_read_only",
                    "product_id": "prod_read_only",
                    "merchant_id": "m_governance",
                    "merchant_name": "Governance Merchant",
                    "title": "Read Only Serum",
                    "category": "beauty",
                    "brand": "Pivota",
                    "price": "18.00",
                    "currency": "USD",
                    "platform": "shopify",
                    "source": "products_cache",
                    "cached_at": "2026-03-19T00:00:00+00:00",
                    "variant_id": "var_read_only",
                    "score": 0.87,
                }
            ],
            "pagination": {"total": 1, "limit": 10, "offset": 0, "has_more": False},
            "metadata": {"reason_code": "ok"},
        }

    async def fail_if_called(agent_id: str, *, fail_closed: bool = False) -> None:
        raise AssertionError("read-only search should not invoke mutating governance validation")

    app.dependency_overrides[get_agent_context] = _override_get_agent_context
    monkeypatch.setattr(agent_v2, "agent_v1_search_products", fake_v1_search)
    monkeypatch.setattr(governance_module.agent_governance, "validate_request", fail_if_called)

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/agent/v2/products/search",
                json={"query": "serum", "merchant_id": "m_governance", "limit": 10},
            )
    finally:
        app.dependency_overrides.pop(get_agent_context, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["products"][0]["product_id"] == "prod_read_only"
