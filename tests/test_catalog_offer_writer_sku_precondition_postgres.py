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

THE SECOND GATE COLUMN, added after review. The capture lane read
`catalog_products.suppressed_at` NOWHERE — not in the candidate scan, not in the
mint, not in the offer upsert — so a withdrawn product went through the whole
lane and came out with a live US offer, re-creating exactly the
`suppressed_product_with_live_offer` class the reconciler's cascade pass drains.
And the SKU-side refusal turned out to depend on SPELLING: a suppressed row
holding the derived `<pk>::canonical` key counted as `existing` and the offer was
written, while the same row under another lane's key was refused. Both are
covered below, at the layer that decides and at the statement that writes.

WHAT A RE-RUN MAY UNDO is covered here too. After the reconciler tombstones this
lane's orphans, a later capture run has to repoint `sku_key` and lift THAT
tombstone — and only that one. The pair of tests around
`test_a_refresh_does_not_lift_a_tombstone_it_did_not_cause` is what keeps a
nightly writer from becoming a blanket un-suppressor.
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
    # `index_pipeline_state` has no SQLAlchemy model (migration 098 owns it), and
    # this gate's database has no migrations. Created ADDITIVELY, exactly as a
    # dozen other `*_postgres.py` gate files do — IF NOT EXISTS plus ADD COLUMN
    # IF NOT EXISTS, never a DROP, because the gate runs every file against ONE
    # database. `blocker_code` is the only column CANDIDATES_SQL reads.
    await database.execute(
        "CREATE TABLE IF NOT EXISTS index_pipeline_state (content_key text PRIMARY KEY)")
    await database.execute(
        "ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS blocker_code text")


async def _clear(database):
    """Rows, never tables — the dialect gate shares one database."""
    await database.execute(
        "DELETE FROM catalog_offers WHERE product_key = :pk", {"pk": PK})
    await database.execute(
        "DELETE FROM catalog_skus WHERE product_key = :pk", {"pk": PK})
    await database.execute(
        "DELETE FROM catalog_products WHERE product_key = :pk", {"pk": PK})
    await database.execute(
        "DELETE FROM index_pipeline_state WHERE content_key = :ck", {"ck": CONTENT_KEY})
    await database.execute(
        "DELETE FROM writer_audit_log WHERE writer_name = ANY(:w)",
        {"w": list(_WRITERS)})


async def _product(database, *, merchant_id=MERCHANT, content_key=CONTENT_KEY,
                   suppressed=False):
    """`content_key=None` keeps `attach_retailer_offer` from calling the PDP
    view assembler, whose own dependency chain (product_group_members and
    friends) is a dozen tables this file has no business building. The offer
    INSERT and the refusal — the two things under test — are upstream of it."""
    await database.execute(
        """INSERT INTO catalog_products
             (product_key, merchant_id, platform, source_product_id, source_domain,
              content_key, canonical_url, title, suppressed_at, suppression_reason)
           VALUES (:pk,:m,:p,:spid,:dom,:ck,:url,'Precondition Fixture',
                   CASE WHEN :sup THEN NOW() ELSE NULL END,
                   CASE WHEN :sup THEN 'fixture' ELSE NULL END)""",
        {"pk": PK, "m": merchant_id, "p": PLATFORM, "spid": "spid-1",
         "dom": DOMAIN, "ck": content_key, "sup": suppressed,
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
    # AN ADOPTION IS NOT A MINT. `skus_minted` incremented before this branch, so
    # every adoption was reported as a row this writer created — a counter that
    # cannot be reconciled against a COUNT of catalog_skus after a run, which is
    # the only external check it has.
    assert counts["skus_minted"] == 0
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
    # WHICH GUARD REFUSED. Two guards close this door — the mint's DO UPDATE
    # WHERE, and `guard_catalog_offer_rows(live_only=True)` after it — and with
    # only "the offer was refused" asserted, either could be deleted alone and
    # every test would stay green. The mint-WHERE path refuses BEFORE adopting
    # the suppressed row, so nothing was adopted; the backstop path (the next
    # test) adopts first and refuses after. 0 here pins the mint's WHERE.
    assert counts["skus_adopted_other_key"] == 0


async def test_the_guard_backstop_refuses_what_a_mint_without_its_where_adopts(db, monkeypatch):
    """THE SECOND GUARD, PINNED ON ITS OWN. The mint is run WITHOUT its DO UPDATE
    WHERE — the future mint bug the backstop exists for — so it adopts the
    suppressed identity and hands the offer on. `guard_catalog_offer_rows(...,
    live_only=True)` must then refuse it. `skus_adopted_other_key == 1` is what
    says the refusal came from the backstop and not from the mint: remove the
    backstop and the offer is written against a suppressed SKU; remove the
    mint's WHERE and the sibling test above fails instead."""
    from scripts import capture_us_market_offers as mod

    where = ("     WHERE catalog_skus.suppressed_at IS NULL\n"
             "       AND catalog_skus.suppression_reason IS NULL\n")
    assert where in mod.MINT_CANONICAL_SKU_SQL
    monkeypatch.setattr(mod, "MINT_CANONICAL_SKU_SQL",
                        mod.MINT_CANONICAL_SKU_SQL.replace(where, ""))

    await _product(db)
    await _existing_sku(db, "other::lane::spelling", source_variant_id=PK,
                        suppressed=True)

    writable, counts = await mod.ensure_skus_for_planned(
        [_planned()], apply=True, batch_id="batch-backstop")

    assert counts["skus_adopted_other_key"] == 1
    assert counts["offers_refused_no_sku"] == 1
    assert writable == []
    # Adoption touched only updated_at; the identity is still suppressed.
    assert await db.fetch_val(
        "SELECT suppressed_at IS NOT NULL FROM catalog_skus "
        "WHERE sku_key = 'other::lane::spelling'") is True


async def test_the_mint_itself_refuses_a_product_suppressed_after_the_probe(db, monkeypatch):
    """MINT_CANONICAL_SKU_SQL's own `cp.suppressed_at IS NULL` is unreachable
    through the module's call path: SUPPRESSED_PRODUCT_PROBE_SQL runs first and
    drops the product's rows before any mint. It is race-only — the product is
    suppressed between the probe and the INSERT — so the probe is stubbed to
    miss, which is the only way to drive the statement against a suppressed
    product. Remove the predicate and a SKU is minted under a withdrawn product
    (measured: `skus_minted: 1`, no catalog_skus row before, one after)."""
    from scripts import capture_us_market_offers as mod

    monkeypatch.setattr(mod, "SUPPRESSED_PRODUCT_PROBE_SQL",
                        mod.SUPPRESSED_PRODUCT_PROBE_SQL + "\n       AND FALSE")
    await _product(db, suppressed=True)

    writable, counts = await mod.ensure_skus_for_planned(
        [_planned()], apply=True, batch_id="batch-mint-race")

    assert counts["offers_refused_product_suppressed"] == 0  # the probe missed
    assert counts["skus_to_mint"] == 1
    assert counts["skus_minted"] == 0
    assert counts["offers_refused_no_sku"] == 1
    assert writable == []
    assert await db.fetch_val(
        "SELECT count(*) FROM catalog_skus WHERE product_key = :pk", {"pk": PK}) == 0


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


async def test_capture_refuses_the_offer_when_the_CANONICAL_spelling_is_suppressed(db):
    """THE REFUSAL MUST NOT DEPEND ON WHICH SPELLING THE SUPPRESSED ROW CARRIES.

    The sibling test above suppresses the identity under ANOTHER lane's sku_key,
    so the derived `<pk>::canonical` key is absent, the mint runs, its DO UPDATE
    finds a suppressed row and returns nothing, and the offer is refused. When the
    SUPPRESSED row already holds the derived key, none of that happens: the
    existence lookup counted it as `existing`, the mint never ran, and the offer
    was written against a suppressed SKU — measured. `%::canonical` is 39.4% of
    catalog_skus, so that was the majority case, and the two spellings had
    opposite outcomes for one situation.
    """
    from scripts.capture_us_market_offers import ensure_skus_for_planned

    await _product(db)
    await _existing_sku(db, SKU, suppressed=True)

    writable, counts = await ensure_skus_for_planned(
        [_planned()], apply=True, batch_id="batch-canonical-suppressed")

    assert counts["skus_existing"] == 0
    assert counts["offers_refused_no_sku"] == 1
    assert writable == []


async def test_capture_refuses_every_offer_of_a_suppressed_product(db):
    """A SUPPRESSED PRODUCT IS THE THING THE CASCADE JUST GATED. Writing a live US
    offer against it re-creates `suppressed_product_with_live_offer`, so the lane
    would regrow the class the reconciler drained the night before. The refusal
    has to be its own check and not a side effect of the mint, because the
    already-has-a-SKU path never reaches the mint at all — which is the shape
    used here.
    """
    from scripts.capture_us_market_offers import ensure_skus_for_planned

    await _product(db, suppressed=True)
    await _existing_sku(db, SKU)

    writable, counts = await ensure_skus_for_planned(
        [_planned()], apply=True, batch_id="batch-product-suppressed")

    assert counts["offers_refused_product_suppressed"] == 1
    assert counts["skus_existing"] == 0
    assert writable == []


async def test_capture_dry_run_predicts_the_suppressed_product_refusal(db):
    """The plan and the run must agree about this one too."""
    from scripts.capture_us_market_offers import ensure_skus_for_planned

    await _product(db, suppressed=True)

    writable, counts = await ensure_skus_for_planned(
        [_planned()], apply=False, batch_id="batch-product-suppressed-dry")

    assert counts["offers_refused_product_suppressed"] == 1
    assert counts["skus_to_mint"] == 0
    assert writable == []
    assert await db.fetch_val(
        "SELECT count(*) FROM catalog_skus WHERE product_key = :pk", {"pk": PK}) == 0


async def test_the_candidate_scan_never_offers_a_suppressed_product(db):
    """The same refusal one layer up, EXECUTED. CANDIDATES_SQL selects the cohort
    minutes before any write; leaving suppressed products in it means the lane
    spends HTTP budget on rows it must then refuse, and means the only thing
    standing between a withdrawn product and a live offer is the later check.
    """
    from scripts.capture_us_market_offers import CANDIDATES_SQL

    await _product(db)
    await db.execute(
        "INSERT INTO index_pipeline_state (content_key, blocker_code) "
        "VALUES (:ck, 'no_us_offer')", {"ck": CONTENT_KEY})
    await db.execute(
        """INSERT INTO catalog_offers
             (offer_id, sku_key, product_key, merchant_id, catalog_track,
              truth_tier, readiness_tier, offer_mode, channel, market,
              availability, currency, list_price, source_system, source_domain)
           VALUES ('o:foreign',:sk,:pk,:m,'external_referral','observed',
                   'referral_only','redirect','external_referral','GB',
                   'in_stock','GBP',19.0,'fixture',:dom)""",
        {"sk": SKU, "pk": PK, "m": MERCHANT, "dom": DOMAIN},
    )

    live = [dict(r) for r in await db.fetch_all(CANDIDATES_SQL)]
    assert [r["product_key"] for r in live if r["product_key"] == PK] == [PK]

    await db.execute(
        "UPDATE catalog_products SET suppressed_at = NOW(), "
        "suppression_reason = 'fixture' WHERE product_key = :pk", {"pk": PK})

    after = [dict(r) for r in await db.fetch_all(CANDIDATES_SQL)]
    assert [r for r in after if r["product_key"] == PK] == []


# ---------------------------------------------------------------------------
# the reconciler's tombstone, and what a re-run of the writer may lift
# ---------------------------------------------------------------------------
async def _offer_state(database, offer_id):
    row = await database.fetch_one(
        "SELECT sku_key, list_price, suppressed_at, suppression_reason, "
        "       suppression_metadata FROM catalog_offers WHERE offer_id = :oid",
        {"oid": offer_id},
    )
    return dict(row) if row else None


async def test_a_reconciled_orphan_comes_back_live_on_the_key_that_now_exists(db):
    """THE WHOLE POINT OF THE RECONCILER PLUS THE WRITER FIX, END TO END.

    The reconciler suppressed this lane's 529 orphans as `orphan_no_sku`. A later
    capture run resolves the identity and refreshes the offer — and before this
    change the `ON CONFLICT (offer_id) DO UPDATE` touched price and merchant
    only: the row kept the DEAD sku_key and kept its tombstone, while the report
    said `written: 1`. The offer was fixed everywhere except in the two columns
    that decide whether anything can see it.

    THE IDENTITY RESOLVES TO ANOTHER LANE'S SPELLING here, deliberately. With a
    mint the derived key and the resolved key are equal, so a DO UPDATE that
    never repoints `sku_key` still lands on the right row by accident and the
    test would prove only half of what it claims. Under adoption the two differ,
    and the refresh has to follow the identity.
    """
    from scripts.capture_us_market_offers import (
        OFFER_UPSERT_SQL, derive_us_offer_id, ensure_skus_for_planned,
    )
    from scripts.reconcile_catalog_offers import REASON_ORPHAN, run as reconcile

    offer_id = derive_us_offer_id(PK)
    await _product(db)
    # The identity exists, but under a key this lane does not derive.
    await _existing_sku(db, "other::lane::spelling", source_variant_id=PK)
    # The orphan exactly as the lane minted it: a live offer on a key with no
    # catalog_skus row.
    await db.execute(
        """INSERT INTO catalog_offers
             (offer_id, sku_key, product_key, merchant_id, catalog_track,
              truth_tier, readiness_tier, offer_mode, channel, market,
              availability, currency, list_price, source_system)
           VALUES (:oid,:sk,:pk,:m,'external_referral','observed','referral_only',
                   'redirect','external_referral','US','in_stock','USD',9.0,
                   'us_market_capture')""",
        {"oid": offer_id, "sk": SKU, "pk": PK, "m": MERCHANT},
    )

    report = await reconcile(apply=True, limit=0, passes=("orphans",))
    assert report["orphans"]["suppressed"] == 1
    gated = await _offer_state(db, offer_id)
    assert gated["suppression_reason"] == REASON_ORPHAN
    assert json.loads(gated["suppression_metadata"])["reconcile_pass"] == "orphans"

    # The lane runs again; this time it resolves the identity first.
    writable, counts = await ensure_skus_for_planned(
        [_planned()], apply=True, batch_id="batch-revive")
    assert counts["skus_adopted_other_key"] == 1
    assert [r["sku_key"] for r in writable] == ["other::lane::spelling"]
    for row in writable:
        await db.execute(OFFER_UPSERT_SQL,
                         {k: v for k, v in row.items() if k != "content_key"})

    healed = await _offer_state(db, offer_id)
    assert healed["suppressed_at"] is None
    assert healed["suppression_reason"] is None
    assert healed["sku_key"] == "other::lane::spelling"
    # The reconciler's stamp goes with the tombstone; leaving it would make
    # `--revert-batch` count a row it can no longer restore.
    assert "reconcile_pass" not in (json.loads(healed["suppression_metadata"] or "{}"))
    assert float(healed["list_price"]) == 24.0
    joined = await db.fetch_val(
        """SELECT count(*) FROM catalog_offers co
             JOIN catalog_skus s ON s.sku_key = co.sku_key
            WHERE co.offer_id = :oid""", {"oid": offer_id})
    assert joined == 1


async def test_a_refresh_does_not_lift_a_tombstone_it_did_not_cause(db):
    """`orphan_no_sku` IS THE ONLY LABEL THIS WRITE MAY CLEAR, because it is the
    only one whose cause the write itself has just removed. A `product_suppressed`
    row is a live decision by another lane, and a refresh that resurrected it
    would make this writer a blanket un-suppressor — reachable nightly, with no
    operator in the loop.

    (Reaching this state at all takes a race: the product is suppressed here after
    the plan was built. That is the case the CASE expression is for.)
    """
    from scripts.capture_us_market_offers import (
        OFFER_UPSERT_SQL, derive_us_offer_id, ensure_skus_for_planned,
    )
    from services.catalog_offer_suppression import PRODUCT_SUPPRESSED_REASON

    offer_id = derive_us_offer_id(PK)
    await _product(db)
    await _existing_sku(db, SKU)
    writable, _ = await ensure_skus_for_planned(
        [_planned()], apply=True, batch_id="batch-tombstone")
    params = {k: v for k, v in writable[0].items() if k != "content_key"}
    await db.execute(OFFER_UPSERT_SQL, params)

    await db.execute(
        "UPDATE catalog_offers SET suppressed_at = NOW(), suppression_reason = :r "
        "WHERE offer_id = :oid",
        {"oid": offer_id, "r": PRODUCT_SUPPRESSED_REASON},
    )

    await db.execute(OFFER_UPSERT_SQL, params)

    state = await _offer_state(db, offer_id)
    assert state["suppressed_at"] is not None
    assert state["suppression_reason"] == PRODUCT_SUPPRESSED_REASON

    # The label in the CASE is a LITERAL inside the statement (an f-string would
    # drop the whole constant out of the repo-wide PREPARE sweep, which accepts
    # only ast.Constant), so the coupling to the guard's vocabulary is asserted
    # here instead of enforced by interpolation.
    from scripts.capture_us_market_offers import OFFER_UPSERT_SQL as _sql
    from services.catalog_offer_writer_guard import ORPHAN_NO_SKU

    assert f"suppression_reason = '{ORPHAN_NO_SKU}'" in _sql
    assert f"'{PRODUCT_SUPPRESSED_REASON}'" not in _sql


async def test_a_refresh_does_not_lift_a_duplicate_offer_tombstone(db):
    """THE LIKELIEST LABEL FOR THIS LANE TO MEET. The mirror and the capture
    write the same shelf under two offer_id namespaces, so the duplicate pass
    will gate one of them as `duplicate_offer` with the other as keeper — and the
    capture's next nightly refresh then hits the gated row. The DO UPDATE's CASE
    lifts `orphan_no_sku` only; widening it to `duplicate_offer` too passed every
    test before this one and would have rebuilt the group on the next refresh.
    Driven with the reconciler's real duplicate pass, so the row carries exactly
    the stamp prod rows will."""
    from scripts.capture_us_market_offers import (
        OFFER_UPSERT_SQL, derive_us_offer_id, ensure_skus_for_planned,
    )
    from scripts.reconcile_catalog_offers import REASON_DUPLICATE, run as reconcile
    from services.catalog_invariant_checks import _CHECKS

    dup_sql = [c for c in _CHECKS
               if c["name"] == "duplicate_offers_per_sku_channel_market"][0]["count_sql"]

    async def excess():
        return int((await db.fetch_one(dup_sql))["c"] or 0)

    baseline = await excess()
    offer_id = derive_us_offer_id(PK)
    await _product(db)
    await _existing_sku(db, SKU)
    writable, _ = await ensure_skus_for_planned(
        [_planned()], apply=True, batch_id="batch-dup")
    params = {k: v for k, v in writable[0].items() if k != "content_key"}
    await db.execute(OFFER_UPSERT_SQL, params)
    await db.execute(
        "UPDATE catalog_offers SET updated_at = TIMESTAMP '2026-01-01' "
        "WHERE offer_id = :oid", {"oid": offer_id})
    # The mirror's row for the same shelf, newer, so it wins the election.
    await db.execute(
        """INSERT INTO catalog_offers
             (offer_id, sku_key, product_key, merchant_id, catalog_track,
              truth_tier, readiness_tier, offer_mode, channel, market,
              availability, currency, list_price, source_system, updated_at)
           VALUES ('offer:external_seed:fixture',:sk,:pk,:m,'external_referral',
                   'observed','referral_only','redirect','external_referral','US',
                   'in_stock','USD',24.0,'external_seed_mirror',
                   TIMESTAMP '2026-09-01')""",
        {"sk": SKU, "pk": PK, "m": MERCHANT})
    assert await excess() == baseline + 1

    report = await reconcile(apply=True, limit=0, passes=("duplicates",))
    assert report["duplicates"]["sample"] == [offer_id]
    gated = await _offer_state(db, offer_id)
    assert gated["suppression_reason"] == REASON_DUPLICATE
    assert await excess() == baseline

    # The nightly refresh lands on the gated row.
    assert await db.fetch_val(OFFER_UPSERT_SQL, params) == offer_id

    after = await _offer_state(db, offer_id)
    assert after["suppressed_at"] is not None
    assert after["suppression_reason"] == REASON_DUPLICATE
    meta = json.loads(after["suppression_metadata"])
    assert meta["reconcile_pass"] == "duplicates"
    assert meta["reconcile_keeper_offer_id"] == "offer:external_seed:fixture"
    assert float(after["list_price"]) == 24.0  # the price still refreshes
    assert (await _offer_state(db, "offer:external_seed:fixture"))["suppressed_at"] is None
    assert await excess() == baseline
    assert f"'{REASON_DUPLICATE}'" not in OFFER_UPSERT_SQL


async def test_the_upsert_writes_nothing_when_the_product_is_suppressed(db):
    """The fail-closed backstop, and the count that goes with it. `execute()`
    returns no rowcount through `databases` + asyncpg, so a writer that assumed
    its INSERT landed would report `written: 1` for a statement whose SELECT
    source yielded nothing."""
    from scripts.capture_us_market_offers import (
        OFFER_UPSERT_SQL, apply_offers, derive_us_offer_id, ensure_skus_for_planned,
    )

    await _product(db)
    await _existing_sku(db, SKU)
    writable, _ = await ensure_skus_for_planned(
        [_planned()], apply=True, batch_id="batch-upsert-guard")
    await db.execute(
        "UPDATE catalog_products SET suppressed_at = NOW(), "
        "suppression_reason = 'fixture' WHERE product_key = :pk", {"pk": PK})

    summary = await apply_offers(writable)

    assert summary["written"] == 0
    assert summary["not_written"] == [PK]
    assert await db.fetch_val(
        "SELECT count(*) FROM catalog_offers WHERE offer_id = :oid",
        {"oid": derive_us_offer_id(PK)}) == 0
    assert OFFER_UPSERT_SQL.strip().endswith("RETURNING offer_id")


# ---------------------------------------------------------------------------
# the shared guard, asked the LIVE question
# ---------------------------------------------------------------------------
async def test_the_guard_refuses_a_suppressed_sku_only_when_asked_for_live_only(db):
    """`live_only` is opt-in, and both answers are pinned. The existing callers
    were written against "does a row exist"; flipping that under them would change
    which rows they refuse without anyone deciding to, so the capture lane asks
    for the live question explicitly and everyone else keeps the old one."""
    from services.catalog_offer_writer_guard import (
        ORPHAN_NO_SKU, guard_catalog_offer_rows,
    )

    await _product(db)
    await _existing_sku(db, SKU, suppressed=True)
    row = {"offer_id": "o:guarded", "sku_key": SKU, "list_price": 12.0}

    accepted, _reasons, rejected = await guard_catalog_offer_rows([dict(row)])
    assert [r["offer_id"] for r in accepted] == ["o:guarded"]
    assert rejected == []

    accepted, reasons, rejected = await guard_catalog_offer_rows(
        [dict(row)], live_only=True)
    assert accepted == []
    assert reasons[ORPHAN_NO_SKU] == 1
    assert rejected[0]["reasons"] == [ORPHAN_NO_SKU]


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


async def test_attach_refuses_when_the_canonical_sku_is_suppressed(db):
    """LIVE, NOT MERELY EXISTING. The first cut asked `fetch_existing_catalog_sku_keys`,
    so a suppressed `::canonical` row let a live retailer offer through onto an
    identity somebody had withdrawn — the same unservable state as an orphan,
    reached through a row that happens to exist. The capture lane asks the live
    question; so must this one."""
    from scripts.attach_retailer_offer import (
        OrphanOfferRefused, attach_retailer_offer, build_retailer_offer_row,
    )

    await _product(db, content_key=None)
    await _existing_sku(db, SKU, suppressed=True)
    row = build_retailer_offer_row(
        product_key=PK, merchant_id="oliveyoung_global", merchant_name="Olive Young",
        retailer_url="https://global.oliveyoung.com/product/detail?prdtNo=1",
        price=25.9,
    )
    with pytest.raises(OrphanOfferRefused) as excinfo:
        await attach_retailer_offer(row)

    assert excinfo.value.sku_key == SKU
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


async def test_attach_drive_exits_2_on_the_refusal_and_writes_no_offer(db):
    """THE EXIT CODE IS THE SIGNAL A WRAPPER SCRIPT READS. A refusal that returned
    0 reads as "offer attached" to every caller in a shell pipeline. Driven
    against the real database with no catalog_skus row, so the 2 comes from the
    refusal and not from a stub."""
    import argparse

    from scripts import attach_retailer_offer as mod

    await _product(db, content_key=None)
    args = argparse.Namespace(
        product_key=PK, merchant_id="oliveyoung_global", merchant_name=None,
        retailer_url="https://global.oliveyoung.com/product/detail?prdtNo=1",
        market="US", currency="USD", price="25.9", availability="in_stock",
        apply=True,
    )

    try:
        assert await mod._drive(args) == 2
    finally:
        # `_drive` owns the connection lifecycle (connect/disconnect around its
        # own work), so it leaves the shared handle CLOSED. Re-open it or the
        # fixture's own teardown fails on a dead pool.
        if not db.is_connected:
            await db.connect()
    assert await db.fetch_val(
        "SELECT count(*) FROM catalog_offers WHERE product_key = :pk", {"pk": PK}) == 0


async def test_attach_dry_run_makes_the_same_refusal_and_exits_2(db, capsys):
    """THE DRY RUN'S REFUSAL, PINNED. The comment in `_drive` says the dry run
    makes the same check; gating that branch on `args.apply` passed every test
    before this one, and would have printed "would attach" for an offer --apply
    then refuses — a report and a run that disagree, the defect this whole change
    is about. A dry run writes no audit row either way."""
    import argparse

    from scripts import attach_retailer_offer as mod

    await _product(db, content_key=None)
    args = argparse.Namespace(
        product_key=PK, merchant_id="oliveyoung_global", merchant_name=None,
        retailer_url="https://global.oliveyoung.com/product/detail?prdtNo=1",
        market="US", currency="USD", price="25.9", availability="in_stock",
        apply=False,
    )
    try:
        assert await mod._drive(args) == 2
    finally:
        if not db.is_connected:
            await db.connect()

    out = capsys.readouterr().out
    line = [l for l in out.splitlines() if l.startswith(mod.REPORT_BEGIN)][0]
    report = json.loads(line[len(mod.REPORT_BEGIN):-len(mod.REPORT_END)])
    assert report["applied"] == 0
    assert report["offers_refused_no_sku"] == 1
    assert report["refusal"] == "orphan_no_sku"
    assert report["written"] == 0
    assert await db.fetch_val(
        "SELECT count(*) FROM catalog_offers WHERE product_key = :pk", {"pk": PK}) == 0
    assert await db.fetch_val(
        "SELECT count(*) FROM writer_audit_log WHERE writer_name = :w",
        {"w": mod.SOURCE_SYSTEM}) == 0


async def test_attach_main_propagates_that_exit_code(db):
    """`main()` is what `raise SystemExit(main())` runs. The `_drive` value has to
    survive `asyncio.run` and the return — a bare `asyncio.run(...)` with no
    `return`, or a `return 0`, throws the refusal away and nothing above would
    notice. Run in a worker thread because `asyncio.run` refuses to start a loop
    inside a running one; `_drive` is stubbed so this test is about the
    propagation and about `_parse_args` accepting the argv, nothing else."""
    import asyncio as _asyncio
    import sys

    from scripts import attach_retailer_offer as mod

    async def refuse(_args):
        return 2

    original_drive, original_argv = mod._drive, sys.argv
    mod._drive = refuse
    sys.argv = [
        "attach_retailer_offer.py",
        "--product-key", PK,
        "--merchant-id", "oliveyoung_global",
        "--retailer-url", "https://global.oliveyoung.com/product/detail?prdtNo=1",
        "--price", "25.9", "--apply",
    ]
    try:
        assert await _asyncio.to_thread(mod.main) == 2
    finally:
        mod._drive, sys.argv = original_drive, original_argv


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
