"""`scripts/reconcile_catalog_offers.py`'s three passes, EXECUTED on real rows.

WHY THIS FILE IS A POSTGRES GATE AND NOT A SQLITE SUITE. Every semantic that
matters here is Postgres-only:

  * `row_number() OVER (... ORDER BY updated_at DESC NULLS LAST, offer_id)` —
    the keeper election. `NULLS LAST` is not SQLite's default ordering, so a
    SQLite twin would pin a different winner than the one that ships.
  * `RETURNING offer_id` from an UPDATE — the only way this repo can count rows
    it changed (`databases` + asyncpg returns NO rowcount from `execute()`;
    SQLite does, which is how a caller comes to believe it has one).
  * `CREATE UNIQUE INDEX CONCURRENTLY ... WHERE suppressed_at IS NULL` — a
    partial index, and one Postgres refuses to build inside a transaction.
  * `jsonb || jsonb` for the batch stamp, and `->>` to read it back in the
    revert.

A string assertion about the SQL cannot tell a correct keeper election from a
plausible-looking wrong one, and that is the defect this pass can actually have.

The file is named `test_*_postgres.py` so `.github/workflows/postgres-dialect-gate.yml`
discovers it with no ride-along edit. That gate runs every such file against ONE
database, so this fixture builds its tables from the real `db/` models via
`tests.model_schema.ensure_model_tables`, never DROPs a shared table, and cleans
up with DELETE scoped to its own keys.
"""

import datetime as dt
import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    not str(os.getenv("DATABASE_URL") or "").startswith("postgres"),
    reason="needs a real Postgres DATABASE_URL — production-dialect gate",
)

MERCHANT = "m_recon_pg"
PLATFORM = "external_seed"
PK_LIVE = "recon::pg::live"
PK_DEAD = "recon::pg::suppressed"
SKU_LIVE = PK_LIVE + "::canonical"
SKU_DEAD = PK_DEAD + "::canonical"
#: A sku_key with no catalog_skus row — the orphan shape prod carries 2,139 of.
SKU_ORPHAN = "recon::pg::nosuch::canonical"

def _jsonb(value):
    """`databases` + asyncpg hands a jsonb column back as a STRING, not a dict —
    the same asymmetry that makes every writer here CAST(:x AS jsonb) on the way
    in. Reading it as a mapping without this is a TypeError, not a wrong answer,
    so it is loud; decoding it once here keeps every assertion honest about what
    the column actually returns."""
    if value is None or isinstance(value, dict):
        return value or {}
    return json.loads(value)


def _AT(*args):
    """asyncpg binds a timestamp from a datetime and REFUSES a string. Naive,
    because catalog_offers.updated_at is a naive DateTime in db/catalog.py — a
    tz-aware value would be converted using the client process tz and the
    fixture would not mean what it reads as."""
    return dt.datetime(*args)


_TABLES = ("catalog_offers", "catalog_skus", "catalog_products", "writer_audit_log")
_ALL_PKS = (PK_LIVE, PK_DEAD)


async def _ddl(database):
    """Production's exact DDL from the models. Never DROP — the dialect gate
    shares one database across every gate file, so a drop here destroys schema
    the next file needs."""
    from db.catalog import (
        catalog_offers, catalog_products, catalog_skus, writer_audit_log,
    )
    from tests.model_schema import ensure_model_tables

    await ensure_model_tables(
        [catalog_products, catalog_skus, catalog_offers, writer_audit_log]
    )


async def _clear(database):
    """This fixture's ROWS only."""
    await database.execute(
        "DELETE FROM catalog_offers WHERE product_key = ANY(:pks)", {"pks": list(_ALL_PKS)}
    )
    await database.execute(
        "DELETE FROM catalog_skus WHERE product_key = ANY(:pks)", {"pks": list(_ALL_PKS)}
    )
    await database.execute(
        "DELETE FROM catalog_products WHERE product_key = ANY(:pks)", {"pks": list(_ALL_PKS)}
    )
    await database.execute(
        "DELETE FROM writer_audit_log WHERE writer_name = :w",
        {"w": "reconcile_catalog_offers"},
    )


async def _product(database, product_key, *, suppressed=False):
    await database.execute(
        """INSERT INTO catalog_products
             (product_key, merchant_id, platform, source_product_id, source_domain,
              title, suppressed_at, suppression_reason)
           VALUES (:pk,:m,:p,:spid,'brand.example','Recon Fixture',
                   CASE WHEN :sup THEN NOW() ELSE NULL END,
                   CASE WHEN :sup THEN 'fixture' ELSE NULL END)""",
        {"pk": product_key, "m": MERCHANT, "p": PLATFORM,
         "spid": product_key, "sup": suppressed},
    )


async def _sku(database, sku_key, product_key):
    await database.execute(
        """INSERT INTO catalog_skus
             (sku_key, product_key, merchant_id, platform, source_product_id,
              source_variant_id, title, readiness_tier)
           VALUES (:sk,:pk,:m,:p,:spid,:pk,'Recon Fixture','referral_only')""",
        {"sk": sku_key, "pk": product_key, "m": MERCHANT, "p": PLATFORM,
         "spid": product_key},
    )


async def _offer(database, offer_id, *, sku_key, product_key,
                 channel="external_referral", market="US", updated_at=None,
                 suppressed=False):
    await database.execute(
        """INSERT INTO catalog_offers
             (offer_id, sku_key, product_key, merchant_id, catalog_track, truth_tier,
              readiness_tier, offer_mode, channel, market, availability, currency,
              list_price, source_system, suppressed_at, suppression_reason,
              created_at, updated_at)
           VALUES (:oid,:sk,:pk,:m,'external_referral','observed','referral_only',
                   'redirect',:ch,:mkt,'in_stock','USD',10.0,'fixture',
                   CASE WHEN :sup THEN NOW() ELSE NULL END,
                   CASE WHEN :sup THEN 'fixture' ELSE NULL END,
                   NOW(), COALESCE(:upd, NOW()))""",
        {"oid": offer_id, "sk": sku_key, "pk": product_key, "m": MERCHANT,
         "ch": channel, "mkt": market, "sup": suppressed, "upd": updated_at},
    )


async def _state(database, offer_id):
    row = await database.fetch_one(
        "SELECT suppressed_at, suppression_reason, suppression_metadata "
        "FROM catalog_offers WHERE offer_id = :oid",
        {"oid": offer_id},
    )
    return dict(row) if row else None


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
        await database.execute(
            f"DROP INDEX IF EXISTS {_index_name()}"
        )
        if not was_connected and database.is_connected:
            await database.disconnect()


PASSES_ALL = ("orphans", "duplicates", "cascade")


def _index_name():
    from scripts.reconcile_catalog_offers import UNIQUE_INDEX_NAME

    return UNIQUE_INDEX_NAME


# ---------------------------------------------------------------------------
# (a) orphan offers
# ---------------------------------------------------------------------------
async def test_orphan_pass_suppresses_offers_whose_sku_does_not_exist(db):
    """The 647-live-row class. BOTH columns are set, and the offer that DOES
    have a SKU is untouched — an orphan sweep that also gated healthy supply
    would be worse than the defect."""
    from scripts.reconcile_catalog_offers import REASON_ORPHAN, run

    await _product(db, PK_LIVE)
    await _sku(db, SKU_LIVE, PK_LIVE)
    await _offer(db, "o:orphan", sku_key=SKU_ORPHAN, product_key=PK_LIVE)
    await _offer(db, "o:healthy", sku_key=SKU_LIVE, product_key=PK_LIVE,
                 channel="native")

    report = await run(apply=True, limit=0, passes=("orphans",),
                       create_index=False)

    assert report["orphans"]["found"] == 1
    assert report["orphans"]["suppressed"] == 1
    orphan = await _state(db, "o:orphan")
    assert orphan["suppressed_at"] is not None
    assert orphan["suppression_reason"] == REASON_ORPHAN
    assert (await _state(db, "o:healthy"))["suppressed_at"] is None


async def test_dry_run_changes_nothing_and_still_reports_the_finding(db):
    """A dry run that under-reports is as bad as one that writes: the operator's
    only signal that --apply is needed is the count."""
    from scripts.reconcile_catalog_offers import run

    await _product(db, PK_LIVE)
    await _offer(db, "o:orphan", sku_key=SKU_ORPHAN, product_key=PK_LIVE)

    report = await run(apply=False, limit=0, passes=("orphans",),
                       create_index=False)

    assert report["orphans"]["found"] == 1
    assert report["orphans"]["suppressed"] == 0
    assert (await _state(db, "o:orphan"))["suppressed_at"] is None
    assert report["applied"] == 0
    # No audit row for a run that wrote nothing.
    n = await db.fetch_val(
        "SELECT count(*) FROM writer_audit_log WHERE writer_name = :w",
        {"w": "reconcile_catalog_offers"},
    )
    assert n == 0


# ---------------------------------------------------------------------------
# (b) duplicates
# ---------------------------------------------------------------------------
async def test_duplicate_pass_keeps_the_newest_updated_at(db):
    """THE ROW IT KEEPS IS THE POINT. Suppressing the newest and keeping a
    stale price is a green run that silently rolls the catalogue's prices
    backwards, and the counts alone cannot tell the two apart."""
    from scripts.reconcile_catalog_offers import REASON_DUPLICATE, run

    await _product(db, PK_LIVE)
    await _sku(db, SKU_LIVE, PK_LIVE)
    await _offer(db, "o:stale", sku_key=SKU_LIVE, product_key=PK_LIVE,
                 updated_at=_AT(2026, 1, 1))
    await _offer(db, "o:fresh", sku_key=SKU_LIVE, product_key=PK_LIVE,
                 updated_at=_AT(2026, 9, 1))

    report = await run(apply=True, limit=0, passes=("duplicates",),
                       create_index=False)

    assert report["duplicates"]["excess_rows_found"] == 1
    assert report["duplicates"]["groups_found"] == 1
    assert (await _state(db, "o:fresh"))["suppressed_at"] is None
    stale = await _state(db, "o:stale")
    assert stale["suppressed_at"] is not None
    assert stale["suppression_reason"] == REASON_DUPLICATE
    # The keeper is NAMED on the suppressed row, so a reader can check the
    # election after the fact instead of re-deriving it.
    assert _jsonb(stale["suppression_metadata"])["reconcile_keeper_offer_id"] == "o:fresh"
    assert report["duplicates"]["live_duplicate_groups_after"] == 0


async def test_duplicate_tie_on_updated_at_keeps_the_lowest_offer_id(db):
    """Without the offer_id tie-break the survivor depends on scan order, which
    makes the pass non-deterministic against exactly the rows two writers race
    to produce (both stamped by the same batch, same second)."""
    from scripts.reconcile_catalog_offers import run

    await _product(db, PK_LIVE)
    await _sku(db, SKU_LIVE, PK_LIVE)
    for oid in ("o:zzz", "o:aaa", "o:mmm"):
        await _offer(db, oid, sku_key=SKU_LIVE, product_key=PK_LIVE,
                     updated_at=_AT(2026, 5, 5, 12))

    await run(apply=True, limit=0, passes=("duplicates",), create_index=False)

    assert (await _state(db, "o:aaa"))["suppressed_at"] is None
    for oid in ("o:mmm", "o:zzz"):
        state = await _state(db, oid)
        assert state["suppressed_at"] is not None
        assert _jsonb(state["suppression_metadata"])["reconcile_keeper_offer_id"] == "o:aaa"


async def test_channel_and_market_separate_shelves_not_duplicates(db):
    """The tuple is (sku_key, channel, market). Two offers on one SKU that
    differ in either are two shelves, and gating one of them deletes real
    supply — the failure this pass must not have."""
    from scripts.reconcile_catalog_offers import run

    await _product(db, PK_LIVE)
    await _sku(db, SKU_LIVE, PK_LIVE)
    await _offer(db, "o:us", sku_key=SKU_LIVE, product_key=PK_LIVE, market="US")
    await _offer(db, "o:gb", sku_key=SKU_LIVE, product_key=PK_LIVE, market="GB")
    await _offer(db, "o:native", sku_key=SKU_LIVE, product_key=PK_LIVE,
                 channel="native")

    report = await run(apply=True, limit=0, passes=("duplicates",),
                       create_index=False)

    assert report["duplicates"]["excess_rows_found"] == 0
    for oid in ("o:us", "o:gb", "o:native"):
        assert (await _state(db, oid))["suppressed_at"] is None


async def test_an_already_suppressed_row_is_not_a_duplicate(db):
    """Because remediation suppresses rather than deletes, a second run must not
    see its own output as a fresh duplicate — otherwise the pass would suppress
    the survivor on the next pass and the shelf would go empty."""
    from scripts.reconcile_catalog_offers import run

    await _product(db, PK_LIVE)
    await _sku(db, SKU_LIVE, PK_LIVE)
    await _offer(db, "o:keep", sku_key=SKU_LIVE, product_key=PK_LIVE)
    await _offer(db, "o:gone", sku_key=SKU_LIVE, product_key=PK_LIVE,
                 suppressed=True)

    report = await run(apply=True, limit=0, passes=("duplicates",),
                       create_index=False)

    assert report["duplicates"]["excess_rows_found"] == 0
    assert (await _state(db, "o:keep"))["suppressed_at"] is None


# ---------------------------------------------------------------------------
# (c) suppression cascade
# ---------------------------------------------------------------------------
async def test_cascade_pass_gates_offers_of_a_suppressed_product(db):
    from scripts.reconcile_catalog_offers import REASON_PRODUCT_SUPPRESSED, run

    await _product(db, PK_DEAD, suppressed=True)
    await _product(db, PK_LIVE)
    await _sku(db, SKU_DEAD, PK_DEAD)
    await _sku(db, SKU_LIVE, PK_LIVE)
    await _offer(db, "o:on_dead", sku_key=SKU_DEAD, product_key=PK_DEAD)
    await _offer(db, "o:on_live", sku_key=SKU_LIVE, product_key=PK_LIVE)

    report = await run(apply=True, limit=0, passes=("cascade",),
                       create_index=False)

    assert report["cascade"]["found"] == 1
    dead = await _state(db, "o:on_dead")
    assert dead["suppressed_at"] is not None
    assert dead["suppression_reason"] == REASON_PRODUCT_SUPPRESSED
    assert (await _state(db, "o:on_live"))["suppressed_at"] is None


async def test_all_three_passes_in_one_run_and_the_audit_row(db):
    """The shipping invocation. Also pins the ORDER: cascade runs before
    duplicates, so an offer on a suppressed product is labelled
    `product_suppressed` rather than being picked up as a duplicate first."""
    from scripts.reconcile_catalog_offers import (
        REASON_ORPHAN, REASON_PRODUCT_SUPPRESSED, run,
    )

    await _product(db, PK_DEAD, suppressed=True)
    await _product(db, PK_LIVE)
    await _sku(db, SKU_DEAD, PK_DEAD)
    await _sku(db, SKU_LIVE, PK_LIVE)
    await _offer(db, "o:orphan", sku_key=SKU_ORPHAN, product_key=PK_LIVE)
    await _offer(db, "o:dup_a", sku_key=SKU_LIVE, product_key=PK_LIVE,
                 updated_at=_AT(2026, 1, 1))
    await _offer(db, "o:dup_b", sku_key=SKU_LIVE, product_key=PK_LIVE,
                 updated_at=_AT(2026, 9, 1))
    # On the suppressed product AND a duplicate of each other: cascade must
    # claim them first.
    await _offer(db, "o:dead_a", sku_key=SKU_DEAD, product_key=PK_DEAD)
    await _offer(db, "o:dead_b", sku_key=SKU_DEAD, product_key=PK_DEAD)

    report = await run(apply=True, limit=0,
                       passes=("orphans", "duplicates", "cascade"),
                       create_index=False)

    assert report["orphans"]["suppressed"] == 1
    assert report["cascade"]["suppressed"] == 2
    assert report["duplicates"]["suppressed"] == 1
    assert report["suppressed_total"] == 4
    assert (await _state(db, "o:orphan"))["suppression_reason"] == REASON_ORPHAN
    for oid in ("o:dead_a", "o:dead_b"):
        assert (await _state(db, oid))["suppression_reason"] == REASON_PRODUCT_SUPPRESSED
    assert (await _state(db, "o:dup_b"))["suppressed_at"] is None

    audit = await db.fetch_one(
        "SELECT applied_rows, reasons, batch_id FROM writer_audit_log "
        "WHERE writer_name = :w ORDER BY id DESC LIMIT 1",
        {"w": "reconcile_catalog_offers"},
    )
    assert audit is not None
    assert audit["applied_rows"] == 4
    assert audit["batch_id"] == report["batch_id"]
    reasons = _jsonb(audit["reasons"])
    assert reasons["orphans_suppressed"] == 1
    assert reasons["duplicates_suppressed"] == 1
    # A MEASURED zero must not be indistinguishable from never-measured.
    assert isinstance(reasons.get("zero_counters"), list)


async def test_the_plan_equals_the_run_when_the_passes_overlap(db):
    """THE DRY RUN MUST PREDICT THE RUN, and the case where it did not is the
    interesting one: a shelf of three live offers whose NEWEST row belongs to a
    suppressed product. The cascade pass takes that row first, which changes
    which row wins the keeper election — so a plan that ranked all three
    reported `excess_rows_found: 2` while --apply moved 1. Measured, then fixed
    by excluding earlier passes' claims from the RANKING rather than filtering
    them out of its result.
    """
    from scripts.reconcile_catalog_offers import run

    await _product(db, PK_LIVE)
    await _product(db, PK_DEAD, suppressed=True)
    await _sku(db, SKU_LIVE, PK_LIVE)
    await _offer(db, "o:dup_a", sku_key=SKU_LIVE, product_key=PK_LIVE,
                 updated_at=_AT(2026, 1, 1))
    await _offer(db, "o:dup_b", sku_key=SKU_LIVE, product_key=PK_LIVE,
                 updated_at=_AT(2026, 2, 1))
    # Newest of the three, and on the suppressed product.
    await _offer(db, "o:on_dead", sku_key=SKU_LIVE, product_key=PK_DEAD,
                 updated_at=_AT(2026, 9, 1))

    plan = await run(apply=False, limit=0, passes=PASSES_ALL, create_index=False)
    applied = await run(apply=True, limit=0, passes=PASSES_ALL, create_index=False)

    assert plan["duplicates"]["excess_rows_found"] == 1
    assert applied["duplicates"]["excess_rows_found"] == 1
    assert plan["duplicates"]["sample"] == applied["duplicates"]["sample"] == ["o:dup_a"]
    assert plan["cascade"]["found"] == applied["cascade"]["found"] == 1
    # The plan's predicted end state, and the run's measured one.
    assert plan["duplicates"]["live_duplicate_groups_after"] == 0
    assert applied["duplicates"]["live_duplicate_groups_after"] == 0
    # And the row the election actually kept is the newest LIVE one.
    assert (await _state(db, "o:dup_b"))["suppressed_at"] is None


async def test_limit_caps_each_pass(db):
    from scripts.reconcile_catalog_offers import run

    await _product(db, PK_LIVE)
    for i in range(4):
        await _offer(db, f"o:orph{i}", sku_key=SKU_ORPHAN, product_key=PK_LIVE)

    report = await run(apply=True, limit=2, passes=("orphans",),
                       create_index=False)

    assert report["orphans"]["found"] == 2
    assert report["orphans"]["suppressed"] == 2
    live = await db.fetch_val(
        "SELECT count(*) FROM catalog_offers "
        "WHERE product_key = :pk AND suppressed_at IS NULL", {"pk": PK_LIVE},
    )
    assert live == 2


async def test_revert_batch_restores_only_that_batch(db):
    from scripts.reconcile_catalog_offers import run

    await _product(db, PK_LIVE)
    await _offer(db, "o:orphan", sku_key=SKU_ORPHAN, product_key=PK_LIVE)
    await _offer(db, "o:other", sku_key=SKU_ORPHAN, product_key=PK_LIVE,
                 suppressed=True)

    first = await run(apply=True, limit=0, passes=("orphans",), create_index=False)
    reverted = await run(apply=True, limit=0, passes=(), create_index=False,
                         revert=first["batch_id"])

    assert reverted["revert"]["restored"] == 1
    assert (await _state(db, "o:orphan"))["suppressed_at"] is None
    # The row someone else tombstoned keeps its gate.
    assert (await _state(db, "o:other"))["suppressed_at"] is not None


# ---------------------------------------------------------------------------
# the partial unique index
# ---------------------------------------------------------------------------
async def test_index_creation_refuses_while_a_live_duplicate_group_remains(db):
    """The whole reason this is a script flag and not a migration: attempting the
    build against duplicated data fails, and at deploy time it takes the deploy
    with it."""
    from scripts.reconcile_catalog_offers import run

    await _product(db, PK_LIVE)
    await _sku(db, SKU_LIVE, PK_LIVE)
    await _offer(db, "o:a", sku_key=SKU_LIVE, product_key=PK_LIVE)
    await _offer(db, "o:b", sku_key=SKU_LIVE, product_key=PK_LIVE)

    report = await run(apply=True, limit=0, passes=(), create_index=True)

    assert report["unique_index"]["attempted"] is False
    assert report["unique_index"]["reason"] == "live_duplicate_groups_remain"
    assert report["unique_index"]["live_duplicate_groups"] >= 1
    exists = await db.fetch_val(
        "SELECT count(*) FROM pg_indexes WHERE indexname = :n", {"n": _index_name()}
    )
    assert exists == 0


async def test_index_is_built_after_dedupe_and_is_partial_on_live_rows(db):
    """The index MUST be partial. Pass (b) suppresses rather than deletes, so the
    losing rows stay in the table — a full-table unique index on
    (sku_key, channel, market) could never be built on this data, not after one
    run and not after any number of them. This test proves both halves: the build
    succeeds WITH a suppressed duplicate still present, and the resulting index
    then rejects a second LIVE row on the same shelf."""
    import asyncpg
    from scripts.reconcile_catalog_offers import run

    await _product(db, PK_LIVE)
    await _sku(db, SKU_LIVE, PK_LIVE)
    await _offer(db, "o:a", sku_key=SKU_LIVE, product_key=PK_LIVE,
                 updated_at=_AT(2026, 1, 1))
    await _offer(db, "o:b", sku_key=SKU_LIVE, product_key=PK_LIVE,
                 updated_at=_AT(2026, 9, 1))

    report = await run(apply=True, limit=0, passes=("duplicates",),
                       create_index=True)

    assert report["duplicates"]["suppressed"] == 1
    assert report["unique_index"]["attempted"] is True
    # "created" is not the claim; ENFORCING is. A CONCURRENTLY build that lost a
    # race leaves an INVALID index that constrains nothing and reports no error.
    assert report["unique_index"]["enforcing"] is True

    definition = await db.fetch_val(
        "SELECT indexdef FROM pg_indexes WHERE indexname = :n", {"n": _index_name()}
    )
    assert "UNIQUE" in definition
    assert "suppressed_at IS NULL" in definition

    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await _offer(db, "o:c", sku_key=SKU_LIVE, product_key=PK_LIVE)


async def test_dry_run_never_builds_the_index(db):
    from scripts.reconcile_catalog_offers import run

    await _product(db, PK_LIVE)
    await _sku(db, SKU_LIVE, PK_LIVE)
    await _offer(db, "o:a", sku_key=SKU_LIVE, product_key=PK_LIVE)

    report = await run(apply=False, limit=0, passes=(), create_index=True)

    assert report["unique_index"] == {
        "attempted": False, "reason": "dry_run", "live_duplicate_groups": 0,
    }
    exists = await db.fetch_val(
        "SELECT count(*) FROM pg_indexes WHERE indexname = :n", {"n": _index_name()}
    )
    assert exists == 0


# ---------------------------------------------------------------------------
# the shared cascade helper, which the WRITERS call
# ---------------------------------------------------------------------------
async def test_cascade_helper_sets_both_columns_and_is_idempotent(db):
    from services.catalog_offer_suppression import (
        PRODUCT_SUPPRESSED_REASON, cascade_offer_suppression,
    )

    await _product(db, PK_DEAD, suppressed=True)
    await _offer(db, "o:x", sku_key=SKU_DEAD, product_key=PK_DEAD)

    moved = await cascade_offer_suppression([PK_DEAD])
    assert moved == ["o:x"]
    state = await _state(db, "o:x")
    assert state["suppressed_at"] is not None
    assert state["suppression_reason"] == PRODUCT_SUPPRESSED_REASON

    # Second call moves nothing — a writer that runs twice must not re-stamp.
    assert await cascade_offer_suppression([PK_DEAD]) == []


async def test_revert_helper_leaves_another_lanes_tombstone_alone(db):
    """A revert that resurrected an offer the merge lane or the duplicate pass
    gated would be un-reverting somebody else's decision."""
    from services.catalog_offer_suppression import (
        cascade_offer_suppression, revert_offer_suppression,
    )

    await _product(db, PK_DEAD, suppressed=True)
    await _offer(db, "o:ours", sku_key=SKU_DEAD, product_key=PK_DEAD)
    await _offer(db, "o:theirs", sku_key=SKU_DEAD, product_key=PK_DEAD,
                 channel="native")
    await db.execute(
        "UPDATE catalog_offers SET suppressed_at = NOW(), "
        "suppression_reason = 'duplicate_offer' WHERE offer_id = 'o:theirs'"
    )

    await cascade_offer_suppression([PK_DEAD])
    restored = await revert_offer_suppression([PK_DEAD])

    assert restored == ["o:ours"]
    assert (await _state(db, "o:theirs"))["suppressed_at"] is not None
    assert (await _state(db, "o:theirs"))["suppression_reason"] == "duplicate_offer"


async def test_cascade_helper_with_no_keys_touches_nothing(db):
    """`product_key = ANY('{}')` matches no row, but the helper short-circuits
    before the statement — proved here because the alternative (a stray
    unbounded UPDATE) would gate the entire table."""
    from services.catalog_offer_suppression import cascade_offer_suppression

    await _product(db, PK_LIVE)
    await _offer(db, "o:untouched", sku_key=SKU_LIVE, product_key=PK_LIVE)

    assert await cascade_offer_suppression([]) == []
    assert await cascade_offer_suppression(["", "  ", None]) == []
    assert (await _state(db, "o:untouched"))["suppressed_at"] is None
