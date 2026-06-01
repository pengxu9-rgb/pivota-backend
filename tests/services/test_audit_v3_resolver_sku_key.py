"""Regression test for the v3 per-SKU resolver sku_key aliasing bug.

Background
----------
The first live Ownist pilot run passed the shape gate (`audit_mode: per_sku`)
but every per-SKU score came back null with `missing_inputs: ["catalog_skus"]`.
Root cause: `_resolve_merchant_and_products` set ``sku_key = product_key`` on
each product dict. But `catalog_skus.sku_key` is minted as
``<product_key>::v::<variant_id>`` (services/catalog_variant_promoter.py), never
the bare product_key. Pre-setting sku_key made `_sku_keys_for_per_sku_mode`
short-circuit (it returns early when any sku_key is present, skipping the
catalog_skus lookup), so `load_sku_context` queried
``WHERE sku_key = <product_key>``, found nothing, and blocked every dimension.

Why this test exists / what makes it different
----------------------------------------------
`tests/integration/test_audit_v3_end_to_end.py` monkeypatches BOTH
`_resolve_merchant_and_products` AND `load_sku_context`, so it never exercises
the real seam that broke (same blind spot that hid PR #706). This test drives
the REAL resolver, the REAL per-SKU key expansion, and the REAL SKU-context
loader against a seeded sqlite catalog — only the unrelated merchant-identity
lookup is stubbed.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

import pytest
from databases import Database

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from db.catalog import catalog_products, catalog_skus

MERCHANT = "merch_test_resolver_001"
PRODUCT_KEY = "rk_test_ownist_p1"
# Real catalog_skus.sku_key per the minting convention: <product_key>::v::<vid>
SKU_KEY = f"{PRODUCT_KEY}::v::var1"
TITLE = "Triple Shine Grape"
CANONICAL_URL = "https://ownist.com/products/triple-shine-grape"


def _create_table_sql(table) -> str:
    # sqlite is dynamically typed; declaring every model column as TEXT lets the
    # real SQLAlchemy `catalog_products.select()` (which names all columns)
    # succeed without reproducing the production Postgres types.
    cols = ", ".join(f'"{c.name}" TEXT' for c in table.columns)
    return f"CREATE TABLE IF NOT EXISTS {table.name} ({cols})"


async def _make_seeded_db(tmp_path) -> Database:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit_resolver_test.db'}")
    await db.connect()
    await db.execute(_create_table_sql(catalog_products))
    await db.execute(_create_table_sql(catalog_skus))
    # canonical_url is set so the resolver skips the pivota-canonical mint branch.
    await db.execute(
        "INSERT INTO catalog_products "
        "(product_key, merchant_id, platform, source_product_id, title, brand, "
        " product_type, canonical_url) "
        "VALUES (:pk, :m, :plat, :spid, :title, :brand, :ptype, :url)",
        {"pk": PRODUCT_KEY, "m": MERCHANT, "plat": "shopify", "spid": "sp1",
         "title": TITLE, "brand": "Ownist", "ptype": "serum", "url": CANONICAL_URL},
    )
    await db.execute(
        "INSERT INTO catalog_skus "
        "(sku_key, product_key, merchant_id, platform, source_product_id, "
        " source_variant_id, title) "
        "VALUES (:sk, :pk, :m, :plat, :spid, :svid, :title)",
        {"sk": SKU_KEY, "pk": PRODUCT_KEY, "m": MERCHANT, "plat": "shopify",
         "spid": "sp1", "svid": "var1", "title": TITLE},
    )
    return db


def _bind(monkeypatch, db: Database) -> None:
    # Repoint the global DB handle (the functions do `from db.database import
    # database` lazily) and a stubbed merchant-identity lookup. We do NOT patch
    # any of the functions under test.
    monkeypatch.setattr("db.database.database", db)

    async def _fake_onboarding(merchant_id: str) -> Dict[str, Any]:
        return {"business_name": "Ownist Test Merchant", "store_url": "https://ownist.com"}

    monkeypatch.setattr("db.merchant_onboarding.get_merchant_onboarding", _fake_onboarding)

    # load_sku_context memoizes results in a module-global cache; clear it so
    # each test sees freshly-seeded rows.
    import services.agent_center_bd_report_service as bd
    bd._SKU_CONTEXT_CACHE.clear()


async def test_resolver_does_not_alias_product_key_into_sku_key(monkeypatch, tmp_path) -> None:
    """The fix: the resolver must not plant product_key into the sku_key slot."""
    db = await _make_seeded_db(tmp_path)
    _bind(monkeypatch, db)
    try:
        from services.audit_run_worker import _resolve_merchant_and_products
        _, _, products, _, _ = await _resolve_merchant_and_products(
            merchant_id=MERCHANT, product_keys=[PRODUCT_KEY],
        )
        assert len(products) == 1
        product = products[0]
        assert product["product_key"] == PRODUCT_KEY
        # The bug was sku_key == product_key. After the fix, sku_key must not be
        # the bare product key (absent/None/empty all acceptable).
        assert (product.get("sku_key") or "") != PRODUCT_KEY
    finally:
        await db.disconnect()


async def test_per_sku_expansion_resolves_real_variant_keys(monkeypatch, tmp_path) -> None:
    """Full real chain: resolver output -> per-SKU key expansion -> real ::v:: key."""
    db = await _make_seeded_db(tmp_path)
    _bind(monkeypatch, db)
    try:
        from services.audit_run_worker import _resolve_merchant_and_products
        from services.agent_center_bd_report_service import _sku_keys_for_per_sku_mode

        _, _, products, _, _ = await _resolve_merchant_and_products(
            merchant_id=MERCHANT, product_keys=[PRODUCT_KEY],
        )
        sku_keys = await _sku_keys_for_per_sku_mode(products, MERCHANT)
        assert sku_keys == [SKU_KEY]
        assert PRODUCT_KEY not in sku_keys  # the bug returned the bare product key
    finally:
        await db.disconnect()


async def test_load_sku_context_resolves_on_real_sku_key(monkeypatch, tmp_path) -> None:
    """load_sku_context resolves real catalog_skus rows -> no blocked dimensions."""
    db = await _make_seeded_db(tmp_path)
    _bind(monkeypatch, db)
    try:
        from services.agent_center_bd_report_service import load_sku_context
        ctx = await load_sku_context(SKU_KEY, MERCHANT)
        assert ctx.get("missing_inputs") != ["catalog_skus"]
        # The SKU row resolved, carrying a real title (not the internal key).
        sku = ctx.get("sku") or {}
        assert sku.get("title") == TITLE or ctx.get("sku_title") == TITLE
    finally:
        await db.disconnect()


async def test_load_sku_context_misses_on_bare_product_key(monkeypatch, tmp_path) -> None:
    """Documents the original failure: querying by product_key misses catalog_skus.

    This is exactly what the aliasing bug caused — every dimension blocked with
    missing_inputs == ["catalog_skus"].
    """
    db = await _make_seeded_db(tmp_path)
    _bind(monkeypatch, db)
    try:
        from services.agent_center_bd_report_service import load_sku_context
        ctx = await load_sku_context(PRODUCT_KEY, MERCHANT)
        assert ctx.get("missing_inputs") == ["catalog_skus"]
    finally:
        await db.disconnect()


async def test_prealiased_sku_key_short_circuits_expansion(monkeypatch, tmp_path) -> None:
    """Guard: if sku_key is pre-aliased to product_key (the bug), expansion
    short-circuits and never resolves the real variant key. Locks in WHY the
    resolver must not set sku_key."""
    db = await _make_seeded_db(tmp_path)
    _bind(monkeypatch, db)
    try:
        from services.agent_center_bd_report_service import _sku_keys_for_per_sku_mode
        buggy_products = [{"product_key": PRODUCT_KEY, "sku_key": PRODUCT_KEY}]
        sku_keys = await _sku_keys_for_per_sku_mode(buggy_products, MERCHANT)
        assert sku_keys == [PRODUCT_KEY]  # short-circuited; real ::v:: key never resolved
    finally:
        await db.disconnect()
