"""Test the merchant Stripe publishable-config resolver surfaced on /prefill.

The checkout page mounts the card form on load using this publishable config (so it doesn't wait for the
order-create response to carry the key). Only the PUBLISHABLE key is ever returned — never a secret.
"""
from __future__ import annotations

import asyncio

import routes.agent_checkout_intents as ci


def _run(coro):
    return asyncio.run(coro)


def _install(monkeypatch, rows):
    async def fake_fetch(*, merchant_id, provider=None, database_override=None):
        return rows

    # patch where the helper imports it (services module)
    import services.merchant_psp_config_service as svc

    monkeypatch.setattr(svc, "fetch_active_merchant_psps", fake_fetch)


def test_returns_publishable_config_prefers_live(monkeypatch):
    _install(
        monkeypatch,
        [
            {
                "provider": "stripe",
                "environment": "test",
                "api_key": "sk_test_x",
                "secret_key": "sk_test_x",
                "runtime_secret_key": "sk_test_x",
                "account_id": None,
                "provider_config": {"public_key": "pk_test_x"},
            },
            {
                "provider": "stripe",
                "environment": "live",
                "api_key": "sk_live_y",
                "secret_key": "sk_live_y",
                "runtime_secret_key": "sk_live_y",
                "account_id": "acct_123",
                "provider_config": {"public_key": "pk_live_y"},
            },
        ],
    )
    out = _run(ci._resolve_merchant_stripe_config("merch_x"))
    assert out is not None
    assert out["publishable_key"] == "pk_live_y"  # the live row, not the test row
    assert out["stripe_account"] == "acct_123"
    assert out["environment"] == "live"


def test_never_leaks_secret_key(monkeypatch):
    _install(
        monkeypatch,
        [
            {
                "provider": "stripe",
                "environment": "live",
                "api_key": "sk_live_secret",
                "secret_key": "sk_live_secret",
                "runtime_secret_key": "sk_live_secret",
                "account_id": None,
                "provider_config": {"public_key": "pk_live_ok"},
            }
        ],
    )
    out = _run(ci._resolve_merchant_stripe_config("merch_x"))
    blob = str(out)
    assert "sk_live_secret" not in blob
    assert out["publishable_key"] == "pk_live_ok"


def test_no_public_key_returns_none(monkeypatch):
    _install(
        monkeypatch,
        [{"provider": "stripe", "environment": "live", "api_key": "sk_live_x", "provider_config": {}}],
    )
    assert _run(ci._resolve_merchant_stripe_config("merch_x")) is None


def test_no_rows_returns_none(monkeypatch):
    _install(monkeypatch, [])
    assert _run(ci._resolve_merchant_stripe_config("merch_x")) is None


def test_blank_merchant_returns_none(monkeypatch):
    assert _run(ci._resolve_merchant_stripe_config("")) is None
