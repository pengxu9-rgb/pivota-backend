"""Tier-2 loop closure + outcome aggregation — REAL Postgres integration test.

The T2-2 (`close_external_order_conversion`) and T2-3 (`refresh_all_outcomes`)
unit tests exercise a SQLite emulator, so the Postgres-specific SQL they emit
(FILTER, ANY(:param), ->>'click_matched', ::boolean IS TRUE, UNION ALL, the
external-conversion LEFT JOIN, and the ON CONFLICT unique-index replay guard) is
never actually executed by those tests. This test drives the REAL production
functions against a real PostgreSQL server so that behavior is verified.

Gated on PIVOTA_TEST_PG_URL (skipped when unset, like the other DB harnesses in
this repo). Point it at a throwaway DB, e.g.:

    createdb t2_ci
    PIVOTA_TEST_PG_URL=postgresql://localhost:5432/t2_ci \
      .venv/bin/python -m pytest tests/test_t2_postgres_integration.py -q
"""
import os
from datetime import datetime, timezone

import pytest

_PG_URL = os.getenv("PIVOTA_TEST_PG_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not _PG_URL, reason="Set PIVOTA_TEST_PG_URL to a Postgres URL to run the T2 integration test."
)

NOW = datetime.now(timezone.utc)
M_INT, M_EXT = "M_INT", "M_EXT"
P_INT, P_EXT = "prodcanon_INT", "prodcanon_EXT"


@pytest.mark.asyncio
async def test_external_conversion_closes_and_aggregates_on_real_postgres():
    # DATABASE_URL must be set before importing db.database (read at import time).
    os.environ["DATABASE_URL"] = _PG_URL
    from sqlalchemy import create_engine
    from db.database import database, metadata
    from db.commerce_attribution import commerce_attribution_edges, surface_click_events
    from db.orders import orders as orders_table
    from services.commerce_attribution_service import close_external_order_conversion
    from services.outcome_aggregation_service import (
        refresh_all_outcomes,
        ensure_aggregated_outcomes_table,
    )

    sync = _PG_URL.replace("postgresql://", "postgresql+psycopg2://", 1)
    eng = create_engine(sync)
    metadata.create_all(eng, tables=[surface_click_events, commerce_attribution_edges, orders_table])
    eng.dispose()

    await database.connect()
    try:
        await ensure_aggregated_outcomes_table()
        for t in ("aggregated_outcomes", "commerce_attribution_edges", "surface_click_events", "orders"):
            await database.execute(f"DELETE FROM {t}")

        # Internal converted (order + edge, state NULL).
        await database.execute(orders_table.insert().values(
            order_id="ord_int1", merchant_id=M_INT, payment_status="paid",
            customer_email="b@example.com", shipping_address={}, items=[],
            subtotal=50.00, total=50.00, currency="USD", created_at=NOW,
        ))
        await database.execute(commerce_attribution_edges.insert().values(
            edge_id="edge_int1", merchant_id=M_INT, order_id="ord_int1", surface="agent",
            canonical_product_id=P_INT, gross_attributed_gmv_cents=5000,
            refund_count=0, refunded_amount=0, created_at=NOW, updated_at=NOW,
        ))
        # External matched — real T2-2 closure binds to the click's product.
        await database.execute(surface_click_events.insert().values(
            click_id="clk_match", merchant_id=M_EXT, surface="agent",
            canonical_product_id=P_EXT, click_count=1, impression_count=0,
            created_at=NOW, updated_at=NOW,
        ))
        r_match = await close_external_order_conversion(
            merchant_id=M_EXT, click_id="clk_match", external_order_id="shop_ext1",
            gross_amount_cents=8000, currency="USD", converted_at=NOW)
        # External unmatched — forgeable (no click row), huge GMV.
        r_unmatch = await close_external_order_conversion(
            merchant_id=M_EXT, click_id="clk_UNKNOWN", external_order_id="shop_ext2",
            gross_amount_cents=99999, currency="USD", converted_at=NOW)
        # Replay of the matched order — must hit the real UNIQUE index.
        r_replay = await close_external_order_conversion(
            merchant_id=M_EXT, click_id="clk_match", external_order_id="shop_ext1",
            gross_amount_cents=8000, currency="USD", converted_at=NOW)
        # Referred (not converted) — must never count.
        await database.execute(commerce_attribution_edges.insert().values(
            edge_id="edge_ref1", merchant_id=M_EXT, order_id="synthetic_ref1", surface="agent",
            canonical_product_id=P_EXT, state="referred", gross_attributed_gmv_cents=123456,
            refund_count=0, refunded_amount=0, created_at=NOW, updated_at=NOW,
        ))

        await refresh_all_outcomes()
        prod = {r["subject_key"]: dict(r) for r in await database.fetch_all(
            "SELECT * FROM aggregated_outcomes WHERE subject_type='product'")}
        merch = {r["subject_key"]: dict(r) for r in await database.fetch_all(
            "SELECT * FROM aggregated_outcomes WHERE subject_type='merchant'")}
        conv = await database.fetch_one(
            "SELECT count(*) c FROM commerce_attribution_edges WHERE state='converted'")

        # T2-2 closure semantics.
        assert r_match and r_match["click_matched"] is True
        assert r_unmatch and r_unmatch["click_matched"] is False
        assert r_replay and r_replay.get("replayed") is True
        assert conv["c"] == 2, "replay must not create a duplicate converted edge"

        # T2-3 aggregation on real Postgres.
        assert prod[P_INT]["transacted_count"] == 1 and prod[P_INT]["gmv_cents"] == 5000
        assert prod[P_EXT]["transacted_count"] == 1 and prod[P_EXT]["gmv_cents"] == 8000
        assert merch[M_INT]["transacted_count"] == 1 and merch[M_INT]["gmv_cents"] == 5000
        # Integrity: the forgeable unmatched (99999) is excluded from the signal.
        assert merch[M_EXT]["gmv_cents"] == 8000, "click_matched=false conversion must not inflate outcomes"
    finally:
        await database.disconnect()


@pytest.mark.asyncio
async def test_seller_mismatch_edge_excluded_on_real_postgres():
    """ADR-009 §D3: a converted+click_matched edge whose converting store did NOT
    correspond to the click's destination seller is stamped seller_mismatch=true by
    the real closure and EXCLUDED by the real Postgres aggregation predicate
    (`->>'seller_mismatch' IS NOT TRUE`, NULL-safe)."""
    os.environ["DATABASE_URL"] = _PG_URL
    from sqlalchemy import create_engine
    from db.database import database, metadata
    from db.commerce_attribution import commerce_attribution_edges, surface_click_events
    from db.orders import orders as orders_table
    from services.commerce_attribution_service import close_external_order_conversion
    from services.outcome_aggregation_service import (
        refresh_all_outcomes,
        ensure_aggregated_outcomes_table,
    )

    sync = _PG_URL.replace("postgresql://", "postgresql+psycopg2://", 1)
    eng = create_engine(sync)
    metadata.create_all(eng, tables=[surface_click_events, commerce_attribution_edges, orders_table])
    eng.dispose()

    M_SELLER, P_SELLER = "M_SELLER", "prodcanon_SELLER"
    await database.connect()
    try:
        await ensure_aggregated_outcomes_table()
        for t in ("aggregated_outcomes", "commerce_attribution_edges", "surface_click_events", "orders"):
            await database.execute(f"DELETE FROM {t}")

        # A click whose redirect DESTINATION is brand-a's own store.
        await database.execute(surface_click_events.insert().values(
            click_id="clk_seller", merchant_id=M_SELLER, surface="agent",
            canonical_product_id=P_SELLER, dest_domain="brand-a.myshopify.com",
            click_count=1, impression_count=0, created_at=NOW, updated_at=NOW,
        ))
        # Matched close but converting store is a DIFFERENT seller → mismatch.
        r_mismatch = await close_external_order_conversion(
            merchant_id=M_SELLER, click_id="clk_seller", external_order_id="shop_mm1",
            gross_amount_cents=6000, currency="USD", converted_at=NOW,
            converting_shop_domain="seller-b.myshopify.com")
        # A clean matched close on the SAME store → counted (control).
        await database.execute(surface_click_events.insert().values(
            click_id="clk_ok", merchant_id=M_SELLER, surface="agent",
            canonical_product_id=P_SELLER, dest_domain="brand-a.myshopify.com",
            click_count=1, impression_count=0, created_at=NOW, updated_at=NOW,
        ))
        r_ok = await close_external_order_conversion(
            merchant_id=M_SELLER, click_id="clk_ok", external_order_id="shop_ok1",
            gross_amount_cents=5000, currency="USD", converted_at=NOW,
            converting_shop_domain="brand-a.myshopify.com")

        await refresh_all_outcomes()
        merch = {r["subject_key"]: dict(r) for r in await database.fetch_all(
            "SELECT * FROM aggregated_outcomes WHERE subject_type='merchant'")}
        mism = await database.fetch_one(
            "SELECT (metadata->>'seller_mismatch') AS sm FROM commerce_attribution_edges "
            "WHERE external_order_id = 'shop_mm1'")

        assert r_mismatch and r_mismatch["seller_mismatch"] is True
        assert r_ok and r_ok["seller_mismatch"] is False
        assert mism["sm"] == "true", "mismatch edge must be stamped in Postgres JSONB"
        # Only the clean 5000 conversion counts — the 6000 mismatch is excluded.
        assert merch[M_SELLER]["gmv_cents"] == 5000, "seller_mismatch conversion must not inflate outcomes"
        assert merch[M_SELLER]["transacted_count"] == 1
    finally:
        await database.disconnect()


@pytest.mark.asyncio
async def test_seller_ref_keyed_subject_on_real_postgres():
    """A9-3 (ADR-009 §D3): a click whose JSONB context carries seller_ref/seed_kind
    (threaded by T2-1 from external_product_seeds.seller_ref) closes with the edge
    SUBJECT = seller_ref (not the anchor on the click row), passes the IDENTITY
    seller check even when the converting host differs from the click's dest_domain
    (the A9-1 custom-domain false-mismatch, inverted), replays idempotently on the
    real (merchant_id, external_order_id) unique index under the seller subject,
    and aggregates under the SELLER in aggregated_outcomes."""
    os.environ["DATABASE_URL"] = _PG_URL
    from sqlalchemy import create_engine
    from db.database import database, metadata
    from db.commerce_attribution import commerce_attribution_edges, surface_click_events
    from db.orders import orders as orders_table
    from services.commerce_attribution_service import close_external_order_conversion
    from services.outcome_aggregation_service import (
        refresh_all_outcomes,
        ensure_aggregated_outcomes_table,
    )

    sync = _PG_URL.replace("postgresql://", "postgresql+psycopg2://", 1)
    eng = create_engine(sync)
    metadata.create_all(eng, tables=[surface_click_events, commerce_attribution_edges, orders_table])
    eng.dispose()

    M_ANCHOR, M_SELLER_B, P_X = "M_ANCHOR", "merch_obs_sellerb", "prodcanon_X"
    await database.connect()
    try:
        await ensure_aggregated_outcomes_table()
        for t in ("aggregated_outcomes", "commerce_attribution_edges", "surface_click_events", "orders"):
            await database.execute(f"DELETE FROM {t}")

        # CROSS seed click: column merchant_id = the ANCHOR; JSONB context carries
        # the seed's seller-of-record. dest_domain is the seed's CUSTOM storefront
        # domain (brand.com) — the A9-1 raw-host compare would false-mismatch this.
        await database.execute(surface_click_events.insert().values(
            click_id="clk_seller_ref", merchant_id=M_ANCHOR, surface="agent",
            canonical_product_id=P_X, dest_domain="brand.com",
            context={"seller_ref": M_SELLER_B, "seed_kind": "cross"},
            click_count=1, impression_count=0, created_at=NOW, updated_at=NOW,
        ))
        # Seller B's own webhook closes (converting merchant == seller_ref) with a
        # myshopify host != the click's custom dest host → identity match wins.
        r1 = await close_external_order_conversion(
            merchant_id=M_SELLER_B, click_id="clk_seller_ref", external_order_id="shop_sr1",
            gross_amount_cents=7000, currency="USD", converted_at=NOW,
            converting_shop_domain="sellerb.myshopify.com")
        # Replay under the same seller subject — must hit the real UNIQUE index.
        r2 = await close_external_order_conversion(
            merchant_id=M_SELLER_B, click_id="clk_seller_ref", external_order_id="shop_sr1",
            gross_amount_cents=7000, currency="USD", converted_at=NOW,
            converting_shop_domain="sellerb.myshopify.com")
        # A DIFFERENT merchant closing against the same seller-keyed click →
        # identity mismatch: recorded (under the seller subject) but excluded.
        r3 = await close_external_order_conversion(
            merchant_id="M_IMPOSTOR", click_id="clk_seller_ref", external_order_id="shop_sr2",
            gross_amount_cents=99999, currency="USD", converted_at=NOW,
            converting_shop_domain="impostor.myshopify.com")

        await refresh_all_outcomes()
        merch = {r["subject_key"]: dict(r) for r in await database.fetch_all(
            "SELECT * FROM aggregated_outcomes WHERE subject_type='merchant'")}
        edge = await database.fetch_one(
            "SELECT merchant_id, metadata->>'seller_ref' AS sr, "
            "metadata->>'converting_merchant_id' AS cm "
            "FROM commerce_attribution_edges WHERE external_order_id = 'shop_sr1'")
        conv = await database.fetch_one(
            "SELECT count(*) c FROM commerce_attribution_edges WHERE state='converted'")

        # Subject = seller_ref; identity match despite host difference (A9-1 inverted).
        assert r1 and r1["seller_mismatch"] is False and r1["merchant_id"] == M_SELLER_B
        assert edge["merchant_id"] == M_SELLER_B
        assert edge["sr"] == M_SELLER_B and edge["cm"] == M_SELLER_B
        # Replay idempotent under the new subject (real unique index).
        assert r2 and r2.get("replayed") is True
        assert conv["c"] == 2, "replay must not create a duplicate edge under the seller subject"
        # Impostor close: subject still the seller, but mismatch-stamped + excluded.
        assert r3 and r3["seller_mismatch"] is True and r3["merchant_id"] == M_SELLER_B
        assert merch[M_SELLER_B]["gmv_cents"] == 7000, "identity-mismatch close must not inflate the seller's outcomes"
        assert merch[M_SELLER_B]["transacted_count"] == 1
        assert M_ANCHOR not in merch, "the anchor must NOT receive the cross-seed conversion"
    finally:
        await database.disconnect()
