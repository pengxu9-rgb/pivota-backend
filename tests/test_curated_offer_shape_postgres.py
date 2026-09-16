"""The curated writer's offers, read back from real PostgreSQL in the shape serving needs.

Every other test of this change asserts what the PLAN carries or what the SQL text says.
Neither notices a column/bind misalignment, a NOT NULL violation swallowed by the per-offer
SAVEPOINT, or an ON CONFLICT clause that does not do what its text suggests — all three are
failures that only a real INSERT can show. The gateway's catalog_offers arm selects
`offer_type = 'retailer' AND offer_mode = 'redirect'` (routes/agent_shop_gateway.py), so
that pair is what is asserted here.
"""
import os

import pytest

from services import curated_brand_feed as feed
from services.catalog_enrichment_agent import apply as writer
from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl
from tests.test_sku_identity_upserts_postgres import MERCHANT, db  # noqa: F401

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgres"), reason="requires test Postgres"
)


def _curated_plan(*, host="eyurs.com", role="retailer", brand="A'PIEU", vendor="A'PIEU"):
    record = feed.shopify_product_to_record(
        {
            "id": 9000001, "vendor": vendor, "title": "A'PIEU Honey & Milk Lip Oil (5g)",
            "handle": "apieu-honey-milk-lip-oil", "product_type": "Lip Oil",
            "body_html": "<p>Ingredients: Water, Glycerin</p>",
            "images": [{"src": "https://cdn.example/item.jpg"}],
            "variants": [{"id": 45000000000001, "price": "19.00", "available": True,
                          "option1": "5g", "sku": "LIP1", "barcode": "8809530070499"}],
        },
        domain=host, category_path="beauty/makeup", brand_override=brand, currency="USD",
        source_role=role, retailer_name=host,
    )
    return ingest_validated_jsonl([record])


async def _offer_rows(db, product_key):  # noqa: F811
    return await db.fetch_all(
        "SELECT offer_id, offer_type, offer_mode, market, is_first_party "
        "FROM catalog_offers WHERE product_key = :pk ORDER BY offer_id",
        {"pk": product_key},
    )


@pytest.mark.asyncio
async def test_a_curated_retailer_offer_lands_in_the_shape_the_arm_selects(db):  # noqa: F811
    plan = _curated_plan()
    product_key = plan["pdps"][0]["product_key"]

    counts = await writer.apply_ingest_plan(plan, batch_label="curated_offer_shape", db=db)

    rows = await _offer_rows(db, product_key)
    assert counts["offers"] == len(plan["offers"]) == len(rows), (
        "offers were planned and counted but not stored — a bind the column rejected is "
        "dropped inside its own SAVEPOINT and only logged"
    )
    for row in rows:
        assert row["offer_type"] == "retailer"
        assert row["offer_mode"] == "redirect"
        assert row["is_first_party"] is False
        # Never bound by the writer: the column's own NOT NULL default supplies it.
        assert row["market"] == "US"


@pytest.mark.asyncio
async def test_a_reingest_heals_a_row_left_unservable_by_the_old_writer(db):  # noqa: F811
    """A row written before this change carries (NULL, 'external_referral'). Backfilling
    the type alone would leave (retailer, 'external_referral') — a pair this writer never
    emits and the arm never selects, so the re-ingest would report success while the offer
    stayed invisible."""
    plan = _curated_plan()
    product_key = plan["pdps"][0]["product_key"]
    await writer.apply_ingest_plan(plan, batch_label="curated_offer_shape_seed", db=db)
    await db.execute(
        "UPDATE catalog_offers SET offer_type = NULL, offer_mode = 'external_referral' "
        "WHERE product_key = :pk",
        {"pk": product_key},
    )
    stale = await _offer_rows(db, product_key)
    assert [r["offer_mode"] for r in stale] == ["external_referral"] * len(stale)

    await writer.apply_ingest_plan(_curated_plan(), batch_label="curated_offer_shape_again", db=db)

    healed = await _offer_rows(db, product_key)
    assert len(healed) == len(stale), "the re-ingest must upsert, not duplicate"
    for row in healed:
        assert row["offer_type"] == "retailer"
        assert row["offer_mode"] == "redirect"


@pytest.mark.asyncio
async def test_a_decided_seller_type_survives_a_reingest(db):  # noqa: F811
    """Another lane's (or the classifier's) decision outranks this plan's view of the row,
    and the mode must follow the type that actually stands, not the one being written."""
    plan = _curated_plan()
    product_key = plan["pdps"][0]["product_key"]
    await writer.apply_ingest_plan(plan, batch_label="curated_offer_shape_seed2", db=db)
    await db.execute(
        "UPDATE catalog_offers SET offer_type = 'brand_direct', offer_mode = 'external_referral' "
        "WHERE product_key = :pk",
        {"pk": product_key},
    )

    await writer.apply_ingest_plan(_curated_plan(), batch_label="curated_offer_shape_again2", db=db)

    for row in await _offer_rows(db, product_key):
        assert row["offer_type"] == "brand_direct", "a decided type must not be clobbered"
        assert row["offer_mode"] == "external_referral", "the mode must not contradict the type"
