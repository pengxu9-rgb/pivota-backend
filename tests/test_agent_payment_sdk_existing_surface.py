from __future__ import annotations

import asyncio
from types import SimpleNamespace

from typing import Any, Dict, Optional

import httpx
import pytest


@pytest.fixture(autouse=True)
def _shopify_store_policy_context(monkeypatch: pytest.MonkeyPatch):
    import routes.agent_payment_sdk as payment_module

    async def fake_get_primary_store(_merchant_id: str):
        return {"platform": "shopify", "store_id": "store_test"}

    monkeypatch.setattr(payment_module, "get_primary_store", fake_get_primary_store)


def _live_quote_metadata() -> Dict[str, Any]:
    return {
        "pricing_quote": {
            "quote_id": "q_payment_sdk",
            "live_validation": {"status": "validated"},
            "expires_at": "2099-01-01T00:00:00Z",
        }
    }


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
async def test_agent_payment_non_shopify_direct_purchase_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_payment_sdk as payment_module
    from fastapi import BackgroundTasks

    class _Context:
        agent_id = "agent_test"
        session_id = "sess_test"

        def can_access_merchant(self, merchant_id: Optional[str]) -> bool:
            return merchant_id == "merch_test_123"

    async def fake_get_order(order_id: str) -> Dict[str, Any]:
        return {
            "order_id": order_id,
            "merchant_id": "merch_test_123",
            "payment_status": "unpaid",
            "total": 25.22,
            "currency": "USD",
            "metadata": _live_quote_metadata(),
        }

    async def fake_get_primary_store(_merchant_id: str):
        return {"platform": "bigcommerce", "store_id": "store_big"}

    async def fail_create_payment_with_failover(*args: Any, **kwargs: Any):
        raise AssertionError("non-Shopify direct purchase must not create a PSP payment")

    monkeypatch.setattr(payment_module, "get_order", fake_get_order)
    monkeypatch.setattr(payment_module, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(payment_module, "create_payment_with_failover", fail_create_payment_with_failover)

    with pytest.raises(payment_module.HTTPException) as exc_info:
        await payment_module.create_payment(
            payment_module.PaymentRequest(
                order_id="ORD_BIGCOMMERCE",
                payment_method=payment_module.PaymentMethod(type="dynamic"),
            ),
            BackgroundTasks(),
            context=_Context(),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["error"] == "UNSUPPORTED_COMMERCE_PATH"
    assert exc_info.value.detail["commerce_path"] == "unsupported"
    assert exc_info.value.detail["execution_policy"]["allows_psp_creation"] is False


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
            "metadata": _live_quote_metadata(),
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
            "metadata": _live_quote_metadata(),
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


@pytest.mark.asyncio
async def test_agent_payments_stripe_checkout_request_forces_hosted_stripe_surface(
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
            "merchant_id": "merch_test_123",
            "payment_status": "unpaid",
            "total": 25.22,
            "currency": "USD",
            "shipping_address": {
                "country": "US",
                "postal_code": "94105",
                "city": "San Francisco",
                "state": "CA",
            },
            "metadata": _live_quote_metadata(),
        }

    async def fake_get_merchant_onboarding(merchant_id: str) -> Dict[str, Any]:
        return {"merchant_id": merchant_id}

    async def fake_select_psp(self, *, agent_id: str, merchant_id: str, amount: float, currency: str):
        return "adyen", {
            "route_id": "route_prefers_non_stripe",
            "psp_priority": [
                {"psp": "adyen", "priority": 1},
                {"psp": "checkout", "priority": 2},
            ],
        }

    async def fake_fetch_one(query: Any, values: Dict[str, Any] | None = None):
        return None

    async def fake_execute(*args: Any, **kwargs: Any):
        return 1

    async def fake_update_payment_info(**kwargs: Any) -> bool:
        return True

    captured: Dict[str, Any] = {}

    async def fake_create_payment_with_failover(*args: Any, **kwargs: Any):
        captured.update(kwargs)
        return True, SimpleNamespace(
            id="cs_agent_checkout_123",
            client_secret=None,
            status="requires_action",
            redirect_url="https://checkout.stripe.test/cs_agent_checkout_123",
            raw_response={"id": "cs_agent_checkout_123", "object": "checkout.session"},
            psp_type="stripe_checkout",
        ), None, "stripe"

    class _Decision:
        decision = "allow"
        reason_codes = []
        required_scopes = []
        risk_tier = "low"

    monkeypatch.setattr(payment_module, "get_order", fake_get_order)
    monkeypatch.setattr(payment_module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(payment_module.PaymentRoutingService, "select_psp", fake_select_psp)
    monkeypatch.setattr(payment_module, "create_payment_with_failover", fake_create_payment_with_failover)
    monkeypatch.setattr(payment_module, "update_payment_info", fake_update_payment_info)
    monkeypatch.setattr(database_obj, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(database_obj, "execute", fake_execute)
    monkeypatch.setattr(mvp_events, "emit_best_effort", lambda **_: None)
    monkeypatch.setattr(mvp_governance.governance, "evaluate", lambda *_a, **_k: _Decision())
    monkeypatch.setattr(mvp_governance.governance, "record_audit_event", lambda **_: None)

    response = await payment_module.create_payment(
        payment_module.PaymentRequest(
            order_id="ORD_STRIPE_CHECKOUT",
            payment_method=payment_module.PaymentMethod(type="stripe_checkout"),
            return_url="https://agent.pivota.cc/checkout/return?order_id=ORD_STRIPE_CHECKOUT",
            idempotency_key="idem_stripe_checkout_1",
        ),
        BackgroundTasks(),
        context=_Context(),
    )

    assert captured["metadata"]["psp_mode"] == "stripe_checkout"
    assert captured["metadata"]["return_url"] == "https://agent.pivota.cc/checkout/return?order_id=ORD_STRIPE_CHECKOUT"
    assert captured["metadata"]["payment_method_type"] == "stripe_checkout"
    assert captured["preferred_psps"] == ["stripe"]
    assert captured["restrict_to_preferred_psps"] is True
    assert response.payment_intent_id == "cs_agent_checkout_123"
    assert response.payment_action["type"] == "redirect_url"
    assert response.payment_action["url"] == "https://checkout.stripe.test/cs_agent_checkout_123"
    assert response.payment_action["submit_owner"] == "redirect"


@pytest.mark.asyncio
async def test_agent_payments_retries_transient_db_busy_after_psp_without_double_create(
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
            "merchant_id": "merch_test_123",
            "payment_status": "unpaid",
            "total": 25.22,
            "currency": "USD",
            "shipping_address": {
                "country": "US",
                "postal_code": "94105",
                "city": "San Francisco",
                "state": "CA",
            },
            "metadata": _live_quote_metadata(),
        }

    async def fake_get_merchant_onboarding(merchant_id: str) -> Dict[str, Any]:
        return {"merchant_id": merchant_id}

    async def fake_select_psp(self, *, agent_id: str, merchant_id: str, amount: float, currency: str):
        return "stripe", {
            "route_id": "route_test",
            "psp_priority": [{"psp": "stripe", "priority": 1}],
        }

    async def fake_fetch_one(query: Any, values: Dict[str, Any] | None = None):
        return None

    execute_calls = {"payment_insert": 0}

    async def fake_execute(query: Any, values: Dict[str, Any] | None = None):
        query_text = str(query)
        if "INSERT INTO payments" in query_text:
            execute_calls["payment_insert"] += 1
            if execute_calls["payment_insert"] == 1:
                raise RuntimeError("cannot perform operation: another operation is in progress")
        return 1

    async def fake_update_payment_info(**kwargs: Any) -> bool:
        return True

    psp_calls = {"count": 0}

    async def fake_create_payment_with_failover(*args: Any, **kwargs: Any):
        psp_calls["count"] += 1
        return True, SimpleNamespace(
            id="pi_retry_123",
            client_secret="pi_retry_123_secret_456",
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
    monkeypatch.setattr(payment_module.PaymentRoutingService, "select_psp", fake_select_psp)
    monkeypatch.setattr(payment_module, "create_payment_with_failover", fake_create_payment_with_failover)
    monkeypatch.setattr(payment_module, "update_payment_info", fake_update_payment_info)
    monkeypatch.setattr(database_obj, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(database_obj, "execute", fake_execute)
    monkeypatch.setattr(mvp_events, "emit_best_effort", lambda **_: None)
    monkeypatch.setattr(mvp_governance.governance, "evaluate", lambda *_a, **_k: _Decision())
    monkeypatch.setattr(mvp_governance.governance, "record_audit_event", lambda **_: None)

    response = await payment_module.create_payment(
        payment_module.PaymentRequest(
            order_id="ORD_RETRY_1",
            payment_method=payment_module.PaymentMethod(type="dynamic"),
            return_url="https://agent.pivota.cc/order/success?orderId=ORD_RETRY_1&finalizing=1",
            idempotency_key="idem_retry_1",
        ),
        BackgroundTasks(),
        context=_Context(),
    )

    assert response.payment_intent_id == "pi_retry_123"
    assert response.payment_action["type"] == "stripe_client_secret"
    assert psp_calls["count"] == 1
    assert execute_calls["payment_insert"] == 2


@pytest.mark.asyncio
async def test_protocol_order_does_not_reuse_existing_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A protocol-tier (ACP) order with a pre-existing client-confirm surface must
    # NOT reuse it — it has to fall through to the kill-switch + off-session lane.
    # With submit_payment off (test default) the kill-switch blocks → 403, proving
    # the reuse branch was skipped (otherwise it would return 200 with the reused
    # surface and never reach the kill-switch). Calls create_payment directly (no
    # `from main import app`) to avoid the repo-governance startup check.
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
        meta = dict(_live_quote_metadata())
        meta["protocol_name"] = "acp"  # <-- guarded protocol
        return {
            "order_id": order_id,
            "merchant_id": "merch_test_123",
            "payment_status": "processing",
            "total": 1.69,
            "currency": "USD",
            "shipping_address": {"country": "US", "postal_code": "94105", "city": "SF", "state": "CA"},
            "psp_used": "stripe",
            "payment_intent_id": "pi_existing_123",       # an existing surface...
            "client_secret": "pi_existing_123_secret_456",
            "items": [{"product_id": "prod_1", "merchant_id": "merch_test_123"}],
            "metadata": meta,
        }

    async def fake_get_merchant_onboarding(merchant_id: str) -> Dict[str, Any]:
        return {"merchant_id": merchant_id}

    async def fake_select_psp(self, *, agent_id: str, merchant_id: str, amount: float, currency: str):
        return "stripe", {"route_id": "r", "psp_priority": [{"psp": "stripe", "priority": 1}]}

    async def fail_failover(*args: Any, **kwargs: Any):
        raise AssertionError("must not reach PSP for a kill-switch-blocked protocol charge")

    async def fake_fetch_one(*args: Any, **kwargs: Any):
        return None

    class _Decision:
        decision = "allow"; reason_codes = []; required_scopes = []; risk_tier = "low"

    monkeypatch.setattr(payment_module, "get_order", fake_get_order)
    monkeypatch.setattr(payment_module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(payment_module.PaymentRoutingService, "select_psp", fake_select_psp)
    monkeypatch.setattr(payment_module, "create_payment_with_failover", fail_failover)
    monkeypatch.setattr(database_obj, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(mvp_events, "emit_best_effort", lambda **_: None)
    monkeypatch.setattr(mvp_governance.governance, "evaluate", lambda *_a, **_k: _Decision())
    monkeypatch.setattr(mvp_governance.governance, "record_audit_event", lambda **_: None)

    with pytest.raises(payment_module.HTTPException) as exc:
        await payment_module.create_payment(
            payment_module.PaymentRequest(
                order_id="ORD_ACP_1",
                payment_method=payment_module.PaymentMethod(type="dynamic"),
            ),
            BackgroundTasks(),
            context=_Context(),
        )

    # Reuse was skipped → kill-switch blocked the protocol charge (submit off).
    assert exc.value.status_code == 403
    assert exc.value.detail["error"] == "TIER2_CHARGE_DISABLED"
