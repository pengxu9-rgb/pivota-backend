import json
import os
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
