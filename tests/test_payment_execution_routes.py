import pytest
from fastapi import HTTPException


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
async def test_execute_payment_falls_back_to_next_processor(monkeypatch: pytest.MonkeyPatch) -> None:
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
                {"provider": "stripe", "api_key": "sk_test_x"},
                {"provider": "adyen", "api_key": "adyen_test_x", "account_id": "AdyenMerchant"},
            ],
            {"psp_priority": [{"psp": "stripe", "priority": 1}, {"psp": "adyen", "priority": 2}]},
        )

    async def fake_stripe(stripe_key, merchant, payment_data):
        return {
            "success": False,
            "payment_id": "failed_stripe",
            "status": "failed",
            "transaction_id": None,
            "error_message": "Stripe declined",
        }

    async def fake_adyen(adyen_key, merchant_account, payment_data):
        assert merchant_account == "AdyenMerchant"
        return {
            "success": True,
            "payment_id": "adyen_payment_1",
            "status": "completed",
            "transaction_id": "adyen_payment_1",
            "error_message": None,
        }

    emitted = []

    async def fake_emit(merchant_id: str, *, event_type: str, payment_request, result, psp_used: str):
        emitted.append((merchant_id, event_type, psp_used))

    monkeypatch.setattr(module, "verify_merchant_api_key", fake_verify)
    monkeypatch.setattr(module, "_resolve_payment_candidates", fake_candidates)
    monkeypatch.setattr(module, "execute_stripe_payment", fake_stripe)
    monkeypatch.setattr(module, "execute_adyen_payment", fake_adyen)
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
    assert response.payment_id == "adyen_payment_1"
    assert emitted == [("merch_test_payment", "payment.completed", "adyen")]


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
                {"provider": "stripe", "api_key": "sk_test_x"},
                {"provider": "checkout", "api_key": "cko_test_x"},
            ],
            {"psp_priority": [{"psp": "stripe", "priority": 1}, {"psp": "checkout", "priority": 2}]},
        )

    async def fake_stripe(stripe_key, merchant, payment_data):
        return {
            "success": False,
            "payment_id": "failed_stripe",
            "status": "failed",
            "transaction_id": None,
            "error_message": "Stripe declined",
        }

    emitted = []

    async def fake_emit(merchant_id: str, *, event_type: str, payment_request, result, psp_used: str):
        emitted.append((merchant_id, event_type, psp_used))

    monkeypatch.setattr(module, "verify_merchant_api_key", fake_verify)
    monkeypatch.setattr(module, "_resolve_payment_candidates", fake_candidates)
    monkeypatch.setattr(module, "execute_stripe_payment", fake_stripe)
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
    assert response.psp_used == "stripe"
    assert response.error_message == "Stripe declined"
    assert emitted == [("merch_test_payment", "payment.failed", "stripe")]
