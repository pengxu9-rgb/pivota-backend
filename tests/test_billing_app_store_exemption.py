"""Regression tests for App Store billing exemption (_merchant_is_billing_free).

The exemption decides whether a merchant sees off-platform (Stripe) billing.
App Store merchants must NOT — showing them Stripe plans is a 2.1.1 / 1.2.1
violation. The subtle bug this guards: get_merchant_active_stores() rewrites each
store's `api_key` to the bare access token and moves the JSON blob (which carries
install_source) to `api_key_raw`, so parsing `api_key` loses the source entirely.
"""

import json

import pytest

import routes.billing_routes as billing_routes


def _active_store_shape(*, install_source: str | None, is_primary: bool = True) -> dict:
    """Mirror the dict get_merchant_active_stores() actually returns.

    api_key = bare token (parsed), api_key_raw = original JSON blob.
    """
    blob = {"access_token": "shpat_live_xxx"}
    if install_source:
        blob["install_source"] = install_source
    return {
        "store_id": "store_1",
        "platform": "shopify",
        "domain": "demo.myshopify.com",
        "is_primary": is_primary,
        "api_key": "shpat_live_xxx",  # <-- parse_api_key() output, token only
        "api_key_raw": json.dumps(blob),  # <-- where install_source really lives
    }


@pytest.mark.asyncio
async def test_app_store_primary_store_is_billing_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact shape a fresh reviewer account has: one app_store Shopify store."""
    monkeypatch.setenv("BILLING_EXEMPT_MERCHANT_IDS", "")

    async def fake_active_stores(merchant_id: str):
        return [_active_store_shape(install_source="app_store")]

    monkeypatch.setattr(billing_routes, "get_merchant_active_stores", fake_active_stores)

    assert await billing_routes._merchant_is_billing_free("merch_shopify_abc") is True


@pytest.mark.asyncio
async def test_app_store_store_is_free_even_when_not_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An existing merchant who then installs App A must still be exempt."""
    monkeypatch.setenv("BILLING_EXEMPT_MERCHANT_IDS", "")

    async def fake_active_stores(merchant_id: str):
        return [
            _active_store_shape(install_source=None, is_primary=True),  # older BYO store
            _active_store_shape(install_source="app_store", is_primary=False),
        ]

    monkeypatch.setattr(billing_routes, "get_merchant_active_stores", fake_active_stores)

    assert await billing_routes._merchant_is_billing_free("merch_mixed") is True


@pytest.mark.asyncio
async def test_headless_only_merchant_still_billed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A custom-token / BYO merchant keeps Stripe billing (must NOT be exempt)."""
    monkeypatch.setenv("BILLING_EXEMPT_MERCHANT_IDS", "")

    async def fake_active_stores(merchant_id: str):
        return [_active_store_shape(install_source="merchant_portal", is_primary=True)]

    monkeypatch.setattr(billing_routes, "get_merchant_active_stores", fake_active_stores)

    assert await billing_routes._merchant_is_billing_free("merch_byo") is False


@pytest.mark.asyncio
async def test_no_stores_is_billed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BILLING_EXEMPT_MERCHANT_IDS", "")

    async def fake_active_stores(merchant_id: str):
        return []

    monkeypatch.setattr(billing_routes, "get_merchant_active_stores", fake_active_stores)

    assert await billing_routes._merchant_is_billing_free("merch_empty") is False


@pytest.mark.asyncio
async def test_explicit_exempt_list_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BILLING_EXEMPT_MERCHANT_IDS", "merch_review_acct")

    async def fake_active_stores(merchant_id: str):  # pragma: no cover - must short-circuit
        raise AssertionError("exempt-list merchants should not need a store lookup")

    monkeypatch.setattr(billing_routes, "get_merchant_active_stores", fake_active_stores)

    assert await billing_routes._merchant_is_billing_free("merch_review_acct") is True


@pytest.mark.asyncio
async def test_regression_bare_token_in_api_key_does_not_leak_billing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Directly pins the original bug: install_source lives in api_key_raw, and the
    check must not read the bare-token api_key field (which parses to {})."""
    monkeypatch.setenv("BILLING_EXEMPT_MERCHANT_IDS", "")

    store = _active_store_shape(install_source="app_store")
    # Sanity: the mangled api_key really is just the token, not JSON.
    assert billing_routes.parse_api_credentials(store["api_key"]) == {}

    async def fake_active_stores(merchant_id: str):
        return [store]

    monkeypatch.setattr(billing_routes, "get_merchant_active_stores", fake_active_stores)

    assert await billing_routes._merchant_is_billing_free("merch_shopify_abc") is True
