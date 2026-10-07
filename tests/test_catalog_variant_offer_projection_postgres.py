import asyncio
import json
import os
import uuid
import pytest
from services import catalog_variant_offer_projection as mod

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.getenv("DATABASE_URL", "").startswith("postgres"), reason="requires PostgreSQL"),
]
PK = "variant-projection-test"
M = "projection-test-merchant"
SEED = "projection-test-seed"


@pytest.fixture
async def db():
    from db.database import database
    from db.catalog import catalog_products, catalog_skus, catalog_offers, writer_audit_log
    from tests.model_schema import ensure_model_tables

    await database.connect()
    await ensure_model_tables([catalog_products, catalog_skus, catalog_offers, writer_audit_log])
    await database.execute("ALTER TABLE catalog_offers ADD COLUMN IF NOT EXISTS price_checked_at timestamptz")
    await database.execute(
        "CREATE TABLE IF NOT EXISTS external_product_seeds(id text,attached_product_key text,status text,seed_data jsonb)"
    )
    for column in ("seller_ref", "domain", "canonical_url", "destination_url", "market"):
        await database.execute(f"ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS {column} text")
    await database.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_catalog_skus_source_identity_v2 ON catalog_skus(merchant_id,platform,product_key,source_variant_id)"
    )
    async with database.transaction(force_rollback=True):
        await database.execute(
            """INSERT INTO catalog_products(product_key,merchant_id,platform,source_product_id,source_domain,source_system,source_ref,title,catalog_track)
   VALUES(:pk,:m,'external_seed','external-product','brand.example',:src,:seed,'Test','external_referral')""",
            {"pk": PK, "m": M, "src": mod.MIRROR, "seed": SEED},
        )
        await database.execute(
            "INSERT INTO external_product_seeds(id,attached_product_key,status,seed_data) VALUES(:seed,:pk,'active',CAST(:data AS jsonb))",
            {
                "seed": SEED,
                "pk": PK,
                "data": json.dumps(
                    {
                        "snapshot": {
                            "variants": [
                                {"variant_id": "677289689108", "price": "16", "currency": "USD", "stock": "In Stock"},
                                {
                                    "variant_id": "42199434526795",
                                    "price": "27",
                                    "currency": "USD",
                                    "stock": "Out of Stock",
                                },
                            ]
                        }
                    }
                ),
            },
        )
        await database.execute(
            """UPDATE external_product_seeds SET seller_ref=:m,domain='brand.example',
            destination_url='https://brand.example/products/test',market='US' WHERE id=:seed""",
            {"m": M, "seed": SEED},
        )
        await database.execute(
            """INSERT INTO catalog_skus(sku_key,product_key,merchant_id,platform,source_product_id,source_variant_id,title,currency)
            VALUES(:sk,:pk,:m,'external_seed','external-product',:pk,'Canonical','USD')""",
            {"sk": PK + "::canonical", "pk": PK, "m": M},
        )
        for vid in ["677289689108", "42199434526795"]:
            await database.execute(
                """INSERT INTO catalog_skus(sku_key,product_key,merchant_id,platform,source_product_id,source_variant_id,title,currency)
    VALUES(:sk,:pk,:m,'external_seed','external-product',:vid,'Test','USD')""",
                {"sk": PK + "::v::" + vid, "pk": PK, "m": M, "vid": vid},
            )
        await database.execute(
            """INSERT INTO catalog_offers(offer_id,sku_key,product_key,merchant_id,currency,market,source_domain,offer_payload,offer_type,is_first_party)
   VALUES('projection-template',:sk,:pk,:m,'USD','US','brand.example',CAST(:payload AS jsonb),'brand_direct',true)""",
            {
                "sk": PK + "::canonical",
                "pk": PK,
                "m": M,
                "payload": json.dumps({"destination_url": "https://brand.example/products/test"}),
            },
        )
        yield database
    await database.disconnect()


async def test_real_insert_own_prices_idempotent_and_no_freshness_stamp(db):
    dry = await mod.project_missing_variant_offers(PK, db=db)
    assert dry["planned"] == 2 and dry["inserted"] == 0
    result = await mod.project_missing_variant_offers(PK, apply=True, db=db)
    assert result["inserted"] == 2
    rows = await db.fetch_all(
        "SELECT sku_key,list_price,availability,readiness_tier,offer_mode,price_checked_at FROM catalog_offers WHERE source_system=:src ORDER BY sku_key",
        {"src": mod.SOURCE},
    )
    assert {r["sku_key"]: (float(r["list_price"]), r["availability"]) for r in rows} == {
        PK + "::v::677289689108": (16, "in_stock"),
        PK + "::v::42199434526795": (27, "out_of_stock"),
    }
    assert all(
        r["readiness_tier"] == "referral_only" and r["offer_mode"] == "redirect" and r["price_checked_at"] is None
        for r in rows
    )
    assert (await mod.project_missing_variant_offers(PK, apply=True, db=db))["inserted"] == 0


async def test_suppressed_offer_not_revived_or_duplicated(db):
    await db.execute(
        """INSERT INTO catalog_offers(offer_id,sku_key,product_key,merchant_id,currency,market,list_price,suppression_reason)
  VALUES('withdrawn-offer',:sk,:pk,:m,'USD','US',99,'withdrawn')""",
        {"sk": PK + "::v::677289689108", "pk": PK, "m": M},
    )
    assert (await mod.project_missing_variant_offers(PK, apply=True, db=db))["inserted"] == 1
    row = await db.fetch_one(
        "SELECT list_price,suppression_reason FROM catalog_offers WHERE offer_id='withdrawn-offer'"
    )
    assert float(row["list_price"]) == 99 and row["suppression_reason"] == "withdrawn"
    assert (
        await db.fetch_val("SELECT count(*) FROM catalog_offers WHERE sku_key=:sk", {"sk": PK + "::v::677289689108"})
        == 1
    )


async def test_suppressed_sku_and_wrong_seed_attachment_refused(db):
    await db.execute(
        "UPDATE catalog_skus SET suppression_reason='withdrawn' WHERE sku_key=:sk", {"sk": PK + "::v::677289689108"}
    )
    assert (await mod.project_missing_variant_offers(PK, db=db))["planned"] == 1
    await db.execute(
        "UPDATE external_product_seeds SET attached_product_key='another-product' WHERE id=:seed", {"seed": SEED}
    )
    result = await mod.project_missing_variant_offers(PK, apply=True, db=db)
    assert result["inserted"] == 0 and result["skips"]["active_attached_seed_missing"] == 1


async def test_current_seed_seller_correction_cannot_price_the_old_mirror(db):
    await db.execute("UPDATE external_product_seeds SET seller_ref='another-seller' WHERE id=:seed", {"seed": SEED})
    result = await mod.project_missing_variant_offers(PK, apply=True, db=db)
    assert result["inserted"] == 0 and result["skips"]["seed_listing_or_seller_mismatch"] == 1


async def test_conflicting_variant_alias_is_not_written(db):
    await db.execute(
        """UPDATE external_product_seeds SET seed_data=jsonb_set(seed_data,
        '{snapshot,variants,0,id}', '"42199434526795"'::jsonb) WHERE id=:seed""",
        {"seed": SEED},
    )
    result = await mod.project_missing_variant_offers(PK, apply=True, db=db)
    assert result["inserted"] == 1 and result["skips"]["conflicting_variant_identity"] == 1
    assert (
        await db.fetch_val(
            "SELECT count(*) FROM catalog_offers WHERE sku_key=:sk AND source_system=:src",
            {"sk": PK + "::v::677289689108", "src": mod.SOURCE},
        )
        == 0
    )


async def test_write_failure_rolls_back_all_new_offers(db, monkeypatch):
    original = db.fetch_val

    async def failing(query, values=None):
        if query == mod.INSERT_SQL and values["sku_key"].endswith("42199434526795"):
            raise RuntimeError("injected write failure")
        return await original(query, values)

    monkeypatch.setattr(db, "fetch_val", failing)
    with pytest.raises(RuntimeError):
        await mod.project_missing_variant_offers(PK, apply=True, db=db)
    assert await original("SELECT count(*) FROM catalog_offers WHERE source_system=:src", {"src": mod.SOURCE}) == 0


async def test_canonical_seed_writer_also_projects_variant_offers(db):
    from services.external_offer_dual_write import upsert_catalog_offer_from_seed_row

    await db.execute("DELETE FROM catalog_offers WHERE offer_id='projection-template'")
    await upsert_catalog_offer_from_seed_row(
        PK,
        {
            "id": SEED,
            "price_amount": 16,
            "price_currency": "USD",
            "availability": "in_stock",
            "domain": "brand.example",
            "market": "US",
            "destination_url": "https://brand.example/products/test",
            "seed_kind": "self",
            "snapshot_variants": [{"variant_id": "677289689108"}, {"variant_id": "42199434526795"}],
        },
        merchant_id=M,
    )
    assert (
        await db.fetch_val("SELECT count(*) FROM catalog_offers WHERE source_system=:source", {"source": mod.SOURCE})
        == 2
    )


async def test_concurrent_promotion_waits_for_product_before_locking_skus(db, monkeypatch):
    """Two actual pool connections reproduce the repair/promotion lock ordering."""
    from databases import Database
    from services import catalog_variant_promoter as promoter

    live = Database(os.environ["DATABASE_URL"])
    promoting = Database(os.environ["DATABASE_URL"])
    await live.connect()
    await promoting.connect()
    pk = "projection-concurrent-" + uuid.uuid4().hex
    seed = pk + "-seed"
    merchant = pk + "-merchant"
    variants = [
        {"variant_id": "677289689108", "price": "16", "currency": "USD", "title": "120 mL"},
        {"variant_id": "42199434526795", "price": "27", "currency": "USD", "title": "2x120 mL"},
    ]
    source = {"snapshot": {"variants": variants}}
    primary = {
        "product_key": pk,
        "merchant_id": merchant,
        "platform": "external_seed",
        "source_product_id": pk,
        "parent_title": "Test",
        "source_system": mod.MIRROR,
        "catalog_track": "external_referral",
        "seed_data": source,
    }
    locked = asyncio.Event()
    promotion_waiting = asyncio.Event()
    continue_repair = asyncio.Event()
    tasks = []

    class Proxy:
        def __init__(self, repair=False):
            self.repair = repair
            self.db = live if repair else promoting

        def __getattr__(self, name):
            return getattr(self.db, name)

        async def fetch_one(self, query, values=None):
            if query == promoter.SELECT_GROUP_PRIMARY_SQL:
                return primary
            if not self.repair and "catalog_products" in str(query) and "FOR UPDATE" in str(query):
                promotion_waiting.set()
            row = await self.db.fetch_one(query, values)
            if self.repair and query == mod.PRODUCT_SQL + " FOR UPDATE":
                locked.set()
                await continue_repair.wait()
            return row

    try:
        await live.execute(
            """INSERT INTO catalog_products(product_key,merchant_id,platform,
            source_product_id,source_domain,source_system,source_ref,title,catalog_track)
            VALUES(:pk,:m,'external_seed',:source_product,'brand.example',:src,:seed,'Test','external_referral')""",
            {"pk": pk, "m": merchant, "src": mod.MIRROR, "source_product": pk, "seed": seed},
        )
        await live.execute(
            """INSERT INTO external_product_seeds(id,attached_product_key,status,seed_data,
            seller_ref,domain,destination_url,market) VALUES(:seed,:pk,'active',CAST(:data AS jsonb),
            :m,'brand.example','https://brand.example/products/test','US')""",
            {"seed": seed, "pk": pk, "data": json.dumps(source), "m": merchant},
        )
        for variant in variants:
            await live.execute(
                """INSERT INTO catalog_skus(sku_key,product_key,merchant_id,platform,
                source_product_id,source_variant_id,title,currency) VALUES(:sk,:pk,:m,'external_seed',
                :source_product,:vid,'Test','USD')""",
                {
                    "sk": pk + "::v::" + variant["variant_id"],
                    "pk": pk,
                    "m": merchant,
                    "vid": variant["variant_id"],
                    "source_product": pk,
                },
            )
        await live.execute(
            """INSERT INTO catalog_offers(offer_id,sku_key,product_key,merchant_id,currency,
            market,source_domain,offer_payload) VALUES(:id,:sk,:pk,:m,'USD','US','brand.example',CAST(:payload AS jsonb))""",
            {
                "id": pk + "-template",
                "sk": pk + "::canonical",
                "pk": pk,
                "m": merchant,
                "payload": json.dumps({"destination_url": "https://brand.example/products/test"}),
            },
        )
        monkeypatch.setattr(promoter, "database", Proxy())
        tasks.append(asyncio.create_task(mod.project_missing_variant_offers(pk, apply=True, db=Proxy(repair=True))))
        await asyncio.wait_for(locked.wait(), 5)
        tasks.append(asyncio.create_task(promoter.promote_variants_for_group(group_id="concurrent", apply=True)))
        await asyncio.wait_for(promotion_waiting.wait(), 5)
        continue_repair.set()
        repaired, promoted = await asyncio.wait_for(asyncio.gather(*tasks), 10)
        assert repaired["inserted"] == 2 and promoted.variant_offers_created == 0
        assert (
            await live.fetch_val(
                "SELECT count(*) FROM catalog_offers WHERE product_key=:pk AND source_system=:src",
                {"pk": pk, "src": mod.SOURCE},
            )
            == 2
        )
    finally:
        continue_repair.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await live.execute("DELETE FROM catalog_offers WHERE product_key=:pk", {"pk": pk})
        await live.execute("DELETE FROM catalog_skus WHERE product_key=:pk", {"pk": pk})
        await live.execute("DELETE FROM external_product_seeds WHERE id=:seed", {"seed": seed})
        await live.execute("DELETE FROM catalog_products WHERE product_key=:pk", {"pk": pk})
        await promoting.disconnect()
        await live.disconnect()

async def test_reviewed_native_money_cannot_be_replaced_by_canonical_seed_replay(db):
 from services.external_offer_dual_write import upsert_catalog_offer_from_seed_row,derive_mirror_offer_id
 data={'id':SEED,'price_amount':16,'price_currency':'USD','availability':'out_of_stock','domain':'brand.example',
       'market':'US','destination_url':'https://brand.example/products/test','seed_kind':'self'}
 await upsert_catalog_offer_from_seed_row(PK,data,merchant_id=M)
 oid=derive_mirror_offer_id(PK)
 await db.execute("UPDATE catalog_offers SET offer_payload=offer_payload||CAST(:marker AS jsonb) WHERE offer_id=:oid",
                  {'oid':oid,'marker':json.dumps({'price_repair':{'writer':'catalog_variant_price_repair_v1'}})})
 await upsert_catalog_offer_from_seed_row(PK,{**data,'price_amount':99,'availability':'in_stock'},merchant_id=M,price_read=True)
 row=await db.fetch_one("SELECT list_price,availability,offer_payload,price_checked_at FROM catalog_offers WHERE offer_id=:oid",{'oid':oid})
 assert float(row['list_price'])==16 and row['availability']=='out_of_stock' and row['price_checked_at'] is None
 assert json.loads(row['offer_payload'])['price_repair']['writer']=='catalog_variant_price_repair_v1'

async def test_expected_manifest_refuses_source_disappearance(db):
 dry=await mod.project_missing_variant_offers(PK,db=db,include_manifest=True)
 await db.execute("UPDATE external_product_seeds SET status='inactive' WHERE id=:seed",{'seed':SEED})
 with pytest.raises(RuntimeError,match='plan_changed'):
  await mod.project_missing_variant_offers(PK,db=db,apply=True,expected_plan_hash=dry['plan_sha256'])
 assert await db.fetch_val("SELECT count(*) FROM catalog_offers WHERE source_system=:src",{'src':mod.SOURCE})==0


async def test_a_projection_failure_keeps_the_groups_sku_writes(db, monkeypatch, caplog):
    """The REAL projection hits a real Postgres error after it has inserted an offer (which
    aborts the transaction it runs in). Its own transaction, a savepoint inside the group's,
    is the only isolation: the projection's insert is rolled back, the group's new SKUs commit,
    and the connection is usable for the next group. The promoter opens no savepoint of its own."""
    import logging
    from services import catalog_variant_promoter as promoter

    new_variants = [
        {"variant_id": "900000000001", "price": "16", "currency": "USD", "title": "Shade One"},
        {"variant_id": "900000000002", "price": "27", "currency": "USD", "title": "Shade Two"},
    ]
    primary = {
        "product_key": PK, "merchant_id": M, "platform": "external_seed",
        "source_product_id": "external-product", "parent_title": "Test", "source_system": mod.MIRROR,
        "catalog_track": "external_referral", "seed_data": {"snapshot": {"variants": new_variants}},
    }
    offer_inserts = []

    class Proxy:
        def __getattr__(self, name):
            return getattr(db, name)

        async def fetch_one(self, query, values=None):
            if query == promoter.SELECT_GROUP_PRIMARY_SQL:
                return primary
            return await db.fetch_one(query, values)

        async def fetch_val(self, query, values=None):
            result = await db.fetch_val(query, values)
            if query == mod.INSERT_SQL:
                offer_inserts.append(result)
                if len(offer_inserts) == 2:
                    await db.fetch_val("SELECT 1/0")  # a real error, after a real offer insert
            return result

    monkeypatch.setattr(promoter, "database", Proxy())
    with caplog.at_level(logging.WARNING, logger=promoter.logger.name):
        out = await promoter.promote_variants_for_group(group_id="projection-failure", apply=True)
    assert len(offer_inserts) == 2 and offer_inserts[0]  # the projection really wrote first
    assert out.variant_offer_projection_failed is True and out.variant_offers_created == 0
    assert out.variants_tier_held == 2 and out.skus_write_failed == 0
    assert any("variant offer projection failed" in r.getMessage() and PK in r.getMessage()
               and "DivisionByZeroError" in r.getMessage() for r in caplog.records)
    # The group's own writes are in, the projection's are not, and the connection still works.
    assert await db.fetch_val(
        "SELECT count(*) FROM catalog_skus WHERE product_key=:pk AND source_variant_id IN ('900000000001','900000000002')",
        {"pk": PK}) == 2
    assert await db.fetch_val("SELECT count(*) FROM catalog_offers WHERE source_system=:src", {"src": mod.SOURCE}) == 0


async def test_a_switched_off_projection_writes_no_offers_from_the_promoter(db, monkeypatch):
    from services import catalog_variant_promoter as promoter

    primary = {
        "product_key": PK, "merchant_id": M, "platform": "external_seed",
        "source_product_id": "external-product", "parent_title": "Test", "source_system": mod.MIRROR,
        "catalog_track": "external_referral",
        "seed_data": {"snapshot": {"variants": [
            {"variant_id": "677289689108", "price": "16", "currency": "USD", "title": "120 mL"},
            {"variant_id": "42199434526795", "price": "27", "currency": "USD", "title": "2x120 mL"}]}},
    }

    class Proxy:
        def __getattr__(self, name):
            return getattr(db, name)

        async def fetch_one(self, query, values=None):
            if query == promoter.SELECT_GROUP_PRIMARY_SQL:
                return primary
            return await db.fetch_one(query, values)

    monkeypatch.setattr(promoter, "database", Proxy())
    monkeypatch.setenv("CATALOG_VARIANT_OFFER_PROJECTION_ENABLED", "0")
    off = await promoter.promote_variants_for_group(group_id="projection-off", apply=True)
    assert off.variant_offers_created == 0
    assert await db.fetch_val("SELECT count(*) FROM catalog_offers WHERE source_system=:src", {"src": mod.SOURCE}) == 0
    monkeypatch.delenv("CATALOG_VARIANT_OFFER_PROJECTION_ENABLED")
    on = await promoter.promote_variants_for_group(group_id="projection-on", apply=True)
    assert on.variant_offers_created == 2
