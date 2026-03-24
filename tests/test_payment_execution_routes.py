import pytest
from fastapi import HTTPException
from starlette.requests import Request
from typing import List, Optional, Tuple


@pytest.mark.asyncio
async def test_resolve_payment_candidates_respects_route_subset(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.payment_execution_routes as module

    async def fake_load_active_merchant_psps(merchant_id: str):
        assert merchant_id == "merch_test_payment"
        return [
            {"provider": "adyen", "api_key": "adyen_test", "account_id": "AdyenMerchant"},
            {"provider": "stripe", "api_key": "sk_test_x"},
            {"provider": "checkout", "api_key": "cko_test_x"},
        ]

    class FakeRoutingService:
        def __init__(self, database):
            self.database = database

        async def select_psp(self, agent_id, merchant_id=None, amount=0, currency="USD"):
            return (
                "adyen",
                {"psp_priority": [{"psp": "adyen", "priority": 1}]},
            )

    monkeypatch.setattr(module, "_load_active_merchant_psps", fake_load_active_merchant_psps)
    monkeypatch.setattr(module, "PaymentRoutingService", FakeRoutingService)

    candidates, route_config = await module._resolve_payment_candidates(
        {"merchant_id": "merch_test_payment"},
        module.PaymentExecuteRequest(amount=1000, currency="USD", order_id="ord_subset"),
    )

    assert route_config["psp_priority"] == [{"psp": "adyen", "priority": 1}]
    assert [candidate["provider"] for candidate in candidates] == ["adyen"]


@pytest.mark.asyncio
async def test_resolve_payment_candidates_does_not_fallback_to_legacy_payment_router_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.payment_execution_routes as module

    async def fake_load_active_merchant_psps(merchant_id: str):
        assert merchant_id == "merch_test_payment"
        return []

    class FakeRoutingService:
        def __init__(self, database):
            self.database = database

        async def select_psp(self, agent_id, merchant_id=None, amount=0, currency="USD"):
            return (None, {})

    monkeypatch.setattr(module, "_load_active_merchant_psps", fake_load_active_merchant_psps)
    monkeypatch.setattr(module, "PaymentRoutingService", FakeRoutingService)

    candidates, route_config = await module._resolve_payment_candidates(
        {"merchant_id": "merch_test_payment"},
        module.PaymentExecuteRequest(amount=1000, currency="USD", order_id="ord_no_fallback"),
    )

    assert candidates == []
    assert route_config == {}


@pytest.mark.asyncio
async def test_execute_payment_returns_unified_action(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.payment_execution_routes as module

    async def fake_verify(api_key: str):
        assert api_key == "merchant_test_key"
        return {
            "merchant_id": "merch_test_payment",
            "business_name": "Glow Commerce",
            "status": "approved",
            "psp_connected": True,
        }

    async def fake_candidates(merchant, payment_request):
        return (
            [
                {
                    "provider": "adyen",
                    "api_key": "live_adyen_key",
                    "status": "active",
                    "account_id": "AdyenMerchant",
                    "environment": "live",
                    "provider_config": {"merchant_account": "AdyenMerchant", "client_key": "pub_123"},
                    "validation_status": "valid",
                },
                {
                    "provider": "stripe",
                    "api_key": "sk_live_x",
                    "status": "active",
                    "environment": "live",
                    "provider_config": {"mode": "payment_intent"},
                    "validation_status": "valid",
                },
            ],
            {"route_id": "route_1", "psp_priority": [{"psp": "adyen", "priority": 1}, {"psp": "stripe", "priority": 2}]},
        )

    async def fake_initiate(**kwargs):
        assert kwargs["merchant_id"] == "merch_test_payment"
        assert kwargs["preferred_psps"] == ["adyen", "stripe"]
        assert kwargs["candidates"][0]["provider"] == "adyen"
        return {
            "success": True,
            "payment_id": "adyen_session_1",
            "status": "requires_action",
            "transaction_id": "adyen_txn_1",
            "psp_used": "adyen",
            "requires_customer_action": True,
            "payment_action": {
                "type": "adyen_session",
                "client_secret": "session_data_1",
                "session_data": "session_data_1",
                "client_key": "pub_123",
                "raw": {},
            },
            "error_message": None,
        }

    emitted = []

    async def fake_emit(merchant_id: str, *, event_type: str, payment_request, result, psp_used: str):
        emitted.append((merchant_id, event_type, psp_used, result["payment_id"]))

    monkeypatch.setattr(module, "verify_merchant_api_key", fake_verify)
    monkeypatch.setattr(module, "_resolve_payment_candidates", fake_candidates)
    monkeypatch.setattr(module, "initiate_merchant_payment", fake_initiate)
    monkeypatch.setattr(module, "_emit_payment_webhook_best_effort", fake_emit)

    response = await module.execute_payment(
        module.PaymentExecuteRequest(
            amount=1000,
            currency="USD",
            order_id="ord_1001",
            customer_email="merchant@example.com",
        ),
        x_merchant_api_key="merchant_test_key",
    )

    assert response.success is True
    assert response.psp_used == "adyen"
    assert response.requires_customer_action is True
    assert response.payment_action["type"] == "adyen_session"
    assert emitted == []


@pytest.mark.asyncio
async def test_execute_payment_emits_completed_only_for_terminal_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.payment_execution_routes as module

    async def fake_verify(api_key: str):
        return {
            "merchant_id": "merch_test_payment",
            "business_name": "Glow Commerce",
            "status": "approved",
        }

    async def fake_candidates(merchant, payment_request):
        return (
            [
                {
                    "provider": "stripe",
                    "api_key": "sk_live_x",
                    "status": "active",
                    "environment": "live",
                    "provider_config": {"mode": "payment_intent"},
                    "validation_status": "valid",
                }
            ],
            {"route_id": "route_terminal_success", "psp_priority": [{"psp": "stripe", "priority": 1}]},
        )

    async def fake_initiate(**kwargs):
        return {
            "success": True,
            "payment_id": "pi_live_terminal",
            "status": "succeeded",
            "transaction_id": "pi_live_terminal",
            "psp_used": "stripe",
            "requires_customer_action": False,
            "payment_action": None,
            "error_message": None,
        }

    emitted = []

    async def fake_emit(merchant_id: str, *, event_type: str, payment_request, result, psp_used: str):
        emitted.append((merchant_id, event_type, psp_used, result["payment_id"]))

    monkeypatch.setattr(module, "verify_merchant_api_key", fake_verify)
    monkeypatch.setattr(module, "_resolve_payment_candidates", fake_candidates)
    monkeypatch.setattr(module, "initiate_merchant_payment", fake_initiate)
    monkeypatch.setattr(module, "_emit_payment_webhook_best_effort", fake_emit)

    response = await module.execute_payment(
        module.PaymentExecuteRequest(
            amount=1000,
            currency="USD",
            order_id="ord_terminal_success",
            customer_email="merchant@example.com",
        ),
        x_merchant_api_key="merchant_test_key",
    )

    assert response.success is True
    assert response.psp_used == "stripe"
    assert response.requires_customer_action is False
    assert emitted == [("merch_test_payment", "payment.completed", "stripe", "pi_live_terminal")]


@pytest.mark.asyncio
async def test_execute_payment_errors_when_no_supported_processors(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.payment_execution_routes as module

    async def fake_verify(api_key: str):
        return {
            "merchant_id": "merch_test_payment",
            "business_name": "Glow Commerce",
            "status": "approved",
            "psp_connected": True,
        }

    async def fake_candidates(merchant, payment_request):
        return ([], {})

    monkeypatch.setattr(module, "verify_merchant_api_key", fake_verify)
    monkeypatch.setattr(module, "_resolve_payment_candidates", fake_candidates)

    with pytest.raises(HTTPException) as exc:
        await module.execute_payment(
            module.PaymentExecuteRequest(
                amount=1000,
                currency="USD",
                order_id="ord_1001",
            ),
            x_merchant_api_key="merchant_test_key",
        )

    assert exc.value.status_code == 400
    assert "Payment routing is not configured" in exc.value.detail


@pytest.mark.asyncio
async def test_execute_payment_failure_reports_last_attempted_supported_processor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.payment_execution_routes as module

    async def fake_verify(api_key: str):
        return {
            "merchant_id": "merch_test_payment",
            "business_name": "Glow Commerce",
            "status": "approved",
            "psp_connected": True,
        }

    async def fake_candidates(merchant, payment_request):
        return (
            [
                {
                    "provider": "stripe",
                    "api_key": "sk_live_x",
                    "status": "active",
                    "environment": "live",
                    "provider_config": {"mode": "payment_intent"},
                    "validation_status": "valid",
                },
                {
                    "provider": "checkout",
                    "api_key": "sk_live_checkout",
                    "status": "active",
                    "environment": "live",
                    "account_id": "pc_live_123",
                    "provider_config": {
                        "processing_channel_id": "pc_live_123",
                        "public_key": "pk_live_123",
                    },
                    "validation_status": "valid",
                },
            ],
            {"psp_priority": [{"psp": "stripe", "priority": 1}, {"psp": "checkout", "priority": 2}]},
        )

    async def fake_initiate(**kwargs):
        assert kwargs["preferred_psps"] == ["stripe", "checkout"]
        return {
            "success": False,
            "payment_id": "",
            "status": "failed",
            "transaction_id": None,
            "psp_used": "checkout",
            "requires_customer_action": False,
            "payment_action": None,
            "error_message": "Checkout API error",
        }

    emitted = []

    async def fake_emit(merchant_id: str, *, event_type: str, payment_request, result, psp_used: str):
        emitted.append((merchant_id, event_type, psp_used))

    monkeypatch.setattr(module, "verify_merchant_api_key", fake_verify)
    monkeypatch.setattr(module, "_resolve_payment_candidates", fake_candidates)
    monkeypatch.setattr(module, "initiate_merchant_payment", fake_initiate)
    monkeypatch.setattr(module, "_emit_payment_webhook_best_effort", fake_emit)

    response = await module.execute_payment(
        module.PaymentExecuteRequest(
            amount=1000,
            currency="USD",
            order_id="ord_1002",
            customer_email="merchant@example.com",
        ),
        x_merchant_api_key="merchant_test_key",
    )

    assert response.success is False
    assert response.psp_used == "checkout"
    assert response.error_message == "Checkout API error"
    assert response.payment_id.startswith("failed_")
    assert emitted == [("merch_test_payment", "payment.failed", "checkout")]


@pytest.mark.asyncio
async def test_execute_payment_fail_closed_when_processors_are_not_live_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.payment_execution_routes as module

    async def fake_verify(api_key: str):
        return {
            "merchant_id": "merch_test_payment",
            "business_name": "Glow Commerce",
            "status": "approved",
        }

    async def fake_candidates(merchant, payment_request):
        return (
            [
                {
                    "provider": "adyen",
                    "api_key": "live_adyen_key",
                    "status": "active",
                    "account_id": "WoopayECOM",
                    "provider_config": {"merchant_account": "WoopayECOM"},
                    "environment": "unknown",
                    "validation_status": "unknown",
                }
            ],
            {"psp_priority": [{"psp": "adyen", "priority": 1}]},
        )

    monkeypatch.setattr(module, "verify_merchant_api_key", fake_verify)
    monkeypatch.setattr(module, "_resolve_payment_candidates", fake_candidates)

    with pytest.raises(HTTPException) as exc:
        await module.execute_payment(
            module.PaymentExecuteRequest(
                amount=1000,
                currency="USD",
                order_id="ord_blocked",
            ),
            x_merchant_api_key="merchant_test_key",
        )

    assert exc.value.status_code == 400
    assert "No supported live-ready PSPs" in exc.value.detail
    assert "Adyen client key is missing" in exc.value.detail


def _build_request(path: str, headers: Optional[List[Tuple[bytes, bytes]]] = None) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "scheme": "https",
        "path": path,
        "headers": headers or [],
        "query_string": b"",
        "client": ("127.0.0.1", 12345),
        "server": ("api.pivota.cc", 443),
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_execute_internal_payment_canary_uses_internal_key_and_skips_live_gating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.payment_execution_routes as module

    async def fake_load_merchant(merchant_id: str):
        assert merchant_id == "merch_test_payment"
        return {
            "merchant_id": merchant_id,
            "business_name": "Glow Commerce",
            "status": "approved",
        }

    async def fake_candidates(merchant, payment_request):
        return (
            [
                {
                    "provider": "adyen",
                    "api_key": "test_adyen_key",
                    "status": "active",
                    "environment": "test",
                    "account_id": "WoopayTest",
                    "provider_config": {
                        "merchant_account": "WoopayTest",
                        "client_key": "test_client_key",
                    },
                    "validation_status": "valid",
                }
            ],
            {"route_id": "route_test", "psp_priority": [{"psp": "adyen", "priority": 1}]},
        )

    async def fake_initiate(**kwargs):
        assert kwargs["merchant_id"] == "merch_test_payment"
        assert kwargs["preferred_psps"] == ["adyen"]
        assert kwargs["enforce_live_readiness"] is False
        assert kwargs["metadata"]["source"] == "ops_psp_canary_harness"
        return {
            "success": True,
            "payment_id": "adyen_session_test",
            "status": "requires_action",
            "transaction_id": "adyen_txn_test",
            "psp_used": "adyen",
            "requires_customer_action": True,
            "payment_action": {
                "type": "adyen_session",
                "client_secret": "session_test",
                "session_data": "session_test",
                "client_key": "test_client_key",
                "raw": {},
            },
            "error_message": None,
        }

    emitted = []

    async def fake_emit(*args, **kwargs):
        emitted.append((args, kwargs))

    monkeypatch.setattr(module.settings, "readiness_internal_api_key", "internal_test_key", raising=False)
    monkeypatch.setattr(module, "_load_canary_merchant", fake_load_merchant)
    monkeypatch.setattr(module, "_resolve_payment_candidates", fake_candidates)
    monkeypatch.setattr(module, "initiate_merchant_payment", fake_initiate)
    monkeypatch.setattr(module, "_emit_payment_webhook_best_effort", fake_emit)

    response = await module.execute_internal_payment_canary(
        merchant_id="merch_test_payment",
        payment_request=module.InternalPaymentExecuteRequest(
            amount=1000,
            currency="USD",
            order_id="ord_canary_test",
        ),
        request=_build_request("/payment/internal/canary/merchants/merch_test_payment/execute"),
        x_pivota_internal_key="internal_test_key",
    )

    assert response.success is True
    assert response.psp_used == "adyen"
    assert response.payment_action["type"] == "adyen_session"
    assert emitted == []


@pytest.mark.asyncio
async def test_execute_internal_payment_canary_requires_internal_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.payment_execution_routes as module

    monkeypatch.setattr(module.settings, "readiness_internal_api_key", "internal_test_key", raising=False)

    with pytest.raises(HTTPException) as exc:
        await module.execute_internal_payment_canary(
            merchant_id="merch_test_payment",
            payment_request=module.InternalPaymentExecuteRequest(
                amount=1000,
                currency="USD",
                order_id="ord_canary_auth",
            ),
            request=_build_request("/payment/internal/canary/merchants/merch_test_payment/execute"),
            x_pivota_internal_key=None,
        )

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_execute_internal_order_backed_canary_creates_real_order_before_initiation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.payment_execution_routes as module

    async def fake_load_merchant(merchant_id: str):
        assert merchant_id == "merch_test_payment"
        return {
            "merchant_id": merchant_id,
            "business_name": "Glow Commerce",
            "status": "approved",
            "contact_email": "merchant@example.com",
        }

    async def fake_candidates(merchant, payment_request):
        return (
            [
                {
                    "psp_id": "psp_stripe_live",
                    "provider": "stripe",
                    "api_key": "sk_live_x",
                    "status": "active",
                    "environment": "live",
                    "provider_config": {"mode": "payment_intent"},
                    "validation_status": "valid",
                }
            ],
            {"route_id": "route_live_stripe", "psp_priority": [{"psp": "stripe", "priority": 1}]},
        )

    created_orders = []

    async def fake_create_order(order_data):
        created_orders.append(order_data)
        return "ORD_CANARY_LIVE_1"

    async def fake_initiate(**kwargs):
        assert kwargs["merchant_id"] == "merch_test_payment"
        assert kwargs["preferred_psps"] == ["stripe"]
        assert kwargs["metadata"]["order_id"] == "ORD_CANARY_LIVE_1"
        assert kwargs["metadata"]["ops_canary"] is True
        assert kwargs["metadata"]["skip_platform_order_creation"] is True
        assert kwargs["metadata"]["source"] == "ops_order_backed_canary"
        assert kwargs["enforce_live_readiness"] is True
        return {
            "success": True,
            "payment_id": "cs_live_test_order_backed",
            "status": "requires_action",
            "transaction_id": "cs_live_test_order_backed",
            "psp_used": "stripe",
            "requires_customer_action": True,
            "payment_action": {
                "type": "redirect_url",
                "url": "https://checkout.stripe.test/session",
                "raw": {},
            },
            "error_message": None,
        }

    payment_updates = []

    async def fake_update_payment_info(order_id, payment_intent_id, client_secret, payment_status="processing", psp_used=None):
        payment_updates.append(
            {
                "order_id": order_id,
                "payment_intent_id": payment_intent_id,
                "client_secret": client_secret,
                "payment_status": payment_status,
                "psp_used": psp_used,
            }
        )
        return True

    emitted = []

    async def fake_emit(*args, **kwargs):
        emitted.append((args, kwargs))

    monkeypatch.setattr(module.settings, "readiness_internal_api_key", "internal_test_key", raising=False)
    monkeypatch.setattr(module, "_load_canary_merchant", fake_load_merchant)
    monkeypatch.setattr(module, "_resolve_payment_candidates", fake_candidates)
    monkeypatch.setattr(module, "create_order", fake_create_order)
    monkeypatch.setattr(module, "initiate_merchant_payment", fake_initiate)
    monkeypatch.setattr(module, "update_payment_info", fake_update_payment_info)
    monkeypatch.setattr(module, "_emit_payment_webhook_best_effort", fake_emit)

    response = await module.execute_internal_order_backed_canary(
        merchant_id="merch_test_payment",
        payment_request=module.InternalOrderBackedCanaryRequest(
            amount=100,
            currency="USD",
            order_id="requested_canary_ref",
            customer_email="merchant@example.com",
            enforce_live_readiness=True,
        ),
        request=_build_request("/payment/internal/canary/merchants/merch_test_payment/order-backed/execute"),
        x_pivota_internal_key="internal_test_key",
    )

    assert response.success is True
    assert response.order_id == "ORD_CANARY_LIVE_1"
    assert response.psp_used == "stripe"
    assert response.payment_action["type"] == "redirect_url"
    assert created_orders[0]["merchant_id"] == "merch_test_payment"
    assert created_orders[0]["psp_id"] == "psp_stripe_live"
    assert created_orders[0]["metadata"]["requested_order_id"] == "requested_canary_ref"
    assert payment_updates == [
        {
            "order_id": "ORD_CANARY_LIVE_1",
            "payment_intent_id": "cs_live_test_order_backed",
            "client_secret": "https://checkout.stripe.test/session",
            "payment_status": "awaiting_payment",
            "psp_used": "stripe",
        }
    ]
    assert emitted == []
