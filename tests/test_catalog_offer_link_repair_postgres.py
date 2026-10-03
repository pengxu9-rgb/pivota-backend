import json
import pytest
from tests.test_catalog_variant_offer_projection_postgres import db,PK,M,pytestmark
from services import catalog_offer_link_repair as repair
from services.catalog_offer_writer_guard import guard_catalog_offer_rows

async def add_offer(db,name,sku,product=PK,merchant='independent-seller'):
    await db.execute("""INSERT INTO catalog_offers(offer_id,sku_key,product_key,merchant_id,list_price,
    source_system,currency) VALUES(:name,:sku,:pk,:merchant,16,'test-link-owner','USD')""",
    {'name':name,'sku':sku,'pk':product,'merchant':merchant})

async def test_guard_preserves_independent_seller_and_refuses_wrong_parent(db):
    sku=PK+'::v::677289689108'
    rows=[{'offer_id':'good','sku_key':sku,'product_key':PK,'merchant_id':'independent-seller','list_price':16},
          {'offer_id':'bad','sku_key':sku,'product_key':'another-product','merchant_id':M,'list_price':16}]
    accepted,reasons,_=await guard_catalog_offer_rows(rows,db=db,require_live_links=True)
    assert [r['offer_id'] for r in accepted]==['good']
    assert reasons=={'invalid_live_product_sku_link':1}
    await db.execute("UPDATE catalog_products SET suppression_reason='withdrawn' WHERE product_key=:pk",{'pk':PK})
    assert not (await guard_catalog_offer_rows(rows[:1],db=db,require_live_links=True))[0]

async def test_exact_plan_withdraws_bad_links_without_touching_valid_sellers(db):
    await add_offer(db,'link-missing-sku','absent')
    await add_offer(db,'link-wrong-parent',PK+'::v::677289689108','absent-product')
    await add_offer(db,'link-valid-seller',PK+'::v::677289689108')
    dry=await repair.repair_invalid_offer_links(db)
    assert dry['applied']==0
    assert {'link-missing-sku','link-wrong-parent'} <= {r['offer_id'] for r in dry['manifest']}
    result=await repair.repair_invalid_offer_links(db,apply=True,expected_hash=dry['plan_sha256'])
    assert result['applied']==dry['planned']
    assert await db.fetch_val("SELECT suppressed_at IS NULL AND suppression_reason IS NULL FROM catalog_offers WHERE offer_id='link-valid-seller'")
    assert (await repair.repair_invalid_offer_links(db))['planned']==0
    row=await db.fetch_one("SELECT suppression_metadata FROM catalog_offers WHERE offer_id='link-missing-sku'")
    assert json.loads(row['suppression_metadata'])['plan_sha256']==dry['plan_sha256']
    assert await db.fetch_val("SELECT sum(applied_rows) FROM writer_audit_log WHERE writer_name=:writer",{'writer':repair.SOURCE})==result['applied']

async def test_plan_drift_aborts_without_partial_withdrawals(db):
    await add_offer(db,'link-drift','absent')
    dry=await repair.repair_invalid_offer_links(db)
    await db.execute("UPDATE catalog_offers SET updated_at=NOW(),list_price=19 WHERE offer_id='link-drift'")
    # NOW() is transaction-stable, so explicitly change one manifest identity.
    await db.execute("UPDATE catalog_offers SET sku_key='another-absent' WHERE offer_id='link-drift'")
    with pytest.raises(RuntimeError,match='plan_changed'):
        await repair.repair_invalid_offer_links(db,apply=True,expected_hash=dry['plan_sha256'])
    assert await db.fetch_val("SELECT suppressed_at IS NULL FROM catalog_offers WHERE offer_id='link-drift'")

async def test_reason_only_sku_suppression_is_invalid(db):
    sku=PK+'::v::677289689108'
    await add_offer(db,'link-reason-only',sku)
    await db.execute("UPDATE catalog_skus SET suppression_reason='withdrawn' WHERE sku_key=:sku",{'sku':sku})
    dry=await repair.repair_invalid_offer_links(db)
    assert next(r for r in dry['manifest'] if r['offer_id']=='link-reason-only')['reason']=='sku_suppressed'

async def test_native_persistence_boundary_refuses_inactive_or_mismatched_parent(db):
 from services.catalog_sync_service import _upsert_by_pk
 from db.catalog import catalog_offers
 values={'offer_id':'native-link-boundary','sku_key':PK+'::v::677289689108','product_key':PK,
         'merchant_id':'independent-seller','list_price':16,'currency':'USD'}
 await _upsert_by_pk(catalog_offers,'offer_id',values)
 assert await db.fetch_val("SELECT count(*) FROM catalog_offers WHERE offer_id='native-link-boundary'")==1
 with pytest.raises(RuntimeError,match='invalid_live_catalog_offer_link'):
  await _upsert_by_pk(catalog_offers,'offer_id',{**values,'offer_id':'native-wrong-parent','product_key':'wrong'})
 await db.execute("UPDATE catalog_products SET suppression_reason='withdrawn' WHERE product_key=:pk",{'pk':PK})
 with pytest.raises(RuntimeError,match='invalid_live_catalog_offer_link'):
  await _upsert_by_pk(catalog_offers,'offer_id',{**values,'offer_id':'native-inactive-parent'})
