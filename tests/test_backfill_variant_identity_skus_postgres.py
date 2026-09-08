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
#: writer_audit_log is shared by every module in the gate run, and at least one of them
#: (reconcile_catalog_offers) leaves its own row behind — so every read is scoped to this writer.
WRITER_NAME = "backfill_variant_identity_skus"
#: asyncpg binds timestamptz from a datetime, never a string.
_SUPPRESSED = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


_SEEDS_DDL = """
    CREATE TABLE IF NOT EXISTS external_product_seeds (
      external_product_id text, attached_product_key text, status text, seed_data jsonb)
"""

#: external_product_seeds has no db/ model object, so it cannot come from create_all. Twelve
#: earlier gate files leave it behind narrower than this (one leaves it as `(id text)`), and
#: CREATE TABLE IF NOT EXISTS then silently inherits whatever shape is already there — so patch
#: the columns we read rather than assume our CREATE ran.
_SEEDS_COLUMNS = (
    ("external_product_id", "text"),
    ("attached_product_key", "text"),
    ("status", "text"),
    ("seed_data", "jsonb"),
)

#: The two-unique-constraint shape is the whole point of B1, and create_all builds it from the
#: model only if the model declares it. Asserted rather than assumed below.
_IDENTITY_INDEX = """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_catalog_skus_source_identity_v2
    ON catalog_skus (merchant_id, platform, product_key, source_variant_id)
"""

_TABLES = ("catalog_offers", "catalog_skus", "catalog_products",
           "external_product_seeds", "writer_audit_log")


async def _ddl(database):
    """Build the tables from the REAL db/ models, and never drop a shared one.

    An earlier version of this fixture hand-wrote cut-down CREATE TABLEs and DROPPED the
    shared ones. Both halves were wrong. The hand-written DDL declared `market` and
    `availability` nullable when prod has them NOT NULL, so a passing test could have hidden a
    failing INSERT; and the drops destroyed schema the next gate file needed —
    test_connection_layer_postgres died on catalog_products.catalog_track, a column this
    fixture has no reason to know about. The dialect gate runs every tests/test_*_postgres.py
    against ONE database, so a fixture must patch or own its tables and must never assume its
    own CREATE was the one that ran.

    ensure_model_tables gives production's exact DDL — every NOT NULL, every default — and
    patches an already-created narrow table column by column, derived from table.columns
    rather than a hardcoded ALTER list.
    """
    from db.catalog import (
        catalog_offers, catalog_products, catalog_skus, writer_audit_log,
    )
    from tests.model_schema import ensure_model_tables

    await ensure_model_tables(
        [catalog_products, catalog_skus, catalog_offers, writer_audit_log]
    )
    await database.execute(_SEEDS_DDL)
    for name, coltype in _SEEDS_COLUMNS:
        await database.execute(
            f"ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS {name} {coltype}"
        )
    await database.execute(_IDENTITY_INDEX)


async def _clear(database):
    """Remove this fixture's ROWS. Never its tables — see _ddl."""
    await database.execute(
        "DELETE FROM catalog_offers WHERE product_key = :pk OR source_system = :ss",
        {"pk": PK, "ss": "variant_identity_backfill_v1"},
    )
    await database.execute("DELETE FROM catalog_skus WHERE product_key = :pk", {"pk": PK})
    await database.execute("DELETE FROM catalog_products WHERE product_key = :pk", {"pk": PK})
    await database.execute(
        "DELETE FROM external_product_seeds WHERE external_product_id = :e", {"e": SPID}
    )
    await database.execute("DELETE FROM writer_audit_log WHERE writer_name = :w",
                           {"w": WRITER_NAME})


async def _audit_row(database):
    """This module's latest writer_audit_log row (never another module's leftover)."""
    return dict(await database.fetch_one(
        "SELECT * FROM writer_audit_log WHERE writer_name = :w ORDER BY id DESC LIMIT 1",
        {"w": WRITER_NAME},
    ))


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
                 offer_type, is_first_party, why_buy_direct,
                 offer_payload, suppressed_at, created_at, updated_at)
               VALUES (:oid,:sk,:pk,:m,'external_referral','primary','referral_only',
                 'external_referral','default','in_stock',:cur,10.0,'seed',:mkt,
                 :ot,:ifp,:wbd,
                 CAST(:pl AS jsonb),:sup,NOW(),NOW())""",
            {"oid": f"offer:pg:{i}", "sk": PK + "::canonical", "pk": PK,
             "m": o.get("merchant_id", "m_seller"), "cur": o.get("currency", "USD"),
             "mkt": o.get("market", "US"), "ot": o.get("offer_type"),
             "ifp": o.get("is_first_party", False), "wbd": o.get("why_buy_direct"),
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
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await _ddl(database)
    await _clear(database)
    try:
        yield database
    finally:
        await _clear(database)
        if not was_connected and database.is_connected:
            await database.disconnect()


async def test_the_identity_index_the_conflict_target_needs_actually_exists(db):
    """B1 rests on catalog_skus carrying TWO unique constraints. If create_all ever stops
    building the identity index, every adoption test would still pass by inserting a fresh
    row and the conflict path would go untested."""
    n = await db.fetch_val(
        "SELECT count(*) FROM pg_indexes WHERE tablename='catalog_skus' "
        "AND indexname='idx_catalog_skus_source_identity_v2'")
    assert n == 1


async def _run(**kw):
    import scripts.backfill_variant_identity_skus as backfill
    return await backfill.run(**{"apply": True, "limit": 0, "after": "", "page": 100,
                                 "adopt_existing_offers": False, **kw})


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

    await _clear(db)   # rows, not tables — _ddl no longer drops anything
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
    row = await _audit_row(db)
    assert row["writer_name"] == WRITER_NAME
    assert row["batch_id"] == report["batch_id"]
    assert row["applied_rows"] == 2


# ---------------------------------------------------------------------------
# Round-3 review: 15 of 16 semantic mutations survived the suite above. These
# close the ones that matter, executed rather than grepped.
# ---------------------------------------------------------------------------


async def test_the_offer_is_attributed_to_the_seller_not_the_product_row(db):
    """The highest-value survivor. `merchant_id` must come from the live OFFER (the seller of
    record), not from catalog_products — those differ, and attributing 6,090 offers to the
    wrong party is a commercial claim, not a cosmetic one."""
    await _seed(db, variants=[_variant()], offers=[{"merchant_id": "m_the_seller"}])
    await _run()
    assert await db.fetch_val(
        "SELECT merchant_id FROM catalog_offers WHERE source_system=:s",
        {"s": "variant_identity_backfill_v1"}) == "m_the_seller"
    assert MERCHANT != "m_the_seller"  # the product row's merchant, deliberately different


async def test_market_and_the_decision_fields_are_carried_from_the_chosen_offer(db):
    """F4. A NULL offer_type on external_referral reads as authoritative "unknown" and
    is_first_party false, so a dropped field makes the new offer look like an unknown,
    non-official seller beside a canonical sibling that reads brand_direct/official."""
    await _seed(db, variants=[_variant()], offers=[{
        "market": "GB", "offer_type": "brand_direct",
        "is_first_party": True, "why_buy_direct": "official",
    }])
    await _run()
    o = dict(await db.fetch_one("SELECT * FROM catalog_offers WHERE source_system=:s",
                                {"s": "variant_identity_backfill_v1"}))
    assert o["market"] == "GB"
    assert o["offer_type"] == "brand_direct"
    assert o["is_first_party"] is True
    assert o["why_buy_direct"] == "official"
    assert o["source_ref"] == DEST


async def test_a_variant_currency_disagreement_is_refused_when_it_reaches_the_writer(db):
    """The SQLite counterpart only checked a counter. This one proves no ROW is written."""
    await _seed(db, variants=[_variant(currency="EUR")], offers=[{"currency": "USD"}])
    await _run()
    assert await db.fetch_val("SELECT count(*) FROM catalog_offers WHERE source_system=:s",
                              {"s": "variant_identity_backfill_v1"}) == 0
    assert await db.fetch_val("SELECT count(*) FROM catalog_skus") == 0


async def test_a_seed_attached_to_another_product_contributes_nothing(db):
    """B3, executed. An id borrowed from the wrong product is a real merchant id and passes
    every string check — only the join scoping refuses it."""
    await _seed(db, variants=[], offers=[{}])
    await db.execute(
        """INSERT INTO external_product_seeds
             (external_product_id, attached_product_key, status, seed_data)
           VALUES (:spid,'ext:some-other-product::ffff','active',CAST(:sd AS jsonb))""",
        {"spid": SPID, "sd": json.dumps({"snapshot": {"variants": [_variant()]}})},
    )
    report = await _run()
    assert report["skus"] == 0
    assert await db.fetch_val("SELECT count(*) FROM catalog_skus") == 0


async def test_an_inactive_seed_contributes_nothing(db):
    await _seed(db, variants=[], offers=[{}])
    await db.execute(
        """INSERT INTO external_product_seeds
             (external_product_id, attached_product_key, status, seed_data)
           VALUES (:spid,:pk,'archived',CAST(:sd AS jsonb))""",
        {"spid": SPID, "pk": PK, "sd": json.dumps({"snapshot": {"variants": [_variant()]}})},
    )
    assert (await _run())["skus"] == 0


async def test_a_run_whose_every_pair_is_refused_does_not_read_as_success(db):
    """F2. `skus` counted planned rows, so a fully-rolled-back run reported skus: N and an
    audit row saying the same — indistinguishable from success."""
    import services.catalog_offer_writer_guard as guard
    real = guard.validate_catalog_offer_rows
    guard.validate_catalog_offer_rows = lambda rows, **kw: ([], {"zero_or_missing_price": 1}, [])
    try:
        await _seed(db, variants=[_variant()], offers=[{}])
        report = await _run()
    finally:
        guard.validate_catalog_offer_rows = real
    assert report["skus"] == 0, "a rolled-back pair must not be counted as written"
    assert report["rolled_back_offer_refused_by_guard"] == 1
    assert await db.fetch_val("SELECT count(*) FROM catalog_skus") == 0
    assert (await _audit_row(db))["applied_rows"] == 0


async def test_an_offer_is_never_attached_to_a_suppressed_sku(db):
    """F6. Recall filters suppressed SKUs, so an offer there is supply nothing can surface."""
    await db.execute(
        """INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform,
             source_product_id, source_variant_id, title, sku_payload, suppressed_at, updated_at)
           VALUES (:sk,:pk,:m,:p,:spid,:v,'Ruby','{}'::jsonb,:sup,NOW())""",
        {"sk": PK + "::v::" + VID, "pk": PK, "m": MERCHANT, "p": PLATFORM,
         "spid": SPID, "v": VID, "sup": _SUPPRESSED},
    )
    await _seed(db, variants=[_variant()], offers=[{}])
    report = await _run()
    assert report["skipped_suppressed_sku"] == 1
    assert await db.fetch_val("SELECT count(*) FROM catalog_offers WHERE source_system=:s",
                              {"s": "variant_identity_backfill_v1"}) == 0


async def test_the_audit_row_and_cursor_survive_an_error_mid_run(db):
    """F3. An exception used to lose the report, the resume cursor AND the audit row while
    committed pairs stayed — recoverable only by hand."""
    import scripts.backfill_variant_identity_skus as backfill
    await _seed(db, variants=[_variant()], offers=[{}])
    real = backfill.guard_catalog_offer_rows
    calls = {"n": 0}

    async def boom(rows, **kw):
        calls["n"] += 1
        raise RuntimeError("simulated mid-run failure")

    backfill.guard_catalog_offer_rows = boom
    try:
        with pytest.raises(RuntimeError):
            await _run()
    finally:
        backfill.guard_catalog_offer_rows = real
    assert calls["n"] == 1
    row = await _audit_row(db)
    assert row["writer_name"] == WRITER_NAME


async def test_a_missing_arbiter_index_is_not_swallowed_as_a_collision(db):
    """F1. `"unique" in repr(exc)` also matched SQLSTATE 42P10 — "no unique or exclusion
    constraint matching the ON CONFLICT specification", i.e. the arbiter index being gone.
    That turned the exact B1 failure into a silent no-op reported as row-level collisions."""
    await db.execute("DROP INDEX idx_catalog_skus_source_identity_v2")
    await _seed(db, variants=[_variant()], offers=[{}])
    with pytest.raises(Exception) as caught:
        await _run()
    assert "42P10" in str(getattr(caught.value, "sqlstate", "")) or "ON CONFLICT" in str(
        caught.value
    ), f"a missing arbiter index must not be swallowed: {caught.value!r}"


async def test_a_destination_that_lives_only_in_source_ref_is_still_carried(db):
    """R7. SELECT_LIVE_OFFERS_SQL coalesces offer_payload->>'destination_url' with source_ref.
    Every fixture above sets the payload key, so dropping the coalesce changed nothing and the
    mutation survived. Ingestion writes the destination to source_ref (ingestion.py:1014), so
    this is a shape that genuinely occurs."""
    await _seed(db, variants=[_variant()], offers=[{}])
    await db.execute(
        "UPDATE catalog_offers SET offer_payload='{}'::jsonb, source_ref=:d "
        "WHERE source_system='seed'", {"d": DEST})
    report = await _run()
    assert report["skus"] == 1, "the destination in source_ref was not seen"
    o = dict(await db.fetch_one("SELECT * FROM catalog_offers WHERE source_system=:s",
                                {"s": "variant_identity_backfill_v1"}))
    assert json.loads(o["offer_payload"])["destination_url"] == DEST


async def test_an_offer_another_writer_owns_is_not_overwritten(db):
    """derive_offer_id uses the same triple ingestion does, so an id can land on a row
    another writer owns; DO UPDATE would revert a price the nightly refresh had moved and
    re-stamp source_system as ours. 0 of 6,090 today — but an ingest run between measurement
    and execution creates the case, so refuse rather than trust a count."""
    import scripts.backfill_variant_identity_skus as backfill
    from services.catalog_enrichment_agent.ingestion import (
        derive_offer_id, derive_variant_sku_key,
    )
    await _seed(db, variants=[_variant()], offers=[{}])
    sk = derive_variant_sku_key(PK, VID)
    oid = derive_offer_id(PK, sk, DEST)
    await db.execute(
        """INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id,
             catalog_track, availability, currency, list_price, source_system, market,
             offer_payload, created_at, updated_at)
           VALUES (:oid,:sk,:pk,'m_seller','external_referral','out_of_stock','USD',
             99.0,'nightly_refresh','US','{}'::jsonb,NOW(),NOW())""",
        {"oid": oid, "sk": sk, "pk": PK},
    )
    report = await _run()
    assert report["skipped_offer_owned_by_other_writer"] == 1
    assert report["skus"] == 0
    row = dict(await db.fetch_one("SELECT * FROM catalog_offers WHERE offer_id=:o", {"o": oid}))
    assert row["source_system"] == "nightly_refresh"
    assert float(row["list_price"]) == 99.0          # the refresh's price, not the snapshot's
    assert row["availability"] == "out_of_stock"

    # ...and the opt-in does adopt it
    report2 = await _run(adopt_existing_offers=True)
    assert report2["skus"] == 1
    row2 = dict(await db.fetch_one("SELECT * FROM catalog_offers WHERE offer_id=:o", {"o": oid}))
    assert row2["source_system"] == "variant_identity_backfill_v1"


async def test_a_silent_variant_never_overwrites_a_measured_availability(db):
    """_availability_of falls back to the chosen offer's value, so on an UPDATE a variant
    that states nothing would flip a measured out_of_stock to an inherited in_stock."""
    from services.catalog_enrichment_agent.ingestion import (
        derive_offer_id, derive_variant_sku_key,
    )
    await _seed(db, variants=[_variant()], offers=[{}])
    await _run()
    sk = await db.fetch_val(
        "SELECT sku_key FROM catalog_skus WHERE source_variant_id=:v", {"v": VID})
    oid = derive_offer_id(PK, sk, DEST)
    await db.execute("UPDATE catalog_offers SET availability='out_of_stock' WHERE offer_id=:o",
                     {"o": oid})
    # a re-run whose variant says nothing about availability must leave that alone
    await db.execute(
        "UPDATE external_product_seeds SET seed_data=CAST(:sd AS jsonb)",
        {"sd": json.dumps({"snapshot": {"variants": [
            {"variant_id": VID, "title": "Ruby", "price_amount": "24.00", "currency": "USD"}]}})},
    )
    await _run()
    assert await db.fetch_val(
        "SELECT availability FROM catalog_offers WHERE offer_id=:o", {"o": oid}) == "out_of_stock"


async def test_a_product_suppressed_by_reason_alone_is_skipped(db):
    """catalog_trust_policy tombstones on the reason ALONE; a reason without a timestamp is a
    threshold-0 class in catalog_invariant_checks."""
    await _seed(db, variants=[_variant()], offers=[{}])
    await db.execute("UPDATE catalog_products SET suppression_reason='withdrawn'")
    assert (await _run())["skus"] == 0
