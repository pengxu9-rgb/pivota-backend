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


def test_resolve_order_live_readiness_requirement_defaults_to_true() -> None:
    import routes.order_routes as module

    assert module._resolve_order_live_readiness_requirement(None) is True
    assert module._resolve_order_live_readiness_requirement({}) is True


def test_resolve_order_live_readiness_requirement_honors_explicit_test_override() -> None:
    import routes.order_routes as module

    assert (
        module._resolve_order_live_readiness_requirement({"enforce_live_readiness": False})
        is False
    )
    assert (
        module._resolve_order_live_readiness_requirement({"allow_test_psp_surfaces": True})
        is False
    )


@pytest.mark.asyncio
async def test_resolve_active_order_psp_falls_back_to_first_active(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.order_routes as module

    async def fake_fetch_active_runtime_merchant_psp(*, merchant_id: str, provider=None, psp_id=None):
        assert merchant_id == "merch_1"
        assert provider == "stripe"
        assert psp_id is None
        return {"provider": "checkout", "psp_id": "psp_checkout_live"}

    monkeypatch.setattr(
        module,
        "fetch_active_runtime_merchant_psp",
        fake_fetch_active_runtime_merchant_psp,
    )

    provider, psp_id = await module._resolve_active_order_psp("merch_1", "stripe")

    assert provider == "checkout"
    assert psp_id == "psp_checkout_live"


@pytest.mark.asyncio
async def test_resolve_order_psp_adapter_uses_canonical_row(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.order_routes as module

    captured = {}
    adapter_sentinel = object()

    async def fake_fetch_active_runtime_merchant_psp(*, merchant_id: str, provider=None, psp_id=None):
        assert merchant_id == "merch_1"
        assert provider == "stripe"
        assert psp_id == "psp_stripe_live"
        return {
            "provider": "stripe",
            "api_key": "sk_live_123",
            "account_id": "acct_live_123",
            "secret_key": None,
            "environment": "live",
            "provider_config": {"mode": "payment_intent"},
        }

    def fake_get_psp_adapter(provider: str, api_key: str, **kwargs):
        captured["provider"] = provider
        captured["api_key"] = api_key
        captured["kwargs"] = kwargs
        return adapter_sentinel

    monkeypatch.setattr(module, "fetch_active_runtime_merchant_psp", fake_fetch_active_runtime_merchant_psp)
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
