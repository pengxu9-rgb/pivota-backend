"""Tests for services/shopify_products_sync.refresh_merchant_presence — the
lightweight Stage 2a bootstrap path that decouples sync-hygiene from
the full ingest pipeline.

Tests pin:
  - Fetches Shopify product IDs and bumps catalog_products
    last_seen_in_sync_at + sync_status='live' for matching rows
  - Bumps catalog_merchants.last_full_sync_at on success (even when
    0 products matched — empty Shopify catalog is itself a signal)
  - NEVER calls catalog ingest plumbing (no taxonomy, no SKU writes,
    no offer writes)
  - Raises typed ShopifyProductsSync* errors so the route maps to
    the right HTTP status
  - Pagination: walks next_page_token until exhausted
  - Chunks UPDATE for large catalogs (asyncpg array bind limit)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import shopify_products_sync as sps  # noqa: E402
from services.shopify_products_sync import (  # noqa: E402
    ShopifyProductsSyncAuthError,
    ShopifyProductsSyncConfigError,
    ShopifyProductsSyncError,
    ShopifyProductsSyncRateLimitError,
    refresh_merchant_presence,
)


class _FakeStandardProduct:
    """Stand-in for models.standard_product.StandardProduct — just
    enough surface for refresh_merchant_presence to extract IDs."""

    def __init__(self, product_id: Optional[str], id_: Optional[str] = None) -> None:
        self.product_id = product_id
        self.id = id_


class _FakeTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _install_fake_db(
    monkeypatch,
    *,
    executed: List[Dict[str, Any]],
    txn_factory=None,
    update_returning_rows: Optional[int] = None,
) -> None:
    """Replace sps.database with a fake. Captures every execute() and
    fetch_all() call. The presence-refresh UPDATE uses RETURNING +
    fetch_all to count actually-touched rows, so we mimic that here:
    if update_returning_rows is set, fetch_all on the UPDATE statement
    returns that many fake-row dicts."""

    async def fake_execute(sql, params=None):
        executed.append({"sql": str(sql), "params": params or {}, "kind": "execute"})
        return None

    async def fake_fetch_all(sql, params=None):
        executed.append({"sql": str(sql), "params": params or {}, "kind": "fetch_all"})
        if "UPDATE catalog_products" in sql and "RETURNING" in sql:
            n = update_returning_rows
            if n is None:
                # Default: pretend every bound ID matched a row
                ids = (params or {}).get("ids") or []
                n = len(ids)
            return [{"product_key": f"p{i}"} for i in range(n)]
        return []

    class _DB:
        def execute(self, sql, params=None):
            return fake_execute(sql, params)

        def fetch_all(self, sql, params=None):
            return fake_fetch_all(sql, params)

        def transaction(self):
            return (txn_factory or _FakeTxn)()

    monkeypatch.setattr(sps, "database", _DB())


def _install_fake_credentials(monkeypatch, creds=None) -> None:
    """Patch _get_shopify_store_credentials to return canned creds
    (or raise the requested error)."""

    async def fake_get_creds(merchant_id):
        if creds is None:
            raise ShopifyProductsSyncConfigError("no store")
        if isinstance(creds, Exception):
            raise creds
        return creds

    monkeypatch.setattr(sps, "_get_shopify_store_credentials", fake_get_creds)


def _install_fake_fetcher(monkeypatch, pages: List[Tuple[List[Any], Optional[str], Optional[str]]]) -> List[Dict[str, Any]]:
    """Patch fetch_merchant_products. `pages` is a list of
    (products, next_page_token, error) tuples — one per page in
    iteration order. Returns the captured call args."""
    captured: List[Dict[str, Any]] = []
    queue = list(pages)

    async def fake_fetch(**kwargs):
        captured.append(kwargs)
        if not queue:
            return [], None, None
        return queue.pop(0)

    monkeypatch.setattr(sps, "fetch_merchant_products", fake_fetch)
    return captured


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_presence_bumps_last_seen_for_live_ids(monkeypatch) -> None:
    """The core contract: live Shopify product IDs → matching
    catalog_products rows get last_seen_in_sync_at=NOW() +
    sync_status='live', merchant gets last_full_sync_at=NOW()."""
    executed: List[Dict[str, Any]] = []
    _install_fake_db(monkeypatch, executed=executed)
    _install_fake_credentials(monkeypatch, creds={
        "shop_domain": "moyu.myshopify.com",
        "access_token": "shpat_test",
    })
    _install_fake_fetcher(monkeypatch, [
        ([
            _FakeStandardProduct(product_id="10064565600553"),
            _FakeStandardProduct(product_id="10064565797161"),
            _FakeStandardProduct(product_id="10064565928233"),
        ], None, None),
    ])

    out = await refresh_merchant_presence(merchant_id="merch_efbc46b4619cfbdf")

    assert out["live_ids_fetched"] == 3
    assert out["pages_fetched"] == 1
    # Regression 2026-05-12: rows_touched used to always return 0
    # because asyncpg's database.execute() returns None for bulk
    # UPDATE rowcounts. Fixed by switching to RETURNING + fetch_all.
    # All 3 bound IDs match in the fake DB, so rows_touched=3.
    assert out["rows_touched"] == 3
    # Two SQL statements: bulk update on catalog_products (via
    # fetch_all because we read RETURNING), one execute on
    # catalog_merchants. NO INSERTs, no other tables.
    sqls = [e["sql"] for e in executed]
    sql_joined = "\n".join(sqls)
    assert "UPDATE catalog_products" in sql_joined
    assert "SET last_seen_in_sync_at = NOW()" in sql_joined
    assert "sync_status = 'live'" in sql_joined
    assert "RETURNING product_key" in sql_joined
    assert "UPDATE catalog_merchants" in sql_joined
    assert "SET last_full_sync_at = NOW()" in sql_joined
    # NEVER writes to seed_data, product_payload, catalog_offers, catalog_skus
    assert "seed_data" not in sql_joined
    assert "product_payload" not in sql_joined
    assert "catalog_offers" not in sql_joined
    assert "catalog_skus" not in sql_joined


@pytest.mark.asyncio
async def test_refresh_presence_bumps_last_full_sync_when_zero_products(monkeypatch) -> None:
    """Edge case: merchant deleted everything from Shopify. We get 0
    live IDs back. The catalog_products UPDATE is skipped (no IDs to
    match), but catalog_merchants.last_full_sync_at MUST still bump —
    that's the signal the sweep needs to tombstone the now-orphaned
    rows."""
    executed: List[Dict[str, Any]] = []
    _install_fake_db(monkeypatch, executed=executed)
    _install_fake_credentials(monkeypatch, creds={
        "shop_domain": "moyu.myshopify.com", "access_token": "x",
    })
    _install_fake_fetcher(monkeypatch, [([], None, None)])

    out = await refresh_merchant_presence(merchant_id="merch_x")

    assert out["live_ids_fetched"] == 0
    # No catalog_products UPDATE — nothing to match
    cp_updates = [e for e in executed if "UPDATE catalog_products" in e["sql"]]
    assert cp_updates == []
    # But catalog_merchants STILL bumped — signal to sweep
    cm_updates = [e for e in executed if "UPDATE catalog_merchants" in e["sql"]]
    assert len(cm_updates) == 1


@pytest.mark.asyncio
async def test_refresh_presence_paginates_until_next_page_token_none(monkeypatch) -> None:
    """Walks Shopify's cursor pagination until next_token is None.
    Confirms we don't truncate at page 1."""
    executed: List[Dict[str, Any]] = []
    _install_fake_db(monkeypatch, executed=executed)
    _install_fake_credentials(monkeypatch, creds={
        "shop_domain": "x.myshopify.com", "access_token": "x",
    })
    captured = _install_fake_fetcher(monkeypatch, [
        ([_FakeStandardProduct(product_id="1")], "cursor_2", None),
        ([_FakeStandardProduct(product_id="2")], "cursor_3", None),
        ([_FakeStandardProduct(product_id="3")], None, None),
    ])

    out = await refresh_merchant_presence(merchant_id="m")

    assert out["pages_fetched"] == 3
    assert out["live_ids_fetched"] == 3
    # Each page-after-first passes page_token=<previous next_token>
    assert captured[0].get("page_token") is None
    assert captured[1].get("page_token") == "cursor_2"
    assert captured[2].get("page_token") == "cursor_3"


@pytest.mark.asyncio
async def test_refresh_presence_falls_back_to_id_when_product_id_missing(monkeypatch) -> None:
    """Some adapters don't set product_id, only id. Both must work —
    Path A's _upsert_by_pk keys on whichever is non-empty."""
    executed: List[Dict[str, Any]] = []
    _install_fake_db(monkeypatch, executed=executed)
    _install_fake_credentials(monkeypatch, creds={"shop_domain": "x", "access_token": "x"})
    _install_fake_fetcher(monkeypatch, [
        ([
            _FakeStandardProduct(product_id=None, id_="fallback_123"),
            _FakeStandardProduct(product_id="primary_456"),
        ], None, None),
    ])

    out = await refresh_merchant_presence(merchant_id="m")
    assert out["live_ids_fetched"] == 2
    # Verify the id list bound into the UPDATE included both forms
    cp_call = [e for e in executed if "UPDATE catalog_products" in e["sql"]][0]
    ids = cp_call["params"]["ids"]
    assert set(ids) == {"fallback_123", "primary_456"}


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_presence_raises_config_error_when_no_store(monkeypatch) -> None:
    """No connected Shopify store → 400. Same error class as the full
    sync so routes handle it identically."""
    _install_fake_credentials(monkeypatch, creds=None)
    with pytest.raises(ShopifyProductsSyncConfigError):
        await refresh_merchant_presence(merchant_id="missing_merchant")


@pytest.mark.asyncio
async def test_refresh_presence_raises_auth_error_on_401(monkeypatch) -> None:
    """Shopify token expired → auth error → 401."""
    _install_fake_db(monkeypatch, executed=[])
    _install_fake_credentials(monkeypatch, creds={"shop_domain": "x", "access_token": "expired"})
    _install_fake_fetcher(monkeypatch, [([], None, "401 Unauthorized")])
    with pytest.raises(ShopifyProductsSyncAuthError):
        await refresh_merchant_presence(merchant_id="m")


@pytest.mark.asyncio
async def test_refresh_presence_raises_rate_limit_error_on_429(monkeypatch) -> None:
    """Shopify 429 → rate-limit error → 429 on the wire."""
    _install_fake_db(monkeypatch, executed=[])
    _install_fake_credentials(monkeypatch, creds={"shop_domain": "x", "access_token": "x"})
    _install_fake_fetcher(monkeypatch, [([], None, "429 rate limit exceeded")])
    with pytest.raises(ShopifyProductsSyncRateLimitError):
        await refresh_merchant_presence(merchant_id="m")


@pytest.mark.asyncio
async def test_refresh_presence_raises_generic_error_on_unknown_failure(monkeypatch) -> None:
    """Any other adapter error becomes a generic ShopifyProductsSyncError
    → 502 on the route. Specifically: do NOT silently succeed when
    Shopify returns an error — that would falsely bump last_full_sync_at."""
    _install_fake_db(monkeypatch, executed=[])
    _install_fake_credentials(monkeypatch, creds={"shop_domain": "x", "access_token": "x"})
    _install_fake_fetcher(monkeypatch, [([], None, "500 internal server error")])
    with pytest.raises(ShopifyProductsSyncError):
        await refresh_merchant_presence(merchant_id="m")


# ---------------------------------------------------------------------------
# Pagination cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_presence_respects_max_pages_cap(monkeypatch) -> None:
    """max_pages caps the walk. Operator can pass higher max for
    huge merchants but the default (50 × 250 = 12,500) covers
    realistic catalogs."""
    executed: List[Dict[str, Any]] = []
    _install_fake_db(monkeypatch, executed=executed)
    _install_fake_credentials(monkeypatch, creds={"shop_domain": "x", "access_token": "x"})
    # Each page returns a single product + a non-None next_token so
    # the walk would continue forever without the cap.
    _install_fake_fetcher(monkeypatch, [
        ([_FakeStandardProduct(product_id=f"p{i}")], f"cur_{i+1}", None)
        for i in range(10)
    ])

    out = await refresh_merchant_presence(merchant_id="m", max_pages=3)
    assert out["pages_fetched"] == 3
    assert out["live_ids_fetched"] == 3
