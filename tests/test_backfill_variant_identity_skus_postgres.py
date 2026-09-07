"""Production-dialect gate for scripts/backfill_variant_identity_skus.py.

WHY THIS FILE EXISTS. The script shipped in #2113 with zero tests, and its post-merge review
found four things no Python-level test could see because they are SQL semantics:

  * `ON CONFLICT (sku_key)` did not match the live unique index
    `idx_catalog_skus_source_identity_v2 (merchant_id, platform, product_key, source_variant_id)`,
    and `services/catalog_variant_promoter` writes `<pk>::v::<id>` rows with that exact identity
    -- so the INSERT raised UniqueViolation, and with no try/except the run aborted part-way;
  * the canonical-offer read only PREFERRED unsuppressed rows, so an all-suppressed product still
    yielded a source and gained a live variant offer;
  * `availability` was copied verbatim from the crawl (`https://schema.org/InStock`);
  * the whole table was fetched before `--limit` was applied.

So this module EXECUTES the real statements against a real Postgres, twice where idempotency
is the claim.

    createdb pivota_dialect_check
    DATABASE_URL=postgresql://localhost/pivota_dialect_check \\
        pytest tests/test_backfill_variant_identity_skus_postgres.py

Never point this at prod. CI runs it automatically — the dialect-gate workflow globs
`tests/test_*_postgres.py`.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason=(
        "needs a Postgres DATABASE_URL — this is the production-dialect gate; "
        "see the module docstring for the one-line setup"
    ),
)

SEEDS_DDL = """
CREATE TABLE IF NOT EXISTS external_product_seeds (
  id TEXT PRIMARY KEY,
  external_product_id TEXT NULL,
  market TEXT NOT NULL DEFAULT 'US',
  tool TEXT NOT NULL DEFAULT '*',
  destination_url TEXT NOT NULL,
  seed_data JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'active',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

REAL_VID = "54057345745090"          # a merchant-issued Shopify variant id
REAL_VID_2 = "54057345745091"


@pytest.fixture(autouse=True)
async def _db():
    from db.catalog import catalog_offers, catalog_products, catalog_skus
    from db.database import database
    from tests.model_schema import ensure_model_tables

    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await ensure_model_tables([catalog_products, catalog_skus, catalog_offers])
    await database.execute(SEEDS_DDL)
    for t in ("catalog_offers", "catalog_skus", "catalog_products", "external_product_seeds"):
        await database.execute(f"TRUNCATE {t}")
    yield database
    if not was_connected and database.is_connected:
        await database.disconnect()


async def _product(
    db, pk: str, *, variants: List[Dict[str, Any]], epid: Optional[str] = None,
    merchant: str = "merch_obs_brand", title: Optional[str] = "Retro Matte Lipstick",
    via_seed: bool = False,
) -> None:
    epid = epid or f"brand:{pk}"
    await db.execute(
        """
        INSERT INTO catalog_products
            (product_key, merchant_id, platform, source_product_id, source_domain, title, brand,
             product_payload)
        VALUES (:pk, :m, 'external_seed', :epid, 'brand.com', :title, 'Brand',
                CAST(:payload AS jsonb))
        """,
        {
            "pk": pk, "m": merchant, "epid": epid, "title": title,
            "payload": json.dumps({} if via_seed else {"variants": variants}),
        },
    )
    if via_seed:
        await db.execute(
            """
            INSERT INTO external_product_seeds (id, external_product_id, destination_url, seed_data)
            VALUES (:id, :epid, 'https://brand.com/p', CAST(:sd AS jsonb))
            """,
            {"id": f"seed:{pk}", "epid": epid,
             "sd": json.dumps({"snapshot": {"variants": variants}})},
        )


async def _canonical_offer(db, pk: str, *, price: float = 24.0, suppressed: bool = False,
                           availability: str = "in_stock") -> None:
    await db.execute(
        """
        INSERT INTO catalog_offers
            (offer_id, sku_key, product_key, merchant_id, catalog_track, truth_tier,
             readiness_tier, offer_mode, channel, availability, currency,
             list_price, merchant_effective_price, estimated_best_price, market,
             suppressed_at, offer_payload)
        VALUES (:oid, :sk, :pk, 'merch_obs_brand', 'external_referral', 'primary',
                'referral_only', 'merchant_checkout', 'default', :av, 'USD',
                :price, :price, :price, 'US',
                CASE WHEN :sup THEN NOW() ELSE NULL END, '{}'::jsonb)
        """,
        {"oid": f"offer:{pk}:canonical", "sk": f"{pk}::canonical", "pk": pk,
         "price": price, "sup": suppressed, "av": availability},
    )


async def _run(apply: bool = True, limit: int = 0, inherit: bool = False) -> Dict[str, Any]:
    from scripts.backfill_variant_identity_skus import run

    return await run(apply=apply, limit=limit, allow_inherited_price=inherit)


async def _skus(db, pk: str) -> List[Dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT sku_key, source_variant_id, sku_payload FROM catalog_skus "
        "WHERE product_key = :pk ORDER BY sku_key", {"pk": pk})
    return [dict(r) for r in rows]


async def _offers(db, pk: str) -> List[Dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT offer_id, sku_key, availability, list_price, offer_payload, suppressed_at "
        "FROM catalog_offers WHERE product_key = :pk ORDER BY offer_id", {"pk": pk})
    return [dict(r) for r in rows]


def _payload(v: Any) -> Dict[str, Any]:
    return json.loads(v) if isinstance(v, str) else (v or {})


# ------------------------------------------------------------------ the P1: identity collision

async def test_a_promoter_owned_identity_is_stamped_in_place_not_inserted_beside(_db) -> None:
    """THE P1 THIS FILE EXISTS FOR. The promoter already wrote `<pk>::v::<vid>` with the same
    (merchant_id, platform, product_key, source_variant_id). An INSERT under our `<pk>::v:<vid>`
    key would violate idx_catalog_skus_source_identity_v2 -- previously an uncaught
    UniqueViolation that aborted the run."""
    pk = "ext:brand:retro"
    await _product(_db, pk, variants=[{"variant_id": REAL_VID, "title": "Ruby Woo", "price": "24.00"}])
    await _canonical_offer(_db, pk)
    promoter_key = f"{pk}::v::{REAL_VID}"
    await _db.execute(
        """
        INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform, source_product_id,
                                  source_variant_id, title, sku_payload)
        VALUES (:sk, :pk, 'merch_obs_brand', 'external_seed', :epid, :vid, 'Ruby Woo',
                '{"promoted_by": "catalog_variant_promoter"}'::jsonb)
        """,
        {"sk": promoter_key, "pk": pk, "epid": f"brand:{pk}", "vid": REAL_VID},
    )

    report = await _run()

    assert report.get("products_failed", 0) == 0
    assert report["skus_existing_reused"] == 1 and report.get("skus_inserted", 0) == 0
    skus = await _skus(_db, pk)
    assert [s["sku_key"] for s in skus] == [promoter_key]            # no second row
    payload = _payload(skus[0]["sku_payload"])
    assert payload["promoted_by"] == "catalog_variant_promoter"      # merged, not replaced
    assert payload["variant_id_provenance"] == "merchant_issued"
    offers = await _offers(_db, pk)
    variant_offers = [o for o in offers if o["sku_key"] != f"{pk}::canonical"]
    assert [o["sku_key"] for o in variant_offers] == [promoter_key]  # offer hangs off ITS key
    assert float(variant_offers[0]["list_price"]) == 24.0


# ------------------------------------------------------------------------------- idempotency

async def test_a_second_run_writes_nothing_new(_db) -> None:
    pk = "ext:brand:two-shades"
    await _product(_db, pk, variants=[
        {"variant_id": REAL_VID, "title": "Ruby Woo", "price": "24.00", "in_stock": True},
        {"variant_id": REAL_VID_2, "title": "Bronx", "price": "25.00", "in_stock": False},
    ])
    await _canonical_offer(_db, pk)

    first = await _run()
    skus_1, offers_1 = await _skus(_db, pk), await _offers(_db, pk)
    second = await _run()
    skus_2, offers_2 = await _skus(_db, pk), await _offers(_db, pk)

    assert first["skus_inserted"] == 2 and first["offers_inserted"] == 2
    assert second["skus_existing_reused"] == 2 and second.get("skus_inserted", 0) == 0
    assert second["offers_existing_updated"] == 2 and second.get("offers_inserted", 0) == 0
    assert [s["sku_key"] for s in skus_1] == [s["sku_key"] for s in skus_2]
    assert [o["offer_id"] for o in offers_1] == [o["offer_id"] for o in offers_2]
    assert len(offers_2) == 3                                          # canonical + 2, twice


async def test_an_ingest_written_variant_offer_is_updated_not_doubled(_db) -> None:
    """ingestion writes `<pk>::v:<vid>` + an offer keyed on the destination_url. The backfill
    must find that live offer by sku_key and update its price, not stand a second offer
    beside it under a different offer_id."""
    from services.catalog_enrichment_agent.ingestion import derive_variant_sku_key

    pk = "ext:brand:ingested"
    await _product(_db, pk, variants=[{"variant_id": REAL_VID, "title": "Ruby Woo", "price": "26.00"}])
    await _canonical_offer(_db, pk)
    vkey = derive_variant_sku_key(pk, REAL_VID)
    await _db.execute(
        """
        INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform, source_product_id,
                                  source_variant_id, title)
        VALUES (:sk, :pk, 'merch_obs_brand', 'external_seed', :epid, :vid, 'Ruby Woo')
        """, {"sk": vkey, "pk": pk, "epid": f"brand:{pk}", "vid": REAL_VID})
    await _db.execute(
        """
        INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, list_price,
                                    source_system, offer_payload)
        VALUES ('offer:ingest:abc', :sk, :pk, 'merch_obs_brand', 24.00,
                'catalog_enrichment_agent_v1', '{}'::jsonb)
        """, {"sk": vkey, "pk": pk})

    report = await _run()

    assert report["offers_existing_updated"] == 1 and report.get("offers_inserted", 0) == 0
    offers = [o for o in await _offers(_db, pk) if o["sku_key"] == vkey]
    assert [o["offer_id"] for o in offers] == ["offer:ingest:abc"]
    assert float(offers[0]["list_price"]) == 26.0


# ----------------------------------------------------------------- suppressed offers stay dead

async def test_an_all_suppressed_product_gets_no_variant_offer(_db) -> None:
    pk = "ext:brand:dead"
    await _product(_db, pk, variants=[{"variant_id": REAL_VID, "title": "Ruby Woo", "price": "24.00"}])
    await _canonical_offer(_db, pk, suppressed=True)

    report = await _run()

    assert report["skipped_all_offers_suppressed"] == 1
    assert report.get("offers", 0) == 0
    assert await _skus(_db, pk) == []
    offers = await _offers(_db, pk)
    assert len(offers) == 1 and offers[0]["suppressed_at"] is not None


async def test_a_product_with_no_offer_at_all_is_counted_separately(_db) -> None:
    pk = "ext:brand:orphan"
    await _product(_db, pk, variants=[{"variant_id": REAL_VID, "title": "Ruby Woo", "price": "24.00"}])

    report = await _run()

    assert report["skipped_no_canonical_offer"] == 1
    assert "skipped_all_offers_suppressed" not in report


# ------------------------------------------------------------------ availability vocabulary

async def test_crawled_availability_lands_in_the_one_vocabulary(_db) -> None:
    pk = "ext:brand:avail"
    await _product(_db, pk, variants=[
        {"variant_id": REAL_VID, "title": "A", "price": "24.00", "availability": "https://schema.org/InStock"},
        {"variant_id": REAL_VID_2, "title": "B", "price": "24.00", "availability": "Sold Out"},
        {"variant_id": "54057345745092", "title": "C", "price": "24.00", "availability": "Ships in 3 weeks"},
    ])
    await _canonical_offer(_db, pk, availability="in_stock")

    await _run()

    offers = {o["sku_key"]: o["availability"] for o in await _offers(_db, pk)}
    by_vid = {s["source_variant_id"]: s["sku_key"] for s in await _skus(_db, pk)}
    assert offers[by_vid[REAL_VID]] == "in_stock"
    assert offers[by_vid[REAL_VID_2]] == "out_of_stock"
    # unclassifiable prose is unknown -- NOT the canonical's in_stock, which would fabricate a positive
    assert offers[by_vid["54057345745092"]] == "unknown"


async def test_a_variant_that_says_nothing_takes_the_products_availability(_db) -> None:
    pk = "ext:brand:silent"
    await _product(_db, pk, variants=[
        {"variant_id": REAL_VID, "title": "A", "price": "24.00"},
        {"variant_id": REAL_VID_2, "title": "B", "price": "24.00", "in_stock": False},
    ])
    await _canonical_offer(_db, pk, availability="InStock")           # a raw column value, normalized too

    await _run()

    offers = {o["sku_key"]: o["availability"] for o in await _offers(_db, pk)}
    by_vid = {s["source_variant_id"]: s["sku_key"] for s in await _skus(_db, pk)}
    assert offers[by_vid[REAL_VID]] == "in_stock"
    assert offers[by_vid[REAL_VID_2]] == "out_of_stock"                # the boolean beats the parent


# -------------------------------------------------------- one failing product does not abort

async def test_one_failing_product_is_counted_and_the_run_continues(_db) -> None:
    """catalog_skus.currency is String(16); a crawled variant carrying a 30-character currency
    string raises StringDataRightTruncation inside the write. Before: traceback, unknown rows
    applied. Now: counted, next product written, nothing of the bad product half-written."""
    await _product(_db, "ext:brand:a-bad", variants=[
        {"variant_id": REAL_VID, "title": "Ruby", "price": "24.00", "currency": "X" * 30}])
    await _canonical_offer(_db, "ext:brand:a-bad")
    await _product(_db, "ext:brand:b-good", variants=[{"variant_id": REAL_VID, "title": "Ruby", "price": "24.00"}])
    await _canonical_offer(_db, "ext:brand:b-good")

    report = await _run()

    assert report["products_failed"] == 1
    assert await _skus(_db, "ext:brand:a-bad") == []
    assert len(await _offers(_db, "ext:brand:a-bad")) == 1          # canonical only, nothing half-written
    assert len(await _skus(_db, "ext:brand:b-good")) == 1


# ----------------------------------------------------------------------- limit and paging

async def test_limit_bounds_the_products_touched_and_paging_covers_the_table(_db, monkeypatch) -> None:
    from scripts import backfill_variant_identity_skus as bf

    monkeypatch.setattr(bf, "PAGE_SIZE", 2)                            # force several pages
    for i in range(5):
        pk = f"ext:brand:p{i}"
        await _product(_db, pk, variants=[{"variant_id": f"5405734574510{i}", "title": "X", "price": "9.00"}])
        await _canonical_offer(_db, pk)

    limited = await _run(limit=3)
    assert limited["products_planned"] == 3 and limited["stopped_at_limit"] == 1
    assert limited["products_scanned"] <= 4                            # read stopped with the write

    full = await _run()
    assert full["products_scanned"] == 5 and full["products_planned"] == 5
    assert full["skus_existing_reused"] == 3 and full["skus_inserted"] == 2


# ------------------------------------------------------------------------ price provenance

async def test_a_minted_id_and_an_unpriced_variant_are_both_refused(_db) -> None:
    pk = "ext:brand:refuse"
    await _product(_db, pk, epid="brand-retro", variants=[
        {"variant_id": "brand-retro-default", "title": "Standard", "price": "24.00"},  # minted by us
        {"variant_id": REAL_VID, "title": "Ruby Woo", "price": "0"},                    # no price
    ])
    await _canonical_offer(_db, pk)

    report = await _run()

    assert report["skipped_not_merchant_issued"] == 1
    assert report["skipped_no_variant_price"] == 1
    assert await _skus(_db, pk) == []


async def test_inherited_price_is_opt_in_single_variant_only_and_stamped(_db) -> None:
    lone, pair = "ext:brand:lone", "ext:brand:pair"
    await _product(_db, lone, variants=[{"variant_id": REAL_VID, "title": "Only"}])
    await _canonical_offer(_db, lone, price=31.0)
    await _product(_db, pair, variants=[
        {"variant_id": REAL_VID, "title": "A"}, {"variant_id": REAL_VID_2, "title": "B", "price": "5.00"},
    ])
    await _canonical_offer(_db, pair, price=31.0)

    off = await _run(inherit=False)
    assert off["skipped_no_variant_price"] == 2 and await _skus(_db, lone) == []

    on = await _run(inherit=True)
    assert on["offers_price_inherited"] == 1
    lone_offers = [o for o in await _offers(_db, lone) if o["sku_key"] != f"{lone}::canonical"]
    assert float(lone_offers[0]["list_price"]) == 31.0
    assert _payload(lone_offers[0]["offer_payload"])["price_from"] == "canonical"
    # the two-variant product's unpriced sibling still gets nothing, flag or no flag
    pair_vids = {s["source_variant_id"] for s in await _skus(_db, pair)}
    assert pair_vids == {REAL_VID_2}


async def test_the_newest_seed_wins_and_a_product_is_never_fanned_out(_db) -> None:
    pk = "ext:brand:seeded"
    await _product(_db, pk, via_seed=True, variants=[{"variant_id": REAL_VID, "title": "Old", "price": "1.00"}])
    await _db.execute(
        """
        INSERT INTO external_product_seeds (id, external_product_id, destination_url, seed_data, updated_at)
        VALUES ('seed:newer', :epid, 'https://brand.com/p', CAST(:sd AS jsonb), NOW() + interval '1 hour')
        """,
        {"epid": f"brand:{pk}", "sd": json.dumps({"snapshot": {"variants": [
            {"variant_id": REAL_VID, "title": "New", "price": "2.00"}]}})},
    )
    await _canonical_offer(_db, pk)

    report = await _run()

    assert report["products_scanned"] == 1
    offers = [o for o in await _offers(_db, pk) if o["sku_key"] != f"{pk}::canonical"]
    assert len(offers) == 1 and float(offers[0]["list_price"]) == 2.0
