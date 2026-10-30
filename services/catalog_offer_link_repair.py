"""Withdraw live offers with invalid physical catalog links, without seller deduplication."""
import hashlib
import json
from collections import Counter

SOURCE = 'catalog_offer_link_repair_v1'
INVALID_LINK = """NOT EXISTS (SELECT 1 FROM catalog_products p JOIN catalog_skus s
 ON s.product_key=p.product_key WHERE p.product_key=o.product_key AND s.sku_key=o.sku_key
 AND p.suppressed_at IS NULL AND p.suppression_reason IS NULL
 AND s.suppressed_at IS NULL AND s.suppression_reason IS NULL)"""
SCAN_SQL = """SELECT o.offer_id,o.product_key,o.sku_key,o.source_system,o.updated_at,
 CASE WHEN p.product_key IS NULL THEN 'product_absent'
 WHEN p.suppressed_at IS NOT NULL OR p.suppression_reason IS NOT NULL THEN 'product_suppressed'
 WHEN s.sku_key IS NULL THEN 'sku_absent'
 WHEN s.product_key<>o.product_key THEN 'sku_product_mismatch' ELSE 'sku_suppressed' END AS reason
 FROM catalog_offers o LEFT JOIN catalog_products p ON p.product_key=o.product_key
 LEFT JOIN catalog_skus s ON s.sku_key=o.sku_key
 WHERE o.suppressed_at IS NULL AND o.suppression_reason IS NULL AND """ + INVALID_LINK + " ORDER BY o.offer_id"
LOCK_PRODUCTS_SQL = "SELECT product_key FROM catalog_products WHERE product_key=ANY(:keys) ORDER BY product_key FOR UPDATE"
LOCK_SKUS_SQL = "SELECT sku_key FROM catalog_skus WHERE sku_key=ANY(:keys) ORDER BY sku_key FOR UPDATE"
LOCK_OFFERS_SQL = "SELECT offer_id FROM catalog_offers WHERE offer_id=ANY(:keys) ORDER BY offer_id FOR UPDATE"
UPDATE_SQL = """UPDATE catalog_offers o SET suppressed_at=NOW(),
 suppression_reason=:reason,
 suppression_metadata=COALESCE(suppression_metadata,CAST('{}' AS jsonb))||CAST(:metadata AS jsonb),
 updated_at=NOW()
 WHERE offer_id=:offer_id AND product_key=:product_key AND sku_key=:sku_key
 AND source_system IS NOT DISTINCT FROM :source_system
 AND updated_at IS NOT DISTINCT FROM :updated_at
 AND suppressed_at IS NULL AND suppression_reason IS NULL AND """ + INVALID_LINK + " RETURNING offer_id"


def plan_hash(rows):
    return hashlib.sha256(json.dumps([dict(r) for r in rows],sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()


async def repair_invalid_offer_links(db, *, apply=False, expected_hash=None):
    async with db.transaction():
        rows=[dict(r) for r in await db.fetch_all(SCAN_SQL)]
        if apply:
            # Same order as the owning product/variant writers. Lock and recompute
            # before touching any offer; a drifted manifest aborts the entire batch.
            await db.fetch_all(LOCK_PRODUCTS_SQL,{'keys':sorted({r['product_key'] for r in rows})})
            await db.fetch_all(LOCK_SKUS_SQL,{'keys':sorted({r['sku_key'] for r in rows})})
            await db.fetch_all(LOCK_OFFERS_SQL,{'keys':[r['offer_id'] for r in rows]})
            rows=[dict(r) for r in await db.fetch_all(SCAN_SQL)]
        digest=plan_hash(rows)
        if apply and (not expected_hash or expected_hash!=digest):
            raise RuntimeError('catalog_link_repair_plan_changed')
        applied=0
        if apply:
            for row in rows:
                reason=SOURCE+':'+row['reason']
                metadata=json.dumps({'writer':SOURCE,'plan_sha256':digest,'link_failure':row['reason']})
                values={k:v for k,v in row.items() if k!='reason'}
                values.update(reason=reason,metadata=metadata)
                if not await db.fetch_val(UPDATE_SQL,values):
                    raise RuntimeError('catalog_link_repair_row_changed')
                applied+=1
            if rows:
                await db.execute("""INSERT INTO writer_audit_log(writer_name,batch_id,dry_run_report_hash,
 applied_rows,skipped_rows,reasons,actor) VALUES(:writer,:batch,:digest,:applied,0,CAST(:reasons AS jsonb),:writer)""",
                {'writer':SOURCE,'batch':SOURCE+':'+digest[:32],'digest':digest,'applied':applied,
                 'reasons':json.dumps(dict(Counter(r['reason'] for r in rows)))})
        return {'kind':SOURCE,'apply':apply,'plan_sha256':digest,'planned':len(rows),'applied':applied,
                'reasons':dict(Counter(r['reason'] for r in rows)),
                'source_counts':dict(Counter(r['source_system'] for r in rows)),
                'manifest':[{k:v for k,v in r.items() if k!='updated_at'} for r in rows],
                'merchant_calls':False,'freshness_changed':False,'checkout_enabled':False}
