"""The post-apply readback's SQL, executed on real Postgres.

The pipeline tests fake `db.fetch_all`, so they never run the readback query itself. This file does:
the per-PRODUCT evidence (priced offer, image, resolved identity) must come from the applied row, not
from the content_key-grain index_pipeline_state flags a sibling can own, and the query must plan
against the real schema (column names, the EXISTS helpers, the ANY(:list) binds).
"""
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

URL = os.getenv("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL.startswith("postgres"), reason="requires test Postgres")

PREFIX = "rbq-gate-"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
async def db():
    import asyncpg
    from databases import Database
    from sqlalchemy import create_engine

    import db.catalog  # noqa: F401  (registers catalog_products / catalog_offers on the MetaData)
    from db.database import metadata

    if "test" not in urlsplit(URL).path.rsplit("/", 1)[-1] and not URL.endswith("pivota_dialect_check"):
        pytest.skip("synthetic readback tests require a test database")
    engine = create_engine(URL)
    metadata.create_all(engine, checkfirst=True)
    engine.dispose()
    connection = await asyncpg.connect(URL)
    try:
        for mig in ("045_product_groups.sql", "098_index_pipeline_state.sql"):
            await connection.execute((ROOT / "db" / "migrations" / mig).read_text())
    finally:
        await connection.close()
    database = Database(URL)
    await database.connect()
    try:
        yield database
    finally:
        await database.execute("DELETE FROM catalog_offers WHERE offer_id LIKE :p", {"p": PREFIX + "%"})
        await database.execute("DELETE FROM index_pipeline_state WHERE content_key LIKE :p", {"p": PREFIX + "%"})
        await database.execute("DELETE FROM catalog_products WHERE product_key LIKE :p", {"p": PREFIX + "%"})
        await database.disconnect()


async def _product(db, key, *, content_key, image=True, scope="multi_merchant_canonical"):
    await db.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, category_path,"
        " image_url, pdp_scope, content_key, pdp_lifecycle_stage) VALUES (:k, 'rbq_m', 'external_seed', :k, 'Tint',"
        " 'beauty/makeup/lip/tint', :img, :scope, :ck, 'published')",
        {"k": key, "img": "https://cdn.example/i.jpg" if image else None, "scope": scope, "ck": content_key})


async def _offer(db, key, *, price=18.0):
    await db.execute(
        "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, currency, merchant_effective_price)"
        " VALUES (:o, :k, :k, 'rbq_m', 'USD', :p)", {"o": PREFIX + "o-" + key, "k": key, "p": price})


async def _ips(db, content_key, *, blocker="low_quality", serving=False):
    await db.execute(
        "INSERT INTO index_pipeline_state (content_key, pipeline_stage, blocker_code, blocker_detail, serving_eligible,"
        " has_price, identity_resolved, has_image) VALUES (:ck, 'extracted', :b, 'content_quality_score=71.2 < 71.4',"
        " :s, TRUE, TRUE, TRUE)", {"ck": content_key, "b": blocker, "s": serving})


async def test_a_content_refusal_on_a_priced_resolved_imaged_product_is_a_note(db):
    from services.retailer_ingest.pipeline import _readback
    key, ck = PREFIX + "a", PREFIX + "ck-a"
    await _product(db, key, content_key=ck)
    await _offer(db, key)
    await _ips(db, ck)
    out = await _readback([key], "USD", db, planned_images={key: True})
    assert out["problems"] == [] and out["ok"]
    [note] = out["notes"]
    assert note["kind"] == "index_refused" and "low_quality" in note["note"]


async def test_a_priced_sibling_on_the_shared_content_key_cannot_vouch_for_an_unpriced_product(db):
    """The IPS row (one per content_key) says has_price=TRUE because the SIBLING is priced; the applied
    product itself has no priced offer, so its refusal must fail the readback."""
    from services.retailer_ingest.pipeline import _readback
    ck = PREFIX + "ck-shared"
    applied, sibling = PREFIX + "applied", PREFIX + "sibling"
    await _product(db, applied, content_key=ck)
    await _product(db, sibling, content_key=ck)
    await _offer(db, sibling)                      # only the sibling is priced
    await _offer(db, applied, price=0)             # the applied row's offer carries no price
    await _ips(db, ck)
    out = await _readback([applied], "USD", db, planned_images={applied: True})
    assert not out["ok"]
    assert out["problems"][0]["problem"].startswith("not serving-eligible (blocker low_quality)")


async def test_a_planned_image_that_did_not_land_fails_even_on_a_content_refusal(db):
    from services.retailer_ingest.pipeline import _readback
    key, ck = PREFIX + "img", PREFIX + "ck-img"
    await _product(db, key, content_key=ck, image=False)
    await _offer(db, key)
    await _ips(db, ck, blocker="no_image")
    assert not (await _readback([key], "USD", db, planned_images={key: True}))["ok"]
    assert (await _readback([key], "USD", db, planned_images={key: False}))["ok"]


async def test_an_unresolved_identity_fails_even_on_a_content_refusal(db):
    from services.retailer_ingest.pipeline import _readback
    key, ck = PREFIX + "id", PREFIX + "ck-id"
    await _product(db, key, content_key=ck, scope="single_merchant")  # not a resolved scope, no group member
    await _offer(db, key)
    await _ips(db, ck)
    assert not (await _readback([key], "USD", db, planned_images={key: True}))["ok"]
