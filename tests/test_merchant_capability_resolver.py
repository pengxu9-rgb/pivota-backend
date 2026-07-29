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
        return {"platform": "shopify", "domain": "brand.myshopify.com", "status": "active"}
        # `status` is not decoration: `get_merchant_active_stores` always
        # SELECTs it, and ADR-018's connection-layer ceiling reads it. A
        # status-less fake silently scores ceiling 1 and stops meaning what
        # this fixture's name says.

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
        return {"platform": "shopify", "domain": "brand.myshopify.com", "status": "active"}
        # `status` is not decoration: `get_merchant_active_stores` always
        # SELECTs it, and ADR-018's connection-layer ceiling reads it. A
        # status-less fake silently scores ceiling 1 and stops meaning what
        # this fixture's name says.

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


# --- ADR-018: connection-layer ceiling --------------------------------------
#
# The resolver is MERCHANT-scoped and carries no `catalog_track`, so what it can
# honestly answer is the CEILING — the highest layer this merchant's own synced
# rows could reach. A crawled ROW under the same merchant stays layer 1
# (ADR-001), which is why the key is `_ceiling` and not `connection_layer`.


@pytest.mark.asyncio
async def test_connection_layer_ceiling_is_3_for_connected_shopify_with_portal_psp_flag(monkeypatch):
    from services import merchant_capability_resolver as res

    async def fake_get_primary_store(mid):
        return {"platform": "shopify", "domain": "brand.myshopify.com", "status": "active"}

    async def fake_get_merchant_onboarding(mid):
        # The PORTAL flag is what makes this layer 3 (founder ruling 2026-07-28).
        # This fixture used to return `{}` and rely on `fake_live_psp` — which
        # passed only because the resolver was feeding the wrong fact.
        return {"psp_connected": True}

    async def fake_live_psp(mid):
        return "stripe"

    monkeypatch.setattr(res, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(res, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(res, "_resolve_live_psp_provider", fake_live_psp)

    cap = await res.resolve_merchant_capability("merch_shop")
    assert cap["connection_layer_ceiling"] == 3
    assert cap["connection_layer_ceiling_slug"] == "product_synced_psp"


# The two tests below are mirror images and exist to pin WHICH FACT decides the
# layer, in both directions. Neither can pass while the resolver feeds
# `has_live_psp`, and the pair is the only thing standing between the founder's
# ruling and a silent re-drift back to the PSP-row fact.
#
# The first is the live prod divergence: measured 2026-07-28, 2 merchants hold an
# active `merchant_psps` row with the portal flag unset. Both are ceiling 1 today
# for want of a live store, so this fixture is what that pair becomes the day one
# of them connects a store — which is precisely when nobody would be looking.


@pytest.mark.asyncio
async def test_connection_layer_ceiling_is_2_when_psp_row_is_active_but_portal_flag_unset(monkeypatch):
    from services import merchant_capability_resolver as res

    async def fake_get_primary_store(mid):
        return {"platform": "shopify", "domain": "brand.myshopify.com", "status": "active"}

    async def fake_get_merchant_onboarding(mid):
        return {}  # never went through the portal flow — not yes

    async def fake_live_psp(mid):
        return "stripe"  # an active merchant_psps row DOES exist

    monkeypatch.setattr(res, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(res, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(res, "_resolve_live_psp_provider", fake_live_psp)

    cap = await res.resolve_merchant_capability("merch_psp_row_no_flag")
    assert cap["connection_layer_ceiling"] == 2
    assert cap["connection_layer_ceiling_slug"] == "product_synced"
    # The PSP fact itself is unchanged and still reported — only the LAYER input
    # moved. Without this line the test would also pass if `has_live_psp` were
    # simply dropped from the resolver.
    assert cap["has_live_psp"] is True


@pytest.mark.asyncio
async def test_connection_layer_ceiling_is_3_from_portal_flag_with_no_active_psp_row(monkeypatch):
    from services import merchant_capability_resolver as res

    async def fake_get_primary_store(mid):
        return {"platform": "shopify", "domain": "brand.myshopify.com", "status": "active"}

    async def fake_get_merchant_onboarding(mid):
        return {"psp_connected": True}

    async def fake_live_psp(mid):
        return None  # no active merchant_psps row

    monkeypatch.setattr(res, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(res, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(res, "_resolve_live_psp_provider", fake_live_psp)

    cap = await res.resolve_merchant_capability("merch_flag_no_psp_row")
    assert cap["connection_layer_ceiling"] == 3
    assert cap["has_live_psp"] is False


@pytest.mark.asyncio
async def test_connection_layer_ceiling_treats_null_portal_flag_as_not_yes(monkeypatch):
    """An explicit NULL must read as "not yes", matching the SQL twin's
    ``COALESCE(mo.psp_connected, false)``. A `.get()` returning None and a column
    that is NULL are the same case here, and coercing either to False in Python
    would diverge from the twin the day the twin changes."""
    from services import merchant_capability_resolver as res

    async def fake_get_primary_store(mid):
        return {"platform": "shopify", "domain": "brand.myshopify.com", "status": "active"}

    async def fake_get_merchant_onboarding(mid):
        return {"psp_connected": None}

    async def fake_live_psp(mid):
        return None

    monkeypatch.setattr(res, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(res, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(res, "_resolve_live_psp_provider", fake_live_psp)

    cap = await res.resolve_merchant_capability("merch_null_flag")
    assert cap["connection_layer_ceiling"] == 2


@pytest.mark.asyncio
async def test_connection_layer_ceiling_is_2_when_connected_without_a_psp(monkeypatch):
    from services import merchant_capability_resolver as res

    async def fake_get_primary_store(mid):
        return {"platform": "shopify", "domain": "brand.myshopify.com", "status": "active"}

    async def fake_get_merchant_onboarding(mid):
        return {}

    async def fake_live_psp(mid):
        return None

    monkeypatch.setattr(res, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(res, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(res, "_resolve_live_psp_provider", fake_live_psp)

    cap = await res.resolve_merchant_capability("merch_shop_nopsp")
    assert cap["connection_layer_ceiling"] == 2


@pytest.mark.asyncio
async def test_connection_layer_ceiling_is_1_for_a_crawl_merchant(monkeypatch):
    """The founder's policy in one assertion: a crawled seller is REAL and
    transactable — it is simply layer 1. No store connection, no PSP, still a
    first-class row in the census."""
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
    # Fingerprinted platform, but nothing was ever synced from it.
    assert cap["platform_source"] == "domain_fingerprint"
    assert cap["connection_layer_ceiling"] == 1
    assert cap["connection_layer_ceiling_slug"] == "crawled"


@pytest.mark.asyncio
async def test_connection_layer_ceiling_present_on_every_early_return(monkeypatch):
    """A key that appears only on the happy path teaches consumers to default it."""
    from services.merchant_capability_resolver import resolve_merchant_capability

    empty = await resolve_merchant_capability("")
    assert empty["connection_layer_ceiling"] == 1

    _patch_multistore(monkeypatch)
    unmatched = await resolve_merchant_capability("m", platform_override="bigcommerce")
    assert unmatched["store_selector"]["matched"] is False
    assert unmatched["connection_layer_ceiling"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["active", "connected"])
async def test_connection_layer_ceiling_accepts_both_live_store_statuses(monkeypatch, status):
    """`connected` is a live status everywhere else in this repo
    (`merchant_store_service`: `status IN ('active','connected')`). A narrower
    set here would make this module disagree with every other read path."""
    from services import merchant_capability_resolver as res

    async def fake_get_primary_store(mid):
        return {"platform": "shopify", "domain": "b.myshopify.com", "status": status}

    async def fake_get_merchant_onboarding(mid):
        return {}

    async def fake_live_psp(mid):
        return None

    monkeypatch.setattr(res, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(res, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(res, "_resolve_live_psp_provider", fake_live_psp)

    cap = await res.resolve_merchant_capability("merch_live")
    assert cap["connection_layer_ceiling"] == 2


@pytest.mark.asyncio
async def test_disconnected_legacy_mcp_store_is_not_layer_2(monkeypatch):
    """REGRESSION. `get_merchant_active_stores`' legacy-MCP leg synthesises a
    store row whenever `mcp_platform` is set and stamps it
    `status = 'active' if mcp_connected else 'disconnected'` — appending it
    EITHER WAY. So a plain `bool(store)` test labels a merchant with NO live
    connection layer 2, while the SQL twin (no `merchant_stores` row exists)
    says layer 1. Two twins, two answers, no error.
    """
    from services import merchant_capability_resolver as res

    async def fake_get_primary_store(mid):
        return {
            "store_id": f"legacy_{mid}",
            "platform": "shopify",
            "domain": "legacy.myshopify.com",
            "status": "disconnected",
            "source": "legacy_mcp",
        }

    async def fake_get_merchant_onboarding(mid):
        return {"mcp_platform": "shopify", "mcp_connected": False}

    async def fake_live_psp(mid):
        return None

    monkeypatch.setattr(res, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(res, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(res, "_resolve_live_psp_provider", fake_live_psp)

    cap = await res.resolve_merchant_capability("merch_legacy_dead")
    assert cap["connection_layer_ceiling"] == 1
    assert cap["connection_layer_ceiling_slug"] == "crawled"


@pytest.mark.asyncio
async def test_onboarding_query_still_selects_psp_connected(monkeypatch):
    """The layer input is `(merchant or {}).get("psp_connected")`, and `.get` on a
    narrowed row is indistinguishable from a merchant who never connected a PSP.

    Concrete failure this pins: `get_merchant_onboarding` currently SELECTs all
    ~40 columns to read one flag, which is an obvious future perf trim. Narrow it
    and `.get` returns None, EVERY merchant silently drops to ceiling 2, and every
    other test in this file stays green — they all monkeypatch the function away,
    so none of them can see its query. Assert on the query itself, which is the
    only place the contract is observable.
    """
    from db import merchant_onboarding as mo

    captured = {}

    async def fake_fetch_one(query):
        captured["sql"] = str(query)
        return None

    monkeypatch.setattr(mo.database, "fetch_one", fake_fetch_one)
    await mo.get_merchant_onboarding("merch_query_shape")

    assert "psp_connected" in captured["sql"], (
        "get_merchant_onboarding no longer selects psp_connected — "
        "services/merchant_capability_resolver reads it off this row to decide "
        "the ADR-018 connection layer, and a missing column reads as 'no PSP'."
    )
