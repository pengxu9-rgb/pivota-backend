"""A drain row's structured image gallery, through both apply executors on real PostgreSQL.

tests/services/test_drain_structured_image_gallery.py covers the producer, ingestion and the guard on fakes.
This file runs the real upsert SQL (per-row and the batched executor's split multi-row statement), the
canonical-owner guard, migration 138's content-change trigger and the real PDP-view assembler on an
isolated schema (the fixture of tests/test_curated_primary_readiness_postgres.py), with
ingestion.STRUCTURED_GALLERY_FLAG on unless a test says otherwise. Never run against production. Hosts and
names are made up.
"""
import copy
import json
import os
from pathlib import Path

import pytest

from tests.test_curated_primary_readiness_postgres import readiness_db  # noqa: F401 -- fixture

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgres"), reason="requires test PostgreSQL"
)
ROOT = Path(__file__).parents[1]
CDN = "https://cdn.example/files"
OWNER = "owner-store.example"


@pytest.fixture(autouse=True)
def gallery_on(monkeypatch):
    from services.catalog_enrichment_agent.ingestion import STRUCTURED_GALLERY_FLAG

    monkeypatch.setenv(STRUCTURED_GALLERY_FLAG, "1")


def _record(images, *, host=OWNER, source_role="brand_official", currency="USD"):
    from services.curated_brand_feed import shopify_product_to_record

    product = {
        "id": 930000101, "handle": "gallery-glow-serum", "vendor": "Example Gallery", "title": "Glow Serum",
        "product_type": "Face Serum", "tags": ["Serum", "Skincare"],
        "body_html": "<p>A lightweight hydrating face serum with a dewy finish. Apply two drops to clean skin "
                     "morning and night, then follow with moisturiser. For external use only. Avoid contact "
                     "with eyes.</p>",
        "images": [{"src": f"{CDN}/{name}"} for name in images],
        "variants": [{"id": 450000093000101, "price": "24.00", "available": True, "title": "Original"}],
    }
    return shopify_product_to_record(product, domain=host, category_path="beauty", currency=currency,
                                     brand_override="Example Gallery", source_role=source_role,
                                     retailer_name=host if source_role == "retailer" else None,
                                     emit_native_variants=True)


def _plan(record, market="US"):
    from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl

    return ingest_validated_jsonl([record], market=market)


async def _install_content_change_trigger(db):
    async with db.connection() as connection:
        await connection.raw_connection.execute(
            (ROOT / "db/migrations/138_catalog_products_content_changed_at.sql").read_text())


async def _row(db, key, host=OWNER):
    row = dict(await db.fetch_one(
        "SELECT image_url, product_payload, content_changed_at FROM catalog_products WHERE product_key = :k",
        {"k": key}))
    seed = await db.fetch_val(
        "SELECT seed_data FROM external_product_seeds WHERE attached_product_key = :k AND domain = :h",
        {"k": key, "h": host})
    return row, json.loads(row["product_payload"]), json.loads(seed)


async def _view_images(db, plan):
    view = await db.fetch_one("SELECT image_url, image_urls FROM agent_pdp_view WHERE content_key = :c",
                              {"c": plan["pdps"][0]["content_key"]})
    return view["image_url"], json.loads(view["image_urls"])


@pytest.mark.parametrize("batch", [False, True])
async def test_a_re_crawl_owns_the_gallery_and_an_unchanged_feed_never_churns(readiness_db, batch):  # noqa: F811
    from services.catalog_enrichment_agent.apply import apply_ingest_plan

    db = readiness_db
    await _install_content_change_trigger(db)

    async def apply(plan):
        counts = await apply_ingest_plan(copy.deepcopy(plan), db=db, batch=batch, batch_label="gallery_pg",
                                         primary_readiness=True)
        assert counts["primary_readiness"]["status"] == "complete"
        return plan["pdps"][0]["product_key"]

    feed = ["front.jpg?v=1", "back.jpg", "front.jpg?v=2", "texture.jpg"]
    gallery = [f"{CDN}/front.jpg?v=1", f"{CDN}/back.jpg", f"{CDN}/texture.jpg"]
    # Before this change: the record carried no gallery, so the row and its seed held one image.
    before = _record(feed)
    assert before["pdp"].pop("image_urls") == gallery
    key = await apply(_plan(before))
    row, payload, seed = await _row(db, key)
    assert list(payload) == ["enrichment_meta"] and seed["image_urls"] == [gallery[0]]
    assert "image_urls_source" not in seed
    changed_before = row["content_changed_at"]

    # The first re-crawl with the flag on: the payload changes ONCE (content_changed_at advances once)...
    plan = _plan(_record(feed))
    assert await apply(plan) == key
    row, payload, seed = await _row(db, key)
    assert payload["image_urls"] == gallery and seed["image_urls"] == gallery
    assert seed["image_urls_source"] == "storefront_products_json"
    assert row["image_url"] == gallery[0]
    assert row["content_changed_at"] > changed_before
    assert await _view_images(db, plan) == (gallery[0], gallery)
    changed_once = row["content_changed_at"]

    # ...and the same feed again is byte-identical: no content change, no re-assembly trigger.
    await apply(_plan(_record(feed)))
    row, payload, seed = await _row(db, key)
    assert payload["image_urls"] == gallery and row["content_changed_at"] == changed_once

    # A store that changes its gallery replaces it on the row it owns.
    new_gallery = [f"{CDN}/front.jpg?v=1", f"{CDN}/swatch.jpg"]
    await apply(_plan(_record(["front.jpg?v=1", "swatch.jpg"])))
    row, payload, seed = await _row(db, key)
    assert payload["image_urls"] == new_gallery and seed["image_urls"] == new_gallery
    assert seed["image_urls_source"] == "storefront_products_json"
    assert row["content_changed_at"] > changed_once
    assert await _view_images(db, plan) == (new_gallery[0], new_gallery)


@pytest.mark.parametrize("batch", [False, True])
async def test_a_one_image_row_and_a_flag_off_run_never_change_the_row(readiness_db, batch, monkeypatch):  # noqa: F811
    from services.catalog_enrichment_agent.apply import apply_ingest_plan
    from services.catalog_enrichment_agent.ingestion import STRUCTURED_GALLERY_FLAG

    db = readiness_db
    await _install_content_change_trigger(db)
    monkeypatch.delenv(STRUCTURED_GALLERY_FLAG)
    plan = _plan(_record(["front.jpg", "back.jpg"]))
    await apply_ingest_plan(copy.deepcopy(plan), db=db, batch=batch, batch_label="gallery_off")
    key = plan["pdps"][0]["product_key"]
    row, payload, seed = await _row(db, key)
    assert list(payload) == ["enrichment_meta"] and seed["image_urls"] == [f"{CDN}/front.jpg"]
    assert "image_urls_source" not in seed
    changed = row["content_changed_at"]
    # Flag on, but a store that now lists only its one image: the row stays exactly as main writes it.
    monkeypatch.setenv(STRUCTURED_GALLERY_FLAG, "1")
    await apply_ingest_plan(copy.deepcopy(_plan(_record(["front.jpg"]))), db=db, batch=batch,
                            batch_label="gallery_one")
    row, payload, seed = await _row(db, key)
    assert list(payload) == ["enrichment_meta"] and seed["image_urls"] == [f"{CDN}/front.jpg"]
    assert "image_urls_source" not in seed
    assert row["content_changed_at"] == changed


@pytest.mark.parametrize("batch", [False, True])
async def test_an_off_market_store_never_puts_its_gallery_on_the_owners_product(readiness_db, batch):  # noqa: F811
    from services.catalog_enrichment_agent.apply import apply_ingest_plan

    db = readiness_db
    owner = _plan(_record(["owner-a.jpg", "owner-b.jpg"]))
    await apply_ingest_plan(copy.deepcopy(owner), db=db, batch=batch, batch_label="gallery_owner", market="US")
    key = owner["pdps"][0]["product_key"]
    owner_gallery = [f"{CDN}/owner-a.jpg", f"{CDN}/owner-b.jpg"]
    row, payload, seed = await _row(db, key)
    assert payload["image_urls"] == owner_gallery and seed["image_urls"] == owner_gallery
    assert seed["image_urls_source"] == "storefront_products_json"

    second = _plan(_record(["second-a.jpg", "second-b.jpg"], host="second-store.example", currency="AUD"),
                   market="AU")
    assert second["pdps"][0]["product_key"] == key
    counts = await apply_ingest_plan(copy.deepcopy(second), db=db, batch=batch, batch_label="gallery_second",
                                     market="AU")
    assert counts["pdps_offer_only_canonical_owner"] == 1 and counts["seeds"] == 1
    row, payload, owner_seed = await _row(db, key)
    assert payload["image_urls"] == owner_gallery and row["image_url"] == owner_gallery[0]
    assert owner_seed["image_urls"] == owner_gallery
    assert owner_seed["image_urls_source"] == "storefront_products_json"
    # The second store's own seed lands on the owner's product with its ONE image, as main writes it.
    _row_again, _payload, foreign_seed = await _row(db, key, host="second-store.example")
    assert foreign_seed["image_urls"] == [f"{CDN}/second-a.jpg"]
    assert "image_urls_source" not in foreign_seed

    # The owning store itself, re-crawled off-market, still replaces its own gallery and its seed's.
    again = _plan(_record(["owner-a.jpg", "owner-c.jpg"], currency="AUD"), market="AU")
    counts = await apply_ingest_plan(copy.deepcopy(again), db=db, batch=batch, batch_label="gallery_owner_au",
                                     market="AU")
    assert "pdps_offer_only_canonical_owner" not in counts
    _row_owner, payload, owner_seed = await _row(db, key)
    assert payload["image_urls"] == owner_seed["image_urls"] == [f"{CDN}/owner-a.jpg", f"{CDN}/owner-c.jpg"]


@pytest.mark.parametrize("batch", [False, True])
async def test_a_retailer_rows_gallery_stays_in_its_own_payload(readiness_db, batch):  # noqa: F811
    from services.catalog_enrichment_agent.apply import apply_ingest_plan

    db = readiness_db
    plan = _plan(_record(["r-a.jpg", "r-b.jpg"], host="retail-shop.example", source_role="retailer"))
    await apply_ingest_plan(copy.deepcopy(plan), db=db, batch=batch, batch_label="gallery_retailer")
    _row_r, payload, seed = await _row(db, plan["pdps"][0]["product_key"], host="retail-shop.example")
    assert payload["image_urls"] == [f"{CDN}/r-a.jpg", f"{CDN}/r-b.jpg"]
    assert seed["image_urls"] == [f"{CDN}/r-a.jpg"]
    assert "image_urls_source" not in seed
