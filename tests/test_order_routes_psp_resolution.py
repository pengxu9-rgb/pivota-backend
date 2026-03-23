from __future__ import annotations

import pytest


def test_normalize_order_provider_hint_ignores_non_provider_preference() -> None:
    import routes.order_routes as module

    assert module._normalize_order_provider_hint(None, "stripe_checkout") is None
    assert module._normalize_order_provider_hint("adyen", "stripe_checkout") == "adyen"


def test_finalize_order_psp_used_uses_safe_fallback() -> None:
    import routes.order_routes as module

    assert module._finalize_order_psp_used(None, "checkout") == "checkout"
    assert module._finalize_order_psp_used("", None) == "unknown"


@pytest.mark.asyncio
async def test_resolve_active_order_psp_falls_back_to_first_active(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.order_routes as module

    calls = []

    async def fake_fetch_one(query: str, values=None):
        calls.append((" ".join(query.split()), dict(values or {})))
        if values and values.get("provider") == "stripe":
            return None
        return {"provider": "checkout", "psp_id": "psp_checkout_live"}

    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)

    provider, psp_id = await module._resolve_active_order_psp("merch_1", "stripe")

    assert provider == "checkout"
    assert psp_id == "psp_checkout_live"
    assert len(calls) == 2
    assert calls[0][1] == {"merchant_id": "merch_1", "provider": "stripe"}
    assert calls[1][1] == {"merchant_id": "merch_1"}


@pytest.mark.asyncio
async def test_resolve_order_psp_adapter_uses_canonical_row(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.order_routes as module

    captured = {}
    adapter_sentinel = object()

    async def fake_fetch_one(query: str, values=None):
        normalized = " ".join(query.split())
        if "psp_id = :psp_id" in normalized:
            return {
                "provider": "stripe",
                "api_key": "sk_live_123",
                "account_id": "acct_live_123",
                "secret_key": None,
                "environment": "live",
                "provider_config": {"mode": "payment_intent"},
            }
        return None

    def fake_get_psp_adapter(provider: str, api_key: str, **kwargs):
        captured["provider"] = provider
        captured["api_key"] = api_key
        captured["kwargs"] = kwargs
        return adapter_sentinel

    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(module, "get_psp_adapter", fake_get_psp_adapter)

    provider, adapter = await module._resolve_order_psp_adapter(
        {
            "merchant_id": "merch_1",
            "psp_id": "psp_stripe_live",
            "psp_used": "stripe",
        }
    )

    assert provider == "stripe"
    assert adapter is adapter_sentinel
    assert captured["provider"] == "stripe"
    assert captured["api_key"] == "sk_live_123"
    assert captured["kwargs"]["mode"] == "payment_intent"
    assert captured["kwargs"]["environment"] == "live"
    assert captured["kwargs"]["account_id"] == "acct_live_123"
