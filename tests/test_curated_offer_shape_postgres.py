"""The curated writer's offer rows, written to real PostgreSQL and read back.

Every other test of this change asserts what the PLAN carries or what the SQL text says.
Neither notices a column/bind misalignment, a NOT NULL violation swallowed by the per-offer
SAVEPOINT, or an ON CONFLICT clause that does not do what its text suggests — and this
change has already produced two of those three (18 CI failures from a NULL market bind).
Only a real INSERT shows them.

The parent product/SKU rows come from the shared dialect fixture; the OFFER rows are built
by the production builder, so what is written here is the row shape the curated lane
actually emits. The gateway's catalog_offers arm selects
`offer_type = 'retailer' AND offer_mode = 'redirect'`, so that pair is what is asserted.
"""
import os

import pytest

from services.catalog_enrichment_agent.ingestion import _build_offer_inserts, resolve_offer_type
from tests.test_sku_identity_upserts_postgres import (  # noqa: F401
    DEST,
    PK,
    _apply,
    _ingestion_key,
    _plan,
    _planned_sku,
    _seed_product,
    db,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgres"), reason="requires test Postgres"
)

RETAILER_PAYLOAD = {
    "source_role": "retailer", "source_domain": "brand.example", "brand": "A'PIEU",
    "canonical_url": DEST,
}


def curated_offers(sku_key, *, payload=None, price=19.0):
    """Offer rows exactly as the curated lane emits them for a retailer listing."""
    seller = resolve_offer_type(payload or RETAILER_PAYLOAD)
    return _build_offer_inserts(
        product_key=PK,
        sku_key=sku_key,
        offers=[{"canonical_url": DEST, "destination_url": DEST, "price": price,
                 "in_stock": True, "merchant_inferred": "brand.example",
                 "seller_domain": "brand.example"}],
        source_domain="brand.example",
        currency="USD",
        offer_type=seller["offer_type"],
        is_first_party=seller["is_first_party"],
    )


async def _rows(db):  # noqa: F811
    return [dict(r) for r in await db.fetch_all(
        "SELECT offer_id, offer_type, offer_mode, market, is_first_party "
        "FROM catalog_offers WHERE product_key = :pk ORDER BY offer_id", {"pk": PK})]


async def test_a_curated_retailer_offer_lands_in_the_shape_the_arm_selects(db):  # noqa: F811
    await _seed_product(db)
    key = _ingestion_key()

    counts = await _apply(_plan([_planned_sku()], curated_offers(key)), batch=False)

    rows = await _rows(db)
    assert counts["offers"] == 1 and len(rows) == 1, (
        "planned and counted but not stored — a bind the column rejects is dropped inside "
        "its own SAVEPOINT and only logged"
    )
    assert rows[0]["offer_type"] == "retailer"
    assert rows[0]["offer_mode"] == "redirect"
    assert rows[0]["is_first_party"] is False
    # Never bound by the writer: the column's own NOT NULL default supplies it.
    assert rows[0]["market"] == "US"


async def test_a_reingest_heals_a_row_left_unservable_by_the_old_writer(db):  # noqa: F811
    """A row written before this change carries (NULL, 'external_referral'). Backfilling
    the type alone would leave (retailer, 'external_referral') — a pair this writer never
    emits and the arm never selects, so the re-ingest would report success while the offer
    stayed invisible."""
    await _seed_product(db)
    key = _ingestion_key()
    await _apply(_plan([_planned_sku()], curated_offers(key)), batch=False)
    await db.execute(
        "UPDATE catalog_offers SET offer_type = NULL, offer_mode = 'external_referral' "
        "WHERE product_key = :pk", {"pk": PK})
    assert [r["offer_mode"] for r in await _rows(db)] == ["external_referral"]

    await _apply(_plan([_planned_sku()], curated_offers(key)), batch=False)

    healed = await _rows(db)
    assert len(healed) == 1, "the re-ingest must upsert, not duplicate"
    assert healed[0]["offer_type"] == "retailer"
    assert healed[0]["offer_mode"] == "redirect"


async def test_a_decided_seller_type_survives_a_reingest(db):  # noqa: F811
    """Another lane's (or the classifier's) decision outranks this plan's view of the row,
    and the mode must follow the type that actually stands, not the one being written."""
    await _seed_product(db)
    key = _ingestion_key()
    await _apply(_plan([_planned_sku()], curated_offers(key)), batch=False)
    await db.execute(
        "UPDATE catalog_offers SET offer_type = 'brand_direct', offer_mode = 'external_referral' "
        "WHERE product_key = :pk", {"pk": PK})

    await _apply(_plan([_planned_sku()], curated_offers(key)), batch=False)

    row = (await _rows(db))[0]
    assert row["offer_type"] == "brand_direct", "a decided type must not be clobbered"
    assert row["offer_mode"] == "external_referral", "the mode must not contradict the type"


async def test_the_bulk_path_writes_the_same_shape(db):  # noqa: F811
    """The batch writer binds the same statement through a different code path; a column
    added to one and not reaching the other would store a different row."""
    await _seed_product(db)
    key = _ingestion_key()

    counts = await _apply(_plan([_planned_sku()], curated_offers(key)), batch=True)

    rows = await _rows(db)
    assert counts["offers"] == 1 and len(rows) == 1
    assert rows[0]["offer_type"] == "retailer" and rows[0]["offer_mode"] == "redirect"
    assert rows[0]["is_first_party"] is False and rows[0]["market"] == "US"
