"""The two writers that minted 647 live ORPHAN OFFERS, executed against real PG.

`scripts/capture_us_market_offers.py` (529 live orphans) and
`scripts/attach_retailer_offer.py` (118) both derive a `<product_key>::canonical`
sku_key and, until this change, wrote the offer without ever asking whether such
a `catalog_skus` row exists. They take OPPOSITE fixes, and both halves are
executed here because the difference is the interesting part:

    capture  MINTS the canonical SKU (the identity is the product, and every
             column is on the catalog_products row we already validated), then
             writes the offer against whatever key the identity resolved to.
    attach   REFUSES (its product_key is operator-typed and its merchant is the
             RETAILER, so it has no identity of its own to mint).

POSTGRES-ONLY, ALL OF IT. `ON CONFLICT (merchant_id, platform, product_key,
source_variant_id)` chooses between catalog_skus' TWO unique constraints — a
choice SQLite cannot represent and a string assertion cannot check; `RETURNING
sku_key` on the DO UPDATE path is what makes adoption possible at all; and the
DO UPDATE's `WHERE ... suppressed_at IS NULL` yielding NO ROW is the refusal
signal. A SQLite twin of this file would prove none of it.
"""

import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    not str(os.getenv("DATABASE_URL") or "").startswith("postgres"),
    reason="needs a real Postgres DATABASE_URL — production-dialect gate",
)

MERCHANT = "m_skuprecond_pg"
OTHER_MERCHANT = "m_skuprecond_other"
PLATFORM = "external_seed"
PK = "skuprecond::pg::product"
SKU = PK + "::canonical"
CONTENT_KEY = "ck_skuprecond_pg"
DOMAIN = "brand.example"

_WRITERS = ("us_market_capture", "retailer_offer_attach_v1")

#: The identity index the ON CONFLICT target names. Asserted, not assumed: if
#: create_all ever stops building it, every adoption case below would still pass
#: by inserting a fresh row and the conflict path would go untested.
_IDENTITY_INDEX = "idx_catalog_skus_source_identity_v2"


async def _ddl(database):
    from db.catalog import (
        catalog_offers, catalog_products, catalog_skus, writer_audit_log,
    )
    from tests.model_schema import ensure_model_tables

    await ensure_model_tables(
        [catalog_products, catalog_skus, catalog_offers, writer_audit_log]
    )


async def _clear(database):
    """Rows, never tables — the dialect gate shares one database."""
    await database.execute(
        "DELETE FROM catalog_offers WHERE product_key = :pk", {"pk": PK})
    await database.execute(
        "DELETE FROM catalog_skus WHERE product_key = :pk", {"pk": PK})
    await database.execute(
        "DELETE FROM catalog_products WHERE product_key = :pk", {"pk": PK})
    await database.execute(
        "DELETE FROM writer_audit_log WHERE writer_name = ANY(:w)",
        {"w": list(_WRITERS)})


async def _product(database, *, merchant_id=MERCHANT, content_key=CONTENT_KEY):
    """`content_key=None` keeps `attach_retailer_offer` from calling the PDP
    view assembler, whose own dependency chain (product_group_members and
    friends) is a dozen tables this file has no business building. The offer
    INSERT and the refusal — the two things under test — are upstream of it."""
    await database.execute(
        """INSERT INTO catalog_products
             (product_key, merchant_id, platform, source_product_id, source_domain,
              content_key, canonical_url, title)
           VALUES (:pk,:m,:p,:spid,:dom,:ck,:url,'Precondition Fixture')""",
        {"pk": PK, "m": merchant_id, "p": PLATFORM, "spid": "spid-1",
         "dom": DOMAIN, "ck": content_key,
         "url": f"https://{DOMAIN}/products/fixture"},
    )


async def _existing_sku(database, sku_key, *, source_variant_id=PK,
                        merchant_id=MERCHANT, suppressed=False):
    await database.execute(
        """INSERT INTO catalog_skus
             (sku_key, product_key, merchant_id, platform, source_product_id,
              source_variant_id, title, readiness_tier, sku_payload,
              suppressed_at, suppression_reason)
           VALUES (:sk,:pk,:m,:p,'spid-1',:svid,'Existing','referral_only',
                   CAST(:payload AS jsonb),
                   CASE WHEN :sup THEN NOW() ELSE NULL END,
                   CASE WHEN :sup THEN 'fixture' ELSE NULL END)""",
        {"sk": sku_key, "pk": PK, "m": merchant_id, "p": PLATFORM,
         "svid": source_variant_id, "sup": suppressed,
         "payload": json.dumps({"source": "some_other_lane", "title_kept": True})},
    )


def _planned(sku_key=SKU):
    """One row in the exact shape `capture_us_market_offers.plan_offer` emits."""
    from scripts.capture_us_market_offers import SOURCE_SYSTEM, derive_us_offer_id

    return {
        "offer_id": derive_us_offer_id(PK),
        "sku_key": sku_key,
        "product_key": PK,
        "availability": "in_stock",
        "list_price": 24.0,
        "source_system": SOURCE_SYSTEM,
        "source_domain": DOMAIN,
        "offer_payload": json.dumps({"capture": "fixture"}),
        "content_key": CONTENT_KEY,
    }


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


async def test_the_identity_index_the_conflict_target_names_exists(db):
    n = await db.fetch_val(
        "SELECT count(*) FROM pg_indexes WHERE tablename='catalog_skus' "
        "AND indexname = :n", {"n": _IDENTITY_INDEX},
    )
    assert n == 1, (
        f"{_IDENTITY_INDEX} is missing: every ON CONFLICT case below would then "
        "pass by inserting a fresh row and prove nothing"
    )


# ---------------------------------------------------------------------------
# capture_us_market_offers: MINT
# ---------------------------------------------------------------------------
async def test_capture_mints_the_canonical_sku_before_writing_the_offer(db):
    """The fix for 529 live orphans. The SKU is written in the shape ingestion
    writes: source_variant_id = product_key, identity read from the catalog row."""
    from scripts.capture_us_market_offers import ensure_skus_for_planned

    await _product(db)
    writable, counts = await ensure_skus_for_planned(
        [_planned()], apply=True, batch_id="batch-mint")

    assert counts["skus_to_mint"] == 1
    assert counts["skus_minted"] == 1
    assert counts["offers_refused_no_sku"] == 0
    assert [row["sku_key"] for row in writable] == [SKU]

    sku = dict(await db.fetch_one(
        "SELECT merchant_id, platform, source_product_id, source_variant_id, "
        "       source_domain, title, readiness_tier, sku_payload "
        "FROM catalog_skus WHERE sku_key = :sk", {"sk": SKU}))
    # Identity read FROM THE CATALOG ROW at write time, not from the plan: the
    # capture probes over HTTP for minutes between the scan and the write, and a
    # bind-carried merchant snapshot is exactly the ADR-009 orphan this lane
    # produced once already.
    assert sku["merchant_id"] == MERCHANT
    assert sku["platform"] == PLATFORM
    assert sku["source_product_id"] == "spid-1"
    assert sku["source_variant_id"] == PK
    assert sku["source_domain"] == DOMAIN
    payload = json.loads(sku["sku_payload"])
    # PRODUCT_DERIVED and nothing else: this identity IS the product key. Calling
    # it merchant_issued would fabricate the provenance the new
    # skus_without_merchant_issued_identity_share invariant then reads.
    assert payload["variant_id_provenance"] == "product_derived"
    assert payload["synthetic_canonical_variant"] is True


async def test_capture_adopts_an_existing_identity_under_another_spelling(db):
    """The identity already exists under a different sku_key. #2135: adopt that
    row and point the offer at it — minting our spelling would be a SECOND
    identity row for one variant, which the 4-column index exists to forbid."""
    from scripts.capture_us_market_offers import ensure_skus_for_planned

    await _product(db)
    await _existing_sku(db, "other::lane::spelling", source_variant_id=PK)

    writable, counts = await ensure_skus_for_planned(
        [_planned()], apply=True, batch_id="batch-adopt")

    assert counts["skus_adopted_other_key"] == 1
    assert [row["sku_key"] for row in writable] == ["other::lane::spelling"]
    # No rival row was minted.
    n = await db.fetch_val(
        "SELECT count(*) FROM catalog_skus WHERE product_key = :pk", {"pk": PK})
    assert n == 1
    # And the adopted row keeps ITS OWN payload — the DO UPDATE touches only
    # updated_at, so adoption never restamps another lane's provenance as ours.
    payload = json.loads(await db.fetch_val(
        "SELECT sku_payload FROM catalog_skus WHERE sku_key = 'other::lane::spelling'"))
    assert payload["source"] == "some_other_lane"
    assert "variant_id_provenance" not in payload


async def test_capture_refuses_the_offer_when_the_identity_is_suppressed(db):
    """A suppressed SKU is invisible to the recall candidate CTE, so attaching a
    live offer to it creates supply nothing can ever surface. The DO UPDATE's
    WHERE returns no row and the caller must read that as a refusal, not as a
    write it can proceed from."""
    from scripts.capture_us_market_offers import ensure_skus_for_planned

    await _product(db)
    await _existing_sku(db, "other::lane::spelling", source_variant_id=PK,
                        suppressed=True)

    writable, counts = await ensure_skus_for_planned(
        [_planned()], apply=True, batch_id="batch-suppressed")

    assert counts["offers_refused_no_sku"] == 1
    assert writable == []


async def test_capture_leaves_an_already_existing_sku_alone(db):
    from scripts.capture_us_market_offers import ensure_skus_for_planned

    await _product(db)
    await _existing_sku(db, SKU)

    writable, counts = await ensure_skus_for_planned(
        [_planned()], apply=True, batch_id="batch-existing")

    assert counts["skus_existing"] == 1
    assert counts["skus_to_mint"] == 0
    assert counts["skus_minted"] == 0
    assert [row["sku_key"] for row in writable] == [SKU]


async def test_capture_refuses_when_the_product_row_is_gone(db):
    """The INSERT ... SELECT FROM catalog_products yields no row, so no SKU is
    minted under a seller we would have had to invent — the fail-closed
    direction. Nothing at all is written."""
    from scripts.capture_us_market_offers import ensure_skus_for_planned

    writable, counts = await ensure_skus_for_planned(
        [_planned()], apply=True, batch_id="batch-noproduct")

    assert counts["offers_refused_no_sku"] == 1
    assert writable == []
    assert await db.fetch_val(
        "SELECT count(*) FROM catalog_skus WHERE product_key = :pk", {"pk": PK}) == 0


async def test_capture_dry_run_mints_nothing_but_still_predicts_the_refusal(db):
    """A plan that promised an offer --apply then refuses is the same class of
    lie as a dropped report line."""
    from scripts.capture_us_market_offers import ensure_skus_for_planned

    await _product(db)
    await _existing_sku(db, "other::lane::spelling", source_variant_id=PK,
                        suppressed=True)

    writable, counts = await ensure_skus_for_planned(
        [_planned()], apply=False, batch_id="batch-dry")

    assert counts["skus_to_mint"] == 1
    assert counts["skus_minted"] == 0
    assert counts["offers_refused_no_sku"] == 1
    assert writable == []
    n = await db.fetch_val(
        "SELECT count(*) FROM catalog_skus WHERE product_key = :pk", {"pk": PK})
    assert n == 1  # only the fixture's row


async def test_capture_offer_upsert_lands_a_non_orphan_row_end_to_end(db):
    """The claim the whole change is about: after the writer runs, the offer it
    wrote JOINS to a catalog_skus row. Asserted by running the join, not by
    reading the two counters separately."""
    from scripts.capture_us_market_offers import OFFER_UPSERT_SQL, ensure_skus_for_planned

    await _product(db)
    writable, _ = await ensure_skus_for_planned(
        [_planned()], apply=True, batch_id="batch-e2e")
    for row in writable:
        await db.execute(OFFER_UPSERT_SQL,
                         {k: v for k, v in row.items() if k != "content_key"})

    joined = await db.fetch_val(
        """SELECT count(*) FROM catalog_offers co
             JOIN catalog_skus s ON s.sku_key = co.sku_key
            WHERE co.product_key = :pk""", {"pk": PK})
    assert joined == 1
    orphaned = await db.fetch_val(
        """SELECT count(*) FROM catalog_offers co
            WHERE co.product_key = :pk
              AND NOT EXISTS (SELECT 1 FROM catalog_skus s
                               WHERE s.sku_key = co.sku_key)""", {"pk": PK})
    assert orphaned == 0


# ---------------------------------------------------------------------------
# attach_retailer_offer: REFUSE
# ---------------------------------------------------------------------------
async def test_attach_refuses_an_offer_whose_sku_does_not_exist(db):
    """The 118-live-orphan half. The refusal is in `attach_retailer_offer`, not
    in the CLI, so it cannot be skipped by calling the function directly."""
    from scripts.attach_retailer_offer import (
        OrphanOfferRefused, attach_retailer_offer, build_retailer_offer_row,
    )

    await _product(db)
    row = build_retailer_offer_row(
        product_key=PK, merchant_id="oliveyoung_global", merchant_name="Olive Young",
        retailer_url="https://global.oliveyoung.com/product/detail?prdtNo=1",
        price=25.9,
    )
    with pytest.raises(OrphanOfferRefused) as excinfo:
        await attach_retailer_offer(row)

    assert excinfo.value.sku_key == SKU
    assert "orphan_no_sku" in str(excinfo.value)
    # AND NOTHING WAS WRITTEN. A refusal that still inserted would be worse than
    # the defect, because the exception would send the operator looking elsewhere.
    assert await db.fetch_val(
        "SELECT count(*) FROM catalog_offers WHERE product_key = :pk", {"pk": PK}) == 0


async def test_attach_writes_when_the_canonical_sku_is_there(db):
    """The refusal must not be a blanket one: with the chain materialized the
    tool still does its job."""
    from scripts.attach_retailer_offer import (
        attach_retailer_offer, build_retailer_offer_row,
    )

    await _product(db, content_key=None)
    await _existing_sku(db, SKU)
    row = build_retailer_offer_row(
        product_key=PK, merchant_id="oliveyoung_global", merchant_name="Olive Young",
        retailer_url="https://global.oliveyoung.com/product/detail?prdtNo=1",
        price=25.9,
    )
    content_key = await attach_retailer_offer(row)

    assert content_key is None
    written = dict(await db.fetch_one(
        "SELECT sku_key, merchant_id, offer_type, market, currency "
        "FROM catalog_offers WHERE product_key = :pk", {"pk": PK}))
    assert written["sku_key"] == SKU
    # The offer's merchant is the RETAILER — which is exactly why this writer
    # cannot mint the SKU: a catalog_skus row under `oliveyoung_global` would be
    # a second identity tuple for one product.
    assert written["merchant_id"] == "oliveyoung_global"
    assert written["offer_type"] == "retailer"


async def test_attach_refusal_does_not_depend_on_the_price_being_present(db):
    """The full `guard_catalog_offer_rows` would ALSO reject a null price as
    zero_or_missing_price and so retire the documented destination-only offer.
    This writer checks only the orphan half, deliberately — pinned here so a
    later "just use the guard" simplification cannot quietly change policy."""
    from scripts.attach_retailer_offer import (
        attach_retailer_offer, build_retailer_offer_row,
    )

    await _product(db, content_key=None)
    await _existing_sku(db, SKU)
    row = build_retailer_offer_row(
        product_key=PK, merchant_id="amazon_us", merchant_name=None,
        retailer_url="https://www.amazon.com/dp/B000", price=None,
    )
    await attach_retailer_offer(row)

    written = dict(await db.fetch_one(
        "SELECT list_price, sku_key FROM catalog_offers WHERE product_key = :pk",
        {"pk": PK}))
    assert written["list_price"] is None
    assert written["sku_key"] == SKU
