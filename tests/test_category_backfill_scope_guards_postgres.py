"""Run the widened backfill SQL and canonical-scope classifier on real rows."""

import os
import uuid

import pytest

from services.pdp_scope_classifier import SCOPE_CANONICAL, ScopeSignals, classify

URL = os.getenv("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL.startswith("postgres"), reason="requires test Postgres")


@pytest.fixture
async def backfill_db(monkeypatch):
    import asyncpg
    from databases import Database
    from scripts import backfill_pdp_category_path as backfill

    if "test" not in URL.rsplit("/", 1)[-1]:
        pytest.skip("backfill fixtures require a test database")
    schema = "test_category_guards_" + uuid.uuid4().hex
    conn = await asyncpg.connect(URL)
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    await conn.execute(f'SET search_path TO "{schema}"')
    await conn.execute("""
        CREATE TABLE catalog_products (
            product_key text PRIMARY KEY, pivota_signature_id text, brand text,
            category text, product_type text, title text, category_path text,
            category_label_source text, category_confidence double precision,
            pdp_scope text NOT NULL DEFAULT 'multi_merchant_canonical');
    """)
    db = Database(URL, server_settings={"search_path": schema})
    await db.connect()
    monkeypatch.setattr(backfill, "database", db)
    try:
        yield backfill, db
    finally:
        await db.disconnect()
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await conn.close()


async def add(db, key, path, *, source="enrichment_agent_v1", title="Matte Lipstick"):
    await db.execute(
        "INSERT INTO catalog_products(product_key,title,category_path,category_label_source) "
        "VALUES(:key,:title,:path,:source)",
        {"key": key, "title": title, "path": path, "source": source})


async def test_guarded_deepening_preserves_canonical_lane_and_scope(backfill_db):
    backfill, db = backfill_db
    await add(db, "canonical", "beauty/makeup")
    assert await backfill._apply_update(
        "canonical", "beauty/makeup/lip/lipstick", previous_path="beauty/makeup") is True
    row = await db.fetch_one("SELECT * FROM catalog_products WHERE product_key='canonical'")
    assert row["category_path"] == "beauty/makeup/lip/lipstick"
    assert row["category_label_source"] == "enrichment_agent_v1"
    assert row["category_confidence"] == 0.85
    assert row["pdp_scope"] == SCOPE_CANONICAL
    # Recompute, do not merely observe the unchanged stored scope column. Losing
    # the source marker demotes this one-seller row on the next scope pass.
    assert classify(ScopeSignals(category_label_source=row["category_label_source"], seller_count=1)) == SCOPE_CANONICAL


async def test_stale_previous_path_declines_without_clobbering_concurrent_decision(backfill_db):
    backfill, db = backfill_db
    await add(db, "raced", "beauty/makeup")
    before = (await backfill._fetch_batch(10, None, include_shallow=True))[0]
    await db.execute("UPDATE catalog_products SET category_path='beauty/makeup/lip/gloss', "
                     "category_label_source='manual_review', category_confidence=1 WHERE product_key='raced'")
    assert await backfill._apply_update(
        "raced", "beauty/makeup/lip/lipstick", previous_path=before["category_path"]) is False
    row = await db.fetch_one("SELECT * FROM catalog_products WHERE product_key='raced'")
    assert (row["category_path"], row["category_label_source"], row["category_confidence"]) == (
        "beauty/makeup/lip/gloss", "manual_review", 1)


async def test_default_fetch_includes_null_and_bare_beauty_but_not_other_shallow_rows(backfill_db):
    backfill, db = backfill_db
    for key, path in [("empty", None), ("bare", "beauty"), ("interior", "beauty/makeup"),
                      ("short-leaf", "fashion/shoes")]:
        await add(db, key, path)
    default_rows = await backfill._fetch_batch(10, None)
    assert {row["product_key"] for row in default_rows} == {"empty", "bare"}
    report = await backfill.run_category_path_backfill(dry_run=True, include_shallow=True)
    assert report["skipped_already_routable"] == 1
    assert (await db.fetch_one("SELECT category_path FROM catalog_products WHERE product_key='bare'"))["category_path"] == "beauty"
