from __future__ import annotations

import asyncio
from types import SimpleNamespace

from typing import Any, Dict, Optional

import httpx
import pytest


def test_resolve_order_merchant_id_falls_back_to_unique_item_merchant() -> None:
    import routes.agent_payment_sdk as payment_module

    order = {
        "order_id": "ORD_TEST_1",
        "payment_status": "processing",
        "items": [
            {
                "product_id": "prod_1",
                "merchant_id": "merch_test_123",
            }
        ],
        "metadata": {},
    }

    assert payment_module._resolve_order_merchant_id(order) == "merch_test_123"


def test_build_existing_order_payment_surface_accepts_awaiting_payment() -> None:
    import routes.agent_payment_sdk as payment_module

    order = {
        "order_id": "ORD_TEST_2",
        "payment_status": "awaiting_payment",
        "psp_used": "stripe",
        "payment_intent_id": "pi_existing_awaiting_123",
        "client_secret": "pi_existing_awaiting_123_secret_456",
        "items": [
            {
                "product_id": "prod_1",
                "merchant_id": "merch_test_123",
            }
        ],
        "metadata": {},
    }

    surface = asyncio.run(payment_module._build_existing_order_payment_surface(order))

    # No PSP runtime row is required for the resume decision itself; the client
    # secret is enough to reuse the original payment surface.
    assert surface is not None
    assert surface["merchant_id"] == "merch_test_123"
    assert surface["psp_used"] == "stripe"
    assert surface["client_secret"] == "pi_existing_awaiting_123_secret_456"
    assert surface["payment_action"]["type"] == "stripe_client_secret"


def test_build_existing_order_payment_surface_rejects_awaiting_payment_when_redirect_ready_required() -> None:
    import routes.agent_payment_sdk as payment_module

    order = {
        "order_id": "ORD_TEST_2B",
        "payment_status": "awaiting_payment",
        "psp_used": "stripe",
        "payment_intent_id": "pi_existing_awaiting_789",
        "client_secret": "pi_existing_awaiting_789_secret_000",
        "items": [
            {
                "product_id": "prod_1",
                "merchant_id": "merch_test_123",
            }
        ],
        "metadata": {},
    }

    surface = asyncio.run(
        payment_module._build_existing_order_payment_surface(
            order,
            require_redirect_ready=True,
        )
    )

    assert surface is None


@pytest.mark.asyncio
async def test_agent_payments_reuses_existing_order_payment_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mvp.events as mvp_events
    import mvp.governance as mvp_governance
    import routes.agent_payment_sdk as payment_module
    from db.database import database as database_obj
    from main import app
    from routes.agent_auth import get_agent_context

    class _Context:
        agent_id = "agent_test"
        session_id = "sess_test"

        def can_access_merchant(self, merchant_id: Optional[str]) -> bool:
            return merchant_id == "merch_test_123"

    async def _override_context() -> _Context:
        return _Context()

    app.dependency_overrides[get_agent_context] = _override_context

    async def fake_get_order(order_id: str) -> Dict[str, Any]:
        return {
            "order_id": order_id,
            "payment_status": "processing",
            "total": 25.22,
            "currency": "EUR",
            "shipping_address": {
                "country": "US",
                "postal_code": "94105",
                "city": "San Francisco",
                "state": "CA",
            },
            "psp_used": "stripe",
            "payment_intent_id": "pi_existing_123",
            "client_secret": "pi_existing_123_secret_456",
            "items": [
                {
                    "product_id": "prod_1",
                    "merchant_id": "merch_test_123",
                }
            ],
            "metadata": {},
        }

    async def fake_fetch_active_runtime_merchant_psp(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {
            "api_key": "sk_live_test_123",
            "account_id": "acct_live_test_123",
            "provider_config": {"public_key": "pk_live_test_123"},
            "environment": "live",
            "secret_key": None,
        }

    async def fake_get_merchant_onboarding(merchant_id: str) -> Dict[str, Any]:
        return {"merchant_id": merchant_id}

    async def fake_fetch_one(query: Any, values: Dict[str, Any] | None = None):
        return None

    calls = {"count": 0}

    async def fake_create_payment_with_failover(*args: Any, **kwargs: Any):
        calls["count"] += 1
        raise AssertionError("submit_payment should reuse the existing order payment surface")

    class _Decision:
        decision = "allow"
        reason_codes = []
        required_scopes = []
        risk_tier = "low"

    monkeypatch.setattr(payment_module, "get_order", fake_get_order)
    async def fake_get_merchant_onboarding(merchant_id: str) -> Dict[str, Any]:
        return {"merchant_id": merchant_id}

    monkeypatch.setattr(
        payment_module,
        "get_merchant_onboarding",
        fake_get_merchant_onboarding,
    )
    monkeypatch.setattr(
        payment_module,
        "fetch_active_runtime_merchant_psp",
        fake_fetch_active_runtime_merchant_psp,
    )
    monkeypatch.setattr(payment_module, "create_payment_with_failover", fake_create_payment_with_failover)
    monkeypatch.setattr(database_obj, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(mvp_events, "emit_best_effort", lambda **_: None)
    monkeypatch.setattr(mvp_governance.governance, "evaluate", lambda *_a, **_k: _Decision())
    monkeypatch.setattr(mvp_governance.governance, "record_audit_event", lambda **_: None)

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/agent/v1/payments",
                json={
                    "order_id": "ORD_TEST_1",
                    "payment_method": {"type": "dynamic"},
                },
            )
    finally:
        app.dependency_overrides.pop(get_agent_context, None)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "requires_action"
    assert body["psp_used"] == "stripe"
    assert body["payment_action"]["type"] == "stripe_client_secret"
    assert body["payment_action"]["public_key"] == "pk_live_test_123"
    assert body["payment_action"]["stripe_account"] == "acct_live_test_123"
    assert calls["count"] == 0


@pytest.mark.asyncio
async def test_agent_payments_refreshes_awaiting_stripe_surface_when_return_url_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mvp.events as mvp_events
    import mvp.governance as mvp_governance
    import routes.agent_payment_sdk as payment_module
    from fastapi import BackgroundTasks
    from db.database import database as database_obj

    class _Context:
        agent_id = "agent_test"
        session_id = "sess_test"

        def can_access_merchant(self, merchant_id: Optional[str]) -> bool:
            return merchant_id == "merch_test_123"

    async def fake_get_order(order_id: str) -> Dict[str, Any]:
        return {
            "order_id": order_id,
            "payment_status": "awaiting_payment",
            "total": 25.22,
            "currency": "EUR",
            "shipping_address": {
                "country": "US",
                "postal_code": "94105",
                "city": "San Francisco",
                "state": "CA",
            },
            "psp_used": "stripe",
            "payment_intent_id": "pi_existing_awaiting_123",
            "client_secret": "pi_existing_awaiting_123_secret_456",
            "items": [
                {
                    "product_id": "prod_1",
                    "merchant_id": "merch_test_123",
                }
            ],
            "metadata": {},
        }

    async def fake_fetch_active_runtime_merchant_psp(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {
            "api_key": "sk_live_test_123",
            "account_id": "acct_live_test_123",
            "provider_config": {"public_key": "pk_live_test_123"},
            "environment": "live",
            "secret_key": None,
        }

    async def fake_get_merchant_onboarding(merchant_id: str) -> Dict[str, Any]:
        return {"merchant_id": merchant_id}

    async def fake_fetch_one(query: Any, values: Dict[str, Any] | None = None):
        return None

    async def fake_execute(*args: Any, **kwargs: Any):
        return 1

    calls = {"count": 0}

    async def fake_create_payment_with_failover(*args: Any, **kwargs: Any):
        calls["count"] += 1
        assert kwargs["metadata"].get("return_url")
        return True, SimpleNamespace(
            id="pi_new_123",
            client_secret="pi_new_123_secret_456",
            status="requires_action",
            raw_response={"public_key": "pk_live_test_123"},
        ), None, "stripe"

    class _Decision:
        decision = "allow"
        reason_codes = []
        required_scopes = []
        risk_tier = "low"

    monkeypatch.setattr(payment_module, "get_order", fake_get_order)
    monkeypatch.setattr(payment_module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(
        payment_module,
        "fetch_active_runtime_merchant_psp",
        fake_fetch_active_runtime_merchant_psp,
    )
    monkeypatch.setattr(payment_module, "create_payment_with_failover", fake_create_payment_with_failover)
    monkeypatch.setattr(database_obj, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(database_obj, "execute", fake_execute)
    monkeypatch.setattr(mvp_events, "emit_best_effort", lambda **_: None)
    monkeypatch.setattr(mvp_governance.governance, "evaluate", lambda *_a, **_k: _Decision())
    monkeypatch.setattr(mvp_governance.governance, "record_audit_event", lambda **_: None)

    response = await payment_module.create_payment(
        payment_module.PaymentRequest(
            order_id="ORD_TEST_3",
            payment_method=payment_module.PaymentMethod(type="dynamic"),
            return_url="https://agent.pivota.cc/order/success?orderId=ORD_TEST_3&finalizing=1",
        ),
        BackgroundTasks(),
        context=_Context(),
    )

    assert response.payment_intent_id == "pi_new_123"
    assert response.payment_action["type"] == "stripe_client_secret"
    assert calls["count"] == 1
