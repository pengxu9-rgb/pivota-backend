from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

import httpx
import pytest


# Ensure we import the pivota-backend modules (external_repos/pivota-backend), not the workspace root.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


class _TestAgentContext:
    agent_id = "agent_test"
    agent_name = "Test Agent"
    allowed_merchants = None

    def can_access_merchant(self, merchant_id: str) -> bool:
        return True


async def _override_get_agent_context() -> _TestAgentContext:
    return _TestAgentContext()


@pytest.fixture(autouse=True)
def _shopify_store_policy_context(monkeypatch: pytest.MonkeyPatch):
    import routes.agent_api as agent_api_module
    import routes.agent_payment_sdk as payment_module

    async def fake_get_primary_store(_merchant_id: str):
        return {"platform": "shopify", "store_id": "store_test"}

    monkeypatch.setattr(agent_api_module, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(payment_module, "get_primary_store", fake_get_primary_store)


def test_agent_order_payment_instructions_do_not_claim_client_secret_when_missing() -> None:
    import routes.agent_api as agent_api_module

    assert (
        agent_api_module._format_agent_order_payment_instructions("stripe", None, None)
        == "Payment initiation is unavailable; retry PSP payment creation before asking the shopper to pay."
    )
    assert (
        agent_api_module._format_agent_order_payment_instructions("stripe", None, "cs_live")
        == "Use client_secret for Stripe payment confirmation"
    )


@pytest.mark.asyncio
async def test_agent_create_order_without_quote_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from main import app
    from routes.agent_auth import get_agent_context

    app.dependency_overrides[get_agent_context] = _override_get_agent_context
    try:
        import mvp.events as mvp_events
        import routes.agent_api as agent_api_module
        import routes.order_routes as order_routes_module
        import services.agent_governance as governance_module

        monkeypatch.setattr(mvp_events, "emit_best_effort", lambda **_: None)

        async def noop_validate_request(agent_id: str) -> None:
            return None

        async def noop_record_response(agent_id: str, latency_ms: int, success: bool) -> None:
            return None

        async def fail_create_new_order(*_: Any, **__: Any):
            raise AssertionError("unquoted agent purchase path must not create an order")

        monkeypatch.setattr(governance_module.agent_governance, "validate_request", noop_validate_request)
        monkeypatch.setattr(governance_module.agent_governance, "record_response", noop_record_response)
        monkeypatch.setattr(order_routes_module, "create_new_order", fail_create_new_order)

        payload = {
            "merchant_id": "m_test",
            "customer_email": "test@example.com",
            "items": [
                {
                    "product_id": "p_1",
                    "product_title": "Test Product",
                    "variant_id": "v_1",
                    "quantity": 1,
                    "unit_price": "10.00",
                    "subtotal": "10.00",
                }
            ],
            "shipping_address": {
                "name": "Test",
                "address_line1": "1 Test St",
                "city": "SF",
                "state": "CA",
                "postal_code": "94107",
                "country": "US",
            },
            "currency": "USD",
            "metadata": {},
        }

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/agent/v1/orders/create", json=payload)

        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "QUOTE_REQUIRED_BEFORE_PURCHASE"
    finally:
        app.dependency_overrides.pop(get_agent_context, None)


@pytest.mark.asyncio
async def test_agent_create_order_non_shopify_direct_purchase_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from main import app
    from routes.agent_auth import get_agent_context

    app.dependency_overrides[get_agent_context] = _override_get_agent_context
    try:
        import mvp.events as mvp_events
        import routes.agent_api as agent_api_module
        import routes.order_routes as order_routes_module
        import services.agent_governance as governance_module

        monkeypatch.setattr(mvp_events, "emit_best_effort", lambda **_: None)

        async def noop_validate_request(agent_id: str) -> None:
            return None

        async def noop_record_response(agent_id: str, latency_ms: int, success: bool) -> None:
            return None

        async def fake_get_primary_store(_merchant_id: str):
            return {"platform": "woocommerce", "store_id": "store_woo"}

        async def fail_create_new_order(*_: Any, **__: Any):
            raise AssertionError("non-Shopify direct purchase must not create a Pivota order")

        monkeypatch.setattr(governance_module.agent_governance, "validate_request", noop_validate_request)
        monkeypatch.setattr(governance_module.agent_governance, "record_response", noop_record_response)
        monkeypatch.setattr(agent_api_module, "get_primary_store", fake_get_primary_store)
        monkeypatch.setattr(order_routes_module, "create_new_order", fail_create_new_order)

        payload = {
            "merchant_id": "m_test",
            "quote_id": "q_test",
            "customer_email": "test@example.com",
            "items": [{"product_id": "p_1", "variant_id": "v_1", "quantity": 1}],
            "shipping_address": {
                "name": "Test",
                "address_line1": "1 Test St",
                "city": "SF",
                "state": "CA",
                "postal_code": "94107",
                "country": "US",
            },
            "currency": "USD",
            "metadata": {},
        }

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/agent/v1/orders/create", json=payload)

        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["error"] == "UNSUPPORTED_COMMERCE_PATH"
        assert detail["commerce_path"] == "unsupported"
        assert detail["execution_policy"]["allows_pivota_order"] is False
        assert detail["execution_policy"]["allows_psp_creation"] is False
    finally:
        app.dependency_overrides.pop(get_agent_context, None)


@pytest.mark.asyncio
async def test_agent_create_order_replay_returns_cached_response(monkeypatch: pytest.MonkeyPatch) -> None:
    from main import app
    from routes.agent_auth import get_agent_context

    app.dependency_overrides[get_agent_context] = _override_get_agent_context
    try:
        # Disable best-effort telemetry to avoid DB/file side effects in unit tests.
        import mvp.events as mvp_events

        monkeypatch.setattr(mvp_events, "emit_best_effort", lambda **_: None)
        import mvp.ledger_events as ledger_events

        monkeypatch.setattr(ledger_events, "emit_best_effort", lambda **_: None)

        # Governance should not block tests.
        import services.agent_governance as governance_module

        async def noop_validate_request(agent_id: str) -> None:
            return None

        async def noop_record_response(agent_id: str, latency_ms: int, success: bool) -> None:
            return None

        monkeypatch.setattr(governance_module.agent_governance, "validate_request", noop_validate_request)
        monkeypatch.setattr(governance_module.agent_governance, "record_response", noop_record_response)

        import services.agent_webhook_service as agent_webhook_service

        async def noop_emit_agent_webhook_event(*_: Any, **__: Any) -> None:
            return None

        monkeypatch.setattr(
            agent_webhook_service,
            "emit_agent_webhook_event",
            noop_emit_agent_webhook_event,
        )

        # Avoid DB usage logging.
        import routes.agent_auth as agent_auth_module

        async def noop_log_agent_request(*_: Any, **__: Any) -> None:
            return None

        monkeypatch.setattr(agent_auth_module, "log_agent_request", noop_log_agent_request)
        import routes.agent_api as agent_api_module

        monkeypatch.setattr(agent_api_module, "log_agent_request", noop_log_agent_request)

        # Use an in-memory idempotency store behind the Postgres interface.
        from mvp.idempotency import InMemoryIdempotencyStore

        shared_store = InMemoryIdempotencyStore()

        class FakePostgresIdempotencyStore:
            async def get(self, *, scope: str, key: str):
                return await shared_store.get(scope=scope, key=key)

            async def put(self, *, scope: str, key: str, value: Dict[str, Any]):
                return await shared_store.put(scope=scope, key=key, value=value)

        monkeypatch.setattr(agent_api_module, "_AGENT_ORDER_IDEMPOTENCY_STORE", FakePostgresIdempotencyStore())

        # Ensure the underlying order creation is only executed once; the second call must replay.
        import routes.order_routes as order_routes_module
        from models.order import OrderResponse, PaymentAction

        calls = {"count": 0}

        async def fake_create_new_order(order_request: Any, background_tasks: Any):
            calls["count"] += 1
            order_id = f"ord_{calls['count']}"
            now = datetime.now(timezone.utc)
            return OrderResponse(
                order_id=order_id,
                merchant_id=order_request.merchant_id,
                customer_email=order_request.customer_email,
                items=order_request.items,
                shipping_address=order_request.shipping_address,
                subtotal=Decimal("10.00"),
                shipping_fee=Decimal("0.00"),
                tax=Decimal("0.00"),
                total=Decimal("10.00"),
                currency=order_request.currency or "USD",
                status="pending",
                payment_status="unpaid",
                fulfillment_status=None,
                payment_intent_id="pi_test",
                client_secret="cs_test",
                psp="stripe",
                payment_action=PaymentAction(type="stripe_client_secret", client_secret="cs_test"),
                shopify_order_id=None,
                tracking_number=None,
                created_at=now,
                updated_at=now,
                paid_at=None,
                shipped_at=None,
                agent_session_id=order_request.agent_session_id,
                metadata=order_request.metadata,
            )

        monkeypatch.setattr(order_routes_module, "create_new_order", fake_create_new_order)

        payload = {
            "merchant_id": "m_test",
            "quote_id": "q_test",
            "customer_email": "test@example.com",
            "items": [
                {
                    "product_id": "p_1",
                    "product_title": "Test Product",
                    "variant_id": "v_1",
                    "quantity": 1,
                    "unit_price": "10.00",
                    "subtotal": "10.00",
                }
            ],
            "shipping_address": {
                "name": "Test",
                "address_line1": "1 Test St",
                "city": "SF",
                "state": "CA",
                "postal_code": "94107",
                "country": "US",
            },
            "currency": "USD",
            "idempotency_key": "idem_order_create_1",
            "metadata": {},
        }

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post("/agent/v1/orders/create", json=payload)
            second = await client.post("/agent/v1/orders/create", json=payload)

        assert first.status_code == 200
        assert second.status_code == 200
        body1 = first.json()
        body2 = second.json()
        assert body1["order_id"] == body2["order_id"]
        assert calls["count"] == 1
    finally:
        app.dependency_overrides.pop(get_agent_context, None)


@pytest.mark.asyncio
async def test_agent_create_order_replays_existing_order_before_recreating(monkeypatch: pytest.MonkeyPatch) -> None:
    from main import app
    from routes.agent_auth import get_agent_context

    app.dependency_overrides[get_agent_context] = _override_get_agent_context
    try:
        import mvp.events as mvp_events
        import mvp.ledger_events as ledger_events
        import services.agent_governance as governance_module
        import services.agent_webhook_service as agent_webhook_service
        import routes.agent_api as agent_api_module
        import routes.agent_auth as agent_auth_module
        import routes.order_routes as order_routes_module

        async def noop_sleep(_: float) -> None:
            return None

        monkeypatch.setattr(agent_api_module.asyncio, "sleep", noop_sleep)
        monkeypatch.setattr(mvp_events, "emit_best_effort", lambda **_: None)
        monkeypatch.setattr(ledger_events, "emit_best_effort", lambda **_: None)

        async def noop_validate_request(agent_id: str) -> None:
            return None

        async def noop_record_response(agent_id: str, latency_ms: int, success: bool) -> None:
            return None

        monkeypatch.setattr(governance_module.agent_governance, "validate_request", noop_validate_request)
        monkeypatch.setattr(governance_module.agent_governance, "record_response", noop_record_response)

        async def noop_emit_agent_webhook_event(*_: Any, **__: Any) -> None:
            return None

        monkeypatch.setattr(
            agent_webhook_service,
            "emit_agent_webhook_event",
            noop_emit_agent_webhook_event,
        )

        async def noop_log_agent_request(*_: Any, **__: Any) -> None:
            return None

        monkeypatch.setattr(agent_auth_module, "log_agent_request", noop_log_agent_request)
        monkeypatch.setattr(agent_api_module, "log_agent_request", noop_log_agent_request)

        class FakePostgresIdempotencyStore:
            async def get(self, *, scope: str, key: str):
                return None

            async def put(self, *, scope: str, key: str, value: Dict[str, Any]):
                return None

        monkeypatch.setattr(agent_api_module, "_AGENT_ORDER_IDEMPOTENCY_STORE", FakePostgresIdempotencyStore())

        expected = {
            "status": "success",
            "order_id": "ord_existing",
            "merchant_id": "m_test",
            "total": "10.00",
            "total_amount": 10.0,
            "currency": "USD",
            "presentment_currency": "USD",
            "charge_currency": "USD",
            "settlement_currency": None,
            "payment": {
                "psp": "stripe",
                "client_secret": "cs_existing",
                "payment_intent_id": "pi_existing",
                "payment_action": {"type": "stripe_client_secret", "client_secret": "cs_existing"},
                "instructions": "Use client_secret for Stripe payment confirmation",
            },
            "tracking": {
                "agent_session_id": "agent_test_123",
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        }

        async def fake_load_replayable_agent_order_create_response(order_request: Any):
            return expected

        async def fail_create_new_order(order_request: Any, background_tasks: Any):
            raise AssertionError("create_new_order should not run when replayable order exists")

        monkeypatch.setattr(
            agent_api_module,
            "_load_replayable_agent_order_create_response",
            fake_load_replayable_agent_order_create_response,
        )
        monkeypatch.setattr(order_routes_module, "create_new_order", fail_create_new_order)

        payload = {
            "merchant_id": "m_test",
            "quote_id": "q_test",
            "customer_email": "test@example.com",
            "items": [{"product_id": "p_1", "variant_id": "v_1", "quantity": 1}],
            "shipping_address": {
                "name": "Test",
                "address_line1": "1 Test St",
                "city": "SF",
                "state": "CA",
                "postal_code": "94107",
                "country": "US",
            },
            "currency": "USD",
            "idempotency_key": "idem_order_create_replay_existing",
            "metadata": {},
        }

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/agent/v1/orders/create", json=payload)

        assert resp.status_code == 200
        assert resp.json()["order_id"] == "ord_existing"
    finally:
        app.dependency_overrides.pop(get_agent_context, None)


@pytest.mark.asyncio
async def test_agent_create_order_busy_replays_existing_order_without_second_create(monkeypatch: pytest.MonkeyPatch) -> None:
    from main import app
    from routes.agent_auth import get_agent_context

    app.dependency_overrides[get_agent_context] = _override_get_agent_context
    try:
        import mvp.events as mvp_events
        import mvp.ledger_events as ledger_events
        import services.agent_governance as governance_module
        import services.agent_webhook_service as agent_webhook_service
        import routes.agent_api as agent_api_module
        import routes.agent_auth as agent_auth_module
        import routes.order_routes as order_routes_module

        monkeypatch.setattr(mvp_events, "emit_best_effort", lambda **_: None)
        monkeypatch.setattr(ledger_events, "emit_best_effort", lambda **_: None)

        async def noop_validate_request(agent_id: str) -> None:
            return None

        async def noop_record_response(agent_id: str, latency_ms: int, success: bool) -> None:
            return None

        monkeypatch.setattr(governance_module.agent_governance, "validate_request", noop_validate_request)
        monkeypatch.setattr(governance_module.agent_governance, "record_response", noop_record_response)

        async def noop_emit_agent_webhook_event(*_: Any, **__: Any) -> None:
            return None

        monkeypatch.setattr(
            agent_webhook_service,
            "emit_agent_webhook_event",
            noop_emit_agent_webhook_event,
        )

        async def noop_log_agent_request(*_: Any, **__: Any) -> None:
            return None

        monkeypatch.setattr(agent_auth_module, "log_agent_request", noop_log_agent_request)
        monkeypatch.setattr(agent_api_module, "log_agent_request", noop_log_agent_request)

        class FakePostgresIdempotencyStore:
            async def get(self, *, scope: str, key: str):
                return None

            async def put(self, *, scope: str, key: str, value: Dict[str, Any]):
                return None

        monkeypatch.setattr(agent_api_module, "_AGENT_ORDER_IDEMPOTENCY_STORE", FakePostgresIdempotencyStore())

        replay_checks = {"count": 0}
        expected = {
            "status": "success",
            "order_id": "ord_replayed_after_busy",
            "merchant_id": "m_test",
            "total": "10.00",
            "total_amount": 10.0,
            "currency": "USD",
            "presentment_currency": "USD",
            "charge_currency": "USD",
            "settlement_currency": None,
            "payment": {
                "psp": "stripe",
                "client_secret": "cs_existing",
                "payment_intent_id": "pi_existing",
                "payment_action": {"type": "stripe_client_secret", "client_secret": "cs_existing"},
                "instructions": "Use client_secret for Stripe payment confirmation",
            },
            "tracking": {
                "agent_session_id": "agent_test_123",
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        }

        async def fake_load_replayable_agent_order_create_response(order_request: Any):
            replay_checks["count"] += 1
            if replay_checks["count"] == 1:
                return None
            return expected

        calls = {"count": 0}

        async def fake_create_new_order(order_request: Any, background_tasks: Any):
            calls["count"] += 1
            raise RuntimeError("cannot perform operation: another operation is in progress")

        monkeypatch.setattr(
            agent_api_module,
            "_load_replayable_agent_order_create_response",
            fake_load_replayable_agent_order_create_response,
        )
        monkeypatch.setattr(order_routes_module, "create_new_order", fake_create_new_order)

        payload = {
            "merchant_id": "m_test",
            "quote_id": "q_test",
            "customer_email": "test@example.com",
            "items": [{"product_id": "p_1", "variant_id": "v_1", "quantity": 1}],
            "shipping_address": {
                "name": "Test",
                "address_line1": "1 Test St",
                "city": "SF",
                "state": "CA",
                "postal_code": "94107",
                "country": "US",
            },
            "currency": "USD",
            "idempotency_key": "idem_order_create_busy_replay",
            "metadata": {},
        }

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/agent/v1/orders/create", json=payload)

        assert resp.status_code == 200
        assert resp.json()["order_id"] == "ord_replayed_after_busy"
        assert calls["count"] == 1
        assert replay_checks["count"] == 2
    finally:
        app.dependency_overrides.pop(get_agent_context, None)


@pytest.mark.asyncio
async def test_agent_create_order_busy_retries_once_when_no_existing_order(monkeypatch: pytest.MonkeyPatch) -> None:
    from main import app
    from routes.agent_auth import get_agent_context

    app.dependency_overrides[get_agent_context] = _override_get_agent_context
    try:
        import mvp.events as mvp_events
        import mvp.ledger_events as ledger_events
        import services.agent_governance as governance_module
        import services.agent_webhook_service as agent_webhook_service
        import routes.agent_api as agent_api_module
        import routes.agent_auth as agent_auth_module
        import routes.order_routes as order_routes_module
        from models.order import OrderResponse, PaymentAction

        async def noop_sleep(_: float) -> None:
            return None

        monkeypatch.setattr(agent_api_module.asyncio, "sleep", noop_sleep)
        monkeypatch.setattr(mvp_events, "emit_best_effort", lambda **_: None)
        monkeypatch.setattr(ledger_events, "emit_best_effort", lambda **_: None)

        async def noop_validate_request(agent_id: str) -> None:
            return None

        async def noop_record_response(agent_id: str, latency_ms: int, success: bool) -> None:
            return None

        monkeypatch.setattr(governance_module.agent_governance, "validate_request", noop_validate_request)
        monkeypatch.setattr(governance_module.agent_governance, "record_response", noop_record_response)

        async def noop_emit_agent_webhook_event(*_: Any, **__: Any) -> None:
            return None

        monkeypatch.setattr(
            agent_webhook_service,
            "emit_agent_webhook_event",
            noop_emit_agent_webhook_event,
        )

        async def noop_log_agent_request(*_: Any, **__: Any) -> None:
            return None

        monkeypatch.setattr(agent_auth_module, "log_agent_request", noop_log_agent_request)
        monkeypatch.setattr(agent_api_module, "log_agent_request", noop_log_agent_request)

        class FakePostgresIdempotencyStore:
            async def get(self, *, scope: str, key: str):
                return None

            async def put(self, *, scope: str, key: str, value: Dict[str, Any]):
                return None

        monkeypatch.setattr(agent_api_module, "_AGENT_ORDER_IDEMPOTENCY_STORE", FakePostgresIdempotencyStore())

        async def fake_load_replayable_agent_order_create_response(order_request: Any):
            return None

        calls = {"count": 0}

        async def fake_create_new_order(order_request: Any, background_tasks: Any):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("cannot perform operation: another operation is in progress")
            now = datetime.now(timezone.utc)
            return OrderResponse(
                order_id="ord_retry_success",
                merchant_id=order_request.merchant_id,
                customer_email=order_request.customer_email,
                items=order_request.items,
                shipping_address=order_request.shipping_address,
                subtotal=Decimal("10.00"),
                shipping_fee=Decimal("0.00"),
                tax=Decimal("0.00"),
                total=Decimal("10.00"),
                currency=order_request.currency or "USD",
                status="pending",
                payment_status="unpaid",
                fulfillment_status=None,
                payment_intent_id="pi_retry_success",
                client_secret="cs_retry_success",
                psp="stripe",
                payment_action=PaymentAction(type="stripe_client_secret", client_secret="cs_retry_success"),
                shopify_order_id=None,
                tracking_number=None,
                created_at=now,
                updated_at=now,
                paid_at=None,
                shipped_at=None,
                agent_session_id=order_request.agent_session_id,
                metadata=order_request.metadata,
            )

        monkeypatch.setattr(
            agent_api_module,
            "_load_replayable_agent_order_create_response",
            fake_load_replayable_agent_order_create_response,
        )
        monkeypatch.setattr(order_routes_module, "create_new_order", fake_create_new_order)

        payload = {
            "merchant_id": "m_test",
            "quote_id": "q_test",
            "customer_email": "test@example.com",
            "items": [
                {
                    "product_id": "p_1",
                    "product_title": "Test Product",
                    "variant_id": "v_1",
                    "quantity": 1,
                    "unit_price": "10.00",
                    "subtotal": "10.00",
                }
            ],
            "shipping_address": {
                "name": "Test",
                "address_line1": "1 Test St",
                "city": "SF",
                "state": "CA",
                "postal_code": "94107",
                "country": "US",
            },
            "currency": "USD",
            "idempotency_key": "idem_order_create_busy_retry",
            "metadata": {},
        }

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/agent/v1/orders/create", json=payload)

        assert resp.status_code == 200
        assert resp.json()["order_id"] == "ord_retry_success"
        assert calls["count"] == 2
    finally:
        app.dependency_overrides.pop(get_agent_context, None)


@pytest.mark.asyncio
async def test_agent_create_order_success_ignores_usage_logging_busy_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from main import app
    from routes.agent_auth import get_agent_context

    app.dependency_overrides[get_agent_context] = _override_get_agent_context
    try:
        import mvp.events as mvp_events
        import mvp.ledger_events as ledger_events
        import services.agent_governance as governance_module
        import services.agent_webhook_service as agent_webhook_service
        import routes.agent_api as agent_api_module
        import routes.agent_auth as agent_auth_module
        import routes.order_routes as order_routes_module
        from models.order import OrderResponse, PaymentAction

        monkeypatch.setattr(mvp_events, "emit_best_effort", lambda **_: None)
        monkeypatch.setattr(ledger_events, "emit_best_effort", lambda **_: None)

        async def noop_validate_request(agent_id: str) -> None:
            return None

        async def noop_record_response(agent_id: str, latency_ms: int, success: bool) -> None:
            return None

        monkeypatch.setattr(governance_module.agent_governance, "validate_request", noop_validate_request)
        monkeypatch.setattr(governance_module.agent_governance, "record_response", noop_record_response)

        async def noop_emit_agent_webhook_event(*_: Any, **__: Any) -> None:
            return None

        monkeypatch.setattr(
            agent_webhook_service,
            "emit_agent_webhook_event",
            noop_emit_agent_webhook_event,
        )

        async def failing_log_agent_request(*_: Any, **__: Any) -> None:
            raise RuntimeError("cannot perform operation: another operation is in progress")

        monkeypatch.setattr(agent_auth_module, "log_agent_request", failing_log_agent_request)
        monkeypatch.setattr(agent_api_module, "log_agent_request", failing_log_agent_request)

        class FakePostgresIdempotencyStore:
            async def get(self, *, scope: str, key: str):
                return None

            async def put(self, *, scope: str, key: str, value: Dict[str, Any]):
                return None

        monkeypatch.setattr(agent_api_module, "_AGENT_ORDER_IDEMPOTENCY_STORE", FakePostgresIdempotencyStore())

        async def fake_get_primary_store(merchant_id: str):
            return {"platform": "shopify", "store_id": "store_test"}

        monkeypatch.setattr(agent_api_module, "get_primary_store", fake_get_primary_store)

        async def fake_create_new_order(order_request: Any, background_tasks: Any):
            now = datetime.now(timezone.utc)
            return OrderResponse(
                order_id="ord_success_despite_log_busy",
                merchant_id=order_request.merchant_id,
                customer_email=order_request.customer_email,
                items=order_request.items,
                shipping_address=order_request.shipping_address,
                subtotal=Decimal("10.00"),
                shipping_fee=Decimal("0.00"),
                tax=Decimal("0.00"),
                total=Decimal("10.00"),
                currency=order_request.currency or "USD",
                status="pending",
                payment_status="unpaid",
                fulfillment_status=None,
                payment_intent_id="pi_success",
                client_secret="cs_success",
                psp="stripe",
                payment_action=PaymentAction(type="stripe_client_secret", client_secret="cs_success"),
                shopify_order_id=None,
                tracking_number=None,
                created_at=now,
                updated_at=now,
                paid_at=None,
                shipped_at=None,
                agent_session_id=order_request.agent_session_id,
                metadata=order_request.metadata,
            )

        monkeypatch.setattr(order_routes_module, "create_new_order", fake_create_new_order)

        payload = {
            "merchant_id": "m_test",
            "quote_id": "q_test",
            "customer_email": "test@example.com",
            "items": [
                {
                    "product_id": "p_1",
                    "product_title": "Test Product",
                    "variant_id": "v_1",
                    "quantity": 1,
                    "unit_price": "10.00",
                    "subtotal": "10.00",
                }
            ],
            "shipping_address": {
                "name": "Test",
                "address_line1": "1 Test St",
                "city": "SF",
                "state": "CA",
                "postal_code": "94107",
                "country": "US",
            },
            "currency": "USD",
            "idempotency_key": "idem_order_create_log_busy",
            "metadata": {},
        }

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/agent/v1/orders/create", json=payload)

        assert resp.status_code == 200
        assert resp.json()["order_id"] == "ord_success_despite_log_busy"
    finally:
        app.dependency_overrides.pop(get_agent_context, None)


@pytest.mark.asyncio
async def test_agent_payments_replay_does_not_double_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    from main import app
    from routes.agent_auth import get_agent_context

    app.dependency_overrides[get_agent_context] = _override_get_agent_context
    try:
        # Disable best-effort telemetry to avoid DB/file side effects in unit tests.
        import mvp.events as mvp_events

        monkeypatch.setattr(mvp_events, "emit_best_effort", lambda **_: None)
        import mvp.ledger_events as ledger_events

        monkeypatch.setattr(ledger_events, "emit_best_effort", lambda **_: None)

        # Avoid DB usage logging.
        import routes.agent_auth as agent_auth_module

        async def noop_log_agent_request(*_: Any, **__: Any) -> None:
            return None

        monkeypatch.setattr(agent_auth_module, "log_agent_request", noop_log_agent_request)
        import routes.agent_payment_sdk as payment_module

        monkeypatch.setattr(payment_module, "log_agent_request", noop_log_agent_request)

        # Stub order lookup.
        async def fake_get_order(order_id: str) -> Optional[Dict[str, Any]]:
            return {
                "order_id": order_id,
                "merchant_id": "m_test",
                "payment_status": "unpaid",
                "total": 10.0,
                "currency": "USD",
                "metadata": {
                    "pricing_quote": {
                        "quote_id": "q_payment_test",
                        "expires_at": "2099-01-01T00:00:00+00:00",
                        "live_validation": {"status": "validated"},
                    }
                },
                "shipping_address": {
                    "country": "US",
                    "postal_code": "94107",
                    "city": "SF",
                    "state": "CA",
                },
            }

        async def fake_update_payment_info(**_: Any) -> None:
            return None

        monkeypatch.setattr(payment_module, "get_order", fake_get_order)
        monkeypatch.setattr(payment_module, "update_payment_info", fake_update_payment_info)

        # Stub merchant onboarding lookup.
        async def fake_get_merchant_onboarding(merchant_id: str) -> Dict[str, Any]:
            return {"merchant_id": merchant_id, "psp_connected": True}

        monkeypatch.setattr(payment_module, "get_merchant_onboarding", fake_get_merchant_onboarding)

        # Stub PSP routing selection.
        async def fake_select_psp(self, *, agent_id: str, merchant_id: str, amount: float, currency: str):
            return "stripe", {"route_id": "route_test"}

        monkeypatch.setattr(payment_module.PaymentRoutingService, "select_psp", fake_select_psp)

        # In-memory payment table for idempotency checks.
        payments: Dict[tuple[str, str], Dict[str, Any]] = {}

        from db.database import database as database_obj

        async def fake_fetch_one(query: Any, values: Optional[Dict[str, Any]] = None):
            if isinstance(query, str) and "FROM payments" in query:
                values = values or {}
                key = (str(values.get("key")), str(values.get("order_id")))
                return payments.get(key)
            return None

        async def fake_execute(query: Any, values: Optional[Dict[str, Any]] = None):
            if isinstance(query, str) and "INSERT INTO payments" in query:
                values = values or {}
                idem_key = str(values.get("idem_key") or "")
                order_id = str(values.get("order_id") or "")
                payments[(idem_key, order_id)] = {
                    "payment_id": values.get("payment_id"),
                    "payment_intent_id": values.get("intent_id"),
                    "status": values.get("status"),
                }
                return None
            return None

        monkeypatch.setattr(database_obj, "fetch_one", fake_fetch_one)
        monkeypatch.setattr(database_obj, "execute", fake_execute)

        calls = {"count": 0}

        async def fake_create_payment_with_failover(*_: Any, **__: Any):
            calls["count"] += 1
            payment_intent = SimpleNamespace(
                id="pi_test_1",
                status="succeeded",
                client_secret="cs_test_1",
            )
            return True, payment_intent, None, "stripe"

        monkeypatch.setattr(payment_module, "create_payment_with_failover", fake_create_payment_with_failover)

        body = {
            "order_id": "ord_test",
            "payment_method": {"type": "card", "token": "tok_test"},
            "idempotency_key": "idem_pay_1",
        }

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post("/agent/v1/payments", json=body)
            second = await client.post("/agent/v1/payments", json=body)

        assert first.status_code == 200
        assert second.status_code == 200
        payload1 = first.json()
        payload2 = second.json()
        assert payload1["payment_id"] == payload2["payment_id"]
        assert payload1["payment_intent_id"] == payload2["payment_intent_id"]
        assert payload1["status"] == payload2["status"]
        assert calls["count"] == 1
    finally:
        app.dependency_overrides.pop(get_agent_context, None)


@pytest.mark.asyncio
async def test_agent_payment_without_live_validated_quote_blocks_psp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from main import app
    from routes.agent_auth import get_agent_context

    app.dependency_overrides[get_agent_context] = _override_get_agent_context
    try:
        import routes.agent_payment_sdk as payment_module

        async def fake_get_order(order_id: str) -> Optional[Dict[str, Any]]:
            return {
                "order_id": order_id,
                "merchant_id": "m_test",
                "payment_status": "unpaid",
                "total": 10.0,
                "currency": "USD",
                "metadata": {},
            }

        async def fail_create_payment_with_failover(*_: Any, **__: Any):
            raise AssertionError("PSP creation must not run without live quote validation")

        monkeypatch.setattr(payment_module, "get_order", fake_get_order)
        monkeypatch.setattr(payment_module, "create_payment_with_failover", fail_create_payment_with_failover)

        body = {
            "order_id": "ord_without_quote",
            "payment_method": {"type": "card", "token": "tok_test"},
            "idempotency_key": "idem_pay_without_quote",
        }

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/agent/v1/payments", json=body)

        assert resp.status_code == 409
        assert resp.json()["detail"]["error"] == "QUOTE_REQUIRED_BEFORE_PAYMENT"
    finally:
        app.dependency_overrides.pop(get_agent_context, None)
