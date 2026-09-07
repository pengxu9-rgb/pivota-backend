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

SEEDS_PATCH_DDL = [
    # the first file alphabetically creates it as `(id text)` and nothing more
    "ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS destination_url TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS external_product_id TEXT NULL",
    "ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'",
    "ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
    "ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS market TEXT NOT NULL DEFAULT 'US'",
    "ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS tool TEXT NOT NULL DEFAULT '*'",
    "ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS seed_data JSONB NOT NULL DEFAULT '{}'::jsonb",
    "ALTER TABLE external_product_seeds ALTER COLUMN status SET DEFAULT 'active'",
]

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
    # Twelve other gate files CREATE this table IF NOT EXISTS with narrower hand-written DDLs,
    # and the alphabetically-first one wins in CI: without these, `status` had no default and
    # the active-seed subquery matched nothing (seen on run 34169629995). Patch, don't assume.
    for ddl in SEEDS_PATCH_DDL:
        await database.execute(ddl)
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
            INSERT INTO external_product_seeds (id, external_product_id, destination_url, seed_data,
                                                status, market, tool)
            VALUES (:id, :epid, 'https://brand.com/p', CAST(:sd AS jsonb), 'active', 'US', '*')
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


async def _run(apply: bool = True, limit: int = 0, inherit: bool = False, adopt: bool = False) -> Dict[str, Any]:
    from scripts.backfill_variant_identity_skus import run

    return await run(apply=apply, limit=limit, allow_inherited_price=inherit, adopt_existing_offers=adopt)


async def _counts(db) -> Dict[str, int]:
    out = {}
    for t in ("catalog_skus", "catalog_offers"):
        out[t] = await db.fetch_val(f"SELECT COUNT(*) FROM {t}")
    return out


async def _skus(db, pk: str) -> List[Dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT sku_key, source_variant_id, sku_payload FROM catalog_skus "
        "WHERE product_key = :pk ORDER BY sku_key", {"pk": pk})
    return [dict(r) for r in rows]


async def _offers(db, pk: str) -> List[Dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT offer_id, sku_key, availability, list_price, offer_payload, suppressed_at, "
        "source_system, price_confidence, market, catalog_track, source_domain "
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


async def _ingest_owned_variant(db, pk: str, *, variant: Dict[str, Any]) -> str:
    """ingestion already wrote `<pk>::v:<vid>` and an offer keyed on the destination_url,
    measured at 12.00 / out_of_stock / confidence 0.7 with its own payload."""
    from services.catalog_enrichment_agent.ingestion import derive_variant_sku_key

    await _product(db, pk, variants=[variant])
    await _canonical_offer(db, pk, price=31.0, availability="in_stock")
    vkey = derive_variant_sku_key(pk, REAL_VID)
    await db.execute(
        """
        INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform, source_product_id,
                                  source_variant_id, title, currency, readiness_tier)
        VALUES (:sk, :pk, 'merch_obs_brand', 'external_seed', :epid, :vid, 'Ruby Woo', 'GBP', 'commerce_ready')
        """, {"sk": vkey, "pk": pk, "epid": f"brand:{pk}", "vid": REAL_VID})
    await db.execute(
        """
        INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, list_price,
                                    merchant_effective_price, availability, price_confidence,
                                    source_system, offer_payload)
        VALUES ('offer:ingest:abc', :sk, :pk, 'merch_obs_brand', 12.00, 12.00, 'out_of_stock', 0.7,
                'catalog_enrichment_agent_v1', '{"agent": "ingest"}'::jsonb)
        """, {"sk": vkey, "pk": pk})
    return vkey


async def test_an_offer_another_writer_owns_is_left_alone_by_default(_db) -> None:
    """The script's job is products with NO variant offer. An ingest-measured offer is fresher
    than any seed snapshot; the default run stamps the SKU's identity and touches nothing else."""
    pk = "ext:brand:ingested"
    vkey = await _ingest_owned_variant(_db, pk, variant={"variant_id": REAL_VID, "title": "Ruby Woo", "price": "26.00"})

    report = await _run()

    assert report["skipped_offer_owned_by_other_writer"] == 1
    assert report.get("offers_inserted", 0) == 0 and report.get("offers_adopted", 0) == 0
    offers = [o for o in await _offers(_db, pk) if o["sku_key"] == vkey]
    assert [o["offer_id"] for o in offers] == ["offer:ingest:abc"]
    assert float(offers[0]["list_price"]) == 12.0 and offers[0]["availability"] == "out_of_stock"
    assert offers[0]["source_system"] == "catalog_enrichment_agent_v1"
    sku = (await _skus(_db, pk))[0]
    assert _payload(sku["sku_payload"])["variant_id_provenance"] == "merchant_issued"


async def test_an_adopted_offer_is_restamped_and_a_silent_variant_keeps_its_measured_availability(_db) -> None:
    """THE P0 OF THE FIRST REVIEW ROUND. With --adopt-existing-offers the ingest offer is
    re-priced -- but under THIS writer's provenance (source_system, payload merge, confidence
    reset), and its measured out_of_stock survives a variant that said nothing about stock."""
    pk = "ext:brand:adopted"
    vkey = await _ingest_owned_variant(_db, pk, variant={"variant_id": REAL_VID, "title": "Ruby Woo"})  # no price, silent

    report = await _run(adopt=True, inherit=True)

    assert report["offers_adopted"] == 1 and report["offers_price_inherited"] == 1
    offers = [o for o in await _offers(_db, pk) if o["sku_key"] == vkey]
    assert [o["offer_id"] for o in offers] == ["offer:ingest:abc"]           # updated, not doubled
    o = offers[0]
    assert float(o["list_price"]) == 31.0                                    # inherited, flagged on
    assert o["availability"] == "out_of_stock"                               # measured value kept
    assert o["source_system"] == "variant_identity_backfill_v1"
    assert o["price_confidence"] is None
    payload = _payload(o["offer_payload"])
    assert payload["agent"] == "ingest"                                      # merged, not replaced
    assert payload["price_from"] == "canonical"
    assert payload["variant_id_provenance"] == "merchant_issued"
    # the reused SKU row now agrees with the offer beside it
    row = await _db.fetch_one("SELECT currency, readiness_tier FROM catalog_skus WHERE sku_key = :k", {"k": vkey})
    assert row["currency"] == "USD" and row["readiness_tier"] == "referral_only"


async def test_an_adopted_offer_takes_the_variants_own_availability_when_it_spoke(_db) -> None:
    pk = "ext:brand:adopted-spoke"
    vkey = await _ingest_owned_variant(_db, pk, variant={
        "variant_id": REAL_VID, "title": "Ruby Woo", "price": "26.00", "in_stock": True})

    await _run(adopt=True)

    o = [o for o in await _offers(_db, pk) if o["sku_key"] == vkey][0]
    assert o["availability"] == "in_stock" and float(o["list_price"]) == 26.0


async def test_a_string_false_in_stock_is_not_in_stock(_db) -> None:
    pk = "ext:brand:stringy"
    await _product(_db, pk, variants=[
        {"variant_id": REAL_VID, "title": "A", "price": "9.00", "in_stock": "false"},
        {"variant_id": REAL_VID_2, "title": "B", "price": "9.00", "in_stock": "no"},
    ])
    await _canonical_offer(_db, pk, availability="in_stock")

    await _run()

    offers = {o["sku_key"]: o["availability"] for o in await _offers(_db, pk)}
    by_vid = {s["source_variant_id"]: s["sku_key"] for s in await _skus(_db, pk)}
    # a string is not a boolean: neither may be read as in_stock (truthiness did), and neither is a
    # claim of out_of_stock either -- unknown, which the serving denylist treats as servable
    assert offers[by_vid[REAL_VID]] == "unknown"
    assert offers[by_vid[REAL_VID_2]] == "unknown"


# ------------------------------------------------------------------------------ dry run

async def test_a_dry_run_writes_nothing_and_reports_what_apply_would_do(_db) -> None:
    """The default invocation IS the dry run; its `if apply:` guard is the only thing between an
    operator's preview and 13,270 writes."""
    pk = "ext:brand:dry"
    await _product(_db, pk, variants=[
        {"variant_id": REAL_VID, "title": "A", "price": "9.00"},
        {"variant_id": REAL_VID_2, "title": "B", "price": "9.00"},
    ])
    await _canonical_offer(_db, pk)
    before = await _counts(_db)

    dry = await _run(apply=False)
    assert await _counts(_db) == before
    assert dry["applied"] == 0 and dry["skus_inserted"] == 2 and dry["offers_inserted"] == 2

    wet = await _run(apply=True)
    assert wet["applied"] == 1
    assert {k: v for k, v in wet.items() if k != "applied"} == {k: v for k, v in dry.items() if k != "applied"}
    assert (await _counts(_db))["catalog_skus"] == before["catalog_skus"] + 2


# ------------------------------------------------------- the canonical read never picks a variant

async def test_the_canonical_read_skips_variant_offers_even_without_a_canonical_key(_db) -> None:
    """Two guards closed this door (the NOT LIKE and the ORDER BY tie-break on `::canonical`);
    this fixture defeats the tie-break -- the product's only non-variant offer is keyed
    `<pk>::merchantsku` -- so only the NOT LIKE can keep run 2's second variant from sourcing its
    market / track / domain from the variant offer run 1 wrote."""
    pk = "ext:brand:nocanon"
    await _product(_db, pk, variants=[{"variant_id": REAL_VID, "title": "A", "price": "9.00"}])
    await _db.execute(
        """
        INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, catalog_track, market,
                                    source_domain, availability, list_price, offer_payload)
        VALUES ('offer:merchantsku', :sk, :pk, 'merch_obs_brand', 'external_referral', 'GB',
                'brand.co.uk', 'in_stock', 9.00, '{}'::jsonb)
        """, {"sk": f"{pk}::merchantsku", "pk": pk})
    await _run()
    # run 1's variant offer is now the NEWEST unsuppressed offer on the product; poison it
    await _db.execute(
        "UPDATE catalog_offers SET market = 'XX', catalog_track = 'poison', source_domain = 'poison.example', "
        "updated_at = NOW() + interval '1 hour' WHERE product_key = :pk AND sku_key <> :sk",
        {"pk": pk, "sk": f"{pk}::merchantsku"})
    await _db.execute(
        "UPDATE catalog_products SET product_payload = CAST(:p AS jsonb) WHERE product_key = :pk",
        {"pk": pk, "p": json.dumps({"variants": [
            {"variant_id": REAL_VID, "title": "A", "price": "9.00"},
            {"variant_id": REAL_VID_2, "title": "B", "price": "9.00"}]})})

    await _run()

    by_vid = {s["source_variant_id"]: s["sku_key"] for s in await _skus(_db, pk)}
    second = [o for o in await _offers(_db, pk) if o["sku_key"] == by_vid[REAL_VID_2]][0]
    assert second["market"] == "GB" and second["catalog_track"] == "external_referral"
    assert second["source_domain"] == "brand.com" or second["source_domain"] == "brand.co.uk"
    assert second["source_domain"] != "poison.example"


# ------------------------------------------------------------------------ suppression filters

async def test_a_suppressed_sku_owning_the_identity_gets_no_live_offer(_db) -> None:
    pk = "ext:brand:supsku"
    await _product(_db, pk, variants=[{"variant_id": REAL_VID, "title": "A", "price": "9.00"}])
    await _canonical_offer(_db, pk)
    await _db.execute(
        """
        INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform, source_product_id,
                                  source_variant_id, title, suppressed_at, suppression_reason)
        VALUES (:sk, :pk, 'merch_obs_brand', 'external_seed', :epid, :vid, 'A', NOW(), 'merged_loser')
        """, {"sk": f"{pk}::v::{REAL_VID}", "pk": pk, "epid": f"brand:{pk}", "vid": REAL_VID})

    report = await _run()

    assert report["skipped_sku_suppressed"] == 1
    assert report.get("offers_inserted", 0) == 0
    assert len(await _skus(_db, pk)) == 1                                   # nothing inserted beside it
    assert len(await _offers(_db, pk)) == 1                                 # canonical only


async def test_a_suppressed_product_is_not_scanned(_db) -> None:
    pk = "ext:brand:supprod"
    await _product(_db, pk, variants=[{"variant_id": REAL_VID, "title": "A", "price": "9.00"}])
    await _canonical_offer(_db, pk)
    await _db.execute("UPDATE catalog_products SET suppressed_at = NOW(), suppression_reason = 'unpublished' "
                      "WHERE product_key = :pk", {"pk": pk})

    report = await _run()

    assert report.get("products_scanned", 0) == 0 and await _skus(_db, pk) == []


async def test_a_derived_key_owned_by_a_different_variant_is_never_reidentified(_db) -> None:
    pk = "ext:brand:collide"
    await _product(_db, pk, variants=[{"variant_id": REAL_VID, "title": "A", "price": "9.00"}])
    await _canonical_offer(_db, pk)
    from services.catalog_enrichment_agent.ingestion import derive_variant_sku_key
    key = derive_variant_sku_key(pk, REAL_VID)
    await _db.execute(
        """
        INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform, source_product_id,
                                  source_variant_id, title)
        VALUES (:sk, :pk, 'merch_obs_brand', 'external_seed', :epid, 'some-other-variant', 'Other')
        """, {"sk": key, "pk": pk, "epid": f"brand:{pk}"})

    report = await _run()

    assert report["skipped_sku_key_collision"] == 1
    row = await _db.fetch_one("SELECT source_variant_id FROM catalog_skus WHERE sku_key = :k", {"k": key})
    assert row["source_variant_id"] == "some-other-variant"


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
    assert report["skus_inserted"] == 1 and report["offers_inserted"] == 1    # the good product only
    assert report["products_planned"] == 1
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


async def test_a_default_title_placeholder_beside_one_real_variant_does_not_qualify_for_inheritance(_db) -> None:
    pk = "ext:brand:placeholder"
    await _product(_db, pk, variants=[
        {"variant_id": "brand-x-default", "title": "Default Title"},
        {"variant_id": REAL_VID, "title": "Only real"},                  # unpriced
    ])
    await _canonical_offer(_db, pk, price=31.0)

    report = await _run(inherit=True)

    assert report.get("offers_price_inherited", 0) == 0
    assert await _skus(_db, pk) == []


async def test_the_newest_seed_wins_and_a_product_is_never_fanned_out(_db) -> None:
    pk = "ext:brand:seeded"
    await _product(_db, pk, via_seed=True, variants=[{"variant_id": REAL_VID, "title": "Old", "price": "1.00"}])
    await _db.execute(
        """
        INSERT INTO external_product_seeds (id, external_product_id, destination_url, seed_data, updated_at,
                                            status, market, tool)
        VALUES ('seed:newer', :epid, 'https://brand.com/p', CAST(:sd AS jsonb), NOW() + interval '1 hour',
                'active', 'US', 'other-tool')
        """,
        {"epid": f"brand:{pk}", "sd": json.dumps({"snapshot": {"variants": [
            {"variant_id": REAL_VID, "title": "New", "price": "2.00"}]}})},
    )
    await _canonical_offer(_db, pk)

    await _db.execute(
        """
        INSERT INTO external_product_seeds (id, external_product_id, destination_url, seed_data, updated_at,
                                            status, market, tool)
        VALUES ('seed:retired', :epid, 'https://brand.com/p', CAST(:sd AS jsonb), NOW() + interval '2 hour',
                'retired', 'US', 'third-tool')
        """,
        {"epid": f"brand:{pk}", "sd": json.dumps({"snapshot": {"variants": [
            {"variant_id": REAL_VID, "title": "Dead", "price": "3.00"}]}})},
    )

    report = await _run()

    assert report["products_scanned"] == 1
    offers = [o for o in await _offers(_db, pk) if o["sku_key"] != f"{pk}::canonical"]
    assert len(offers) == 1 and float(offers[0]["list_price"]) == 2.0     # newest ACTIVE, not newest
