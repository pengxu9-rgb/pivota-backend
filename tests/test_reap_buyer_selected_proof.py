from datetime import datetime, timedelta, timezone
from copy import deepcopy
import pytest
from scripts.backfill_shopify_variant_ids import build_selected_variant_proofs
from services.shopify_variant_identity import verified_selected_cart_variant_id

NOW = datetime.now(timezone.utc)
IDS = ['677289689108', '42199434526795']
URL = 'https://kravebeauty.com/products/matcha-hemp-hydrating-cleanser'
VARIANTS = [{'shopify_variant_id': vid, 'variant_id': vid} for vid in IDS]
PAYLOAD = {'handle':'matcha-hemp-hydrating-cleanser', 'variants': [{'id': int(vid), 'title': title, 'available': True, 'price': price} for vid, title, price in zip(IDS, ['1 Pack - 120 mL','2 Pack - 2x120 mL'], [1600,2700])]}

def seed():
    return {'snapshot': {'variants': deepcopy(VARIANTS), 'shopify_cart_variant_proofs': build_selected_variant_proofs(VARIANTS, PAYLOAD, js_url=URL+'.js', checked_at=NOW)}}

def verify(data, selected):
    return verified_selected_cart_variant_id(data, product_urls=[URL], shop_domain='kravebeauty.com', catalog_variant_id=selected, now=NOW)

@pytest.mark.parametrize('selected', IDS)
def test_each_buyer_selection_is_proven_by_the_same_complete_fetch(selected):
    proven = verify(seed(), selected)
    assert proven.variant_id == selected and proven.scope == 'named_variant'

@pytest.mark.parametrize('failure', ['stale','foreign_host','foreign_handle','unavailable','wrong_id','duplicate_snapshot','snapshot_contradiction'])
def test_selected_proof_fails_closed(failure):
    data=seed(); proof=data['snapshot']['shopify_cart_variant_proofs'][IDS[0]]
    if failure=='stale': proof['checked_at']=(NOW-timedelta(days=8)).isoformat()
    elif failure=='foreign_host': proof['product_js_url']='https://other.com/products/matcha-hemp-hydrating-cleanser.js'
    elif failure=='foreign_handle': proof['product_js_url']='https://kravebeauty.com/products/other.js'
    elif failure=='unavailable': proof['available']=False
    elif failure=='wrong_id': proof['variant_id']=IDS[1]
    elif failure=='duplicate_snapshot': data['snapshot']['variants'].append(deepcopy(VARIANTS[0]))
    elif failure=='snapshot_contradiction': data['snapshot']['variants'][0]['variant_id']=IDS[1]
    assert verify(data,IDS[0]) is None

@pytest.mark.parametrize('failure',['duplicate','malformed','no_stock','gone','wrong_handle'])
def test_writer_never_keeps_unproven_or_unavailable_selector(failure):
    payload=deepcopy(PAYLOAD)
    if failure=='duplicate': payload['variants'][1]['id']=int(IDS[0])
    elif failure=='malformed': payload['variants'][1]['id']='bogus'
    elif failure=='no_stock': payload['variants'][0]['available']=False
    elif failure=='gone': payload['variants'][0]['id']=999
    elif failure=='wrong_handle': payload['handle']='another-product'
    result=build_selected_variant_proofs(VARIANTS,payload,js_url=URL+'.js',checked_at=NOW)
    assert IDS[0] not in result
