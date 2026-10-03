"""Real HTTP + owned SQL regressions for immutable selected offer money."""
import hashlib
import json
import pytest
from db.database import database, IS_POSTGRES
import routes.agent_commerce_reap as reap
from reap_selection_prepare_cases import seed, unchanged_state, prepare_seed_cleanup, body as prepare_body, DOMAIN, PRODUCT, SKU, SEED, BASE as PREPARE
if IS_POSTGRES:
    from test_agent_commerce_reap_routes_postgres import _body, _seed_catalog, _seed_eligibility, _error
else:
    from test_agent_commerce_reap_routes import _body, _seed_catalog, _seed_eligibility, _error
async def _purchase_row(purchase_id):
    return dict(await database.fetch_one("SELECT * FROM reap_agentic_purchases WHERE id=:id", {"id":purchase_id}))
BASE='/agent/v2/commerce/reap/purchases'
async def lane_body(monkeypatch,lane,**changes):
    if lane=='cart_link':
        await seed(monkeypatch)
        return _body(item_source=lane,merchant_domain=DOMAIN,product_key=PRODUCT,variant_key=SKU,**changes),1399
    await _seed_catalog();await _seed_eligibility()
    return _body(**changes),4250
async def drift(lane):
    await database.execute("UPDATE catalog_offers SET merchant_effective_price=:price",{'price':'14.99' if lane=='cart_link' else '43.50'})
    if lane=='cart_link':
        row=await database.fetch_one('SELECT seed_data FROM external_product_seeds WHERE id=:id',{'id':SEED});value=json.loads(row['seed_data']);value['snapshot']['variants'][0]['price']='14.99'
        await database.execute('UPDATE external_product_seeds SET seed_data=:value WHERE id=:id',{'value':json.dumps(value),'id':SEED})
@pytest.mark.parametrize('lane',['cart_link','reap_variant'])
async def test_expected_money_coherent_price_race_refuses_without_business_write(client,monkeypatch,lane):
    body,minor=await lane_body(monkeypatch,lane,idempotency_key='race-priced-attempt')
    if lane=='cart_link':
        selection=(await client.post(PREPARE,json=prepare_body())).json()['selection'];assert selection['unit_price_minor']==minor and selection['currency']=='USD'
    body.update(expected_unit_price_minor=minor,expected_currency='USD');await drift(lane);before=await unchanged_state();response=await client.post(BASE,json=body)
    assert response.status_code==409 and _error(response)=='price_changed',response.text
    assert await unchanged_state()==before
@pytest.mark.parametrize('lane',['cart_link','reap_variant'])
async def test_expected_money_currency_change_refuses_without_business_write(client,monkeypatch,lane):
    body,minor=await lane_body(monkeypatch,lane,idempotency_key='currency-priced-attempt');body.update(expected_unit_price_minor=minor,expected_currency='EUR');before=await unchanged_state();response=await client.post(BASE,json=body)
    assert response.status_code==409 and _error(response)=='price_changed',response.text
    assert await unchanged_state()==before
@pytest.mark.parametrize('lane',['cart_link','reap_variant'])
async def test_expected_money_success_freezes_pair_key_and_readonly_recovery(client,monkeypatch,lane):
    body,minor=await lane_body(monkeypatch,lane,idempotency_key='original-bound-money');body.update(expected_unit_price_minor=minor,expected_currency='USD');accepted=await client.post(BASE,json=body);assert accepted.status_code==202,accepted.text
    purchase=await _purchase_row(accepted.json()['purchase_id']);assert purchase['our_price_minor']==minor and purchase['currency']=='USD'
    key=await database.fetch_one('SELECT request_hash FROM reap_agentic_purchase_keys WHERE idempotency_key=:key',{'key':body['idempotency_key']})
    normalized={'merchant_domain':reap._merchant_domain_key(body['merchant_domain']),'product_key':body['product_key'],'variant_key':body['variant_key'],'quantity':body['quantity'],'email':body['buyer']['email'],'shipping_address':reap._buyer_address_for_client(reap.ReapBuyer.model_validate(body['buyer'])),'return_url':reap._default_return_url(),'item_source':body.get('item_source','reap_variant')}
    assert key['request_hash']==reap._request_hash(**normalized,expected_unit_price_minor=minor,expected_currency='USD') and key['request_hash']!=reap._request_hash(**normalized)
    await drift(lane);before=await unchanged_state();monkeypatch.setenv('REAP_AGENTIC_CREATE_ENABLED','0');monkeypatch.setenv('REAP_AGENTIC_ENABLED','0')
    async def forbidden(*a,**kw):raise AssertionError('Recovery read current selection')
    monkeypatch.setattr(reap,'_load_cart_link_item',forbidden);monkeypatch.setattr(reap,'_load_catalog_row',forbidden)
    for changed in [body,{**body,'expected_unit_price_minor':minor+1},{k:v for k,v in body.items() if not k.startswith('expected_')}]:
        response=await client.post(BASE+'/recover',json=changed)
        if changed==body:assert response.status_code==200 and response.json()['id']==purchase['id'],response.text
        else:assert response.status_code==409 and _error(response)=='idempotency_conflict',response.text
        assert await unchanged_state()==before
@pytest.mark.parametrize('lane',['cart_link','reap_variant'])
async def test_legacy_without_money_retains_original_hash_and_recovery(client,monkeypatch,lane):
    body,minor=await lane_body(monkeypatch,lane,idempotency_key='legacy-unbound-money');response=await client.post(BASE,json=body);assert response.status_code==202,response.text
    purchase=await _purchase_row(response.json()['purchase_id']);assert purchase['our_price_minor']==minor
    facts={'merchant_domain':reap._merchant_domain_key(body['merchant_domain']),'product_key':body['product_key'],'variant_key':body['variant_key'],'quantity':body['quantity'],'email':body['buyer']['email'],'shipping_address':{str(k):str(v) for k,v in reap._buyer_address_for_client(reap.ReapBuyer.model_validate(body['buyer'])).items()},'return_url':reap._default_return_url()}
    if lane=='cart_link':facts['item_source']=lane
    original=hashlib.sha256(json.dumps(facts,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()).hexdigest();key=await database.fetch_one('SELECT request_hash FROM reap_agentic_purchase_keys WHERE idempotency_key=:key',{'key':body['idempotency_key']});assert key['request_hash']==original
    before=await unchanged_state();monkeypatch.setenv('REAP_AGENTIC_ENABLED','0');monkeypatch.setenv('REAP_AGENTIC_CREATE_ENABLED','0');recovered=await client.post(BASE+'/recover',json=body);assert recovered.status_code==200 and recovered.json()['id']==purchase['id'];assert await unchanged_state()==before
@pytest.mark.parametrize('pair',[{'expected_unit_price_minor':1399},{'expected_currency':'USD'},{'expected_unit_price_minor':None,'expected_currency':None},{'expected_unit_price_minor':None,'expected_currency':'USD'},{'expected_unit_price_minor':1399,'expected_currency':None},{'expected_unit_price_minor':True,'expected_currency':'USD'},{'expected_unit_price_minor':'1399','expected_currency':'USD'},{'expected_unit_price_minor':1399.0,'expected_currency':'USD'},{'expected_unit_price_minor':0,'expected_currency':'USD'},{'expected_unit_price_minor':-1,'expected_currency':'USD'},{'expected_unit_price_minor':9007199254740992,'expected_currency':'USD'},{'expected_unit_price_minor':1399,'expected_currency':'usd'},{'expected_unit_price_minor':1399,'expected_currency':'USD\n'},{'expected_unit_price_minor':1399,'expected_currency':1}])
async def test_expected_money_strict_pair_refused_before_sql_and_recovery(client,monkeypatch,pair):
    async def forbidden(*a,**kw):raise AssertionError('Malformed money reached SQL')
    with monkeypatch.context() as patch:
        patch.setattr(database,'fetch_one',forbidden);patch.setattr(database,'fetch_all',forbidden);patch.setattr(database,'execute',forbidden)
        for path in [BASE,BASE+'/recover']:
            response=await client.post(path,json=_body(idempotency_key='malformed-money',**pair));assert response.status_code==400 and _error(response)=='invalid_request',response.text

@pytest.mark.parametrize('lane',['cart_link','reap_variant'])
async def test_money_pair_same_key_create_replay_uses_original_money_without_catalog(client,monkeypatch,lane):
    body,minor=await lane_body(monkeypatch,lane,idempotency_key='bound-create-replay');body.update(expected_unit_price_minor=minor,expected_currency='USD');first=await client.post(BASE,json=body);assert first.status_code==202,first.text
    await drift(lane)
    async def forbidden(*a,**kw):raise AssertionError('Existing key replay read current money')
    monkeypatch.setattr(reap,'_load_cart_link_item',forbidden);monkeypatch.setattr(reap,'_load_catalog_row',forbidden)
    again=await client.post(BASE,json=body);assert again.status_code==202 and again.json()['purchase_id']==first.json()['purchase_id'],again.text
    before=await unchanged_state();changed=await client.post(BASE,json={**body,'expected_currency':'EUR'});assert changed.status_code==409 and _error(changed)=='idempotency_conflict';assert await unchanged_state()==before
    assert await database.fetch_val('SELECT COUNT(*) FROM reap_agentic_purchases')==1

async def test_bound_variant_refusal_before_money_does_not_tombstone_key(client,monkeypatch):
    await _seed_catalog();body=_body(idempotency_key='bound-unapproved-merchant',expected_unit_price_minor=4250,expected_currency='USD');before=await unchanged_state();response=await client.post(BASE,json=body)
    assert response.status_code==409 and _error(response)=='merchant_not_eligible';assert await unchanged_state()==before

@pytest.mark.parametrize('lane',['cart_link','reap_variant'])
async def test_current_offer_currency_drift_after_selection_refuses_without_writes(client,monkeypatch,lane):
    body,minor=await lane_body(monkeypatch,lane,idempotency_key='coherent-currency-race');body.update(expected_unit_price_minor=minor,expected_currency='USD')
    if lane=='cart_link':assert (await client.post(PREPARE,json=prepare_body())).status_code==200
    await database.execute("UPDATE catalog_offers SET currency='EUR'")
    await database.execute("UPDATE catalog_skus SET currency='EUR'")
    before=await unchanged_state();response=await client.post(BASE,json=body)
    assert response.status_code==409 and _error(response) in {'price_changed','row_currency_mismatch','row_unpriced'},response.text
    assert await unchanged_state()==before
