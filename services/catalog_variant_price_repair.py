"""Repair mirror money from exact stored native variant evidence; never infer an FX rate."""
import collections
import json
import math
import re
from services.catalog_variant_offer_projection import as_json,listing_identity,own_availability
from services.catalog_enrichment_agent.ingestion import variant_own_price
from services.offer_seller_identity import normalize_host
from services.region_pricing import require_market_currency
from services.catalog_offer_link_repair import plan_hash

SOURCE='catalog_variant_price_repair_v1'
OWNERS={'external_product_seeds_mirror_v1','variant_identity_backfill_v1'}
PRODUCTS_SQL="""SELECT DISTINCT p.product_key FROM catalog_products p JOIN catalog_skus s ON s.product_key=p.product_key
 WHERE p.source_system='external_product_seeds_mirror_v1' AND p.suppressed_at IS NULL AND p.suppression_reason IS NULL
 AND s.suppressed_at IS NULL AND s.suppression_reason IS NULL AND s.sku_key NOT LIKE '%::canonical'
 AND EXISTS(SELECT 1 FROM catalog_offers o WHERE o.product_key=p.product_key AND o.sku_key=s.sku_key
 AND o.suppressed_at IS NULL AND o.suppression_reason IS NULL)
 AND NOT EXISTS(SELECT 1 FROM catalog_offers o WHERE o.product_key=p.product_key AND o.sku_key=s.sku_key
 AND o.suppressed_at IS NULL AND o.suppression_reason IS NULL AND o.currency=s.currency
 AND COALESCE(o.merchant_effective_price,o.estimated_best_price,o.list_price)>0) ORDER BY p.product_key"""
PRODUCT_SQL="""SELECT product_key,merchant_id,source_product_id,source_domain,source_ref,readiness_tier,catalog_track,updated_at
 FROM catalog_products WHERE product_key=:pk AND source_system='external_product_seeds_mirror_v1'
 AND suppressed_at IS NULL AND suppression_reason IS NULL"""
SEED_SQL="""SELECT id,attached_product_key,status,seller_ref,domain,canonical_url,destination_url,market,seed_data,updated_at
 FROM external_product_seeds WHERE id::text=:id AND attached_product_key=:pk AND status='active'"""
SKU_SQL="""SELECT sku_key,source_variant_id,merchant_id,platform,currency,sku_payload,readiness_tier,updated_at FROM catalog_skus
 WHERE product_key=:pk AND merchant_id=:merchant AND platform='external_seed'
 AND suppressed_at IS NULL AND suppression_reason IS NULL AND sku_key NOT LIKE '%::canonical' ORDER BY sku_key"""
OFFERS_SQL="""SELECT offer_id,sku_key,product_key,merchant_id,currency,market,source_system,source_domain,
 list_price,merchant_effective_price,estimated_best_price,availability,offer_payload,price_checked_at,updated_at
 FROM catalog_offers WHERE product_key=:pk AND suppressed_at IS NULL AND suppression_reason IS NULL ORDER BY offer_id"""
SKU_UPDATE_SQL="""UPDATE catalog_skus SET currency=:currency,
 sku_payload=COALESCE(sku_payload,CAST('{}' AS jsonb))||CAST(:metadata AS jsonb),updated_at=NOW()
 WHERE sku_key=:sku_key AND product_key=:pk AND suppressed_at IS NULL AND suppression_reason IS NULL"""
OFFER_UPDATE_SQL="""UPDATE catalog_offers SET list_price=:price,merchant_effective_price=:price,
 estimated_best_price=:price,availability=:availability,
 offer_payload=COALESCE(offer_payload,CAST('{}' AS jsonb))||CAST(:metadata AS jsonb),updated_at=NOW()
 WHERE offer_id=:offer_id AND product_key=:pk AND sku_key=:sku_key
 AND currency=:currency AND suppressed_at IS NULL AND suppression_reason IS NULL RETURNING offer_id"""


def numeric_claims(v):
    claims=[]
    for key in ('variant_id','id','shopify_variant_id'):
        value=str(v.get(key) or '').strip()
        m=re.fullmatch(r'(?:gid://shopify/ProductVariant/)?([0-9]{8,})',value)
        if m:claims.append(m[1])
        elif re.fullmatch(r'(?:gid://shopify/ProductVariant/)?[0-9]+',value):return set(),False
    return set(claims),len(set(claims))<=1


def resolve_native_variant(product,sku,seed):
    data=as_json(seed['seed_data']) or {};snapshot=as_json(data.get('snapshot')) or {}
    current=data.get('variants')
    if not isinstance(current,list) or not current:return None,'current_variants_missing'
    vid=str(sku['source_variant_id']);prefix=str(product['source_product_id'])+':'
    raw=vid[len(prefix):] if vid.startswith(prefix) else vid
    raw=raw.rsplit('/',1)[-1]
    if not re.fullmatch(r'[0-9]{8,}',raw):return None,'unverified_snapshot_identity'
    snapshots=[v for v in snapshot.get('variants',[]) if isinstance(v,dict) and numeric_claims(v)==({raw},True)]
    direct=[v for v in current if isinstance(v,dict) and numeric_claims(v)==({raw},True)]
    if len(direct)==1:variant=direct[0];bridge='numeric_variant_id'
    elif len(direct)>1:return None,'ambiguous_current_variant'
    else:
        if len(snapshots)!=1:return None,'ambiguous_snapshot_variant'
        code=str(snapshots[0].get('sku') or '').strip()
        if code.lower() in {'default','canonical','offer','variant'} or re.fullmatch(r'offer[_:-]?\d+',code,re.I) or code in {product['product_key'],str(product['source_product_id'])}:
            return None,'synthetic_native_sku_code'
        if sum(str(v.get('sku') or '').strip()==code for v in snapshot.get('variants',[]) if isinstance(v,dict))!=1:
            return None,'ambiguous_snapshot_sku_code'
        matches=[v for v in current if isinstance(v,dict) and str(v.get('variant_id') or v.get('id') or '').strip()==code]
        if not code or len(matches)!=1:return None,'native_sku_bridge_missing_or_ambiguous'
        variant=matches[0];bridge='snapshot_numeric_id_to_native_sku'
        for key in ('variant_id','id','sku'):
            value=str(variant.get(key) or '').strip()
            if value and value!=code:
                numeric=re.fullmatch(r'(?:gid://shopify/ProductVariant/)?([0-9]+)',value)
                if numeric and numeric[1]!=raw:return None,'conflicting_current_numeric_identity'
                if not numeric:return None,'conflicting_current_sku_identity'
        claims,valid=numeric_claims(variant)
        if not valid or (claims and claims!={raw}):return None,'conflicting_current_numeric_identity'
    strings={str(variant.get(k) or '').strip() for k in ('variant_id','id','sku') if variant.get(k) and not re.fullmatch(r'(?:gid://shopify/ProductVariant/)?[0-9]+',str(variant[k]).strip())}
    if len(strings)>1:return None,'conflicting_current_sku_identity'
    currency=str(variant.get('price_currency') or variant.get('currency') or '').strip().upper()
    if snapshot.get('price_currency')!=currency:return None,'native_snapshot_currency_conflict'
    declared={str(variant.get(k)).strip().upper() for k in ('price_currency','currency') if variant.get(k)}
    if declared!={currency}:return None,'native_currency_alias_conflict'
    price=variant_own_price(variant)
    if price is None or not math.isfinite(price) or not (0<price<=9999999999.99) or round(price,2)<=0:return None,'native_own_price_missing'
    aliases=[variant_own_price({k:variant[k],'currency':currency}) for k in ('price_amount','price','list_price') if variant.get(k) not in (None,'')]
    if any(x is None or not math.isfinite(x) or round(x,2)!=round(price,2) for x in aliases):return None,'native_price_alias_conflict'
    return {'variant':variant,'currency':currency,'price':round(price,2),'bridge':bridge,'native_id':str(variant.get('variant_id') or variant.get('id'))},None


def plan_product(product,seed,skus,offers):
    rows=[];skips=collections.Counter()
    domain=normalize_host(product['source_domain'])
    listings={listing_identity(seed.get(k)) for k in ('canonical_url','destination_url') if seed.get(k)}
    if seed.get('seller_ref')!=product['merchant_id'] or not domain or normalize_host(seed.get('domain'))!=domain or not listings or None in listings or any(h!=domain for h,p in listings):
        return [],{'seed_listing_or_seller_mismatch':len(skus)}
    for sku in skus:
        active=[o for o in offers if o['sku_key']==sku['sku_key']]
        if not active or any(o['currency']==sku['currency'] and (o['merchant_effective_price'] or o['estimated_best_price'] or o['list_price'] or 0)>0 for o in active):continue
        if len(active)!=1:skips['multiple_active_sellers_or_markets']+=1;continue
        offer=active[0];payload=as_json(offer['offer_payload']) or {}
        if offer['source_system'] not in OWNERS or offer['merchant_id']!=product['merchant_id'] or payload.get('external_seed_id')!=str(seed['id']) or listing_identity(payload.get('destination_url') or payload.get('canonical_url')) not in listings:
            skips['offer_ownership_or_listing_mismatch']+=1;continue
        if any(value and normalize_host(value)!=domain for value in (offer.get('source_domain'),payload.get('domain'))):
            skips['offer_domain_conflict']+=1;continue
        native,reason=resolve_native_variant(product,sku,seed)
        if reason:skips[reason]+=1;continue
        if native['currency']!=offer['currency']:skips['native_offer_currency_conflict']+=1;continue
        try:require_market_currency(offer['market'],native['currency'])
        except ValueError:skips['native_offer_market_conflict']+=1;continue
        state=own_availability(native['variant'])
        availability='out_of_stock' if state=='out_of_stock' else 'unknown' if state=='unknown' else offer['availability']
        rows.append({'product_key':product['product_key'],'sku_key':sku['sku_key'],'offer_id':offer['offer_id'],
         'seed_id':str(seed['id']),'market':offer['market'],'currency':native['currency'],'price':native['price'],'availability':availability,
         'bridge':native['bridge'],'native_variant_id':native['native_id'],
         'prior_money':{'sku_currency':sku['currency'],'offer_currency':offer['currency'],'list_price':str(offer['list_price']),
          'merchant_effective_price':str(offer['merchant_effective_price']),'estimated_best_price':str(offer['estimated_best_price']),'availability':offer['availability'],'market':offer['market'],
          'price_checked_at':str(offer.get('price_checked_at')) if offer.get('price_checked_at') else None},
         'evidence_hash':plan_hash([product,seed,sku,offer])})
    return rows,dict(skips)


async def read_product(db,pk,apply=False):
    p=await db.fetch_one(PRODUCT_SQL+(' FOR UPDATE' if apply else ''),{'pk':pk})
    if not p:return [],{'product_not_live_mirror':1}
    p=dict(p);seeds=await db.fetch_all(SEED_SQL+(' FOR SHARE' if apply else ''),{'pk':pk,'id':p['source_ref']})
    if len(seeds)!=1:return [],{'active_attached_seed_missing':1}
    skus=[dict(r) for r in await db.fetch_all(SKU_SQL+(' FOR UPDATE' if apply else ''),{'pk':pk,'merchant':p['merchant_id']})]
    offers=[dict(r) for r in await db.fetch_all(OFFERS_SQL+(' FOR UPDATE' if apply else ''),{'pk':pk})]
    return plan_product(p,dict(seeds[0]),skus,offers)

async def repair_variant_prices(db,apply=False,expected_hash=None):
    async with db.transaction():
        products=await db.fetch_all(PRODUCTS_SQL);rows=[];skips=collections.Counter()
        for p in products:
            plans,reasons=await read_product(db,p['product_key'],apply)
            rows.extend(plans);skips.update(reasons)
        digest=plan_hash(rows)
        if apply and (not expected_hash or digest!=expected_hash):raise RuntimeError('variant_price_repair_plan_changed')
        applied=0
        if apply:
            for row in rows:
                metadata=json.dumps({'price_repair':{'writer':SOURCE,'plan_sha256':digest,'seed_id':row['seed_id'],
                    'native_variant_id':row['native_variant_id'],'identity_bridge':row['bridge'],'evidence_hash':row['evidence_hash'],
                    'freshness':'stored_snapshot_only','prior_money':row['prior_money']},
                    'currency':row['currency'],'price_currency':row['currency'],'price':row['price'],
                    'price_amount':row['price'],'list_price':row['price'],'merchant_effective_price':row['price'],
                    'estimated_best_price':row['price'],'availability':row['availability'],
                    **({'available':False,'stock':'out_of_stock'} if row['availability']=='out_of_stock' else {})})
                values={'pk':row['product_key'],'sku_key':row['sku_key'],'currency':row['currency'],'metadata':metadata}
                await db.execute(SKU_UPDATE_SQL,values)
                offer_metadata=json.loads(metadata);offer_metadata['market']=row['market']
                values.update(offer_id=row['offer_id'],price=row['price'],availability=row['availability'],metadata=json.dumps(offer_metadata))
                if not await db.fetch_val(OFFER_UPDATE_SQL,values):raise RuntimeError('variant_price_repair_row_changed')
                applied+=1
            if applied:
                await db.execute("""INSERT INTO writer_audit_log(writer_name,batch_id,dry_run_report_hash,applied_rows,
 skipped_rows,reasons,actor) VALUES(:writer,:batch,:digest,:applied,:skipped,CAST(:reasons AS jsonb),:writer)""",
                 {'writer':SOURCE,'batch':SOURCE+':'+digest[:32],'digest':digest,'applied':applied,'skipped':sum(skips.values()),'reasons':json.dumps(dict(skips))})
        return {'kind':SOURCE,'apply':apply,'plan_sha256':digest,'planned':len(rows),'applied':applied,
            'skips':dict(skips),'manifest':rows,'merchant_calls':False,'freshness_refreshed':False,'price_check_may_be_cleared':True,'checkout_enabled':False}
