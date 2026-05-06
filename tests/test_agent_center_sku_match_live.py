"""
Tests for `services.agent_center_sku_match_live_service`.

Two layers:

  1. Pure-function diff tests (`diff_live_vs_cached`) — the heart of the
     drift detection logic, fed canned live + cached lists.
  2. Runner integration tests using FakeDB + a monkey-patched `_fetch_both`
     so we don't need a real Shopify mock.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from tests.test_agent_center_service import FakeDB


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> FakeDB:
    """Mirror of the fake_db fixture in test_agent_center_sku_match.py:
    install a fresh FakeDB into agent_center_service.database for each test
    so live-runner tests can drive scan_target / issue / usage_event flow
    without a real Postgres."""
    db = FakeDB()
    from services import agent_center_service as ac
    monkeypatch.setattr(ac, "database", db)
    return db


# ---------------------------------------------------------------------------
# Diff fixture helpers
# ---------------------------------------------------------------------------


def _live(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "id": "P1",
        "product_id": "P1",
        "title": "Vitamin C Tonic",
        "price": 19.99,
        "currency": "USD",
        "image_url": "https://example.com/p1.jpg",
        "in_stock": True,
        "inventory_quantity": 12,
    }
    base.update(overrides)
    return base


def _cached(**overrides: Any) -> Dict[str, Any]:
    """Returns the products_cache row shape (with `product_data_decoded`
    pre-populated, since that's what the runner sees after _fetch_cached)."""
    data: Dict[str, Any] = {
        "title": "Vitamin C Tonic",
        "price": 19.99,
        "currency": "USD",
        "image_url": "https://example.com/p1.jpg",
        "in_stock": True,
        "inventory_quantity": 12,
    }
    row: Dict[str, Any] = {
        "id": "pc_1",
        "merchant_id": "m1",
        "platform": "shopify",
        "platform_product_id": "P1",
        "product_data_decoded": data,
        "cached_at": None,
    }
    if "data" in overrides:
        row["product_data_decoded"] = overrides.pop("data")
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# diff_live_vs_cached — pure-function tests
# ---------------------------------------------------------------------------


def test_diff_no_drift_returns_empty() -> None:
    from services.agent_center_sku_match_live_service import diff_live_vs_cached
    out = diff_live_vs_cached(
        live=[_live()], cached=[_cached()],
        merchant_id="m1", platform="shopify",
    )
    assert out == []


def test_diff_price_drift_critical() -> None:
    from services.agent_center_sku_match_live_service import diff_live_vs_cached
    out = diff_live_vs_cached(
        live=[_live(price=24.50)],
        cached=[_cached(data={"title": "Vitamin C Tonic", "price": 19.99,
                              "currency": "USD", "image_url": "https://example.com/p1.jpg",
                              "in_stock": True})],
        merchant_id="m1", platform="shopify",
    )
    assert any(f["issue_type"] == "live_price_drift" and f["severity"] == "critical" for f in out)
    drift = next(f for f in out if f["issue_type"] == "live_price_drift")
    assert drift["evidence"]["live_price"] == 24.50
    assert drift["evidence"]["cached_price"] == 19.99
    assert drift["product_entity_id"] == "m1|shopify|P1"


def test_diff_price_within_epsilon_is_not_drift() -> None:
    """0.01 tolerance — float noise from string parsing shouldn't surface as drift."""
    from services.agent_center_sku_match_live_service import diff_live_vs_cached
    out = diff_live_vs_cached(
        live=[_live(price=19.995)],
        cached=[_cached(data={"title": "Vitamin C Tonic", "price": 19.99,
                              "currency": "USD", "image_url": "https://example.com/p1.jpg",
                              "in_stock": True})],
        merchant_id="m1", platform="shopify",
    )
    assert not any(f["issue_type"] == "live_price_drift" for f in out)


def test_diff_inventory_drift_high_severity() -> None:
    from services.agent_center_sku_match_live_service import diff_live_vs_cached
    out = diff_live_vs_cached(
        live=[_live(in_stock=False, inventory_quantity=0)],
        cached=[_cached(data={"title": "Vitamin C Tonic", "price": 19.99,
                              "currency": "USD", "image_url": "https://example.com/p1.jpg",
                              "in_stock": True, "inventory_quantity": 12})],
        merchant_id="m1", platform="shopify",
    )
    drift = next(f for f in out if f["issue_type"] == "live_inventory_drift")
    assert drift["severity"] == "high"
    assert drift["evidence"]["live_in_stock"] is False
    assert drift["evidence"]["cached_in_stock"] is True


def test_diff_title_drift_low_severity() -> None:
    from services.agent_center_sku_match_live_service import diff_live_vs_cached
    out = diff_live_vs_cached(
        live=[_live(title="Vitamin C Tonic 2.0")],
        cached=[_cached()],
        merchant_id="m1", platform="shopify",
    )
    drift = next(f for f in out if f["issue_type"] == "live_title_drift")
    assert drift["severity"] == "low"


def test_diff_image_drift_medium() -> None:
    from services.agent_center_sku_match_live_service import diff_live_vs_cached
    out = diff_live_vs_cached(
        live=[_live(image_url="https://example.com/p1-v2.jpg")],
        cached=[_cached()],
        merchant_id="m1", platform="shopify",
    )
    drift = next(f for f in out if f["issue_type"] == "live_image_drift")
    assert drift["severity"] == "medium"


def test_diff_live_only_product_surfaces_missing_in_cache() -> None:
    from services.agent_center_sku_match_live_service import diff_live_vs_cached
    out = diff_live_vs_cached(
        live=[_live(id="P1"), _live(id="P2", title="New Tonic")],
        cached=[_cached(platform_product_id="P1")],
        merchant_id="m1", platform="shopify",
    )
    missing = [f for f in out if f["issue_type"] == "live_sku_missing_in_cache"]
    assert len(missing) == 1
    assert missing[0]["product_entity_id"] == "m1|shopify|P2"
    assert missing[0]["severity"] == "high"


def test_diff_cache_only_product_surfaces_unpublished() -> None:
    from services.agent_center_sku_match_live_service import diff_live_vs_cached
    out = diff_live_vs_cached(
        live=[_live(id="P1")],
        cached=[
            _cached(platform_product_id="P1"),
            _cached(platform_product_id="P_GONE", data={"title": "Discontinued"}),
        ],
        merchant_id="m1", platform="shopify",
    )
    unpub = [f for f in out if f["issue_type"] == "live_product_unpublished"]
    assert len(unpub) == 1
    assert unpub[0]["product_entity_id"] == "m1|shopify|P_GONE"


def test_diff_skips_inventory_when_either_side_unknown() -> None:
    """If either side has in_stock=None, we can't say whether they
    disagree. Don't surface a finding (would be noisy)."""
    from services.agent_center_sku_match_live_service import diff_live_vs_cached
    out = diff_live_vs_cached(
        live=[_live(in_stock=None)],
        cached=[_cached(data={"title": "Vitamin C Tonic", "price": 19.99,
                              "currency": "USD", "image_url": "https://example.com/p1.jpg",
                              "in_stock": True})],
        merchant_id="m1", platform="shopify",
    )
    assert not any(f["issue_type"] == "live_inventory_drift" for f in out)


# ---------------------------------------------------------------------------
# Runner integration: drives a scan_target through running → succeeded with
# a stubbed _fetch_both so we don't need real platform credentials.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_sku_match_live_succeeds_and_creates_issues(
    fake_db: FakeDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services import agent_center_service as ac
    from services import agent_center_sku_match_live_service as live

    async def _fake_fetch_both(**_kwargs):
        return (
            [_live(id="P1", price=24.50)],
            [_cached(platform_product_id="P1", data={
                "title": "Vitamin C Tonic", "price": 19.99,
                "currency": "USD", "image_url": "https://example.com/p1.jpg",
                "in_stock": True,
            })],
        )
    monkeypatch.setattr(live, "_fetch_both", _fake_fetch_both)

    target = await ac.create_scan_target(
        merchant_id="m1", store_id="s1", scan_mode="sku_match",
        payload={"options": {"mode": "live", "platform": "shopify", "limit": 50}},
    )
    final = await live.run_sku_match_live(target["id"])
    assert final["status"] == "succeeded"
    assert final["payload"]["run"]["mode"] == "live"
    # Critical price drift becomes an issue.
    issues = fake_db._tables["agent_center_issues"]
    assert len(issues) == 1
    assert issues[0]["issue_type"] == "live_price_drift"
    assert issues[0]["severity"] == "critical"


@pytest.mark.asyncio
async def test_run_sku_match_live_unsupported_platform_marks_failed(
    fake_db: FakeDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services import agent_center_service as ac
    from services import agent_center_sku_match_live_service as live

    target = await ac.create_scan_target(
        merchant_id="m1", store_id="s1", scan_mode="sku_match",
        payload={"options": {"mode": "live", "platform": "amazon"}},
    )
    with pytest.raises(live.LiveAdapterUnsupportedError):
        await live.run_sku_match_live(target["id"])

    final = fake_db._tables["agent_center_scan_targets"][0]
    assert final["status"] == "failed"
    err = final["payload"]["error"]
    assert err["kind"] == "unsupported_platform"
    assert err["platform"] == "amazon"


@pytest.mark.asyncio
async def test_run_sku_match_live_fetch_failure_marks_failed(
    fake_db: FakeDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services import agent_center_service as ac
    from services import agent_center_sku_match_live_service as live

    async def _bad_fetch(**_kwargs):
        raise RuntimeError("Shopify 401 invalid token")
    monkeypatch.setattr(live, "_fetch_both", _bad_fetch)

    target = await ac.create_scan_target(
        merchant_id="m1", store_id="s1", scan_mode="sku_match",
        payload={"options": {"mode": "live", "platform": "shopify"}},
    )
    with pytest.raises(RuntimeError, match="Shopify 401"):
        await live.run_sku_match_live(target["id"])

    final = fake_db._tables["agent_center_scan_targets"][0]
    assert final["status"] == "failed"
    assert final["payload"]["error"]["kind"] == "live_fetch"


# ---------------------------------------------------------------------------
# Dispatch from the V1 runner — payload.options.mode='live' should hand off.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_sku_match_dispatches_to_live_when_mode_set(
    fake_db: FakeDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services import agent_center_service as ac
    from services import agent_center_sku_match_service as sms
    from services import agent_center_sku_match_live_service as live

    called: List[str] = []

    async def _fake_live_runner(scan_target_id: str):
        called.append(scan_target_id)
        return await ac.transition_scan_target(
            scan_target_id=scan_target_id, status="succeeded", finished_at=ac.utcnow(),
        )
    monkeypatch.setattr(live, "run_sku_match_live", _fake_live_runner)

    target = await ac.create_scan_target(
        merchant_id="m1", store_id="s1", scan_mode="sku_match",
        payload={"options": {"mode": "live"}},
    )
    await sms.run_sku_match(target["id"])
    assert called == [target["id"]]


@pytest.mark.asyncio
async def test_run_sku_match_internal_path_unaffected_when_no_mode_set(
    fake_db: FakeDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backward-compat sanity: scan_targets without options.mode use the
    existing internal-data-quality runner, not the live runner."""
    from services import agent_center_service as ac
    from services import agent_center_sku_match_service as sms

    async def _empty_fetch(**_kwargs):
        return []
    monkeypatch.setattr(sms, "_fetch_products_for_merchant", _empty_fetch)

    target = await ac.create_scan_target(
        merchant_id="m1", store_id="s1", scan_mode="sku_match",
    )
    await sms.run_sku_match(target["id"])
    final = fake_db._tables["agent_center_scan_targets"][0]
    # Internal runner transitions to succeeded directly.
    assert final["status"] == "succeeded"
    # Live-mode marker should not appear.
    assert final["payload"].get("run", {}).get("mode") != "live"


# ---------------------------------------------------------------------------
# Multi-platform credential resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["wix", "woocommerce", "bigcommerce"])
async def test_supported_platforms_include_non_shopify(platform: str) -> None:
    """Sanity: SUPPORTED_LIVE_PLATFORMS expanded beyond Shopify in PR 9."""
    from services.agent_center_sku_match_live_service import SUPPORTED_LIVE_PLATFORMS
    assert platform in SUPPORTED_LIVE_PLATFORMS


@pytest.mark.asyncio
async def test_resolve_generic_credentials_parses_wix_blob(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wix store records are stored with site_id+api_key inside the api_key
    JSON blob — the resolver must unpack them so the adapter dispatcher
    sees `credentials.site_id` and `credentials.api_key`."""
    from services import agent_center_sku_match_live_service as live

    async def _fake_fetch_one(query: str, params: Dict[str, Any]):
        assert params["platform"] == "wix"
        return {
            "store_id": "site_zzz",
            "domain": "example.wixsite.com",
            "api_key": '{"site_id":"site_xxx","api_key":"wix_token_yyy"}',
            "status": "active",
        }
    monkeypatch.setattr(live.database, "fetch_one", _fake_fetch_one)

    creds = await live._resolve_generic_store_credentials(
        merchant_id="m1", platform="wix",
    )
    assert creds["site_id"] == "site_xxx"
    assert creds["api_key"] == "wix_token_yyy"


@pytest.mark.asyncio
async def test_resolve_generic_credentials_woocommerce_backfills_store_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WooCommerce records sometimes leave store_url out of the JSON blob
    (it lives on the merchant_stores.domain column instead). Resolver
    backfills from `domain` so the adapter has everything it needs."""
    from services import agent_center_sku_match_live_service as live

    async def _fake_fetch_one(query: str, params: Dict[str, Any]):
        return {
            "store_id": "woo_1",
            "domain": "https://example-shop.com",
            "api_key": '{"consumer_key":"ck_xxx","consumer_secret":"cs_yyy"}',
            "status": "active",
        }
    monkeypatch.setattr(live.database, "fetch_one", _fake_fetch_one)

    creds = await live._resolve_generic_store_credentials(
        merchant_id="m1", platform="woocommerce",
    )
    assert creds["consumer_key"] == "ck_xxx"
    assert creds["consumer_secret"] == "cs_yyy"
    assert creds["store_url"] == "https://example-shop.com"


@pytest.mark.asyncio
async def test_resolve_generic_credentials_raises_when_no_store_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services import agent_center_sku_match_live_service as live

    async def _fake_fetch_one(query: str, params: Dict[str, Any]):
        return None
    monkeypatch.setattr(live.database, "fetch_one", _fake_fetch_one)

    with pytest.raises(ValueError, match="no connected"):
        await live._resolve_generic_store_credentials(
            merchant_id="m1", platform="wix",
        )


@pytest.mark.asyncio
async def test_unsupported_platform_amazon_still_fails_cleanly(
    fake_db: FakeDB,
) -> None:
    """Amazon and Temu are still out of scope until adapters land. Make
    sure the contract from PR 7 (clean failed scan_target with descriptive
    payload.error) still holds after expanding SUPPORTED_LIVE_PLATFORMS."""
    from services import agent_center_service as ac
    from services import agent_center_sku_match_live_service as live

    target = await ac.create_scan_target(
        merchant_id="m1", store_id="s1", scan_mode="sku_match",
        payload={"options": {"mode": "live", "platform": "amazon"}},
    )
    with pytest.raises(live.LiveAdapterUnsupportedError):
        await live.run_sku_match_live(target["id"])
    err = fake_db._tables["agent_center_scan_targets"][0]["payload"]["error"]
    assert err["kind"] == "unsupported_platform"
    assert "shopify" in err["supported"]
    assert "wix" in err["supported"]
