"""ADR-009 decision 1 — pg-singleton backfill, REAL Postgres integration test.

The unit tests drive fakes; this one runs the FULL backfill (`run_backfill`)
end-to-end against a real PostgreSQL server so the Postgres-specific SQL is
actually executed: the NOT EXISTS plan query, `= ANY(:pks)` sig snapshots,
`btrim()` blank-ck filtering, the ON CONFLICT DO NOTHING membership INSERT, the
batched transaction + parity reads, and the checkpoint upsert.

Seeds three shapes and asserts each is handled per the ADR-009 invariants:
  - a MULTI-MEMBER grouped product (two listings already in a curated group):
    SKIPPED — its membership rows are byte-identical after the run (a singleton
    NEVER overwrites a real group; no auto-merge, no re-key);
  - an UNGROUPED product with a content_key: gets its DETERMINISTIC singleton
    (pg byte-identical to what the autogrouper would mint), is_primary TRUE;
  - a NULL-content_key row: stays pg-NULL, counted to review_null_content_key —
    never force-minted from nothing.
Dry-run first (assert ZERO writes: memberships untouched, no checkpoint table),
then execute (assert memberships, sig columns byte-identical before/after,
checkpoint rows, per-batch parity ok), then a re-run (idempotent no-op).

Gated on PIVOTA_TEST_PG_URL (skipped when unset, like the T2/A9-4 harnesses).
Point it at a throwaway DB:
    createdb pg_singleton_ci
    PIVOTA_TEST_PG_URL=postgresql://localhost:5432/pg_singleton_ci \
      .venv/bin/python -m pytest tests/test_pg_singleton_backfill_postgres_integration.py -q
"""

from __future__ import annotations

import os

import pytest

_PG_URL = os.getenv("PIVOTA_TEST_PG_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not _PG_URL,
    reason="Set PIVOTA_TEST_PG_URL to a Postgres URL to run the pg-singleton integration test.",
)

CK_MULTI = "ck_99900000000000000000000000000001"
CK_LONE = "ck_99900000000000000000000000000002"
CURATED_GROUP = "pg_curated_multi_do_not_touch"


async def _create_min_tables(database):
    """product_group_members has no SQLAlchemy Table object — create it with the
    exact shape of db/migrations/045_product_groups.sql (the PK is what the
    ON CONFLICT (merchant_id, platform, platform_product_id) clause targets)."""
    await database.execute(
        """
        CREATE TABLE IF NOT EXISTS product_group_members (
          product_group_id TEXT NOT NULL,
          merchant_id TEXT NOT NULL,
          platform TEXT NOT NULL,
          platform_product_id TEXT NOT NULL,
          is_primary BOOLEAN NOT NULL DEFAULT FALSE,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          PRIMARY KEY (merchant_id, platform, platform_product_id)
        )
        """
    )


async def _seed(database):
    products = [
        # (product_key, merchant, spid, brand, content_key, sig)
        # multi-member grouped product: TWO listings of one physical product,
        # already members of a curated group.
        ("prod::merch_1::shopify::multi-1", "merch_1", "multi-1", "MultiBrand",
         CK_MULTI, "sig_multi000000000000000000000001"),
        ("prod::merch_2::shopify::multi-2", "merch_2", "multi-2", "MultiBrand",
         CK_MULTI, "sig_multi000000000000000000000002"),
        # ungrouped product with a content_key -> deterministic singleton.
        ("prod::merch_1::shopify::lone-1", "merch_1", "lone-1", "LoneBrand",
         CK_LONE, "sig_lone0000000000000000000000001"),
        # NULL content_key -> review, stays pg-NULL.
        ("prod::merch_1::shopify::null-1", "merch_1", "null-1", "NullBrand",
         None, "sig_null0000000000000000000000001"),
    ]
    for pk, mid, spid, brand, ck, sig in products:
        await database.execute(
            "INSERT INTO catalog_products (product_key, merchant_id, platform, "
            "source_product_id, title, brand, content_key, pivota_signature_id, "
            "pivota_canonical_url, sync_status) VALUES "
            "(:pk, :m, 'shopify', :spid, :title, :brand, :ck, :sig, :canon, 'live')",
            {"pk": pk, "m": mid, "spid": spid, "title": f"{brand} Product",
             "brand": brand, "ck": ck, "sig": sig,
             "canon": f"https://agent.pivota.cc/products/{sig}"},
        )
    # The curated multi-member group (pre-existing, must be untouched).
    for mid, spid, primary in (("merch_1", "multi-1", True), ("merch_2", "multi-2", False)):
        await database.execute(
            "INSERT INTO product_group_members (product_group_id, merchant_id, "
            "platform, platform_product_id, is_primary) VALUES "
            "(:pg, :m, 'shopify', :spid, :prim)",
            {"pg": CURATED_GROUP, "m": mid, "spid": spid, "prim": primary},
        )


async def _memberships(database):
    return {
        (dict(r)["merchant_id"], dict(r)["platform_product_id"]): (
            dict(r)["product_group_id"], dict(r)["is_primary"]
        )
        for r in await database.fetch_all(
            "SELECT merchant_id, platform_product_id, product_group_id, is_primary "
            "FROM product_group_members"
        )
    }


async def _sig_snapshot(database):
    return {
        dict(r)["product_key"]: (dict(r)["pivota_signature_id"], dict(r)["pivota_canonical_url"])
        for r in await database.fetch_all(
            "SELECT product_key, pivota_signature_id, pivota_canonical_url FROM catalog_products"
        )
    }


@pytest.mark.asyncio
async def test_pg_singleton_backfill_end_to_end_on_real_postgres():
    os.environ["DATABASE_URL"] = _PG_URL
    from sqlalchemy import create_engine
    import db.database as dbmod

    # `db.database` (and every module that did `from db.database import database`)
    # binds the singleton at IMPORT time from DATABASE_URL. Like the repo's
    # test_t2_postgres_integration / A9-4 harness, this test must be the one that
    # binds it — run it in ISOLATION with PIVOTA_TEST_PG_URL set. If an earlier
    # test in the same process already bound the default sqlite, skip LOUDLY
    # rather than run PG-specific SQL against sqlite.
    if "postgres" not in str(dbmod.DATABASE_URL):
        pytest.skip(
            "db.database is bound to a non-postgres URL (an earlier test imported it "
            "first). Run this file in isolation: PIVOTA_TEST_PG_URL=... .venv/bin/python "
            "-m pytest tests/test_pg_singleton_backfill_postgres_integration.py"
        )
    database, metadata = dbmod.database, dbmod.metadata
    from db.catalog import catalog_products
    from services import product_group_autogrouper as mint_mod
    from scripts.backfill_pg_singleton import run_backfill

    sync = _PG_URL.replace("postgresql://", "postgresql+psycopg2://", 1)
    eng = create_engine(sync)
    metadata.create_all(eng, tables=[catalog_products])
    eng.dispose()

    await database.connect()
    try:
        await _create_min_tables(database)
        # Clean slate.
        for t in ("product_group_members", "catalog_products"):
            await database.execute(f"DELETE FROM {t}")
        await database.execute("DROP TABLE IF EXISTS pg_singleton_backfill_checkpoint")

        await _seed(database)

        expected_lone_pg = mint_mod.make_singleton_product_group_id(CK_LONE)
        members_before = await _memberships(database)
        sigs_before = await _sig_snapshot(database)
        assert len(members_before) == 2  # the curated pair only

        # ---- DRY-RUN: strictly read-only ----
        dry = await run_backfill(database=database, mint_mod=mint_mod,
                                 execute=False, batch_size=100)
        assert dry.mode == "dry_run"
        assert dry.planned == 1  # only the lone ungrouped ck-carrying row
        assert dry.minted == 0
        assert dry.review_null_content_key == 1
        assert dry.skipped_already_grouped == 2
        assert dry.parity[0]["would_mint"] == 1
        assert dry.parity[0]["distinct_pgs"] == 1
        # ZERO writes: memberships untouched, checkpoint table never created.
        assert await _memberships(database) == members_before
        reg = await database.fetch_one(
            "SELECT to_regclass('pg_singleton_backfill_checkpoint') AS t"
        )
        assert dict(reg)["t"] is None, "dry-run must not create the checkpoint table"
        assert await _sig_snapshot(database) == sigs_before

        # ---- EXECUTE ----
        report = await run_backfill(database=database, mint_mod=mint_mod,
                                    execute=True, batch_size=100)
        assert report.mode == "execute"
        assert report.planned == 1
        assert report.minted == 1
        assert all(p["ok"] for p in report.parity)
        assert all(p["sigs_frozen"] for p in report.parity)
        assert all(p["pg_correct"] for p in report.parity)

        members_after = await _memberships(database)
        # the ungrouped row got its deterministic singleton, is_primary TRUE
        assert members_after[("merch_1", "lone-1")] == (expected_lone_pg, True)
        # the curated multi-member group is BYTE-IDENTICAL (skipped, untouched)
        assert members_after[("merch_1", "multi-1")] == (CURATED_GROUP, True)
        assert members_after[("merch_2", "multi-2")] == (CURATED_GROUP, False)
        # the NULL-ck row stayed pg-NULL
        assert ("merch_1", "null-1") not in members_after
        assert len(members_after) == 3

        # sig columns byte-identical before/after (catalog_products never written)
        assert await _sig_snapshot(database) == sigs_before

        # checkpoint row written for the minted product only
        ckpt = {
            dict(r)["product_key"]: dict(r)["product_group_id"]
            for r in await database.fetch_all(
                "SELECT product_key, product_group_id FROM pg_singleton_backfill_checkpoint"
            )
        }
        assert ckpt == {"prod::merch_1::shopify::lone-1": expected_lone_pg}

        # ---- RE-RUN is idempotent ----
        report2 = await run_backfill(database=database, mint_mod=mint_mod,
                                     execute=True, batch_size=100)
        assert report2.planned == 0
        assert report2.minted == 0
        assert report2.counts["catalog_missing_pg"] == 0
        assert report2.review_null_content_key == 1  # honest review count persists
        assert await _memberships(database) == members_after
        assert await _sig_snapshot(database) == sigs_before

    finally:
        await database.disconnect()
