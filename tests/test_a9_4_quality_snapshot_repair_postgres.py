"""The A9-4 quality-snapshot repair may only touch rows the flip itself moved.

WHAT THIS PREVENTS. The A9-4 re-key (2026-08-14) moved 11,099 catalog rows onto
their observed seller but left ``product_quality_snapshot`` behind —
``discover_cascade_tables`` reflects only tables scoped by
product_key/content_key/sku_key, and this one scopes by
``(platform, platform_product_id)``. The classifier looks the score up under the
CURRENT owner, so 6,424 products read ``content_quality_score IS NULL`` and the
sitemap-eligible set halved (8,222 -> 3,884 content_keys).

The repair re-points those snapshots. Its entire safety argument is the cohort
SELECT: a merchant column is being rewritten, so a predicate that is one
conjunct too loose silently merges two merchants' quality history, and the
resulting scores would look perfectly plausible forever after.

POSTGRES GATE because that argument IS the SQL — correlated NOT EXISTS/EXISTS
against a three-way join. A mock replay would assert the counter moved without
proving which rows the statement chose, which is the exact failure class this
directory exists for.

Every conjunct below is driven BOTH ways: one row that must be selected, and one
row that differs from it in a single field and must not be. The third-party-donor
case is the load-bearing one — it is the shape that would corrupt data rather
than merely miss a repair.

🚨 THESE GATE FILES SHARE ONE DATABASE. Additive, order-proof DDL only (#1651).
"""

from __future__ import annotations

import os

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)

# `catalog_products` is OWNED by db.catalog and is built with metadata.create_all
# below — never hand-rolled here. `create_all(checkfirst=True)` SKIPS a table that
# already exists, so a minimal `CREATE TABLE catalog_products (product_key text)`
# in this file would win whenever it ran first and permanently deprive every
# sibling gate of the other 40 columns. Measured: doing exactly that failed 11
# tests in the prepare gate.
#
# Only tables that no MetaData owns get hand-rolled, and only additively:
#   a9_4_backfill_checkpoint — created at runtime by ensure_checkpoint_table
#   product_quality_snapshot — not on the MetaData; the sibling prepare gate
#                              declares this exact shape, so it is repeated
#                              verbatim rather than re-guessed.
_LIGHTWEIGHT_DDL = """
CREATE TABLE IF NOT EXISTS a9_4_backfill_checkpoint (
  phase text NOT NULL, ref_id text NOT NULL, observed_id text,
  status text NOT NULL DEFAULT 'done',
  updated_at timestamptz NOT NULL DEFAULT NOW(),
  PRIMARY KEY (phase, ref_id)
);
-- Additive: a sibling gate deliberately creates this table WITHOUT
-- previous_value so ensure_checkpoint_table's own ALTER stays under test. This
-- ADD COLUMN IF NOT EXISTS is order-proof either way.
ALTER TABLE a9_4_backfill_checkpoint ADD COLUMN IF NOT EXISTS previous_value text;

CREATE TABLE IF NOT EXISTS product_quality_snapshot (id bigserial PRIMARY KEY);
ALTER TABLE product_quality_snapshot ADD COLUMN IF NOT EXISTS merchant_id VARCHAR(100);
ALTER TABLE product_quality_snapshot ADD COLUMN IF NOT EXISTS platform VARCHAR(50);
ALTER TABLE product_quality_snapshot ADD COLUMN IF NOT EXISTS platform_product_id VARCHAR(200);
ALTER TABLE product_quality_snapshot ADD COLUMN IF NOT EXISTS content_quality_score DOUBLE PRECISION;
"""

PREFIX = "qsr_"          # every row this module writes is namespaced and deleted
NEW = PREFIX + "merch_new"
OLD = PREFIX + "merch_old"
THIRD = PREFIX + "merch_third"


def _pk(name: str) -> str:
    return f"{PREFIX}{name}"


async def _seed_product(db, name: str, *, merchant: str, spid: str | None = None) -> None:
    # `title` is NOT NULL on the real table with no server default.
    await db.execute(
        "INSERT INTO catalog_products (product_key, content_key, merchant_id, platform,"
        " source_product_id, title) VALUES (:pk, :ck, :m, 'shopify', :spid, :title)",
        {"pk": _pk(name), "ck": _pk(f"ck_{name}"), "m": merchant,
         "spid": spid or _pk(f"sp_{name}"), "title": f"repair gate {name}"},
    )


async def _seed_checkpoint(
    db, name: str, *, observed: str, previous: str | None, status: str = "done",
    phase: str = "catalog",
) -> None:
    await db.execute(
        "INSERT INTO a9_4_backfill_checkpoint (phase, ref_id, observed_id, previous_value,"
        " status) VALUES (:ph, :ref, :obs, :prev, :st)",
        {"ph": phase, "ref": _pk(name), "obs": observed, "prev": previous, "st": status},
    )


async def _seed_snapshot(db, name: str, *, merchant: str, spid: str | None = None) -> None:
    # No explicit id: the column is bigserial and siblings insert without one.
    await db.execute(
        "INSERT INTO product_quality_snapshot (merchant_id, platform, platform_product_id,"
        " content_quality_score) VALUES (:m, 'shopify', :spid, 88.0)",
        {"m": merchant, "spid": spid or _pk(f"sp_{name}")},
    )


@pytest.fixture()
async def db():
    import db.catalog  # noqa: F401  (registers catalog_products on the shared MetaData)
    from sqlalchemy import create_engine

    from db.database import database as _db, metadata

    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine, checkfirst=True)
    engine.dispose()

    was_connected = getattr(_db, "is_connected", False)
    if not was_connected:
        await _db.connect()
    for stmt in filter(None, (s.strip() for s in _LIGHTWEIGHT_DDL.split(";"))):
        await _db.execute(stmt)
    await _cleanup(_db)
    try:
        yield _db
    finally:
        await _cleanup(_db)
        if not was_connected:
            await _db.disconnect()


async def _cleanup(db) -> None:
    like = PREFIX + "%"
    # By merchant_id, not id: id is bigserial here, and every merchant this
    # module writes is namespaced.
    await db.execute("DELETE FROM product_quality_snapshot WHERE merchant_id LIKE :p", {"p": like})
    await db.execute("DELETE FROM a9_4_backfill_checkpoint WHERE ref_id LIKE :p", {"p": like})
    await db.execute("DELETE FROM catalog_products WHERE product_key LIKE :p", {"p": like})


async def _cohort_keys(db) -> set[str]:
    from scripts.repair_a9_4_orphaned_quality_snapshots import COHORT_SQL

    rows = await db.fetch_all(COHORT_SQL)
    return {dict(r)["product_key"] for r in (rows or []) if str(dict(r)["product_key"]).startswith(PREFIX)}


@pytest.mark.asyncio
async def test_cohort_selects_only_the_flips_own_orphans(db) -> None:
    """One selected row, and six near-misses that differ by a single field."""
    # SELECTED — all three provenance conjuncts hold, destination empty.
    await _seed_product(db, "happy", merchant=NEW)
    await _seed_checkpoint(db, "happy", observed=NEW, previous=OLD)
    await _seed_snapshot(db, "happy", merchant=OLD)

    # NOT selected — destination already has a snapshot (idempotency: a repaired
    # product must leave the cohort, or a re-run would move a second merchant in).
    await _seed_product(db, "already", merchant=NEW)
    await _seed_checkpoint(db, "already", observed=NEW, previous=OLD)
    await _seed_snapshot(db, "already", merchant=OLD)
    await _seed_snapshot(db, "already", merchant=NEW)

    # NOT selected — no donor at all: genuinely never scored, not orphaned.
    await _seed_product(db, "nodonor", merchant=NEW)
    await _seed_checkpoint(db, "nodonor", observed=NEW, previous=OLD)

    # NOT selected — THE DATA-CORRUPTING SHAPE. A snapshot exists under a
    # merchant that is NOT this checkpoint's previous_value, so it is somebody
    # else's quality history and must never be adopted.
    await _seed_product(db, "thirdparty", merchant=NEW)
    await _seed_checkpoint(db, "thirdparty", observed=NEW, previous=OLD)
    await _seed_snapshot(db, "thirdparty", merchant=THIRD)

    # NOT selected — the product has since moved off the flip's target, so the
    # flip's result is no longer standing and this is not ours to repair.
    await _seed_product(db, "moved_on", merchant=THIRD)
    await _seed_checkpoint(db, "moved_on", observed=NEW, previous=OLD)
    await _seed_snapshot(db, "moved_on", merchant=OLD)

    # NOT selected — checkpoint never completed.
    await _seed_product(db, "pending", merchant=NEW)
    await _seed_checkpoint(db, "pending", observed=NEW, previous=OLD, status="in_progress")
    await _seed_snapshot(db, "pending", merchant=OLD)

    # NOT selected — a no-op checkpoint row (donor == target).
    await _seed_product(db, "noop", merchant=NEW)
    await _seed_checkpoint(db, "noop", observed=NEW, previous=NEW)
    await _seed_snapshot(db, "noop", merchant=NEW)

    assert await _cohort_keys(db) == {_pk("happy")}


@pytest.mark.asyncio
async def test_repair_is_idempotent_and_moves_only_the_donors_rows(db) -> None:
    """Applying the re-point empties the cohort and leaves third parties alone."""
    from scripts.repair_a9_4_orphaned_quality_snapshots import REPOINT_SQL

    await _seed_product(db, "happy", merchant=NEW)
    await _seed_checkpoint(db, "happy", observed=NEW, previous=OLD)
    await _seed_snapshot(db, "happy", merchant=OLD)
    # A same-(platform, spid) snapshot under an unrelated merchant must survive
    # untouched: the UPDATE is pinned to the donor, not to the product id.
    await _seed_snapshot(db, "happy", merchant=THIRD)

    assert await _cohort_keys(db) == {_pk("happy")}

    await db.execute(
        REPOINT_SQL,
        {
            "new_merchant": NEW,
            "old_merchant": OLD,
            "platform": "shopify",
            "spid": _pk("sp_happy"),
        },
    )

    # The donor's row moved...
    moved = await db.fetch_val(
        "SELECT count(*) FROM product_quality_snapshot WHERE platform_product_id = :spid"
        "   AND merchant_id = :m",
        {"spid": _pk("sp_happy"), "m": NEW},
    )
    assert int(moved) == 1
    # ...the third party's did not.
    untouched = await db.fetch_val(
        "SELECT count(*) FROM product_quality_snapshot WHERE platform_product_id = :spid"
        "   AND merchant_id = :m",
        {"spid": _pk("sp_happy"), "m": THIRD},
    )
    assert int(untouched) == 1
    # ...and the product has left the cohort, so a re-run is a no-op.
    assert await _cohort_keys(db) == set()
