"""The backfill's write path, executed — not asserted about.

WHY THIS FILE EXISTS. The SQLite suite for this script asserts on SQL *strings*, and an
adversarial review proved that was not enough: it reverted five of the fixes one at a time
against a green 27-test baseline and **all five survived**, because `run()` had zero coverage.
The semantics that actually matter here are Postgres-only —

  - which unique index `ON CONFLICT` infers when a table has two,
  - what `RETURNING` yields on the DO UPDATE path,
  - `NULL || jsonb`,

— so they cannot be exercised on SQLite at all, and a string assertion cannot tell a correct
`ON CONFLICT` target from a plausible-looking wrong one.

The file is named `test_*_postgres.py` so the dialect gate's glob picks it up with no ride-along
edit (`.github/workflows/postgres-dialect-gate.yml`).
"""

import datetime as dt
import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    not str(os.getenv("DATABASE_URL") or "").startswith("postgres"),
    reason="needs a real Postgres DATABASE_URL — production-dialect gate",
)

MERCHANT = "m_backfill_pg"
PLATFORM = "external_seed"
PK = "ext:pgtest-lipstick::deadbeef"
SPID = "pgtest-lipstick"
DEST = "https://brand.example/products/pgtest-lipstick"
VID = "43062643884185"
#: asyncpg binds timestamptz from a datetime, never a string.
_SUPPRESSED = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


async def _ddl(database):
    """Only the columns this script touches. Mirrors the prod shape that matters: TWO unique
    constraints on catalog_skus, which is the whole point of B1."""
    await database.execute("DROP TABLE IF EXISTS catalog_offers")
    await database.execute("DROP TABLE IF EXISTS catalog_skus")
    await database.execute("DROP TABLE IF EXISTS catalog_products")
    await database.execute("DROP TABLE IF EXISTS external_product_seeds")
    await database.execute("DROP TABLE IF EXISTS writer_audit_log")
    await database.execute("""
        CREATE TABLE catalog_products (
          product_key text PRIMARY KEY, merchant_id text, platform text,
          source_product_id text, source_domain text, title text,
          product_payload jsonb, suppressed_at timestamptz)
    """)
    await database.execute("""
        CREATE TABLE external_product_seeds (
          external_product_id text, attached_product_key text, status text, seed_data jsonb)
    """)
    await database.execute("""
        CREATE TABLE catalog_skus (
          sku_key text PRIMARY KEY, product_key text, merchant_id text, platform text,
          source_product_id text, source_variant_id text, source_domain text,
          sku text, barcode text, title text, currency text, image_url text,
          visible_attributes jsonb, visible_option_labels jsonb, ingredient_ids jsonb,
          sku_payload jsonb, readiness_tier text, updated_at timestamptz)
    """)
    await database.execute("""
        CREATE UNIQUE INDEX idx_catalog_skus_source_identity_v2
        ON catalog_skus (merchant_id, platform, product_key, source_variant_id)
    """)
    await database.execute("""
        CREATE TABLE catalog_offers (
          offer_id text PRIMARY KEY, sku_key text, product_key text, merchant_id text,
          catalog_track text, truth_tier text, readiness_tier text, offer_mode text,
          channel text, availability text, currency text, list_price numeric,
          merchant_effective_price numeric, estimated_best_price numeric,
          source_system text, source_domain text, market text, source_ref text,
          offer_payload jsonb, suppressed_at timestamptz,
          created_at timestamptz, updated_at timestamptz)
    """)
    await database.execute("""
        CREATE TABLE writer_audit_log (
          id serial PRIMARY KEY, writer_name text, batch_id text, dry_run_report_hash text,
          applied_rows int, skipped_rows int, reasons jsonb, actor text)
    """)


async def _seed(database, *, variants, offers, suppressed=None):
    await database.execute(
        """INSERT INTO catalog_products (product_key, merchant_id, platform,
             source_product_id, source_domain, title, suppressed_at)
           VALUES (:pk,:m,:p,:spid,'brand.example','PGTest Lipstick',:sup)""",
        {"pk": PK, "m": MERCHANT, "p": PLATFORM, "spid": SPID, "sup": suppressed},
    )
    await database.execute(
        """INSERT INTO external_product_seeds
             (external_product_id, attached_product_key, status, seed_data)
           VALUES (:spid,:pk,'active',CAST(:sd AS jsonb))""",
        {"spid": SPID, "pk": PK, "sd": json.dumps({"snapshot": {"variants": variants}})},
    )
    for i, o in enumerate(offers):
        await database.execute(
            """INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id,
                 catalog_track, truth_tier, readiness_tier, offer_mode, channel,
                 availability, currency, list_price, source_system, market,
                 offer_payload, suppressed_at, created_at, updated_at)
               VALUES (:oid,:sk,:pk,:m,'external_referral','primary','referral_only',
                 'external_referral','default','in_stock',:cur,10.0,'seed','US',
                 CAST(:pl AS jsonb),:sup,NOW(),NOW())""",
            {"oid": f"offer:pg:{i}", "sk": PK + "::canonical", "pk": PK,
             "m": o.get("merchant_id", "m_seller"), "cur": o.get("currency", "USD"),
             "pl": json.dumps({"destination_url": o.get("dest", DEST)}),
             "sup": o.get("suppressed_at")},
        )


def _variant(**kw):
    v = {"variant_id": VID, "title": "Ruby", "price_amount": "24.00", "currency": "USD"}
    v.update(kw)
    return v


@pytest.fixture
async def db():
    from db.database import database
    await database.connect()
    await _ddl(database)
    yield database
    await database.disconnect()


async def _run(**kw):
    import scripts.backfill_variant_identity_skus as backfill
    return await backfill.run(**{"apply": True, "limit": 0, "after": "", "page": 100, **kw})


# ---------------------------------------------------------------------------


async def test_it_writes_the_pair_and_the_offer_carries_the_variants_own_price(db):
    await _seed(db, variants=[_variant()], offers=[{}])
    report = await _run()
    assert report["skus"] == 1 and report["offers"] == 1

    sku = dict(await db.fetch_one("SELECT * FROM catalog_skus WHERE source_variant_id=:v",
                                  {"v": VID}))
    assert sku["sku_key"] == PK + "::v:" + VID
    offer = dict(await db.fetch_one("SELECT * FROM catalog_offers WHERE source_system=:s",
                                    {"s": "variant_identity_backfill_v1"}))
    assert offer["sku_key"] == sku["sku_key"]
    assert float(offer["list_price"]) == 24.0          # the VARIANT's price, not the product's 10.0
    assert offer["availability"] == "in_stock"
    assert json.loads(offer["offer_payload"])["destination_url"] == DEST   # M4


async def test_it_adopts_the_promoter_row_instead_of_colliding_with_it(db):
    """B1, executed. The promoter's `::v::` row carries the SAME identity tuple under a
    DIFFERENT sku_key. `ON CONFLICT (sku_key)` raises unique_violation here; conflicting on
    the identity index updates that row, and RETURNING must yield ITS key so the offer
    attaches to a sku_key that exists (M2)."""
    promoter_key = PK + "::v::" + VID
    await db.execute(
        """INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform,
             source_product_id, source_variant_id, title, sku_payload, updated_at)
           VALUES (:sk,:pk,:m,:p,:spid,:v,'Ruby',CAST(:pl AS jsonb),NOW())""",
        {"sk": promoter_key, "pk": PK, "m": MERCHANT, "p": PLATFORM, "spid": SPID,
         "v": VID, "pl": json.dumps({"agent_version": "promoter_v1"})},
    )
    await _seed(db, variants=[_variant()], offers=[{}])
    report = await _run()

    assert report["adopted_existing_sku_row"] == 1
    assert await db.fetch_val(
        "SELECT count(*) FROM catalog_skus WHERE source_variant_id=:v", {"v": VID}) == 1
    offer = dict(await db.fetch_one("SELECT * FROM catalog_offers WHERE source_system=:s",
                                    {"s": "variant_identity_backfill_v1"}))
    assert offer["sku_key"] == promoter_key
    # the merge kept the promoter's provenance rather than replacing the blob
    payload = json.loads(await db.fetch_val(
        "SELECT sku_payload FROM catalog_skus WHERE sku_key=:k", {"k": promoter_key}))
    assert payload["agent_version"] == "promoter_v1"
    assert payload["variant_id_provenance"] == "merchant_issued"


async def test_a_null_sku_payload_is_not_erased_by_the_merge(db):
    """`NULL || jsonb` is NULL. Without the coalesce the provenance marker this backfill
    exists to write is silently erased on exactly the rows that lack one."""
    await db.execute(
        """INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform,
             source_product_id, source_variant_id, title, sku_payload, updated_at)
           VALUES (:sk,:pk,:m,:p,:spid,:v,'Ruby',NULL,NOW())""",
        {"sk": PK + "::v::" + VID, "pk": PK, "m": MERCHANT, "p": PLATFORM,
         "spid": SPID, "v": VID},
    )
    await _seed(db, variants=[_variant()], offers=[{}])
    await _run()
    payload = await db.fetch_val(
        "SELECT sku_payload FROM catalog_skus WHERE source_variant_id=:v", {"v": VID})
    assert payload is not None, "the merge erased the payload — coalesce is missing"
    assert json.loads(payload)["variant_id_provenance"] == "merchant_issued"


async def test_a_rerun_upserts_rather_than_writing_a_second_offer(db):
    """B4, executed. The offer_id must not move between runs — it used to be derived from
    whichever offer row the ordering returned, and ties have no tiebreaker."""
    await _seed(db, variants=[_variant()], offers=[{}])
    await _run()
    first = await db.fetch_val(
        "SELECT count(*) FROM catalog_offers WHERE source_system=:s",
        {"s": "variant_identity_backfill_v1"})
    # a second live offer appears, which would change any ORDER BY-derived choice
    await db.execute(
        """INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id,
             catalog_track, availability, currency, list_price, source_system, market,
             offer_payload, created_at, updated_at)
           VALUES ('offer:pg:late',:sk,:pk,'m_seller','external_referral','in_stock','USD',
             11.0,'seed','US',CAST(:pl AS jsonb),NOW(),NOW())""",
        {"sk": PK + "::canonical", "pk": PK,
         "pl": json.dumps({"destination_url": DEST})},
    )
    await _run()
    assert await db.fetch_val(
        "SELECT count(*) FROM catalog_offers WHERE source_system=:s",
        {"s": "variant_identity_backfill_v1"}) == first == 1


async def test_a_suppressed_product_and_an_all_suppressed_offer_set_are_left_alone(db):
    await _seed(db, variants=[_variant()], offers=[{}], suppressed=_SUPPRESSED)
    assert (await _run())["skus"] == 0

    await _ddl(db)
    await _seed(db, variants=[_variant()],
                offers=[{"suppressed_at": _SUPPRESSED}])
    report = await _run()
    assert report["skus"] == 0
    assert report["skipped_no_live_offer"] == 1
    assert await db.fetch_val("SELECT count(*) FROM catalog_offers WHERE source_system=:s",
                              {"s": "variant_identity_backfill_v1"}) == 0


async def test_a_variant_id_we_minted_is_never_written(db):
    await _seed(db, variants=[_variant(variant_id=SPID + "-default")], offers=[{}])
    report = await _run()
    assert report["skus"] == 0 and report["skipped_not_merchant_issued"] == 1


async def test_a_multi_merchant_product_is_refused(db):
    await _seed(db, variants=[_variant()],
                offers=[{"merchant_id": "m_a"}, {"merchant_id": "m_b"}])
    report = await _run()
    assert report["skus"] == 0 and report["skipped_multi_merchant_product"] == 1


async def test_a_sold_out_variant_is_written_as_out_of_stock(db):
    """M5. Raw crawl text passed through would leave "Out of Stock", which no reader matches."""
    await _seed(db, variants=[_variant(availability="Out of Stock")], offers=[{}])
    await _run()
    assert await db.fetch_val(
        "SELECT availability FROM catalog_offers WHERE source_system=:s",
        {"s": "variant_identity_backfill_v1"}) == "out_of_stock"


async def test_a_currency_disagreement_is_refused(db):
    """M3. The market comes from the chosen offer; a different currency means we do not know
    which market the price belongs to."""
    await _seed(db, variants=[_variant(currency="EUR")], offers=[{"currency": "USD"}])
    report = await _run()
    assert report["skus"] == 0 and report["skipped_currency_disagrees_with_offer"] == 1


async def test_a_priceless_variant_gets_no_row_at_all(db):
    await _seed(db, variants=[_variant(price_amount=None, price=None)], offers=[{}])
    report = await _run()
    assert report["skus"] == 0 and report["skipped_no_variant_price"] == 1
    assert await db.fetch_val("SELECT count(*) FROM catalog_skus") == 0


async def test_the_run_is_recorded_in_writer_audit_log(db):
    await _seed(db, variants=[_variant()], offers=[{}])
    report = await _run()
    row = dict(await db.fetch_one("SELECT * FROM writer_audit_log"))
    assert row["writer_name"] == "backfill_variant_identity_skus"
    assert row["batch_id"] == report["batch_id"]
    assert row["applied_rows"] == 2
