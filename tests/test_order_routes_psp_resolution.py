from __future__ import annotations

import pytest


def test_normalize_order_provider_hint_ignores_non_provider_preference() -> None:
    import routes.order_routes as module

    assert module._normalize_order_provider_hint(None, "stripe_checkout") is None
    assert module._normalize_order_provider_hint("adyen", "stripe_checkout") == "adyen"


def test_normalize_order_provider_hint_prefers_explicit_provider_override() -> None:
    import routes.order_routes as module

    assert module._normalize_order_provider_hint("stripe", "adyen") == "adyen"


def test_build_order_preferred_psps_treats_explicit_provider_as_strict_subset() -> None:
    import routes.order_routes as module

    assert module._build_order_preferred_psps(
        {
            "psp_priority": [
                {"psp": "stripe", "priority": 1},
                {"psp": "adyen", "priority": 2},
                {"psp": "checkout", "priority": 3},
            ]
        },
        "adyen",
    ) == ["adyen"]


def test_build_order_preferred_psps_ignores_stripe_checkout_as_provider_override() -> None:
    import routes.order_routes as module

    assert module._build_order_preferred_psps(
        {"psp_priority": [{"psp": "stripe", "priority": 1}]},
        "stripe_checkout",
    ) == ["stripe"]


@pytest.mark.asyncio
async def test_ensure_explicit_preferred_psp_available_rejects_non_live_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module
    from fastapi import HTTPException

    async def fake_fetch_active_merchant_psps(*, merchant_id: str, provider=None):
        assert merchant_id == "merch_1"
        assert provider == "adyen"
        return [
            {
                "provider": "adyen",
                "status": "active",
                "api_key": "AQE_test_123",
                "account_id": "WoopayECOM",
                "provider_config": {"client_key": "test_client_key"},
                "environment": "test",
                "validation_status": "valid",
                "validation_error": None,
            }
        ]

    monkeypatch.setattr(module, "fetch_active_merchant_psps", fake_fetch_active_merchant_psps)

    with pytest.raises(HTTPException) as exc:
        await module._ensure_explicit_preferred_psp_available(
            merchant_id="merch_1",
            preferred_psp="adyen",
            enforce_live_readiness=True,
        )

    assert exc.value.status_code == 409
    detail = exc.value.detail
    assert detail["error"] == "PREFERRED_PSP_UNAVAILABLE"
    assert detail["preferred_psp"] == "adyen"


def test_finalize_order_psp_used_uses_safe_fallback() -> None:
    import routes.order_routes as module

    assert module._finalize_order_psp_used(None, "checkout") == "checkout"
    assert module._finalize_order_psp_used("", None) == "unknown"


def test_resolve_order_live_readiness_requirement_defaults_to_true() -> None:
    import routes.order_routes as module

    assert module._resolve_order_live_readiness_requirement(None) is True
    assert module._resolve_order_live_readiness_requirement({}) is True


def test_resolve_order_live_readiness_requirement_ignores_ungated_test_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The metadata bypass is set by EXTERNAL callers (gateway forwards order.metadata), so without the
    server-side probe flag + merchant allowlist it MUST be ignored (closes the prior ungated hole)."""
    import routes.order_routes as module

    monkeypatch.delenv("ALLOW_TEST_PSP_PROBE", raising=False)
    monkeypatch.delenv("TEST_PSP_PROBE_MERCHANTS", raising=False)
    assert module._resolve_order_live_readiness_requirement({"enforce_live_readiness": False}) is True
    assert module._resolve_order_live_readiness_requirement({"allow_test_psp_surfaces": True}) is True
    # even with the flag on, a NON-allowlisted merchant cannot bypass
    monkeypatch.setenv("ALLOW_TEST_PSP_PROBE", "1")
    monkeypatch.setenv("TEST_PSP_PROBE_MERCHANTS", "merch_other")
    assert (
        module._resolve_order_live_readiness_requirement(
            {"allow_test_psp_surfaces": True}, merchant_id="merch_efbc46b4619cfbdf"
        )
        is True
    )


def test_resolve_order_live_readiness_requirement_scoped_probe_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bypass is honored ONLY for an allowlisted merchant while the probe flag is on."""
    import routes.order_routes as module

    monkeypatch.setenv("ALLOW_TEST_PSP_PROBE", "1")
    monkeypatch.setenv("TEST_PSP_PROBE_MERCHANTS", "merch_efbc46b4619cfbdf, merch_two")
    # ALL bypass aliases, as bool AND string token, drive a bypass for an allowlisted merchant (Codex P3)
    for meta in (
        {"enforce_live_readiness": False},
        {"enforce_live_readiness": "false"},
        {"allow_test_psp_surfaces": True},
        {"allow_test_psp_surfaces": "true"},
        {"test_psp_surfaces": True},
        {"allow_test_processors": True},
    ):
        assert (
            module._resolve_order_live_readiness_requirement(meta, merchant_id="merch_efbc46b4619cfbdf")
            is False
        ), meta
    # merchant match tolerates surrounding case/whitespace
    assert (
        module._resolve_order_live_readiness_requirement(
            {"allow_test_psp_surfaces": True}, merchant_id="  MERCH_efbc46b4619cfbdf  "
        )
        is False
    )
    # an explicit request to ENFORCE (stricter) is always honored, even for an allowlisted merchant
    assert (
        module._resolve_order_live_readiness_requirement(
            {"enforce_live_readiness": True}, merchant_id="merch_efbc46b4619cfbdf"
        )
        is True
    )
    # flag OFF → even an allowlisted merchant cannot bypass
    monkeypatch.setenv("ALLOW_TEST_PSP_PROBE", "0")
    assert (
        module._resolve_order_live_readiness_requirement(
            {"allow_test_psp_surfaces": True}, merchant_id="merch_efbc46b4619cfbdf"
        )
        is True
    )
    # flag ON but EMPTY/whitespace allowlist → no merchant can bypass
    monkeypatch.setenv("ALLOW_TEST_PSP_PROBE", "1")
    monkeypatch.setenv("TEST_PSP_PROBE_MERCHANTS", "   ")
    assert (
        module._resolve_order_live_readiness_requirement(
            {"allow_test_psp_surfaces": True}, merchant_id="merch_efbc46b4619cfbdf"
        )
        is True
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
