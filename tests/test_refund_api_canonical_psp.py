import pytest


class _FakeRefundAdapter:
    async def refund_payment(self, **kwargs):
        return True, "re_api_contract", None


def test_shopify_external_refund_cancel_payload_is_cancel_only() -> None:
    import routes.refund_api as module

    payload = module._shopify_external_refund_cancel_payload(
        reason="Agent requested refund",
        restore_inventory=True,
    )

    assert payload == {
        "reason": "other",
        "email": False,
        "refund": False,
        "restock": True,
    }
    assert "amount" not in payload
    assert "currency" not in payload


def test_shopify_external_refund_cancel_payload_allows_shopify_reason() -> None:
    import routes.refund_api as module

    assert module._shopify_external_refund_cancel_payload(
        reason="customer",
        restore_inventory=False,
    ) == {
        "reason": "customer",
        "email": False,
        "refund": False,
        "restock": False,
    }


@pytest.mark.asyncio
async def test_resolve_refund_adapter_prefers_canonical_merchant_psps(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.refund_api as module

    calls = []

    async def fake_fetch_active_runtime_merchant_psp(**kwargs):
        calls.append(kwargs)
        return {
            "psp_id": "psp_checkout_live",
            "provider": "checkout",
            "runtime_secret_key": "sk_live_checkout",
            "environment": "live",
            "provider_config": {
                "processing_channel_id": "pc_live_123",
                "public_key": "pk_live_123",
            },
        }

    monkeypatch.setattr(module, "fetch_active_runtime_merchant_psp", fake_fetch_active_runtime_merchant_psp)

    psp_type, psp_key, adapter_kwargs = await module._resolve_refund_adapter(
        {
            "merchant_id": "merch_1",
            "psp_id": "psp_checkout_live",
            "psp_used": "checkout",
            "payment_intent_id": "pay_123",
        },
    )

    assert psp_type == "checkout"
    assert psp_key == "sk_live_checkout"
    assert adapter_kwargs["environment"] == "live"
    assert adapter_kwargs["processing_channel_id"] == "pc_live_123"
    assert adapter_kwargs["public_key"] == "pk_live_123"
    assert calls == [
        {
            "merchant_id": "merch_1",
            "provider": "checkout",
            "psp_id": "psp_checkout_live",
        }
    ]


@pytest.mark.asyncio
async def test_resolve_refund_adapter_rejects_legacy_psp_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.refund_api as module

    async def fake_fetch_active_runtime_merchant_psp(**kwargs):
        return None

    monkeypatch.setattr(module, "fetch_active_runtime_merchant_psp", fake_fetch_active_runtime_merchant_psp)

    with pytest.raises(ValueError, match="Canonical merchant_psps configuration is missing for stripe refunds"):
        await module._resolve_refund_adapter(
            {
                "merchant_id": "merch_1",
                "psp_used": "stripe",
                "payment_intent_id": "pi_123",
            }
        )


@pytest.mark.asyncio
async def test_process_refund_returns_partial_failure_when_finalize_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import BackgroundTasks, Response, status
    import routes.refund_api as module

    async def fake_get_order(order_id: str):
        return {
            "order_id": order_id,
            "merchant_id": "merch_1",
            "payment_status": "paid",
            "total": "20.00",
            "total_refunded": "0.00",
            "currency": "USD",
            "payment_intent_id": "pi_api_contract",
            "psp_used": "stripe",
            "psp_id": "psp_stripe_1",
            "metadata": {},
        }

    async def fake_get_primary_store(merchant_id: str):
        return None

    async def fake_get_merchant_onboarding(merchant_id: str):
        return {"merchant_id": merchant_id}

    async def fake_resolve_refund_adapter(order):
        return "stripe", "sk_test", {}

    async def fail_finalize_refund_success(*args, **kwargs):
        raise RuntimeError("local finalization unavailable")

    async def fake_emit_merchant_webhook_event(*args, **kwargs):
        return None

    monkeypatch.setattr(module, "get_order", fake_get_order)
    monkeypatch.setattr(module, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(module, "_resolve_refund_adapter", fake_resolve_refund_adapter)
    monkeypatch.setattr(module, "get_psp_adapter", lambda *args, **kwargs: _FakeRefundAdapter())
    monkeypatch.setattr(module, "finalize_refund_success", fail_finalize_refund_success)
    monkeypatch.setattr(module, "emit_merchant_webhook_event", fake_emit_merchant_webhook_event)

    response = Response()
    result = await module.process_refund(
        "ORD_REFUND_API_PARTIAL_FAIL",
        module.RefundRequest(order_id="ORD_REFUND_API_PARTIAL_FAIL", amount=12.0, reason="customer"),
        BackgroundTasks(),
        response=response,
        current_user={"user_id": "admin"},
    )

    assert response.status_code == status.HTTP_207_MULTI_STATUS
    assert result == {
        "status": "partial_failure",
        "refund_id": "re_api_contract",
        "psp_refund_id": "re_api_contract",
        "manual_reconciliation_required": True,
        "error": "local finalization unavailable",
    }


@pytest.mark.asyncio
async def test_process_refund_returns_success_when_finalize_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import BackgroundTasks, Response, status
    import routes.refund_api as module

    finalize_calls = []

    async def fake_get_order(order_id: str):
        return {
            "order_id": order_id,
            "merchant_id": "merch_1",
            "payment_status": "paid",
            "total": "20.00",
            "total_refunded": "0.00",
            "currency": "USD",
            "payment_intent_id": "pi_api_contract",
            "psp_used": "stripe",
            "psp_id": "psp_stripe_1",
            "metadata": {},
        }

    async def fake_get_primary_store(merchant_id: str):
        return None

    async def fake_get_merchant_onboarding(merchant_id: str):
        return {"merchant_id": merchant_id}

    async def fake_resolve_refund_adapter(order):
        return "stripe", "sk_test", {}

    async def fake_finalize_refund_success(*args, **kwargs):
        finalize_calls.append({"args": args, "kwargs": kwargs})
        return {"applied": True}

    async def fake_emit_merchant_webhook_event(*args, **kwargs):
        return None

    monkeypatch.setattr(module, "get_order", fake_get_order)
    monkeypatch.setattr(module, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(module, "_resolve_refund_adapter", fake_resolve_refund_adapter)
    monkeypatch.setattr(module, "get_psp_adapter", lambda *args, **kwargs: _FakeRefundAdapter())
    monkeypatch.setattr(module, "finalize_refund_success", fake_finalize_refund_success)
    monkeypatch.setattr(module, "emit_merchant_webhook_event", fake_emit_merchant_webhook_event)

    response = Response()
    result = await module.process_refund(
        "ORD_REFUND_API_SUCCESS",
        module.RefundRequest(order_id="ORD_REFUND_API_SUCCESS", amount=12.0, reason="customer"),
        BackgroundTasks(),
        response=response,
        current_user={"user_id": "admin"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert result["status"] == "success"
    assert result["refund_id"] == "re_api_contract"
    assert finalize_calls
