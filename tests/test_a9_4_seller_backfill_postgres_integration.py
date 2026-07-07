"""A9-4 (ADR-009 D4) — seller-of-record backfill, REAL Postgres integration test.

The unit tests drive fakes; this one runs the FULL backfill (`run_backfill`)
end-to-end against a real PostgreSQL server so the Postgres-specific SQL the
tooling emits is actually executed: `= ANY(:pks)`, the reflection over
`information_schema.columns`, the `information_schema`-discovered cascade
UPDATEs, the batched transaction + parity snapshots, `ensure_observed_seller`'s
real mint, and the checkpoint upsert.

Seeds a MINI bucket (2 brands: Anuko + GlowLab) with catalog rows + skus +
offers + index_pipeline_state + agent_pdp_view + a beauty_* dependent + external
seeds (self/cross, bare-key, undecodable), then runs `--execute` and asserts:
  - per-brand OBSERVED merchants minted (status='observed');
  - catalog rows re-subjected off the bucket, consistently across every
    dependent (option (b): merchant_id column re-keyed, product_key stable);
  - sigs / canonical URLs BYTE-IDENTICAL (IDENTITY_REFERENCE T5 / ADR-009 D4.3);
  - seeds seller_ref filled (self + cross), the no-destination seed left NULL in
    review, the bare attached key repaired, the undecodable one in review;
  - a re-run is idempotent (0 further re-subjects, counts stable).

Gated on PIVOTA_TEST_PG_URL (skipped when unset, like the T2 harness). Point it
at a throwaway DB:
    createdb a9_4_ci
    PIVOTA_TEST_PG_URL=postgresql://localhost:5432/a9_4_ci \
      .venv/bin/python -m pytest tests/test_a9_4_seller_backfill_postgres_integration.py -q
"""

from __future__ import annotations

import os

import pytest

_PG_URL = os.getenv("PIVOTA_TEST_PG_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not _PG_URL, reason="Set PIVOTA_TEST_PG_URL to a Postgres URL to run the A9-4 integration test."
)

BUCKET = "external_seed"


async def _seed_mini_bucket(database):
    # Merchants: the shared bucket (status active, servable today).
    await database.execute(
        "INSERT INTO catalog_merchants (merchant_id, merchant_name, primary_platform, status, "
        "indexable, source_system, source_ref) VALUES "
        "(:m, 'External Seed Bucket', 'external_seed', 'active', TRUE, 'mirror', NULL)",
        {"m": BUCKET},
    )
    # Two external_seed catalog products with FROZEN sigs.
    products = [
        # (product_key, source_product_id, brand, source_domain, content_key, sig, canon)
        ("prod::external_seed::external_seed::anuko-1", "anuko-1", "Anuko", "anuko.com",
         "ck_anuko00000000000000000000000001", "sig_anuko0000000000000000000000001",
         "https://agent.pivota.cc/products/sig_anuko0000000000000000000000001"),
        ("prod::external_seed::external_seed::glow-1", "glow-1", "GlowLab", "glowlab.com",
         "ck_glow000000000000000000000000001", "sig_glow00000000000000000000000001",
         "https://agent.pivota.cc/products/sig_glow00000000000000000000000001"),
    ]
    for pk, spid, brand, dom, ck, sig, canon in products:
        await database.execute(
            "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, "
            "title, brand, source_domain, canonical_url, content_key, pivota_signature_id, "
            "pivota_canonical_url, sync_status) VALUES "
            "(:pk, :m, 'external_seed', :spid, :title, :brand, :dom, :canon_pdp, :ck, :sig, :canon, 'live')",
            {"pk": pk, "m": BUCKET, "spid": spid, "title": f"{brand} Product",
             "brand": brand, "dom": dom, "canon_pdp": f"https://{dom}/p/{spid}",
             "ck": ck, "sig": sig, "canon": canon},
        )
        sku_key = f"sku::{pk}::{spid}"
        await database.execute(
            "INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform, "
            "source_product_id, source_variant_id, title) VALUES "
            "(:sk, :pk, :m, 'external_seed', :spid, :spid, :title)",
            {"sk": sku_key, "pk": pk, "m": BUCKET, "spid": spid, "title": f"{brand} SKU"},
        )
        await database.execute(
            "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id) VALUES "
            "(:oid, :sk, :pk, :m)",
            {"oid": f"offer::{pk}", "sk": sku_key, "pk": pk, "m": BUCKET},
        )
        await database.execute(
            "INSERT INTO index_pipeline_state (content_key, pivota_signature_id, merchant_id, "
            "pipeline_stage, serving_eligible) VALUES (:ck, :sig, :m, 'discovered', TRUE)",
            {"ck": ck, "sig": sig, "m": BUCKET},
        )
        await database.execute(
            "INSERT INTO agent_pdp_view (content_key, pivota_signature_id, title, primary_merchant_id) "
            "VALUES (:ck, :sig, :title, :m)",
            {"ck": ck, "sig": sig, "title": f"{brand} PDP", "m": BUCKET},
        )
        await database.execute(
            "INSERT INTO beauty_shades (shade_id, sku_key, product_key, merchant_id, shade_name) "
            "VALUES (:sid, :sk, :pk, :m, 'Neutral')",
            {"sid": f"shade::{pk}", "sk": sku_key, "pk": pk, "m": BUCKET},
        )
        # The forward-leak table: beauty_sku_ingredients carries merchant_id + a
        # product/sku key, so it MUST be auto-discovered and re-subjected too
        # (the pool the INCI fix stops refilling).
        await database.execute(
            "INSERT INTO beauty_sku_ingredients (sku_key, product_key, merchant_id, raw_inci, source_system) "
            "VALUES (:sk, :pk, :m, 'Water, Niacinamide', 'pdp_crawl')",
            {"sk": sku_key, "pk": pk, "m": BUCKET},
        )

    # External seeds:
    #   A — cross, seller_ref NULL, attached prod:: (already storage form).
    #   B — cross, seller_ref NULL, BARE attached key + external_product_id=glow-1 (repairable).
    #   C — no destination + no external_product_id: seller_ref UNRESOLVABLE + bare UNDECODABLE.
    seeds = [
        ("seedA", "anuko-1", "prod::external_seed::external_seed::anuko-1", "Anuko",
         "anuko.com", "https://anuko.com/p/anuko-1"),
        ("seedB", "glow-1", "legacy-bare-glow", "GlowLab",
         "glowlab.com", "https://glowlab.com/p/glow-1"),
        ("seedC", "", "legacy-bare-orphan", "", "", ""),
    ]
    for sid, epid, attached, brand, dom, dest in seeds:
        await database.execute(
            "INSERT INTO external_product_seeds (id, external_product_id, market, tool, "
            "destination_url, canonical_url, domain, title, brand, attached_product_key, "
            "seed_data, status) VALUES "
            "(:id, :epid, 'us', '*', :dest, :canon, :dom, :title, :brand, :attached, '{}', 'active')",
            {"id": sid, "epid": (epid or None), "dest": (dest or "https://x/"),
             "canon": (dest or None), "dom": (dom or None), "title": f"{brand or 'Orphan'} seed",
             "brand": (brand or None), "attached": attached},
        )


async def _create_min_tables(database):
    """Tables without a SQLAlchemy Table object — created with the columns the
    tooling touches."""
    await database.execute(
        """
        CREATE TABLE IF NOT EXISTS index_pipeline_state (
            content_key TEXT PRIMARY KEY,
            pivota_signature_id TEXT,
            merchant_id TEXT,
            pipeline_stage TEXT,
            serving_eligible BOOLEAN DEFAULT FALSE
        )
        """
    )
    await database.execute(
        """
        CREATE TABLE IF NOT EXISTS external_product_seeds (
            id TEXT PRIMARY KEY,
            external_product_id TEXT,
            market TEXT NOT NULL,
            tool TEXT NOT NULL DEFAULT '*',
            destination_url TEXT NOT NULL,
            canonical_url TEXT,
            domain TEXT,
            title TEXT,
            brand TEXT,
            attached_product_key TEXT,
            attached_variant_id TEXT,
            seed_data JSONB NOT NULL DEFAULT '{}'::jsonb,
            status TEXT NOT NULL DEFAULT 'active',
            seller_ref TEXT,
            seed_kind TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    await database.execute(
        "CREATE TABLE IF NOT EXISTS merchant_stores (merchant_id TEXT, domain TEXT)"
    )


@pytest.mark.asyncio
async def test_a9_4_backfill_end_to_end_on_real_postgres():
    os.environ["DATABASE_URL"] = _PG_URL
    from sqlalchemy import create_engine
    import db.database as dbmod

    # `db.database` (and every module that did `from db.database import database`)
    # binds the singleton at IMPORT time from DATABASE_URL. Like the repo's
    # test_t2_postgres_integration, this test must be the one that binds it —
    # run it in ISOLATION with PIVOTA_TEST_PG_URL set. If an earlier test in the
    # same process already bound the default sqlite, skip LOUDLY rather than run
    # PG-specific SQL against sqlite (honest gap over a silent wrong-DB pass).
    if "postgres" not in str(dbmod.DATABASE_URL):
        pytest.skip(
            "db.database is bound to a non-postgres URL (an earlier test imported it "
            "first). Run this file in isolation: PIVOTA_TEST_PG_URL=... .venv/bin/python "
            "-m pytest tests/test_a9_4_seller_backfill_postgres_integration.py"
        )
    database, metadata = dbmod.database, dbmod.metadata
    from db.catalog import (
        catalog_merchants, catalog_products, catalog_skus, catalog_offers,
        beauty_shades, beauty_sku_ingredients, agent_pdp_view,
    )
    from db.brand_claims import brand_claims
    from db.commerce_attribution import commerce_attribution_edges
    from services import seller_identity as si
    from scripts.backfill_seller_of_record import run_backfill, BANNED_BUCKET_MERCHANT_ID

    sync = _PG_URL.replace("postgresql://", "postgresql+psycopg2://", 1)
    eng = create_engine(sync)
    metadata.create_all(eng, tables=[
        catalog_merchants, catalog_products, catalog_skus, catalog_offers,
        beauty_shades, beauty_sku_ingredients, agent_pdp_view, brand_claims,
        commerce_attribution_edges,
    ])
    eng.dispose()

    await database.connect()
    try:
        await _create_min_tables(database)
        # Clean slate.
        for t in ("beauty_shades", "beauty_sku_ingredients", "agent_pdp_view",
                  "index_pipeline_state", "catalog_offers", "catalog_skus",
                  "catalog_products", "catalog_merchants", "external_product_seeds",
                  "brand_claims", "commerce_attribution_edges", "merchant_stores"):
            await database.execute(f"DELETE FROM {t}")
        try:
            await database.execute("DROP TABLE IF EXISTS a9_4_backfill_checkpoint")
        except Exception:
            pass

        await _seed_mini_bucket(database)

        obs_anuko = si.make_observed_merchant_id("Anuko", "anuko.com")
        obs_glow = si.make_observed_merchant_id("GlowLab", "glowlab.com")

        # Capture FROZEN sig set before.
        before_sigs = {
            dict(r)["product_key"]: (dict(r)["pivota_signature_id"], dict(r)["pivota_canonical_url"])
            for r in await database.fetch_all(
                "SELECT product_key, pivota_signature_id, pivota_canonical_url FROM catalog_products"
            )
        }

        # ---- EXECUTE ----
        report = await run_backfill(
            database=database, si_mod=si, execute=True, batch_size=100,
            phases=["seeds", "barekey", "catalog", "edges"],
        )

        # -- per-brand observed merchants minted (status='observed') --
        obs_rows = {
            dict(r)["merchant_id"]: dict(r)["status"]
            for r in await database.fetch_all(
                "SELECT merchant_id, status FROM catalog_merchants WHERE merchant_id LIKE 'merch_obs_%'"
            )
        }
        assert obs_rows.get(obs_anuko) == "observed"
        assert obs_rows.get(obs_glow) == "observed"

        # -- catalog rows re-subjected off the bucket --
        assert report.catalog["scanned"] == 2
        assert report.catalog["resubjected"] == 2
        left = await database.fetch_one(
            "SELECT count(*) AS c FROM catalog_products WHERE merchant_id = :m", {"m": BUCKET}
        )
        assert dict(left)["c"] == 0
        pmap = {
            dict(r)["product_key"]: dict(r)["merchant_id"]
            for r in await database.fetch_all("SELECT product_key, merchant_id FROM catalog_products")
        }
        assert pmap["prod::external_seed::external_seed::anuko-1"] == obs_anuko
        assert pmap["prod::external_seed::external_seed::glow-1"] == obs_glow

        # -- beauty_sku_ingredients (the forward-leak table) was auto-discovered --
        cascade_names = {t["table"] for t in report.cascade_tables}
        assert "beauty_sku_ingredients" in cascade_names, report.cascade_tables

        # -- every dependent consistently re-subjected (no bucket residue) --
        for tbl, col in (("catalog_skus", "merchant_id"), ("catalog_offers", "merchant_id"),
                         ("index_pipeline_state", "merchant_id"),
                         ("agent_pdp_view", "primary_merchant_id"),
                         ("beauty_shades", "merchant_id"),
                         ("beauty_sku_ingredients", "merchant_id")):
            row = await database.fetch_one(
                f"SELECT count(*) AS c FROM {tbl} WHERE {col} = :m", {"m": BUCKET}
            )
            assert dict(row)["c"] == 0, f"{tbl}.{col} still holds the bucket"
        # the INCI dependent now points at the per-brand seller
        binci = await database.fetch_one(
            "SELECT merchant_id FROM beauty_sku_ingredients WHERE product_key = "
            "'prod::external_seed::external_seed::glow-1'"
        )
        assert dict(binci)["merchant_id"] == obs_glow
        # and the beauty dependent points at the right per-brand seller
        bshade = await database.fetch_one(
            "SELECT merchant_id FROM beauty_shades WHERE product_key = "
            "'prod::external_seed::external_seed::glow-1'"
        )
        assert dict(bshade)["merchant_id"] == obs_glow

        # -- sigs BYTE-IDENTICAL (T5 freeze) + product_key STABLE (option b) --
        after_sigs = {
            dict(r)["product_key"]: (dict(r)["pivota_signature_id"], dict(r)["pivota_canonical_url"])
            for r in await database.fetch_all(
                "SELECT product_key, pivota_signature_id, pivota_canonical_url FROM catalog_products"
            )
        }
        assert after_sigs == before_sigs
        # every per-batch parity check passed
        assert all(p["ok"] for p in report.parity)
        assert all(p.get("sigs_frozen") for p in report.parity)

        # -- seeds seller_ref filled (cross) + review for unresolvable --
        seed_state = {
            dict(r)["id"]: (dict(r)["seller_ref"], dict(r)["seed_kind"])
            for r in await database.fetch_all(
                "SELECT id, seller_ref, seed_kind FROM external_product_seeds"
            )
        }
        assert seed_state["seedA"] == (obs_anuko, "cross")
        assert seed_state["seedB"] == (obs_glow, "cross")
        assert seed_state["seedC"] == (None, None)  # left NULL, no guess
        assert "seedC" in report.review_queue["seeds_unresolvable_seller_ref"]

        # -- bare attached key repaired for B; undecodable C in review --
        seedB_key = dict(await database.fetch_one(
            "SELECT attached_product_key FROM external_product_seeds WHERE id='seedB'"
        ))["attached_product_key"]
        assert seedB_key == "prod::external_seed::external_seed::glow-1"
        undecodable_ids = [r["seed_id"] for r in report.review_queue["barekey_undecodable"]]
        assert undecodable_ids == ["seedC"]

        # -- edges: 0 under the bucket -> assert-and-log --
        assert report.edges == {"count": 0, "action": "assert_and_log_zero"}

        # ---- RE-RUN is idempotent ----
        report2 = await run_backfill(
            database=database, si_mod=si, execute=True, batch_size=100,
            phases=["seeds", "barekey", "catalog", "edges"],
        )
        assert report2.counts["catalog_external_seed"] == 0
        assert report2.catalog["scanned"] == 0
        assert report2.catalog["resubjected"] == 0
        assert report2.counts["seeds_seller_ref_null"] == 1  # only seedC remains NULL
        assert report2.barekey["repaired"] == 0  # nothing left in bare format but seedC (undecodable)
        # checkpoint rows persisted from the first run
        ck = await database.fetch_one("SELECT count(*) AS c FROM a9_4_backfill_checkpoint")
        assert dict(ck)["c"] >= 2

    finally:
        await database.disconnect()
