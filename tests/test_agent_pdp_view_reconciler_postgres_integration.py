"""agent_pdp_view reconciler — REAL Postgres integration test (ADR-012 P1s1).

The unit tests drive fakes, which by construction cannot exercise the
candidate/drift SQL: the Postgres-only ``count(*) FILTER``/``bool_or``, the
``TIMESTAMPTZ AT TIME ZONE 'UTC' < TIMESTAMP`` staleness comparison, the
``ORDER BY … NULLS FIRST`` stalest-first ordering, the source-quarantine
anti-join, and — the review BLOCKER — the candidacy⇄buildability alignment
(a key the assembler would decline must NOT be a candidate, or it churns at
the head of the queue forever and starves the tail).

This test seeds a mini catalog covering every candidacy class and asserts:
  - exactly the stale + missing-public keys are candidates, in stalest-first
    order (missing NULLS FIRST, then oldest refreshed_at);
  - quarantined-source and title-less keys are excluded from BOTH the
    candidate set and the drift count (no permanent drift floor);
  - LIMIT truncates the ordered head only;
  - a pass over the candidates (refresh stamps refreshed_at=NOW(), the same
    contract as the real primitive's UPSERT) reaches a FIXED POINT: empty
    candidate set, drift total 0.

All seeded timestamps are explicit fixed values (naive columns hold UTC
wall-clock by writer convention; refreshed_at values are tz-aware UTC) so
the assertions are deterministic regardless of the server/session timezone.

Gated on PIVOTA_TEST_PG_URL (skipped when unset, like the A9-4 harness).
Point it at a throwaway DB:
    createdb apv_reconciler_ci
    PIVOTA_TEST_PG_URL=postgresql://localhost:5432/apv_reconciler_ci \\
      .venv/bin/python -m pytest tests/test_agent_pdp_view_reconciler_postgres_integration.py -q
"""

from __future__ import annotations

import os

import pytest

_PG_URL = os.getenv("PIVOTA_TEST_PG_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not _PG_URL,
    reason="Set PIVOTA_TEST_PG_URL to a Postgres URL to run the reconciler integration test.",
)

from datetime import datetime, timezone

# Naive TIMESTAMP values = UTC wall-clock (writer convention); refreshed_at
# values are tz-aware UTC.
T_TRUTH_OLD = datetime(2026, 1, 1, 9, 0, 0)
T_TRUTH_NEW = datetime(2026, 1, 1, 10, 0, 0)
VIEW_BEFORE_TRUTH = datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc)   # < 10:00 truth -> stale
VIEW_MID = datetime(2026, 1, 1, 9, 30, 0, tzinfo=timezone.utc)           # between 09:00 cp and 10:00 offer
VIEW_AFTER_TRUTH = datetime(2026, 1, 1, 11, 0, 0, tzinfo=timezone.utc)   # > all truth -> fresh

QUARANTINED_DOMAIN = "apvq.example.com"


async def _create_min_tables(database):
    """Tables without a SQLAlchemy Table object — created with the columns
    the reconciler SQL touches."""
    await database.execute(
        """
        CREATE TABLE IF NOT EXISTS catalog_row_trust (
            subject_type TEXT NOT NULL,
            subject_key TEXT NOT NULL,
            serving_decision TEXT,
            PRIMARY KEY (subject_type, subject_key)
        )
        """
    )
    await database.execute(
        """
        CREATE TABLE IF NOT EXISTS catalog_source_quarantine (
            quarantine_id SERIAL PRIMARY KEY,
            match_type TEXT NOT NULL,
            match_value TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'active',
            expires_at TIMESTAMPTZ
        )
        """
    )
    # The quarantine anti-join reads the FULL domain chain since #1643
    # (cp.source_domain -> external seed -> minted seed -> merchant store), so
    # these two must exist or every reconciler query raises UndefinedTable.
    # Without them this file — the ONLY harness that executes the reconciler SQL
    # against Postgres — fails on a fresh database while CI stays green, because
    # it is gated on PIVOTA_TEST_PG_URL. That is the green-but-unexercised shape
    # the fragment's own CURRENT_TIMESTAMP comment exists to prevent (#1588).
    await database.execute(
        """
        CREATE TABLE IF NOT EXISTS external_product_seeds (
            id TEXT PRIMARY KEY,
            external_product_id TEXT,
            domain TEXT,
            attached_product_key TEXT,
            status TEXT,
            updated_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ,
            seed_kind TEXT
        )
        """
    )
    await database.execute(
        """
        CREATE TABLE IF NOT EXISTS pdp_identity_listing (
            product_id TEXT,
            source_listing_ref TEXT
        )
        """
    )
    # merchant_id: the minted-seed identity leg is merchant-scoped (#1665), so
    # CATALOG_PRODUCT_DOMAIN_SQL — which this file executes via candidates_sql ->
    # _truth_cte — now emits `spl.merchant_id`. Separate ALTER rather than a
    # column in the CREATE above, per #1651: these gate files share ONE database,
    # so whichever file runs first wins the CREATE and a later CREATE ... IF NOT
    # EXISTS silently keeps the older, narrower shape. Additive and order-proof.
    await database.execute(
        """
        ALTER TABLE pdp_identity_listing
            ADD COLUMN IF NOT EXISTS merchant_id TEXT
        """
    )
    await database.execute(
        """
        CREATE TABLE IF NOT EXISTS merchant_stores (
            store_id TEXT PRIMARY KEY,
            merchant_id TEXT,
            platform TEXT,
            domain TEXT,
            status TEXT,
            is_primary BOOLEAN,
            last_sync TIMESTAMPTZ,
            created_at TIMESTAMPTZ
        )
        """
    )


async def _seed_product(
    database,
    *,
    pk: str,
    ck: str,
    title,
    updated_at,
    source_domain: str = "ok.example.com",
):
    await database.execute(
        """
        INSERT INTO catalog_products (
            product_key, merchant_id, platform, source_product_id,
            source_system, source_ref, source_domain, content_key, title,
            created_at, updated_at, content_changed_at
        ) VALUES (
            :pk, 'merch_apv_it', 'shopify', :pk, 'shopify', :pk, :dom, :ck,
            :title, :ts, :ts,
            :ts
        )
        """,
        {"pk": pk, "ck": ck, "dom": source_domain, "title": title, "ts": updated_at},
    )


async def _seed_view_row(database, *, ck: str, refreshed_at):
    await database.execute(
        """
        INSERT INTO agent_pdp_view (content_key, title, refreshed_at, refresh_source)
        VALUES (:ck, 'seeded', :ra, 'it_seed')
        """,
        {"ck": ck, "ra": refreshed_at},
    )


@pytest.mark.asyncio
async def test_reconciler_sql_selects_orders_and_converges_on_real_postgres():
    os.environ["DATABASE_URL"] = _PG_URL
    from sqlalchemy import create_engine
    import db.database as dbmod

    if "postgres" not in str(dbmod.DATABASE_URL):
        pytest.skip(
            "db.database is bound to a non-postgres URL (an earlier test imported "
            "it first). Run this file in isolation: PIVOTA_TEST_PG_URL=... "
            ".venv/bin/python -m pytest tests/test_agent_pdp_view_reconciler_postgres_integration.py"
        )
    database, metadata = dbmod.database, dbmod.metadata
    from db.catalog import agent_pdp_view, catalog_offers, catalog_products
    from jobs.agent_pdp_view_reconciler_cron import (
        candidates_sql,
        count_agent_pdp_view_drift,
        reconcile_agent_pdp_view,
    )

    sync = _PG_URL.replace("postgresql://", "postgresql+psycopg2://", 1)
    eng = create_engine(sync)
    metadata.create_all(eng, tables=[catalog_products, catalog_offers, agent_pdp_view])
    eng.dispose()

    await database.connect()
    try:
        # Prod's refreshed_at is TIMESTAMPTZ (migration 085); the SQLAlchemy
        # Table object says plain DateTime, so create_all would make it naive
        # and change the AT TIME ZONE semantics under test — align with prod.
        current_type = await database.fetch_val(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'agent_pdp_view' AND column_name = 'refreshed_at'"
        )
        if current_type != "timestamp with time zone":
            await database.execute(
                "ALTER TABLE agent_pdp_view ALTER COLUMN refreshed_at "
                "TYPE timestamptz USING refreshed_at AT TIME ZONE 'UTC'"
            )
        await _create_min_tables(database)
        # Clean slate for the rows this test owns.
        await database.execute("DELETE FROM catalog_products WHERE merchant_id = 'merch_apv_it'")
        await database.execute("DELETE FROM catalog_offers WHERE merchant_id = 'merch_apv_it'")
        await database.execute("DELETE FROM agent_pdp_view WHERE content_key LIKE 'ckapv_%'")
        await database.execute("DELETE FROM catalog_row_trust WHERE subject_key LIKE 'pkapv_%'")
        await database.execute(
            "DELETE FROM catalog_source_quarantine WHERE match_value = :d",
            {"d": QUARANTINED_DOMAIN},
        )

        # 1. STALE: cp truth 10:00 > view 09:00.
        await _seed_product(database, pk="pkapv_stale", ck="ckapv_stale", title="Stale", updated_at=T_TRUTH_NEW)
        await _seed_view_row(database, ck="ckapv_stale", refreshed_at=VIEW_BEFORE_TRUTH)
        # 2. FRESH: cp truth 10:00 < view 11:00 -> not a candidate.
        await _seed_product(database, pk="pkapv_fresh", ck="ckapv_fresh", title="Fresh", updated_at=T_TRUTH_NEW)
        await _seed_view_row(database, ck="ckapv_fresh", refreshed_at=VIEW_AFTER_TRUTH)
        # 3. MISSING + trust-public -> candidate (NULLS FIRST -> ordered first).
        await _seed_product(database, pk="pkapv_missing", ck="ckapv_missing", title="Missing", updated_at=T_TRUTH_NEW)
        await database.execute(
            "INSERT INTO catalog_row_trust (subject_type, subject_key, serving_decision) "
            "VALUES ('product', 'pkapv_missing', 'public')"
        )
        # 4. MISSING + blocked -> not a candidate.
        await _seed_product(database, pk="pkapv_blocked", ck="ckapv_blocked", title="Blocked", updated_at=T_TRUTH_NEW)
        await database.execute(
            "INSERT INTO catalog_row_trust (subject_type, subject_key, serving_decision) "
            "VALUES ('product', 'pkapv_blocked', 'blocked')"
        )
        # 5. QUARANTINED source, otherwise stale -> excluded (candidacy must
        #    match the assembler's anti-join or the key churns forever).
        await _seed_product(
            database, pk="pkapv_quar", ck="ckapv_quar", title="Quar",
            updated_at=T_TRUTH_NEW, source_domain=QUARANTINED_DOMAIN,
        )
        await _seed_view_row(database, ck="ckapv_quar", refreshed_at=VIEW_BEFORE_TRUTH)
        await database.execute(
            "INSERT INTO catalog_source_quarantine (match_type, match_value, state) "
            "VALUES ('domain', :d, 'active')",
            {"d": QUARANTINED_DOMAIN},
        )
        # 6. TITLE-LESS + trust-public, missing -> excluded (assembler would
        #    decline; unbuildable keys must not pin candidacy or drift).
        await _seed_product(database, pk="pkapv_notitle", ck="ckapv_notitle", title="  ", updated_at=T_TRUTH_NEW)
        await database.execute(
            "INSERT INTO catalog_row_trust (subject_type, subject_key, serving_decision) "
            "VALUES ('product', 'pkapv_notitle', 'public')"
        )
        # 7. OFFER-STALE: cp truth 09:00 < view 09:30, but an offer at 10:00
        #    drags truth past the view -> candidate (after ckapv_stale: 09:30 > 09:00).
        await _seed_product(database, pk="pkapv_offer", ck="ckapv_offer", title="Offer", updated_at=T_TRUTH_OLD)
        await _seed_view_row(database, ck="ckapv_offer", refreshed_at=VIEW_MID)
        await database.execute(
            """
            INSERT INTO catalog_offers (
                offer_id, sku_key, product_key, merchant_id,
                created_at, updated_at
            ) VALUES (
                'offapv_1', 'skuapv_1', 'pkapv_offer', 'merch_apv_it',
                :ts, :ts
            )
            """,
            {"ts": T_TRUTH_NEW},
        )

        def _keys(rows):
            return [r["content_key"] for r in rows]

        # --- Candidate selection + stalest-first order ---
        rows = await database.fetch_all(candidates_sql(), {"limit": 50})
        assert _keys(rows) == ["ckapv_missing", "ckapv_stale", "ckapv_offer"]

        # --- LIMIT truncates the ordered head only ---
        rows = await database.fetch_all(candidates_sql(), {"limit": 2})
        assert _keys(rows) == ["ckapv_missing", "ckapv_stale"]

        # --- Drift counts exactly the convergeable divergence ---
        drift = await count_agent_pdp_view_drift(database)
        assert drift == {"missing_public": 1, "stale": 2, "total": 3}

        # --- A pass converges to a fixed point ---
        # Minimal refresh with the primitive's UPSERT contract: upsert on
        # content_key, stamp refreshed_at=NOW(). (The full assembler needs a
        # dozen more tables; the SQL fixed-point is what this test pins.)
        async def refresh(content_key, *, refresh_source, db):
            await db.execute(
                """
                INSERT INTO agent_pdp_view (content_key, title, refreshed_at, refresh_source)
                VALUES (:ck, 'rebuilt', NOW(), :rs)
                ON CONFLICT (content_key) DO UPDATE SET
                    refreshed_at = NOW(), refresh_source = EXCLUDED.refresh_source
                """,
                {"ck": content_key, "rs": refresh_source},
            )
            return True

        counters = await reconcile_agent_pdp_view(db=database, limit=50, refresh=refresh)
        assert counters == {"candidates": 3, "refreshed": 3, "skipped_no_row": 0, "errors": 0}

        assert await database.fetch_all(candidates_sql(), {"limit": 50}) == []
        assert (await count_agent_pdp_view_drift(database))["total"] == 0

        # Idempotent: a second pass is an empty no-op.
        counters = await reconcile_agent_pdp_view(db=database, limit=50, refresh=refresh)
        assert counters == {"candidates": 0, "refreshed": 0, "skipped_no_row": 0, "errors": 0}
    finally:
        await database.disconnect()
