from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import httpx
import pytest
from fastapi import BackgroundTasks


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.chdir(REPO_ROOT)

from main import app


class _TestAgentContext:
    agent_id = "agent_confirm_payment_contract"
    agent_name = "Agent Confirm Payment Contract"
    allowed_merchants = ["m_confirm"]
    session_id = "session_confirm_contract"

    def can_access_merchant(self, merchant_id: str) -> bool:
        return merchant_id in self.allowed_merchants


async def _override_get_agent_context() -> _TestAgentContext:
    return _TestAgentContext()


def _order(order_id: str, **overrides: Any) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    base: Dict[str, Any] = {
        "order_id": order_id,
        "merchant_id": "m_confirm",
        "agent_id": None,
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
        "payment_intent_id": "pi_confirm_contract",
        "client_secret": None,
        "redirect_url": None,
        "psp_used": None,
        "created_at": now,
        "updated_at": now,
    }
    base.update(overrides)
    return base


def _recording_enqueue(sink):
    """Stand-in for db.merchant_order_sync_jobs.enqueue_merchant_order_create."""

    async def _enqueue(*, order_id, merchant_id, require_shopify_primary=False):
        sink.append({
            "order_id": order_id,
            "merchant_id": merchant_id,
            "require_shopify_primary": require_shopify_primary,
        })
        return "job-1"

    return _enqueue


@pytest.mark.asyncio
async def test_agent_confirm_payment_client_owned_psp_waits_for_webhook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_api as agent_api
    import routes.order_routes as order_routes_module
    import services.merchant_store_service as merchant_store_module
    from routes.agent_auth import get_agent_context

    mark_paid_calls: list[str] = []
    shopify_calls: list[str] = []
    order_events: list[str] = []

    async def fake_validate_request(*args: Any, **kwargs: Any) -> None:
        return None

    async def fake_log_agent_request(**kwargs: Any) -> None:
        return None

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        return _order(
            order_id,
            psp_used="adyen",
            client_secret="adyen-session-data",
            payment_status="awaiting_payment",
        )

    async def fake_mark_order_paid(order_id: str) -> None:
        mark_paid_calls.append(order_id)

    async def fake_log_order_event(*, event_type: str, **kwargs: Any) -> None:
        order_events.append(event_type)

    async def fake_verify_order_payment_succeeded(order: Dict[str, Any]) -> tuple[bool, str | None, str | None]:
        return True, "succeeded", None

    async def fake_create_shopify_order(order_id: str) -> None:
        shopify_calls.append(order_id)

    async def fake_get_primary_store(merchant_id: str) -> Dict[str, Any]:
        return {"platform": "shopify", "api_key": "shp_key"}

    app.dependency_overrides[get_agent_context] = _override_get_agent_context
    monkeypatch.setattr(agent_api, "validate_request_compat", fake_validate_request)
    monkeypatch.setattr(agent_api, "log_agent_request", fake_log_agent_request)
    monkeypatch.setattr(order_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(order_routes_module, "mark_order_paid", fake_mark_order_paid)
    monkeypatch.setattr(order_routes_module, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(order_routes_module, "verify_order_payment_succeeded", fake_verify_order_payment_succeeded)
    monkeypatch.setattr(order_routes_module, "create_shopify_order", fake_create_shopify_order)
    monkeypatch.setattr(merchant_store_module, "get_primary_store", fake_get_primary_store)

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/agent/v1/orders/ORD_CLIENT_PENDING/confirm-payment")
    finally:
        app.dependency_overrides.pop(get_agent_context, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["payment_status"] == "pending"
    assert body["payment_status_raw"] == "awaiting_payment"
    assert body["confirmation_owner"] == "client"
    assert body["requires_client_confirmation"] is True
    assert body["payment_action"]["type"] == "adyen_session"
    assert body["payment_action"]["submit_owner"] == "component"
    assert body["payment_action"]["supported_in_shopping_ui"] is True
    assert body["shopify_sync"] == "waiting_for_psp_confirmation"
    assert mark_paid_calls == []
    assert shopify_calls == []
    assert order_events == []


@pytest.mark.asyncio
async def test_agent_confirm_payment_terminal_failure_stays_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_api as agent_api
    import routes.order_routes as order_routes_module
    import services.merchant_store_service as merchant_store_module
    from routes.agent_auth import get_agent_context

    mark_paid_calls: list[str] = []

    async def fake_validate_request(*args: Any, **kwargs: Any) -> None:
        return None

    async def fake_log_agent_request(**kwargs: Any) -> None:
        return None

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        return _order(
            order_id,
            psp_used="adyen",
            client_secret="adyen-session-data",
            payment_status="payment_failed",
            status="payment_failed",
        )

    async def fake_mark_order_paid(order_id: str) -> None:
        mark_paid_calls.append(order_id)

    async def fake_get_primary_store(merchant_id: str) -> Dict[str, Any]:
        return {"platform": "shopify", "api_key": "shp_key"}

    app.dependency_overrides[get_agent_context] = _override_get_agent_context
    monkeypatch.setattr(agent_api, "validate_request_compat", fake_validate_request)
    monkeypatch.setattr(agent_api, "log_agent_request", fake_log_agent_request)
    monkeypatch.setattr(order_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(order_routes_module, "mark_order_paid", fake_mark_order_paid)
    monkeypatch.setattr(merchant_store_module, "get_primary_store", fake_get_primary_store)

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/agent/v1/orders/ORD_CLIENT_FAILED/confirm-payment")
    finally:
        app.dependency_overrides.pop(get_agent_context, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "payment_failed"
    assert body["payment_status"] == "payment_failed"
    assert body["confirmation_owner"] == "backend"
    assert body["requires_client_confirmation"] is False
    assert body["payment_action"] is None
    assert body["shopify_sync"] == "not_started"
    assert mark_paid_calls == []


@pytest.mark.asyncio
async def test_agent_confirm_payment_backend_owned_still_marks_paid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_api as agent_api
    import routes.order_routes as order_routes_module
    import services.merchant_store_service as merchant_store_module
    import services.pcs_evidence_pack_service as pcs_evidence_pack_service

    mark_paid_calls: list[str] = []
    order_events: list[str] = []
    shopify_calls: list[str] = []
    enqueued: list[Dict[str, Any]] = []
    queued_background_tasks: list[tuple[str, tuple[Any, ...], Dict[str, Any]]] = []

    async def fake_validate_request(*args: Any, **kwargs: Any) -> None:
        return None

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        return _order(order_id, payment_status="pending", client_secret=None, psp_used=None)

    async def fake_mark_order_paid(order_id: str) -> None:
        mark_paid_calls.append(order_id)

    async def fake_log_order_event(*, event_type: str, **kwargs: Any) -> None:
        order_events.append(event_type)

    async def fake_verify_order_payment_succeeded(order: Dict[str, Any]) -> tuple[bool, str | None, str | None]:
        return True, "succeeded", None

    async def fake_create_shopify_order(order_id: str) -> None:
        shopify_calls.append(order_id)

    async def fake_create_order_snapshot_evidence_pack(order_id: str, triggered_by: str) -> None:
        return None

    async def fake_get_primary_store(merchant_id: str) -> Dict[str, Any]:
        return {"platform": "shopify", "api_key": "shp_key"}

    monkeypatch.delenv("ORDER_CONFIRMATION_EMAIL_ENABLED", raising=False)
    monkeypatch.setattr(agent_api, "validate_request_compat", fake_validate_request)
    monkeypatch.setattr(order_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(order_routes_module, "mark_order_paid", fake_mark_order_paid)
    monkeypatch.setattr(order_routes_module, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(order_routes_module, "verify_order_payment_succeeded", fake_verify_order_payment_succeeded)
    monkeypatch.setattr(order_routes_module, "create_shopify_order", fake_create_shopify_order)
    monkeypatch.setattr(merchant_store_module, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(
        pcs_evidence_pack_service,
        "create_order_snapshot_evidence_pack",
        fake_create_order_snapshot_evidence_pack,
    )
    # Patch where the name is USED: routes/agent_api.py binds it with a
    # module-level `from ... import`, so patching the source module would not
    # reach the already-bound reference.
    monkeypatch.setattr(
        agent_api, "enqueue_merchant_order_create", _recording_enqueue(enqueued)
    )

    background_tasks = BackgroundTasks()

    original_add_task = background_tasks.add_task

    def recording_add_task(func: Any, *args: Any, **kwargs: Any) -> None:
        queued_background_tasks.append((getattr(func, "__name__", "unknown"), args, kwargs))
        original_add_task(func, *args, **kwargs)

    background_tasks.add_task = recording_add_task  # type: ignore[assignment]

    body = await agent_api.agent_confirm_payment(
        order_id="ORD_BACKEND_PENDING",
        background_tasks=background_tasks,
        context=_TestAgentContext(),
    )

    assert body["status"] == "success"
    assert body["payment_intent_id"] == "pi_confirm_contract"
    assert body["shopify_sync"] == "initiated"
    assert mark_paid_calls == ["ORD_BACKEND_PENDING"]
    assert order_events == ["payment_succeeded"]
    queued_task_names = [name for name, _, _ in queued_background_tasks]
    # The merchant-order create no longer rides on BackgroundTasks: it is
    # enqueued durably, so a revision swap between the response and the sync
    # cannot lose it while the buyer is already charged.
    assert "fake_create_shopify_order" not in queued_task_names
    assert enqueued == [{
        "order_id": "ORD_BACKEND_PENDING",
        "merchant_id": "m_confirm",
        "require_shopify_primary": False,
    }]
    assert "log_agent_request" in queued_task_names
    assert "emit_agent_webhook_event" in queued_task_names
    assert "_send_order_confirmation_email_background" not in queued_task_names


@pytest.mark.asyncio
async def test_agent_confirm_payment_paypal_auth_first_authorizes_before_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_api as agent_api
    import routes.order_routes as order_routes_module
    import services.merchant_store_service as merchant_store_module

    calls: list[tuple[str, Any]] = []

    class _FakePayPalAdapter:
        async def confirm_payment(self, payment_intent_id: str, payment_method_id: str | None = None):
            calls.append(("paypal_authorize", payment_intent_id, payment_method_id))
            return True, "requires_capture", None

    async def fake_validate_request(*args: Any, **kwargs: Any) -> None:
        return None

    async def fake_log_agent_request(**kwargs: Any) -> None:
        return None

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        return _order(
            order_id,
            psp_used="paypal",
            payment_intent_id="PAYPAL_ORDER_AUTH",
            client_secret="https://paypal.test/approve",
            payment_status="awaiting_payment",
            metadata={
                "payment_flow": {
                    "mode": "authorization_first",
                    "psp": "paypal",
                    "store_platform": "shopify",
                    "capture_method": "manual",
                }
            },
        )

    async def fake_resolve_order_psp_adapter(order: Dict[str, Any]):
        return "paypal", _FakePayPalAdapter()

    async def fake_finalize(order_id: str, *, order: Dict[str, Any], source_event: str):
        calls.append(("finalize", order_id, source_event))
        return {
            "status": "success",
            "linked_merchant_order": {
                "platform": "shopify",
                "platform_order_id": "shopify_123",
            },
        }

    async def fake_get_primary_store(merchant_id: str) -> Dict[str, Any]:
        return {"platform": "shopify", "api_key": "shp_key"}

    monkeypatch.setattr(agent_api, "validate_request_compat", fake_validate_request)
    monkeypatch.setattr(agent_api, "log_agent_request", fake_log_agent_request)
    monkeypatch.setattr(order_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(order_routes_module, "_resolve_order_psp_adapter", fake_resolve_order_psp_adapter)
    monkeypatch.setattr(order_routes_module, "finalize_authorized_payment_order", fake_finalize)
    monkeypatch.setattr(merchant_store_module, "get_primary_store", fake_get_primary_store)

    body = await agent_api.agent_confirm_payment(
        order_id="ORD_PAYPAL_AUTH_FIRST",
        background_tasks=BackgroundTasks(),
        context=_TestAgentContext(),
    )

    assert body["status"] == "success"
    assert body["payment_status"] == "paid"
    assert body["authorization_first"] is True
    assert body["linked_merchant_order"]["platform_order_id"] == "shopify_123"
    assert calls[0] == (
        "paypal_authorize",
        "PAYPAL_ORDER_AUTH",
        "agent_confirm_payment:ORD_PAYPAL_AUTH_FIRST",
    )
    assert calls[1] == ("finalize", "ORD_PAYPAL_AUTH_FIRST", "agent_confirm_payment")


@pytest.mark.asyncio
async def test_agent_confirm_payment_already_paid_branch_enqueues_a_retry(monkeypatch):
    """The already-paid branch exists ONLY to be a retry: it logs
    `shopify_sync_retry_requested` and answers "Order already paid; Shopify sync
    initiated". Deleting its enqueue left every suite green — and it is the site
    where a tombstoned job would do the most damage, since the caller is
    explicitly asking again.
    """
    import routes.agent_api as agent_api
    import routes.order_routes as order_routes_module

    enqueued: list[Dict[str, Any]] = []
    order_events: list[str] = []

    async def fake_get_order(order_id: str) -> Dict[str, Any]:
        return {
            "order_id": order_id,
            "merchant_id": "m_confirm",
            "payment_status": "paid",
            "status": "paid",
            # already paid, and NO merchant order yet — the retry condition
            "shopify_order_id": None,
            "payment_intent_id": "pi_retry",
            "total": "31.00",
            "currency": "USD",
            "metadata": {},
        }

    async def fake_log_order_event(*, event_type: str, **kwargs: Any) -> None:
        order_events.append(event_type)

    async def fake_get_primary_store(merchant_id: str) -> Dict[str, Any]:
        return {"platform": "shopify", "api_key": "shp_key"}

    async def fake_validate_request(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(agent_api, "validate_request_compat", fake_validate_request)
    monkeypatch.setattr(order_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(order_routes_module, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(
        __import__("services.merchant_store_service", fromlist=["x"]),
        "get_primary_store",
        fake_get_primary_store,
    )
    monkeypatch.setattr(
        agent_api, "enqueue_merchant_order_create", _recording_enqueue(enqueued)
    )

    body = await agent_api.agent_confirm_payment(
        order_id="ORD_ALREADY_PAID_RETRY",
        background_tasks=BackgroundTasks(),
        context=_TestAgentContext(),
    )

    assert body["shopify_sync"] == "initiated"
    assert "shopify_sync_retry_requested" in order_events
    assert enqueued == [{
        "order_id": "ORD_ALREADY_PAID_RETRY",
        "merchant_id": "m_confirm",
        "require_shopify_primary": False,
    }]
