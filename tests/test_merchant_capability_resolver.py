"""P-T2.1 — merchant capability resolver + protocol matrix.

Covers the routing-gate invariants:
  - the protocol matrix is truthful + PSP-gated (only Shopify + a live PSP → acp)
  - domain fingerprinting resolves a crawl merchant's platform, or honest unknown
  - resolve_merchant_capability returns {platform, psp, protocols[]} for connected,
    crawl, and unknown merchants without ever raising.
"""

from __future__ import annotations

import pytest


# --- protocol matrix --------------------------------------------------------

def test_protocol_matrix_shopify_requires_live_psp():
    from services.platform_capabilities import get_platform_protocols

    assert get_platform_protocols("shopify", has_live_psp=True) == ["acp"]
    # No live PSP → cannot settle → no protocol offered (honest redirect-only).
    assert get_platform_protocols("shopify", has_live_psp=False) == []


@pytest.mark.parametrize("platform", ["wix", "woocommerce", "bigcommerce", "custom", "unknown", None, ""])
def test_protocol_matrix_other_platforms_empty(platform):
    from services.platform_capabilities import get_platform_protocols

    assert get_platform_protocols(platform, has_live_psp=True) == []


# --- P-T2.3.6: Wix production ACP is store-writeback-gated ------------------

def _wix_store(*, status="active", order_writeback_status="enabled"):
    return {
        "store_id": "wix_store_1",
        "platform": "wix",
        "status": status,
        "order_writeback_status": order_writeback_status,
    }


def test_store_aware_wix_enabled_with_live_psp_gets_acp():
    from services.platform_capabilities import get_platform_protocols_for_store

    assert get_platform_protocols_for_store(
        "wix", _wix_store(), has_live_psp=True
    ) == ["acp"]


def test_store_aware_wix_enabled_without_live_psp_empty():
    from services.platform_capabilities import get_platform_protocols_for_store

    # Ready store but no way to settle → honest redirect-only.
    assert get_platform_protocols_for_store(
        "wix", _wix_store(), has_live_psp=False
    ) == []


@pytest.mark.parametrize(
    "store",
    [
        None,  # crawl / fingerprinted merchant, no connected store
        _wix_store(order_writeback_status="disabled"),
        _wix_store(order_writeback_status="paused"),
        _wix_store(order_writeback_status="failed"),
        _wix_store(order_writeback_status="canary"),  # per-order gate, not routable pre-checkout
        _wix_store(status="disconnected"),
    ],
)
def test_store_aware_wix_not_ready_empty(store):
    from services.platform_capabilities import get_platform_protocols_for_store

    assert get_platform_protocols_for_store("wix", store, has_live_psp=True) == []


def test_store_aware_shopify_unchanged_by_store_gate():
    from services.platform_capabilities import get_platform_protocols_for_store

    # Shopify stays platform-global (no per-store writeback gate); store irrelevant.
    assert get_platform_protocols_for_store(
        "shopify", None, has_live_psp=True
    ) == ["acp"]
    assert get_platform_protocols_for_store(
        "shopify", {"platform": "shopify", "status": "active"}, has_live_psp=False
    ) == []


def test_store_aware_global_matrix_stays_honest_for_bare_wix():
    # The platform-global matrix must NEVER grant Wix acp from a bare slug — the
    # grant lives only in the store-aware path.
    from services.platform_capabilities import get_platform_protocols

    assert get_platform_protocols("wix", has_live_psp=True) == []


# --- domain fingerprint -----------------------------------------------------

@pytest.mark.parametrize(
    "domain,expected",
    [
        ("cool-store.myshopify.com", "shopify"),
        ("https://cool-store.myshopify.com/products", "shopify"),
        ("www.brand.wixsite.com", "wix"),
        ("shop.mybigcommerce.com", "bigcommerce"),
        ("brand.editorx.io", "wix"),
        ("example.com", None),
        ("brand.com", None),
        ("", None),
        (None, None),
    ],
)
def test_fingerprint_platform_from_domain(domain, expected):
    from services.merchant_capability_resolver import fingerprint_platform_from_domain

    assert fingerprint_platform_from_domain(domain) == expected


# --- resolver ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_connected_shopify_with_live_psp(monkeypatch):
    from services import merchant_capability_resolver as res

    async def fake_get_primary_store(mid):
        return {"platform": "shopify", "domain": "brand.myshopify.com"}

    async def fake_get_merchant_onboarding(mid):
        return {}

    async def fake_live_psp(mid):
        return "stripe"

    monkeypatch.setattr(res, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(res, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(res, "_resolve_live_psp_provider", fake_live_psp)

    cap = await res.resolve_merchant_capability("merch_shop")
    assert cap["platform"] == "shopify"
    assert cap["platform_source"] == "connected"
    assert cap["psp"] == "stripe"
    assert cap["has_live_psp"] is True
    assert cap["protocols"] == ["acp"]


@pytest.mark.asyncio
async def test_resolve_connected_shopify_without_live_psp(monkeypatch):
    from services import merchant_capability_resolver as res

    async def fake_get_primary_store(mid):
        return {"platform": "shopify", "domain": "brand.myshopify.com"}

    async def fake_get_merchant_onboarding(mid):
        return {}

    async def fake_live_psp(mid):
        return None

    monkeypatch.setattr(res, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(res, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(res, "_resolve_live_psp_provider", fake_live_psp)

    cap = await res.resolve_merchant_capability("merch_shop_nopsp")
    assert cap["platform"] == "shopify"
    assert cap["platform_source"] == "connected"
    # Platform known, but no way to settle → protocols honestly empty.
    assert cap["protocols"] == []


@pytest.mark.asyncio
async def test_resolve_crawl_merchant_fingerprints_platform(monkeypatch):
    from services import merchant_capability_resolver as res

    async def fake_get_primary_store(mid):
        return None  # un-integrated / crawl merchant

    async def fake_get_merchant_onboarding(mid):
        return {"website": "https://indie.myshopify.com"}

    async def fake_live_psp(mid):
        return None

    monkeypatch.setattr(res, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(res, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(res, "_resolve_live_psp_provider", fake_live_psp)

    cap = await res.resolve_merchant_capability("merch_crawl")
    assert cap["platform"] == "shopify"
    assert cap["platform_source"] == "domain_fingerprint"
    # Fingerprinted platform but no connected PSP → not routable to a charge yet.
    assert cap["protocols"] == []


@pytest.mark.asyncio
async def test_resolve_unknown_merchant(monkeypatch):
    from services import merchant_capability_resolver as res

    async def fake_get_primary_store(mid):
        return None

    async def fake_get_merchant_onboarding(mid):
        return {"website": "https://brand-custom-site.com"}

    async def fake_live_psp(mid):
        return None

    monkeypatch.setattr(res, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(res, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(res, "_resolve_live_psp_provider", fake_live_psp)

    cap = await res.resolve_merchant_capability("merch_unknown")
    assert cap["platform"] == "unknown"
    assert cap["platform_source"] == "unknown"
    assert cap["protocols"] == []


@pytest.mark.asyncio
async def test_resolve_connected_wix_enabled_store_with_live_psp(monkeypatch):
    from services import merchant_capability_resolver as res

    async def fake_get_primary_store(mid):
        return {
            "store_id": "wix_1",
            "platform": "wix",
            "status": "active",
            "order_writeback_status": "enabled",
        }

    async def fake_get_merchant_onboarding(mid):
        return {}

    async def fake_live_psp(mid):
        return "stripe"

    monkeypatch.setattr(res, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(res, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(res, "_resolve_live_psp_provider", fake_live_psp)

    cap = await res.resolve_merchant_capability("merch_wix_ready")
    assert cap["platform"] == "wix"
    assert cap["platform_source"] == "connected"
    assert cap["has_live_psp"] is True
    # Wix production ACP lights up: writeback-enabled store + live PSP.
    assert cap["protocols"] == ["acp"]


@pytest.mark.asyncio
async def test_resolve_connected_wix_not_writeback_ready_is_dark(monkeypatch):
    from services import merchant_capability_resolver as res

    async def fake_get_primary_store(mid):
        return {
            "store_id": "wix_1",
            "platform": "wix",
            "status": "active",
            "order_writeback_status": "disabled",  # default until proven
        }

    async def fake_get_merchant_onboarding(mid):
        return {}

    async def fake_live_psp(mid):
        return "stripe"

    monkeypatch.setattr(res, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(res, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(res, "_resolve_live_psp_provider", fake_live_psp)

    cap = await res.resolve_merchant_capability("merch_wix_pending")
    assert cap["platform"] == "wix"
    assert cap["has_live_psp"] is True
    # Live PSP but store not writeback-ready → redirect floor (dark).
    assert cap["protocols"] == []


@pytest.mark.asyncio
async def test_resolve_blank_merchant_id_is_safe():
    from services.merchant_capability_resolver import resolve_merchant_capability

    cap = await resolve_merchant_capability("")
    assert cap["platform"] == "unknown"
    assert cap["protocols"] == []
    assert cap["has_live_psp"] is False


# --- store/platform override (multi-store canary targeting) ------------------

def _patch_multistore(monkeypatch, *, live_psp="stripe"):
    """merch with BOTH a Shopify (primary) and a Wix store connected."""
    from services import merchant_capability_resolver as res

    shopify = {"store_id": "st_shop", "platform": "shopify", "status": "active",
               "domain": "brand.myshopify.com"}
    wix = {"store_id": "st_wix", "platform": "wix", "status": "active",
           "domain": "brand.wixsite.com"}

    async def fake_primary(mid):
        return shopify  # primary = Shopify

    async def fake_active(mid):
        return [shopify, wix]

    async def fake_onboarding(mid):
        return {}

    async def fake_live_psp(mid):
        return live_psp

    monkeypatch.setattr(res, "get_primary_store", fake_primary)
    monkeypatch.setattr(res, "get_merchant_active_stores", fake_active)
    monkeypatch.setattr(res, "get_merchant_onboarding", fake_onboarding)
    monkeypatch.setattr(res, "_resolve_live_psp_provider", fake_live_psp)


@pytest.mark.asyncio
async def test_override_defaults_to_primary_store(monkeypatch):
    from services.merchant_capability_resolver import resolve_merchant_capability
    _patch_multistore(monkeypatch)

    cap = await resolve_merchant_capability("m")  # no selector
    assert cap["platform"] == "shopify"
    assert "store_selector" not in cap


@pytest.mark.asyncio
async def test_override_by_platform_selects_wix(monkeypatch):
    from services.merchant_capability_resolver import resolve_merchant_capability
    _patch_multistore(monkeypatch)

    cap = await resolve_merchant_capability("m", platform_override="wix")
    assert cap["platform"] == "wix"
    assert cap["platform_source"] == "connected"
    assert cap["store_selector"]["matched"] is True
    assert cap["store_selector"]["resolved_store_id"] == "st_wix"


@pytest.mark.asyncio
async def test_override_by_store_id_selects_wix(monkeypatch):
    from services.merchant_capability_resolver import resolve_merchant_capability
    _patch_multistore(monkeypatch)

    cap = await resolve_merchant_capability("m", store_id="st_wix")
    assert cap["platform"] == "wix"
    assert cap["store_selector"]["resolved_store_id"] == "st_wix"


@pytest.mark.asyncio
async def test_override_store_id_wins_over_platform(monkeypatch):
    from services.merchant_capability_resolver import resolve_merchant_capability
    _patch_multistore(monkeypatch)

    # store_id points at Shopify, platform says wix → store_id wins.
    cap = await resolve_merchant_capability("m", store_id="st_shop", platform_override="wix")
    assert cap["platform"] == "shopify"
    assert cap["store_selector"]["resolved_store_id"] == "st_shop"


@pytest.mark.asyncio
async def test_override_unmatched_is_honest_unknown(monkeypatch):
    from services.merchant_capability_resolver import resolve_merchant_capability
    _patch_multistore(monkeypatch)

    # Merchant has no BigCommerce store → must NOT fall back to primary/fingerprint.
    cap = await resolve_merchant_capability("m", platform_override="bigcommerce")
    assert cap["platform"] == "unknown"
    assert cap["protocols"] == []
    assert cap["store_selector"]["matched"] is False


@pytest.mark.asyncio
async def test_override_unmatched_store_id_is_honest_unknown(monkeypatch):
    from services.merchant_capability_resolver import resolve_merchant_capability
    _patch_multistore(monkeypatch)

    cap = await resolve_merchant_capability("m", store_id="st_nope")
    assert cap["platform"] == "unknown"
    assert cap["store_selector"]["matched"] is False
