"""The A9-4 quality-snapshot repair may only touch rows the flip itself moved.

WHAT THIS PREVENTS. The A9-4 re-key (2026-08-14) moved 11,099 catalog rows onto
their observed seller but left ``product_quality_snapshot`` behind:
``discover_cascade_tables`` reflects only tables scoped by
product_key/content_key/sku_key, and that one scopes by
``(platform, platform_product_id)``. The classifier looks the score up under the
CURRENT owner, so 6,424 products read ``content_quality_score IS NULL`` and the
sitemap-eligible set halved (8,222 -> 3,884 content_keys).

The repair re-points those snapshots. Its whole safety argument is the cohort
SELECT: a merchant column is being rewritten, so a predicate one conjunct too
loose silently merges two merchants' quality history, and the resulting scores
would look plausible forever after.

Every conjunct is driven BOTH ways here — one row that must be selected, and one
differing in a single field that must not be. The third-party-donor case is the
load-bearing one: it is the shape that would corrupt data rather than merely
miss a repair.

WHY THIS IS NOT IN ``tests/test_*_postgres.py``. It was, and it should not have
been. Those gate files share ONE database and each declares its own lightweight
DDL, so a new file that provisions tables changes the shape every later sibling
sees — this file's first version failed CI with ``column "id" does not exist``
inside `test_priced_offer_gate_postgres`, having built the catalog tables in
their MetaData shape ahead of the lightweight DDL that declares that column. The
statement under test needs no Postgres-only syntax (CTE + correlated
EXISTS/NOT EXISTS are portable), so the row-selection semantics belong here,
where the fixture owns its own tables. Its PLANNABILITY on the production
dialect is covered separately and unconditionally by
``tests/test_ops_script_sql_prepare_postgres.py``, which PREPAREs COHORT_SQL,
REPOINT_SQL and DONOR_ROWS_SQL against a real Postgres.
"""

from __future__ import annotations

from typing import Optional, Set

import pytest

from db.database import database
from scripts.backfill_seller_of_record import BANNED_BUCKET_MERCHANT_ID
from scripts.repair_a9_4_orphaned_quality_snapshots import (
    COHORT_SQL,
    DONOR_ROWS_SQL,
    REPOINT_SQL,
)

NEW = "merch_new"
# The legacy bucket every moved row came from. Seeded as a CONSTANT on purpose:
# the catalog phase writes `previous_value=BANNED_BUCKET_MERCHANT_ID` for every
# row, so prod holds exactly one distinct value. An earlier version of these
# tests gave each row its own donor merchant — a shape production never
# produces — and so "proved" a per-row provenance the predicate did not have.
OLD = "external_seed"
THIRD = "merch_third"

# catalog_products is built from the MODEL via tests/model_schema.ensure_model_tables, not
# hand-written here: a hand-written subset wins the create race when this module collects
# first and poisons every neighbour that SELECTs a real column (source_domain, canonical_url,
# title NOT NULL ...). See tests/model_schema.py for both failure directions.
_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS a9_4_backfill_checkpoint (
        phase TEXT,
        ref_id TEXT,
        observed_id TEXT,
        previous_value TEXT,
        status TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS product_quality_snapshot (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        merchant_id TEXT,
        platform TEXT,
        platform_product_id TEXT,
        content_quality_score REAL
    )
    """,
)


async def _connect_if_needed() -> bool:
    if database.is_connected:
        return True
    await database.connect()
    return False


async def _cleanup() -> None:
    for table in ("product_quality_snapshot", "a9_4_backfill_checkpoint", "catalog_products"):
        await database.execute(f"DELETE FROM {table}")


@pytest.fixture()
async def db():
    was_connected = await _connect_if_needed()
    from db.catalog import catalog_products as cp_table
    from tests.model_schema import ensure_model_tables

    await ensure_model_tables([cp_table])
    for stmt in _SCHEMA:
        await database.execute(stmt)
    await _cleanup()
    try:
        yield database
    finally:
        await _cleanup()
        if not was_connected and database.is_connected:
            await database.disconnect()


async def _product(name: str, *, merchant: str, spid: Optional[str] = None) -> None:
    await database.execute(
        "INSERT INTO catalog_products (product_key, content_key, merchant_id, platform,"
        " source_product_id, title) VALUES (:pk, :ck, :m, 'shopify', :spid, :t)",
        {"pk": name, "ck": f"ck_{name}", "m": merchant, "spid": spid or f"sp_{name}", "t": name},
    )


async def _checkpoint(
    name: str,
    *,
    observed: str,
    previous: Optional[str],
    status: str = "done",
    phase: str = "catalog",
) -> None:
    await database.execute(
        "INSERT INTO a9_4_backfill_checkpoint (phase, ref_id, observed_id, previous_value,"
        " status) VALUES (:ph, :ref, :obs, :prev, :st)",
        {"ph": phase, "ref": name, "obs": observed, "prev": previous, "st": status},
    )


async def _snapshot(
    name: str, *, merchant: str, spid: Optional[str] = None, score: float = 88.0
) -> None:
    await database.execute(
        "INSERT INTO product_quality_snapshot (merchant_id, platform, platform_product_id,"
        " content_quality_score) VALUES (:m, 'shopify', :spid, :score)",
        {"m": merchant, "spid": spid or f"sp_{name}", "score": score},
    )


async def _cohort() -> Set[str]:
    # OLD is the real BANNED_BUCKET_MERCHANT_ID, so this binds the same value
    # the script does rather than a test-only stand-in.
    assert OLD == BANNED_BUCKET_MERCHANT_ID
    rows = await database.fetch_all(COHORT_SQL, {"banned": BANNED_BUCKET_MERCHANT_ID})
    return {dict(r)["product_key"] for r in (rows or [])}


@pytest.mark.asyncio
async def test_cohort_selects_only_the_flips_own_orphans(db) -> None:
    """One selected row, and six near-misses each differing by a single field."""
    # SELECTED — all three provenance conjuncts hold, destination empty.
    await _product("happy", merchant=NEW)
    await _checkpoint("happy", observed=NEW, previous=OLD)
    await _snapshot("happy", merchant=OLD)

    # NOT selected — destination already populated. This is what makes a re-run
    # a no-op instead of dragging a second merchant's rows in.
    await _product("already", merchant=NEW)
    await _checkpoint("already", observed=NEW, previous=OLD)
    await _snapshot("already", merchant=OLD)
    await _snapshot("already", merchant=NEW)

    # NOT selected — no donor: genuinely never scored, not orphaned.
    await _product("nodonor", merchant=NEW)
    await _checkpoint("nodonor", observed=NEW, previous=OLD)

    # NOT selected — donor is some merchant other than the legacy bucket. Every
    # row the flip moved came OUT of that bucket (`run_catalog` scans
    # `WHERE cp.merchant_id = :banned`), so a donor anywhere else did not score
    # this product and must not be adopted, however unique the live key looks.
    await _product("otherdonor", merchant=NEW)
    await _checkpoint("otherdonor", observed=NEW, previous=OLD)
    await _snapshot("otherdonor", merchant=THIRD)

    # NOT selected — the product has since moved off the flip's target, so the
    # flip's result is no longer standing and this is not ours to repair.
    await _product("moved_on", merchant=THIRD)
    await _checkpoint("moved_on", observed=NEW, previous=OLD)
    await _snapshot("moved_on", merchant=OLD)

    # NOT selected — the checkpoint never completed.
    await _product("pending", merchant=NEW)
    await _checkpoint("pending", observed=NEW, previous=OLD, status="in_progress")
    await _snapshot("pending", merchant=OLD)

    # NOT selected — a no-op checkpoint row (donor == target).
    await _product("noop", merchant=NEW)
    await _checkpoint("noop", observed=NEW, previous=NEW)
    await _snapshot("noop", merchant=NEW)

    assert await _cohort() == {"happy"}


@pytest.mark.asyncio
async def test_a_deleted_siblings_orphan_score_is_never_inherited(db) -> None:
    """`rows_on_key` counts the LIVE catalog; snapshots are HISTORY.

    Nothing in production deletes `product_quality_snapshot` — the product
    delete path cleans `product_enrichment` and leaves this table alone — so a
    sibling that has since been removed leaves its score behind under a key that
    NOW looks unique. Counting live rows cannot see that, and the survivor would
    inherit a score computed for a different merchant's product: silent,
    permanent, and indistinguishable from a real measurement afterwards.

    Only the donor-is-the-bucket conjunct rejects it. `merch_third` here stands
    for the deleted sibling's merchant, whose snapshot outlived its row.
    """
    shared = "sp_orphaned"

    await _product("survivor", merchant=NEW, spid=shared)
    await _checkpoint("survivor", observed=NEW, previous=OLD)
    # The dead sibling's row is gone; only its snapshot remains.
    await _snapshot("ghost", merchant=THIRD, spid=shared)

    assert await _cohort() == set()


@pytest.mark.asyncio
async def test_a_ghost_left_inside_the_bucket_is_the_accepted_residual(db) -> None:
    """THE LIMIT OF THE GUARANTEE, pinned so nobody mistakes it for closed.

    The donor-is-the-bucket conjunct rejects a ghost left under any OTHER
    merchant. It cannot reject one left under the bucket itself: the unique
    index makes `(external_seed, platform, source_product_id)` unique at any
    INSTANT, not across time, so a bucket row that vanished and left its
    snapshot behind can be succeeded on the same key by a row that is then
    moved — and that successor IS selected.

    This test asserts the residual rather than hiding it. Two earlier versions
    of this predicate shipped because a test picked the variant that passed
    and the comment claimed the class was closed; the honest statement is that
    the class is NARROWED. It is accepted because `product_key` is derived from
    (merchant, platform, source id) and the flip preserves it, so ghost and
    successor share a product_key: the inherited score describes the same
    source page, making the worst case a STALE score rather than a different
    merchant's. If that ever stops holding, this test is where to start.
    """
    shared = "sp_reborn"

    await _product("reborn", merchant=NEW, spid=shared)
    await _checkpoint("reborn", observed=NEW, previous=OLD)
    # The ghost: scored while the vanished row sat in the bucket on this key.
    await _snapshot("ghost", merchant=OLD, spid=shared, score=12.0)

    assert await _cohort() == {"reborn"}


@pytest.mark.asyncio
async def test_a_live_bucket_row_sharing_the_natural_key_is_never_harvested(db) -> None:
    """THE DEFECT AN EARLIER DRAFT SHIPPED, pinned as a test.

    `previous_value` is the constant 'external_seed', so "the donor is this
    row's recorded previous_value" says only "the donor is in the legacy
    bucket". Rows legitimately REMAIN in that bucket after the flip. Here
    `victim` is one: a different product that still owns the bucket and holds
    the only snapshot for a `(platform, source_product_id)` it shares with the
    moved `thief`.

    Under the old predicate `thief` was selected and the UPDATE moved `victim`'s
    score onto it — `victim` silently lost its score and stopped serving, while
    `thief` served on a number computed for someone else's product. Neither may
    be touched: the natural key does not identify one product.
    """
    shared = "sp_shared"

    await _product("thief", merchant=NEW, spid=shared)
    await _checkpoint("thief", observed=NEW, previous=OLD)

    await _product("victim", merchant=OLD, spid=shared)
    await _snapshot("victim", merchant=OLD, spid=shared, score=41.5)

    assert await _cohort() == set()


@pytest.mark.asyncio
async def test_two_moved_products_sharing_a_natural_key_are_both_skipped(db) -> None:
    """Neither may silently win the one snapshot.

    Both were moved by the flip and both now address the same
    `(platform, source_product_id)`. Exactly one donor row exists, so under a
    key-blind predicate whichever product sorted first would take it and the
    other would stay unscored — with the report claiming two products repaired.
    A9-4's per-row seller resolution makes this shape reachable.
    """
    shared = "sp_shared"

    await _product("twinA", merchant=NEW, spid=shared)
    await _checkpoint("twinA", observed=NEW, previous=OLD)
    await _product("twinB", merchant=THIRD, spid=shared)
    await _checkpoint("twinB", observed=THIRD, previous=OLD)
    await _snapshot("twin", merchant=OLD, spid=shared)

    assert await _cohort() == set()


@pytest.mark.asyncio
async def test_cohort_requires_a_single_donor_merchant(db) -> None:
    """Two merchants holding snapshots for the key means "the donor" is a guess."""
    await _product("ambiguous", merchant=NEW)
    await _checkpoint("ambiguous", observed=NEW, previous=OLD)
    await _snapshot("ambiguous", merchant=OLD)
    await _snapshot("ambiguous", merchant=THIRD)

    assert await _cohort() == set()


@pytest.mark.asyncio
async def test_cohort_ignores_a_checkpoint_from_another_phase(db) -> None:
    """`phase` is part of the provenance, not decoration: a seeds-phase row is
    not evidence that the CATALOG re-key moved this product."""
    await _product("otherphase", merchant=NEW)
    await _checkpoint("otherphase", observed=NEW, previous=OLD, phase="seeds")
    await _snapshot("otherphase", merchant=OLD)

    assert await _cohort() == set()


@pytest.mark.asyncio
async def test_repair_moves_the_donors_rows_and_leaves_third_parties_alone(db) -> None:
    await _product("happy", merchant=NEW)
    await _checkpoint("happy", observed=NEW, previous=OLD)
    await _snapshot("happy", merchant=OLD)

    assert await _cohort() == {"happy"}

    # Only NOW add a same-key row under an unrelated merchant. Seeding it up
    # front would make the key two-donor and (correctly) drop the product from
    # the cohort, so the UPDATE's own pinning would never be exercised. The
    # property under test is that REPOINT_SQL is bound to the DONOR merchant,
    # not to the product id: it must move one row and leave the other alone.
    await _snapshot("happy", merchant=THIRD)

    binds = {
        "new_merchant": NEW,
        "old_merchant": OLD,
        "platform": "shopify",
        "spid": "sp_happy",
    }
    moving = await database.fetch_val(
        DONOR_ROWS_SQL, {k: binds[k] for k in ("platform", "spid", "old_merchant")}
    )
    assert int(moving) == 1  # the count the report prints must be the real one

    await database.execute(REPOINT_SQL, binds)

    async def _count(merchant: str) -> int:
        return int(
            await database.fetch_val(
                "SELECT count(*) FROM product_quality_snapshot"
                " WHERE platform_product_id = :spid AND merchant_id = :m",
                {"spid": "sp_happy", "m": merchant},
            )
        )

    assert await _count(NEW) == 1     # the donor's row moved
    assert await _count(THIRD) == 1   # the third party's did not
    assert await _count(OLD) == 0

    # ...and the product has left the cohort (its destination is now populated),
    # so re-running is a no-op.
    assert await _cohort() == set()
