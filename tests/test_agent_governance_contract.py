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

os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres")
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
