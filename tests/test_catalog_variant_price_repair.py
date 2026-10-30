import copy
import pytest
from services.catalog_variant_price_repair import plan_product,resolve_native_variant
P={'product_key':'p','merchant_id':'m','source_product_id':'ext_123','source_domain':'brand.example','source_ref':'seed'}
S={'sku_key':'s','source_variant_id':'ext_123:123456789','currency':'USD','sku_payload':{}}
E={'id':'seed','seller_ref':'m','domain':'brand.example','canonical_url':'https://brand.example/products/p','destination_url':'https://brand.example/products/p',
 'seed_data':{'snapshot':{'price_currency':'GBP','variants':[{'variant_id':'123456789','shopify_variant_id':'123456789','sku':'SKU-1','price':4,'currency':'USD'}]},
 'variants':[{'variant_id':'SKU-1','price_amount':2.25,'price_currency':'GBP','availability':'out_of_stock'}]}}
O={'offer_id':'o','sku_key':'s','product_key':'p','merchant_id':'m','currency':'GBP','market':'GB',
 'source_system':'external_product_seeds_mirror_v1','offer_payload':{'external_seed_id':'seed','destination_url':'https://brand.example/products/p'},
 'list_price':4,'merchant_effective_price':4,'estimated_best_price':4,'availability':'in_stock'}

def test_exact_bridge_uses_native_own_money_and_withdraws_stock():
 rows,skips=plan_product(P,E,[S],[O]);assert not skips and len(rows)==1
 assert (rows[0]['price'],rows[0]['currency'],rows[0]['availability'])==(2.25,'GBP','out_of_stock')
 assert rows[0]['bridge']=='snapshot_numeric_id_to_native_sku'

@pytest.mark.parametrize('mutation,reason',[
 ('conflicting_alias','ambiguous_snapshot_variant'),('duplicate_native','native_sku_bridge_missing_or_ambiguous'),
 ('wrong_currency','native_snapshot_currency_conflict'),('zero_price','native_own_price_missing'),
 ('parent_price_only','native_own_price_missing'),('truncated_id','unverified_snapshot_identity')])
def test_conflicting_or_missing_evidence_refused(mutation,reason):
 e=copy.deepcopy(E);s=copy.deepcopy(S)
 if mutation=='conflicting_alias':e['seed_data']['snapshot']['variants'][0]['shopify_variant_id']='987654321'
 elif mutation=='duplicate_native':e['seed_data']['variants'].append(copy.deepcopy(e['seed_data']['variants'][0]))
 elif mutation=='wrong_currency':e['seed_data']['variants'][0]['price_currency']='USD'
 elif mutation=='zero_price':e['seed_data']['variants'][0]['price_amount']=0
 elif mutation=='parent_price_only':e['seed_data']['price_amount']=99;del e['seed_data']['variants'][0]['price_amount']
 else:s['source_variant_id']='own-derived-id'
 assert resolve_native_variant(P,s,e)==(None,reason)

def test_multiple_active_offers_are_preserved_as_ambiguous():
 rows,skips=plan_product(P,E,[S],[O,{**O,'offer_id':'another-seller','merchant_id':'other'}])
 assert not rows and skips=={'multiple_active_sellers_or_markets':1}

def test_repair_never_promotes_old_availability():
 e=copy.deepcopy(E);e['seed_data']['variants'][0]['availability']='in_stock'
 rows,_=plan_product(P,e,[S],[{**O,'availability':'out_of_stock'}]);assert rows[0]['availability']=='out_of_stock'

@pytest.mark.parametrize("mutation,reason",[("shared_snapshot_code","ambiguous_snapshot_sku_code"),
 ("conflicting_current_id","conflicting_current_numeric_identity"),("aggregate_offer_code","synthetic_native_sku_code")])
def test_adversarial_bridge_counterexamples_refused(mutation,reason):
 e=copy.deepcopy(E)
 if mutation=="shared_snapshot_code":e["seed_data"]["snapshot"]["variants"].append({"variant_id":"987654321","sku":"SKU-1"})
 elif mutation=="conflicting_current_id":e["seed_data"]["variants"][0]["shopify_variant_id"]="987654321"
 else:e["seed_data"]["snapshot"]["variants"][0]["sku"]="offer_1";e["seed_data"]["variants"][0]["variant_id"]="offer_1"
 assert resolve_native_variant(P,S,e)==(None,reason)

@pytest.mark.parametrize("field",["seed_domain","offer_domain","payload_domain"])
def test_declared_domain_conflicts_refused(field):
 e=copy.deepcopy(E);o=copy.deepcopy(O)
 if field=="seed_domain":e["domain"]="evil.example"
 elif field=="offer_domain":o["source_domain"]="evil.example"
 else:o["offer_payload"]["domain"]="evil.example"
 rows,skips=plan_product(P,e,[S],[o]);assert not rows and skips

@pytest.mark.parametrize("alias",["id","sku"])
def test_string_sku_identity_contradictions_refused(alias):
 e=copy.deepcopy(E);e["seed_data"]["variants"][0][alias]="OTHER-SKU"
 assert resolve_native_variant(P,S,e)==(None,"conflicting_current_sku_identity")
