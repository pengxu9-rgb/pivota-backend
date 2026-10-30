import json
import pytest
from tests.test_catalog_variant_offer_projection_postgres import db,PK,M,SEED,pytestmark
from services import catalog_variant_price_repair as repair

async def prepare(db):
 data={'snapshot':{'price_currency':'GBP','variants':[{'variant_id':'677289689108','shopify_variant_id':'677289689108','sku':'SKU-1','currency':'USD','price':4}]},
       'variants':[{'variant_id':'SKU-1','currency':'GBP','price':2.25,'available':False}]}
 await db.execute("UPDATE external_product_seeds SET seed_data=CAST(:data AS jsonb) WHERE id=:seed",{'data':json.dumps(data),'seed':SEED})
 await db.execute("ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS updated_at timestamptz DEFAULT NOW()")
 await db.execute("""INSERT INTO catalog_offers(offer_id,sku_key,product_key,merchant_id,currency,market,list_price,
 merchant_effective_price,estimated_best_price,availability,source_system,offer_payload,price_checked_at,readiness_tier)
 VALUES('money-repair-test',:sku,:pk,:m,'GBP','GB',4,4,4,'in_stock','external_product_seeds_mirror_v1',
 CAST(:payload AS jsonb),'2026-09-01T00:00:00Z','referral_only')""",{'sku':PK+'::v::677289689108','pk':PK,'m':M,
 'payload':json.dumps({'external_seed_id':SEED,'destination_url':'https://brand.example/products/test','currency':'USD','price':4})})

async def test_atomic_money_payload_stock_and_no_age_or_identity_promotion(db):
 await prepare(db)
 await db.execute("""CREATE OR REPLACE FUNCTION catalog_offers_forget_unread_price_check() RETURNS TRIGGER AS $$
 BEGIN IF NEW.price_checked_at IS NOT DISTINCT FROM OLD.price_checked_at THEN NEW.price_checked_at := NULL; END IF;
 RETURN NEW; END; $$ LANGUAGE plpgsql""")
 await db.execute("DROP TRIGGER IF EXISTS trg_catalog_offers_forget_unread_price_check ON catalog_offers")
 await db.execute("""CREATE TRIGGER trg_catalog_offers_forget_unread_price_check BEFORE UPDATE OF list_price,
 merchant_effective_price,estimated_best_price,currency ON catalog_offers FOR EACH ROW
 WHEN (OLD.list_price IS DISTINCT FROM NEW.list_price OR OLD.merchant_effective_price IS DISTINCT FROM NEW.merchant_effective_price
 OR OLD.estimated_best_price IS DISTINCT FROM NEW.estimated_best_price OR OLD.currency IS DISTINCT FROM NEW.currency)
 EXECUTE FUNCTION catalog_offers_forget_unread_price_check()""")
 dry=await repair.repair_variant_prices(db);assert dry['planned']==1 and dry['applied']==0
 applied=await repair.repair_variant_prices(db,apply=True,expected_hash=dry['plan_sha256']);assert applied['applied']==1
 o=await db.fetch_one("SELECT * FROM catalog_offers WHERE offer_id='money-repair-test'")
 s=await db.fetch_one("SELECT * FROM catalog_skus WHERE sku_key=:sku",{'sku':PK+'::v::677289689108'})
 assert (o['currency'],float(o['list_price']),o['availability'])==('GBP',2.25,'out_of_stock')
 assert s['currency']=='GBP' and s['source_variant_id']=='677289689108'
 assert o['readiness_tier']=='referral_only' and o['price_checked_at'] is None
 for payload in [json.loads(o['offer_payload']),json.loads(s['sku_payload'])]:
  assert payload['currency']=='GBP' and payload['price']==2.25 and payload['available'] is False
 assert (await repair.repair_variant_prices(db))['planned']==0

async def test_seed_drift_refuses_all_writes(db):
 await prepare(db);dry=await repair.repair_variant_prices(db)
 await db.execute("UPDATE external_product_seeds SET seed_data=jsonb_set(seed_data,'{variants,0,price}','3.25') WHERE id=:seed",{'seed':SEED})
 with pytest.raises(RuntimeError,match='plan_changed'):
  await repair.repair_variant_prices(db,apply=True,expected_hash=dry['plan_sha256'])
 assert await db.fetch_val("SELECT list_price FROM catalog_offers WHERE offer_id='money-repair-test'")==4
 assert await db.fetch_val("SELECT currency FROM catalog_skus WHERE sku_key=:sku",{'sku':PK+'::v::677289689108'})=='USD'
