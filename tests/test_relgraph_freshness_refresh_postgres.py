from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from scripts.ops.relgraph_freshness_refresh import plan, run

URL = os.getenv("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL.startswith(("postgresql://", "postgres://")), reason="requires synthetic PostgreSQL")


@pytest_asyncio.fixture
async def scoped_db():
    import asyncpg
    from databases import Database
    schema = "rg_freshness_" + uuid.uuid4().hex[:12]
    conn = await asyncpg.connect(URL)
    await conn.execute(f"CREATE SCHEMA {schema}")
    await conn.execute(f"SET search_path TO {schema}")
    await conn.execute("""
      CREATE TABLE catalog_products(product_key text, merchant_id text, platform text, source_product_id text,
        source_domain text, sync_status text, suppressed_at timestamptz, suppression_reason text,
        pdp_will_render boolean, pdp_will_render_computed_at timestamptz);
      CREATE TABLE catalog_merchants(merchant_id text, status text);
      CREATE TABLE merchant_stores(merchant_id text, platform text, status text, domain text);
      CREATE TABLE external_product_seeds(id text, external_product_id text, attached_product_key text,
        market text, status text, price_currency text, domain text, last_crawled_at timestamptz,
        last_crawl_attempt_at timestamptz, updated_at timestamptz);
      CREATE TABLE catalog_source_quarantine(match_type text, match_value text, state text, expires_at timestamptz);
      CREATE TABLE catalog_offers(product_key text, market text, suppressed_at timestamptz,
        price_checked_at timestamptz, offer_payload jsonb, source_system text, source_ref text);
      INSERT INTO catalog_products VALUES ('P1','seller','external_seed','ext:canonical','brand.example','live',
        NULL,NULL,true,now()-interval '30 days');
      INSERT INTO catalog_merchants VALUES ('seller','observed');
      INSERT INTO merchant_stores VALUES ('seller','external_seed','active','brand.example');
      INSERT INTO external_product_seeds VALUES
        ('seed_good','actual_seed_id','P1','US','active','USD','brand.example',now()-interval '30 days',NULL,now()),
        ('seed_jp','actual_seed_id_jp','P1','JP','active','JPY','brand.example',now()-interval '30 days',NULL,now()),
        ('seed_sgd','actual_seed_id_sg','P1','US','active','SGD','brand.example',now()-interval '30 days',NULL,now());
      INSERT INTO catalog_offers VALUES ('P1','US',NULL,now()-interval '30 days',
        '{"external_seed_id":"seed_good"}',NULL,NULL);
      ALTER TABLE external_product_seeds ADD COLUMN canonical_url text;
      ALTER TABLE external_product_seeds ADD COLUMN destination_url text;
      ALTER TABLE catalog_offers ADD COLUMN currency text DEFAULT 'USD';
      ALTER TABLE catalog_offers ADD COLUMN availability text DEFAULT 'in_stock';
      ALTER TABLE catalog_offers ADD COLUMN merchant_effective_price numeric DEFAULT 20;
      ALTER TABLE catalog_offers ADD COLUMN list_price numeric;
      ALTER TABLE catalog_offers ADD COLUMN estimated_best_price numeric;
    """)
    db = Database(URL, min_size=1, max_size=2, server_settings={"search_path": schema})
    await db.connect()
    try:
        yield db
    finally:
        await db.disconnect()
        await conn.execute(f"DROP SCHEMA {schema} CASCADE")
        await conn.close()


def manifest():
    return {"schema":"relgraph.freshness_refresh.v1", "market":"US", "currency":"USD", "max_products":2,
            "generated_at":datetime.now(timezone.utc).isoformat(), "product_keys":["P1"]}


@pytest.mark.asyncio
async def test_read_only_plan_resolves_attached_canonical_alias_with_market_currency_guard(scoped_db):
    before = await scoped_db.fetch_one("SELECT pdp_will_render_computed_at FROM catalog_products")
    work = await plan(scoped_db, manifest())
    assert work == {"page_keys":["P1"], "seed_ids":["seed_good"], "matched_products":1,
                    "page_due_products":1, "offer_due_products":1, "unrefreshable_products":0}
    assert (await scoped_db.fetch_one("SELECT pdp_will_render_computed_at FROM catalog_products"))[0] == before[0]


@pytest.mark.asyncio
async def test_store_disconnected_after_manifest_and_retired_product_are_rechecked(scoped_db):
    await scoped_db.execute("UPDATE merchant_stores SET status = 'inactive'")
    assert (await plan(scoped_db, manifest()))["matched_products"] == 0
    await scoped_db.execute("UPDATE merchant_stores SET status = 'active'")
    await scoped_db.execute("UPDATE catalog_products SET sync_status = 'retired'")
    assert (await plan(scoped_db, manifest()))["matched_products"] == 0


@pytest.mark.asyncio
async def test_origin_worklist_rechecks_quarantine_and_product_suppression(scoped_db):
    await scoped_db.execute("INSERT INTO catalog_source_quarantine VALUES('domain','brand.example','active',NULL)")
    assert (await plan(scoped_db, manifest()))["seed_ids"] == []
    await scoped_db.execute("UPDATE catalog_products SET suppressed_at = now()")
    assert (await plan(scoped_db, manifest()))["matched_products"] == 0


@pytest.mark.asyncio
async def test_targeted_owner_selector_uses_its_original_guard_on_real_postgres(scoped_db, monkeypatch):
    import services.external_referral_readiness as owner
    monkeypatch.setattr(owner, "database", scoped_db)
    ids = ["seed_good", "seed_jp", "seed_sgd"]
    assert await owner.get_external_referral_refresh_candidate_seed_ids(seed_ids=ids, market="US") == ["seed_good"]
    await scoped_db.execute("INSERT INTO catalog_source_quarantine VALUES('domain','brand.example','active',NULL)")
    assert await owner.get_external_referral_refresh_candidate_seed_ids(seed_ids=ids, market="US") == []


@pytest.mark.asyncio
async def test_current_invalid_price_is_due_but_current_unavailable_stock_is_a_result(scoped_db):
    await scoped_db.execute("UPDATE external_product_seeds SET last_crawled_at = now()")
    await scoped_db.execute("UPDATE catalog_products SET pdp_will_render_computed_at = now()")
    await scoped_db.execute("UPDATE catalog_offers SET price_checked_at = now(), merchant_effective_price = 0")
    assert (await plan(scoped_db, manifest()))["seed_ids"] == ["seed_good"]
    await scoped_db.execute("UPDATE catalog_offers SET availability = 'out_of_stock'")
    work = await plan(scoped_db, manifest())
    assert work["seed_ids"] == [] and work["unrefreshable_products"] == 0


@pytest.mark.asyncio
async def test_offer_without_an_eligible_origin_owner_remains_unrefreshable(scoped_db):
    await scoped_db.execute("UPDATE external_product_seeds SET status = 'inactive'")
    work = await plan(scoped_db, manifest())
    assert work["seed_ids"] == [] and work["unrefreshable_products"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("price_read,missing_clock", [(False, False), (False, True), (True, False)])
async def test_real_mirror_owner_write_is_not_fresh_without_price_currency_read(scoped_db, monkeypatch, price_read, missing_clock):
    from services import external_offer_dual_write as owner
    # The existing owner's full upsert runs against the synthetic schema. No crawler or
    # simplified price-clock writer stands in for its price_read guard.
    setup_sql = """
      ALTER TABLE catalog_products ADD COLUMN source_system text;
      ALTER TABLE catalog_products ADD COLUMN source_ref text;
      ALTER TABLE catalog_products ADD COLUMN updated_at timestamptz DEFAULT now();
      UPDATE catalog_products SET source_system='external_product_seeds_mirror_v1',source_ref='seed_good';
      ALTER TABLE external_product_seeds ADD COLUMN price_amount numeric DEFAULT 20;
      ALTER TABLE external_product_seeds ADD COLUMN availability text DEFAULT 'in_stock';
      ALTER TABLE external_product_seeds ADD COLUMN seed_data jsonb DEFAULT '{}';
      ALTER TABLE catalog_offers ADD COLUMN offer_id text UNIQUE;
      ALTER TABLE catalog_offers ADD COLUMN sku_key text;
      ALTER TABLE catalog_offers ADD COLUMN merchant_id text;
      ALTER TABLE catalog_offers ADD COLUMN catalog_track text;
      ALTER TABLE catalog_offers ADD COLUMN truth_tier text;
      ALTER TABLE catalog_offers ADD COLUMN readiness_tier text;
      ALTER TABLE catalog_offers ADD COLUMN offer_type text;
      ALTER TABLE catalog_offers ADD COLUMN is_first_party boolean;
      ALTER TABLE catalog_offers ADD COLUMN offer_mode text;
      ALTER TABLE catalog_offers ADD COLUMN channel text;
      ALTER TABLE catalog_offers ADD COLUMN inventory_quantity integer;
      ALTER TABLE catalog_offers ADD COLUMN price_confidence text;
      ALTER TABLE catalog_offers ADD COLUMN source_domain text;
      ALTER TABLE catalog_offers ADD COLUMN updated_at timestamptz;
      ALTER TABLE catalog_offers ADD COLUMN suppression_reason text;
      CREATE TABLE catalog_skus(sku_key text PRIMARY KEY,product_key text,
        suppressed_at timestamptz,suppression_reason text);
      INSERT INTO catalog_skus VALUES('P1::canonical','P1',NULL,NULL);
      UPDATE catalog_offers SET sku_key='P1::canonical',merchant_id='seller';
    """
    for statement in setup_sql.split(";"):
        if statement.strip():
            await scoped_db.execute(statement)
    await scoped_db.execute("UPDATE catalog_offers SET offer_id=:oid", {"oid": owner.derive_mirror_offer_id("P1")})
    if missing_clock:
        await scoped_db.execute("UPDATE catalog_offers SET price_checked_at=NULL")
    before = await scoped_db.fetch_val("SELECT price_checked_at FROM catalog_offers")
    monkeypatch.setattr(owner, "database", scoped_db)
    monkeypatch.setattr(owner, "dual_write_enabled", lambda: True)
    monkeypatch.setattr(owner, "_price_check_column", {"present": False, "checked": None})
    async def batch(**kwargs):
        assert kwargs["candidate_seed_ids"] == ["seed_good"]
        await scoped_db.execute("UPDATE external_product_seeds SET last_crawled_at=now() WHERE id='seed_good'")
        result = await owner.sync_offer_for_seed("seed_good", attached_price_source="refresh", currency_read=price_read)
        assert result["status"] == "synced" and result["target"] == "mirror"
        return {"status":"success", "attempted_count":1, "origin_reads":1,
                "projections_attempted":1, "projections_written":1, "price_unchanged":1}
    async def pages(keys, **kwargs):
        assert keys == ["P1"]
        await scoped_db.execute("UPDATE catalog_products SET pdp_will_render_computed_at=now()")
        return 1
    summary = await run(scoped_db, manifest(), apply=True, authorized=True, batch_fn=batch,
                        page_fn=pages, dual_write_fn=lambda: True)
    checked = await scoped_db.fetch_val("SELECT price_checked_at FROM catalog_offers")
    assert summary["freshness_rechecked"] is True and summary["remaining_page_checks_due"] == 0
    assert summary["complete"] is price_read
    assert summary["remaining_offer_checks_due"] == int(not price_read)
    assert summary["remaining_origin_checks_due"] == int(not price_read)
    assert summary["status"] == ("success" if price_read else "degraded")
    assert (checked != before) if price_read else (checked == before)


@pytest.mark.asyncio
@pytest.mark.parametrize("merchant,platform,source,attachment,bound", [
    ("seller", "shopify", "123", "P2", False),
    ("seller", "external_seed", "123", "P2", False),
    ("seller", "shopify", "123", None, False),
    ("seller", "shopify", "123", "P1", True),
    ("seller", "external_seed", "123", None, True),
    ("external_seed", "shopify", "123", None, False),
    ("seller", "shopify", "ext_shared", None, True),
])
async def test_seed_binding_is_exact_or_external_lane_not_recycled_native_id(scoped_db, merchant, platform, source, attachment, bound):
    await scoped_db.execute("INSERT INTO catalog_products VALUES ('P2','other','shopify','123','other.example','live',NULL,NULL,true,now())")
    await scoped_db.execute("INSERT INTO catalog_merchants VALUES ('other','active')")
    await scoped_db.execute("INSERT INTO merchant_stores VALUES ('other','shopify','active','other.example')")
    await scoped_db.execute("UPDATE catalog_products SET merchant_id=:merchant,platform=:platform,source_product_id=:source,pdp_will_render_computed_at=now() WHERE product_key='P1'",
                            {"merchant": merchant, "platform": platform, "source": source})
    await scoped_db.execute("UPDATE merchant_stores SET platform=:platform WHERE merchant_id='seller'", {"platform": platform})
    await scoped_db.execute("UPDATE external_product_seeds SET status='inactive' WHERE id<>'seed_good'")
    await scoped_db.execute("UPDATE external_product_seeds SET external_product_id=:source,attached_product_key=:attached,last_crawled_at=now() WHERE id='seed_good'",
                            {"source": source, "attached": attachment})
    await scoped_db.execute("UPDATE catalog_offers SET price_checked_at=now()")
    fresh = await plan(scoped_db, manifest())
    assert fresh["offer_due_products"] == int(not bound)
    assert fresh["unrefreshable_products"] == int(not bound) and fresh["seed_ids"] == []
    await scoped_db.execute("UPDATE catalog_offers SET price_checked_at=now()-interval '3 days'")
    stale = await plan(scoped_db, manifest())
    assert stale["seed_ids"] == (["seed_good"] if bound else [])
    # No offer partition exists to admit the product; the seed-only market gate must
    # reject the identical ambiguous/mismatched origin rather than synthesize identity.
    await scoped_db.execute("DELETE FROM catalog_offers")
    seed_only = await plan(scoped_db, manifest())
    assert seed_only["matched_products"] == int(bound)
