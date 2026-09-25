import asyncio, json
from db.database import database

DOM = "%flowerbeauty%"

Q_PRODUCTS = """
SELECT count(*) AS n,
       count(*) FILTER (WHERE eps.seed_data->'snapshot'->'variants' IS NOT NULL) AS with_seed_variants,
       count(*) FILTER (WHERE cp.product_payload->'variants' IS NOT NULL) AS with_payload_variants
FROM catalog_products cp
LEFT JOIN external_product_seeds eps ON eps.external_product_id = cp.source_product_id
WHERE cp.source_domain ILIKE :d OR cp.brand ILIKE '%flower beauty%'
"""

Q_SKUS = """
WITH p AS (
  SELECT product_key FROM catalog_products
  WHERE source_domain ILIKE :d OR brand ILIKE '%flower beauty%'
)
SELECT
  CASE
    WHEN s.source_variant_id ~ '^[0-9]{8,}$' THEN 'real_numeric'
    WHEN s.source_variant_id LIKE 'gid://shopify/ProductVariant/%' THEN 'real_gid'
    WHEN s.source_variant_id = s.product_key THEN 'equals_product_key'
    WHEN s.source_variant_id LIKE 'ext:%' THEN 'ext_prefix'
    WHEN s.source_variant_id LIKE 'prod::%' THEN 'prod_prefix'
    WHEN s.source_variant_id LIKE '%-default' THEN 'suffix_default'
    ELSE 'other'
  END AS shape,
  count(*) AS n,
  count(o.offer_id) AS with_offer,
  count(*) FILTER (WHERE s.sku_key LIKE '%::v:%' AND s.sku_key NOT LIKE '%::v::%') AS ingestion_shape,
  count(*) FILTER (WHERE s.sku_key LIKE '%::v::%') AS promoter_shape,
  count(*) FILTER (WHERE s.sku_payload->>'variant_id_provenance' IS NOT NULL) AS has_provenance
FROM catalog_skus s
JOIN p ON p.product_key = s.product_key
LEFT JOIN catalog_offers o ON o.sku_key = s.sku_key
GROUP BY 1 ORDER BY 2 DESC
"""

TIER_A = ["17281773207622", "32120934727750", "39777342488646", "39406294532166"]

Q_TIER_A_SKU = """
SELECT source_variant_id, sku_key, product_key, title
FROM catalog_skus WHERE source_variant_id = ANY(:ids)
"""

Q_TIER_A_SEED = """
SELECT cp.product_key, cp.title, cp.source_product_id,
       jsonb_array_length(COALESCE(eps.seed_data->'snapshot'->'variants',
                                   cp.product_payload->'variants', '[]'::jsonb)) AS nvar,
       (SELECT jsonb_agg(v->>'variant_id')
          FROM jsonb_array_elements(COALESCE(eps.seed_data->'snapshot'->'variants',
                                             cp.product_payload->'variants', '[]'::jsonb)) v) AS vids
FROM catalog_products cp
LEFT JOIN external_product_seeds eps ON eps.external_product_id = cp.source_product_id
WHERE cp.source_domain ILIKE :d OR cp.brand ILIKE '%flower beauty%'
"""


async def main():
    await database.connect()
    out = {}
    try:
        r = await database.fetch_one(Q_PRODUCTS, {"d": DOM})
        out["products"] = dict(r) if r else None
        rows = await database.fetch_all(Q_SKUS, {"d": DOM})
        out["skus_by_shape"] = [dict(x) for x in rows]
        rows = await database.fetch_all(Q_TIER_A_SKU, {"ids": TIER_A})
        out["tier_a_skus_present"] = [dict(x) for x in rows]
        rows = await database.fetch_all(Q_TIER_A_SEED, {"d": DOM})
        seeds = [dict(x) for x in rows]
        out["n_products_listed"] = len(seeds)
        allv = []
        for s in seeds:
            for v in (s.get("vids") or []):
                if v:
                    allv.append(str(v))
        out["seed_variant_ids_total"] = len(allv)
        out["seed_variant_ids_numeric8"] = sum(1 for v in allv if v.isdigit() and len(v) >= 8)
        out["tier_a_in_seed"] = {t: (t in allv) for t in TIER_A}
        out["sample_products"] = [
            {"title": s["title"], "pk": s["product_key"], "spid": s["source_product_id"],
             "nvar": s["nvar"], "vids": (s.get("vids") or [])[:6]}
            for s in seeds[:8]
        ]
    finally:
        await database.disconnect()
    blob = json.dumps(out, default=str, separators=(",", ":"))
    # Cloud Logging drops/truncates very long lines: emit fixed-size numbered chunks,
    # each its own short line, and reassemble locally.
    CH = 700
    chunks = [blob[i:i + CH] for i in range(0, len(blob), CH)]
    print("PJQ_N %d" % len(chunks))
    for i, c in enumerate(chunks):
        print("PJQ %04d %s" % (i, c))
    print("PJQ_END")

asyncio.run(main())
