"""Tests for scripts/backfill_canonical_chain_for_path_b_mirror.py.

This is the heal script for the Phase 7d gap: existing Path B rows in
prod (mirrored before 7d) have no catalog_skus / catalog_offers. The
mirror's --apply path only processes new rows, so it can't heal them.

Tests cover the orchestration logic — SELECT shape, dry-run no-write
guarantee, apply path calls the chain helpers, idempotency on the
JOIN side."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import backfill_canonical_chain_for_path_b_mirror as backfill  # noqa: E402


def _ns(**kwargs) -> SimpleNamespace:
    base = {"limit": 0, "apply": False}
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_select_sql_filters_to_path_b_with_no_offers():
    """Pin the SELECT shape — it must filter to merchant_id='external_seed'
    AND only rows missing a catalog_offers row. Otherwise the heal would
    either touch wrong rows (other merchants) or re-process already-healed
    rows (waste)."""
    sql = backfill.SELECT_SQL_NO_LIMIT
    assert "FROM catalog_products cp" in sql
    assert "LEFT JOIN catalog_offers o ON o.product_key = cp.product_key" in sql
    assert "LEFT JOIN external_product_seeds eps" in sql
    assert "cp.merchant_id = :merchant_id" in sql
    assert "o.offer_id IS NULL" in sql
    # DISTINCT ON deduplicates when multiple seed rows share an external_product_id
    assert "DISTINCT ON (cp.product_key)" in sql


def test_build_row_dict_for_chain_maps_join_columns_to_helper_keys():
    """The chain helpers expect mirror-script-style row dict keys
    (`id`, `external_product_id`, `price_amount`, etc.). The SELECT
    aliases the seed id to `seed_id` to avoid collision with the
    `id` column on catalog_products. Adapter must rename it back."""
    joined_row = {
        "product_key": "prod::external_seed::external_seed::ext_abc",
        "external_product_id": "ext_abc",
        "title": "Test Lipstick",
        "image_url": "https://example.com/img.jpg",
        "seed_id": "eps_123",
        "price_amount": 28.5,
        "price_currency": "USD",
        "availability": "in_stock",
        "destination_url": "https://example.com/p/x",
        "canonical_url": "https://example.com/p/x",
        "domain": "example.com",
        "market": "US",
    }
    out = backfill._build_row_dict_for_chain(joined_row)
    # Adapter renames seed_id → id (what _upsert_canonical_offer expects)
    assert out["id"] == "eps_123"
    assert out["external_product_id"] == "ext_abc"
    assert out["price_amount"] == 28.5
    assert out["price_currency"] == "USD"
    assert out["availability"] == "in_stock"
    assert out["destination_url"] == "https://example.com/p/x"
    assert out["domain"] == "example.com"
    assert out["market"] == "US"
    # title + image_url come from catalog_products (already populated
    # at original mirror time), not external_product_seeds
    assert out["title"] == "Test Lipstick"
    assert out["image_url"] == "https://example.com/img.jpg"


@pytest.mark.asyncio
async def test_drive_dry_run_does_not_call_chain_helpers(monkeypatch):
    """Default invocation (no --apply) must NEVER call _upsert_*. The
    histogram + sample rows should still populate so operators can audit
    the heal scope before applying."""
    rows = [
        {
            "product_key": "p1",
            "external_product_id": "ext_1",
            "title": "Product 1",
            "image_url": "https://x/1.jpg",
            "seed_id": "eps_1",
            "price_amount": 10.0,
            "price_currency": "USD",
            "availability": "in_stock",
            "destination_url": "https://example.com/1",
            "canonical_url": None,
            "domain": "example.com",
            "market": "US",
        },
        {
            "product_key": "p2",
            "external_product_id": "ext_2",
            "title": "Product 2",
            "image_url": "https://x/2.jpg",
            "seed_id": None,  # no matching external_product_seeds row
            "price_amount": None,
            "price_currency": None,
            "availability": None,
            "destination_url": None,
            "canonical_url": None,
            "domain": None,
            "market": None,
        },
    ]

    sku_calls: list = []
    offer_calls: list = []
    merchant_calls: list = []

    async def fake_fetch_all(_sql, _params):
        return rows

    async def fake_upsert_sku(*args, **_kwargs):
        sku_calls.append(args)

    async def fake_upsert_offer(*args, **_kwargs):
        offer_calls.append(args)

    async def fake_ensure_merchant():
        merchant_calls.append("called")

    monkeypatch.setattr(backfill.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(backfill, "_upsert_canonical_sku_for_mirror_row", fake_upsert_sku)
    monkeypatch.setattr(backfill, "_upsert_canonical_offer_for_mirror_row", fake_upsert_offer)
    monkeypatch.setattr(backfill, "_ensure_external_seed_merchant", fake_ensure_merchant)

    report = await backfill._drive(_ns(apply=False))

    assert sku_calls == []
    assert offer_calls == []
    assert merchant_calls == []
    assert report["candidate_count"] == 2
    assert report["applied_count"] == 0
    assert report["rows_with_seed_data"] == 1
    assert report["rows_with_price"] == 1


@pytest.mark.asyncio
async def test_drive_apply_calls_helpers_per_row_after_merchant_upsert(monkeypatch):
    """With --apply: _ensure_external_seed_merchant runs ONCE before
    the loop (FK target for catalog_offers); each row gets one sku
    upsert + one offer upsert."""
    rows = [
        {
            "product_key": "p1",
            "external_product_id": "ext_1",
            "title": "Product 1",
            "image_url": "https://x/1.jpg",
            "seed_id": "eps_1",
            "price_amount": 10.0,
            "price_currency": "USD",
            "availability": "in_stock",
            "destination_url": "https://example.com/1",
            "canonical_url": None,
            "domain": "example.com",
            "market": "US",
        },
        {
            "product_key": "p2",
            "external_product_id": "ext_2",
            "title": "Product 2",
            "image_url": "https://x/2.jpg",
            "seed_id": "eps_2",
            "price_amount": 20.0,
            "price_currency": "USD",
            "availability": "in_stock",
            "destination_url": "https://example.com/2",
            "canonical_url": None,
            "domain": "example.com",
            "market": "US",
        },
    ]

    call_order: list = []

    async def fake_fetch_all(_sql, _params):
        return rows

    async def fake_upsert_sku(product_key, _row_dict):
        call_order.append(("sku", product_key))

    async def fake_upsert_offer(product_key, _row_dict):
        call_order.append(("offer", product_key))

    async def fake_ensure_merchant():
        call_order.append(("merchant", None))

    monkeypatch.setattr(backfill.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(backfill, "_upsert_canonical_sku_for_mirror_row", fake_upsert_sku)
    monkeypatch.setattr(backfill, "_upsert_canonical_offer_for_mirror_row", fake_upsert_offer)
    monkeypatch.setattr(backfill, "_ensure_external_seed_merchant", fake_ensure_merchant)

    report = await backfill._drive(_ns(apply=True))

    # Merchant upsert MUST be first — FK target for offers
    assert call_order[0] == ("merchant", None)
    # Per row: sku then offer (the FK chain order)
    assert call_order[1:] == [
        ("sku", "p1"), ("offer", "p1"),
        ("sku", "p2"), ("offer", "p2"),
    ]
    assert report["applied_count"] == 2
    assert report["chain_failures"] == 0


@pytest.mark.asyncio
async def test_drive_apply_continues_on_per_row_failure(monkeypatch):
    """One bad row shouldn't kill the whole heal. A failing chain
    write logs + increments chain_failures + the loop continues to
    the next row. Re-running the script picks up where this one left
    off (idempotent select + update)."""
    rows = [
        {
            "product_key": "p_good",
            "external_product_id": "ext_good",
            "title": "Good Product",
            "image_url": "https://x/g.jpg",
            "seed_id": "eps_g",
            "price_amount": 10.0,
            "price_currency": "USD",
            "availability": "in_stock",
            "destination_url": "https://example.com/g",
            "canonical_url": None,
            "domain": "example.com",
            "market": "US",
        },
        {
            "product_key": "p_bad",
            "external_product_id": "ext_bad",
            "title": "Bad Product",
            "image_url": "https://x/b.jpg",
            "seed_id": "eps_b",
            "price_amount": None,
            "price_currency": None,
            "availability": None,
            "destination_url": None,
            "canonical_url": None,
            "domain": None,
            "market": None,
        },
    ]

    sku_calls: list = []

    async def fake_fetch_all(_sql, _params):
        return rows

    async def fake_upsert_sku(product_key, _row_dict):
        sku_calls.append(product_key)
        if product_key == "p_bad":
            raise RuntimeError("simulated DB failure")

    async def fake_upsert_offer(product_key, _row_dict):
        return None

    async def fake_ensure_merchant():
        return None

    monkeypatch.setattr(backfill.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(backfill, "_upsert_canonical_sku_for_mirror_row", fake_upsert_sku)
    monkeypatch.setattr(backfill, "_upsert_canonical_offer_for_mirror_row", fake_upsert_offer)
    monkeypatch.setattr(backfill, "_ensure_external_seed_merchant", fake_ensure_merchant)

    report = await backfill._drive(_ns(apply=True))

    # Both rows attempted (loop kept going through the failure)
    assert sku_calls == ["p_good", "p_bad"]
    # One success, one failure
    assert report["applied_count"] == 1
    assert report["chain_failures"] == 1
