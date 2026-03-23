import pytest


@pytest.mark.asyncio
async def test_resolve_refund_adapter_prefers_canonical_merchant_psps(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.refund_api as module

    queries = []

    async def fake_fetch_one(query: str, values=None):
        queries.append(" ".join(query.split()))
        if "FROM merchant_psps" in query:
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
        return None

    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)

    psp_type, psp_key, adapter_kwargs = await module._resolve_refund_adapter(
        {
            "merchant_id": "merch_1",
            "psp_id": "psp_checkout_live",
            "psp_used": "checkout",
            "payment_intent_id": "pay_123",
        },
        {
            "merchant_id": "merch_1",
            "psp_type": "stripe",
            "psp_sandbox_key": "sk_legacy",
        },
    )

    assert psp_type == "checkout"
    assert psp_key == "sk_live_checkout"
    assert adapter_kwargs["environment"] == "live"
    assert adapter_kwargs["processing_channel_id"] == "pc_live_123"
    assert adapter_kwargs["public_key"] == "pk_live_123"
    assert any("FROM merchant_psps" in query for query in queries)
