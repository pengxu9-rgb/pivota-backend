import pytest


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
