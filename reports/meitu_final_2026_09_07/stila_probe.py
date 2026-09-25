import asyncio, json, os, sys
sys.path.insert(0, "/app")
from db.database import database

OUT = {}

async def q(name, sql, params=None):
    try:
        rows = await database.fetch_all(sql, params or {})
        OUT[name] = [dict(r) for r in rows]
    except Exception as e:
        OUT[name] = {"error": f"{type(e).__name__}: {e}"[:400]}

STILA_PRED = "(lower(COALESCE(p.source_domain,'')) LIKE '%stilacosmetics%' OR lower(COALESCE(p.brand,'')) LIKE '%stila%')"
SEPS = [".", ",", "-", "'", "+", "/", "&", "(", ")"]


def _pad(col):
    expr = "LOWER(COALESCE(%s, ''))" % col
    for sep in SEPS:
        lit = "''''" if sep == "'" else "'" + sep + "'"
        expr = "replace(%s, %s, ' ')" % (expr, lit)
    return "(' ' || " + expr + " || ' ')"


PADB = _pad("p.brand")
PADM = _pad("m.merchant_name")
PADT = _pad("p.title")


def admit(term):
    return "(%s LIKE '%% %s %%' OR %s LIKE '%% %s %%')" % (PADB, term, PADM, term)


def score_anchor(term):
    return "(%s LIKE '%% %s %%' OR %s LIKE '%% %s %%' OR %s LIKE '%% %s %%')" % (
        PADB, term, PADM, term, PADT, term)


async def main():
    await database.connect()

    await q("merchants", """
      SELECT merchant_id, merchant_name, status, indexable, primary_platform,
             (SELECT count(*) FROM catalog_products p WHERE p.merchant_id = m.merchant_id) AS products
      FROM catalog_merchants m
      WHERE lower(COALESCE(merchant_name,'')) LIKE '%stila%'
         OR lower(COALESCE(merchant_id,'')) LIKE '%stila%'
    """)

    await q("stila_census", """
      SELECT COALESCE(p.source_domain,'(null)') AS source_domain,
             COALESCE(p.source_system,'(null)') AS source_system,
             COALESCE(p.sync_status,'(null)') AS sync_status,
             COALESCE(p.pdp_lifecycle_stage,'(null)') AS stage,
             COALESCE(p.pdp_scope,'(null)') AS pdp_scope,
             (p.suppressed_at IS NOT NULL) AS supp_at,
             (p.suppression_reason IS NOT NULL) AS supp_reason,
             (NULLIF(TRIM(COALESCE(p.category_path,'')),'') IS NULL) AS no_category_path,
             (SUBSTR(COALESCE(p.pivota_signature_id,''),1,4)='sig_') AS has_sig,
             count(*) AS n
      FROM catalog_products p
      WHERE """ + STILA_PRED + """
      GROUP BY 1,2,3,4,5,6,7,8,9 ORDER BY n DESC
    """)

    await q("stila_suppression_reasons", """
      SELECT COALESCE(p.suppression_reason,'(null)') AS reason,
             COALESCE(p.suppression_metadata->>'script','(none)') AS script,
             count(*) AS n, min(p.suppressed_at) AS first_at, max(p.suppressed_at) AS last_at
      FROM catalog_products p WHERE """ + STILA_PRED + """
      GROUP BY 1,2 ORDER BY n DESC
    """)

    await q("stila_category_paths", """
      SELECT COALESCE(p.category_path,'(null)') AS category_path, count(*) AS n
      FROM catalog_products p WHERE """ + STILA_PRED + """ GROUP BY 1 ORDER BY n DESC LIMIT 25
    """)

    await q("stila_titles", """
      SELECT p.product_key, p.content_key, p.title, p.brand, p.product_type, p.category_path,
             p.sync_status, p.pdp_lifecycle_stage, p.pdp_scope, p.suppressed_at, p.suppression_reason,
             p.source_domain, p.source_system, p.pivota_signature_id, p.updated_at,
             (SELECT count(*) FROM catalog_skus s WHERE s.product_key=p.product_key) AS skus,
             (SELECT count(*) FROM catalog_skus s WHERE s.product_key=p.product_key AND s.suppressed_at IS NULL AND s.suppression_reason IS NULL) AS live_skus,
             (SELECT count(*) FROM catalog_offers o WHERE o.product_key=p.product_key) AS offers,
             (SELECT count(*) FROM catalog_offers o WHERE o.product_key=p.product_key AND o.suppressed_at IS NULL) AS live_offers
      FROM catalog_products p
      WHERE """ + STILA_PRED + """ AND lower(COALESCE(p.title,'')) LIKE '%stay all day%'
      ORDER BY p.title LIMIT 40
    """)

    await q("stila_sku_offer_totals", """
      SELECT count(DISTINCT p.product_key) AS products,
             count(DISTINCT s.sku_key) AS skus,
             count(DISTINCT CASE WHEN s.suppressed_at IS NULL AND s.suppression_reason IS NULL THEN s.sku_key END) AS live_skus,
             count(DISTINCT o.offer_id) AS offers,
             count(DISTINCT CASE WHEN o.suppressed_at IS NULL THEN o.offer_id END) AS live_offers,
             count(DISTINCT CASE WHEN o.suppressed_at IS NULL AND COALESCE(o.merchant_effective_price, o.list_price, 0) > 0 THEN o.offer_id END) AS priced_live_offers
      FROM catalog_products p
      LEFT JOIN catalog_skus s ON s.product_key = p.product_key
      LEFT JOIN catalog_offers o ON o.product_key = p.product_key
      WHERE """ + STILA_PRED + """
    """)

    await q("stila_ips", """
      SELECT COALESCE(i.pipeline_stage,'(null)') AS pipeline_stage,
             COALESCE(i.blocker_code,'(null)') AS blocker_code,
             i.serving_eligible, i.index_eligible, count(*) AS n,
             round(avg(COALESCE(i.content_quality_score,0))::numeric,1) AS avg_q
      FROM catalog_products p JOIN index_pipeline_state i ON i.content_key = p.content_key
      WHERE """ + STILA_PRED + """ GROUP BY 1,2,3,4 ORDER BY n DESC
    """)
    await q("stila_ips_missing", """
      SELECT count(DISTINCT p.content_key) AS content_keys_total,
             count(DISTINCT CASE WHEN i.content_key IS NULL THEN p.content_key END) AS content_keys_without_ips
      FROM catalog_products p LEFT JOIN index_pipeline_state i ON i.content_key = p.content_key
      WHERE """ + STILA_PRED + """
    """)
    await q("stila_trust", """
      SELECT COALESCE(t.serving_decision,'(null)') AS serving_decision,
             COALESCE(CAST(t.serving_reason_codes AS TEXT),'(null)') AS reasons, count(*) AS n
      FROM catalog_products p LEFT JOIN catalog_row_trust t ON t.product_key = p.product_key
      WHERE """ + STILA_PRED + """ GROUP BY 1,2 ORDER BY n DESC LIMIT 20
    """)
    await q("stila_apv", """
      SELECT count(DISTINCT p.content_key) AS ck,
             count(DISTINCT CASE WHEN v.content_key IS NOT NULL THEN p.content_key END) AS with_apv,
             count(DISTINCT CASE WHEN COALESCE(length(v.description),0) >= 200 THEN p.content_key END) AS apv_desc_200,
             count(DISTINCT CASE WHEN COALESCE(v.image_url,'') <> '' THEN p.content_key END) AS apv_image,
             max(v.updated_at) AS apv_last_updated, min(v.updated_at) AS apv_first_updated
      FROM catalog_products p LEFT JOIN agent_pdp_view v ON v.content_key = p.content_key
      WHERE """ + STILA_PRED + """
    """)

    for tbl in ("catalog_products", "catalog_skus", "catalog_offers"):
        await q("withdraw_blast_" + tbl, """
          SELECT COALESCE(suppression_metadata->>'reason','(none)') AS reason, count(*) AS n,
                 min(suppressed_at) AS first_at, max(suppressed_at) AS last_at
          FROM """ + tbl + """ WHERE suppression_metadata->>'script' = 'withdraw_catalog_rows'
          GROUP BY 1 ORDER BY n DESC
        """)
    await q("withdraw_products_detail", """
      SELECT product_key, title, brand, source_domain, suppressed_at, suppression_reason
      FROM catalog_products WHERE suppression_metadata->>'script' = 'withdraw_catalog_rows'
      ORDER BY suppressed_at DESC LIMIT 30
    """)

    await q("control_brands", """
      SELECT lower(COALESCE(p.brand,'(null)')) AS brand,
             COALESCE(p.source_system,'(null)') AS source_system,
             COALESCE(p.sync_status,'(null)') AS sync_status,
             COALESCE(p.pdp_lifecycle_stage,'(null)') AS stage,
             count(*) AS n,
             count(*) FILTER (WHERE NULLIF(TRIM(COALESCE(p.category_path,'')),'') IS NOT NULL) AS with_category_path,
             count(*) FILTER (WHERE p.category_path LIKE 'beauty/makeup/lip/%') AS lip_category,
             count(*) FILTER (WHERE p.suppressed_at IS NOT NULL OR p.suppression_reason IS NOT NULL) AS suppressed
      FROM catalog_products p
      WHERE lower(COALESCE(p.brand,'')) LIKE ANY (ARRAY['stila%','flower beauty%','flowerbeauty%','m%a%c','mac','tarte%','lord & berry%','clarins%','pixi%','fenty%','kylie%'])
      GROUP BY 1,2,3,4 ORDER BY n DESC LIMIT 60
    """)

    await q("stila_recall_doors", """
      SELECT
        count(*) AS stila_sku_rows,
        count(*) FILTER (WHERE lower(COALESCE(p.title,'')) LIKE :ql
                            OR lower(COALESCE(p.brand,'')) LIKE :ql
                            OR lower(COALESCE(m.merchant_name,'')) LIKE :ql
                            OR lower(COALESCE(s.sku,'')) LIKE :ql
                            OR lower(COALESCE(s.title,'')) LIKE :ql
                            OR lower(COALESCE(s.source_variant_id,'')) LIKE :ql
                            OR lower(COALESCE(p.source_product_id,'')) LIKE :ql) AS door_phrase,
        count(*) FILTER (WHERE p.category_path IS NOT NULL AND p.category_path LIKE :cp) AS door_category,
        count(*) FILTER (WHERE """ + admit('stila') + " AND " + admit('stay') + " AND " + admit('all') + " AND " + admit('day') + """) AS door_brand_admit_identity_only,
        count(*) FILTER (WHERE """ + score_anchor('stila') + " AND " + score_anchor('stay') + " AND " + score_anchor('all') + " AND " + score_anchor('day') + """) AS score_anchor_would_match,
        count(*) FILTER (WHERE p.sync_status = 'live') AS gate_sync_live,
        count(*) FILTER (WHERE p.pdp_lifecycle_stage IN ('validated','published') OR p.pdp_lifecycle_stage IS NULL) AS gate_lifecycle,
        count(*) FILTER (WHERE p.suppressed_at IS NULL AND p.suppression_reason IS NULL) AS gate_prod_unsuppressed,
        count(*) FILTER (WHERE s.suppressed_at IS NULL AND s.suppression_reason IS NULL) AS gate_sku_unsuppressed,
        count(*) FILTER (WHERE lower(COALESCE(m.status,'active')) <> 'inactive') AS gate_merchant_active,
        count(*) FILTER (WHERE COALESCE(m.indexable, TRUE) IS TRUE) AS gate_merchant_indexable,
        count(*) FILTER (WHERE EXISTS (SELECT 1 FROM catalog_offers o WHERE o.sku_key = s.sku_key AND o.suppressed_at IS NULL)) AS gate_has_live_offer
      FROM catalog_products p
      JOIN catalog_skus s ON s.product_key = p.product_key
      LEFT JOIN catalog_merchants m ON m.merchant_id = p.merchant_id
      WHERE """ + STILA_PRED + """
    """, {"ql": "%stila stay all day liquid lipstick%", "cp": "beauty/makeup/lip/%"})

    await q("stila_survivors_all_gates", """
      SELECT p.title, p.brand, p.category_path, p.pdp_lifecycle_stage, count(*) AS sku_rows
      FROM catalog_products p
      JOIN catalog_skus s ON s.product_key = p.product_key
      LEFT JOIN catalog_merchants m ON m.merchant_id = p.merchant_id
      WHERE """ + STILA_PRED + """
        AND p.sync_status='live'
        AND (p.pdp_lifecycle_stage IN ('validated','published') OR p.pdp_lifecycle_stage IS NULL)
        AND p.suppressed_at IS NULL AND p.suppression_reason IS NULL
        AND s.suppressed_at IS NULL AND s.suppression_reason IS NULL
        AND lower(COALESCE(m.status,'active')) <> 'inactive'
        AND COALESCE(m.indexable, TRUE) IS TRUE
        AND EXISTS (SELECT 1 FROM catalog_offers o WHERE o.sku_key = s.sku_key AND o.suppressed_at IS NULL)
      GROUP BY 1,2,3,4 ORDER BY sku_rows DESC LIMIT 30
    """)

    await q("live_recall_top", """
      WITH matched AS (
        SELECT p.product_key, p.title, p.brand, m.merchant_name, p.category_path,
               p.pdp_lifecycle_stage, p.pdp_scope, s.sku_key,
               (CASE WHEN p.pdp_scope='multi_merchant_canonical' THEN 200 ELSE 0 END
                + CASE WHEN lower(COALESCE(p.title,'')) LIKE :ql THEN 90 ELSE 0 END
                + CASE WHEN lower(COALESCE(p.brand,'')) LIKE :ql THEN 70 ELSE 0 END
                + CASE WHEN p.pdp_lifecycle_stage='published' THEN 60 ELSE 0 END
                + CASE WHEN p.pdp_lifecycle_stage='validated' THEN 20 ELSE 0 END
                + CASE WHEN p.category_path IS NOT NULL AND p.category_path LIKE :cp THEN 90 ELSE 0 END
               ) AS rank_score
        FROM catalog_products p
        JOIN catalog_skus s ON s.product_key = p.product_key
        LEFT JOIN catalog_merchants m ON m.merchant_id = p.merchant_id
        WHERE (lower(COALESCE(p.title,'')) LIKE :ql OR lower(COALESCE(p.brand,'')) LIKE :ql
               OR lower(COALESCE(m.merchant_name,'')) LIKE :ql OR lower(COALESCE(s.sku,'')) LIKE :ql
               OR lower(COALESCE(s.title,'')) LIKE :ql OR lower(COALESCE(s.source_variant_id,'')) LIKE :ql
               OR lower(COALESCE(p.source_product_id,'')) LIKE :ql
               OR (p.category_path IS NOT NULL AND p.category_path LIKE :cp))
          AND p.sync_status='live'
          AND (p.pdp_lifecycle_stage IN ('validated','published') OR p.pdp_lifecycle_stage IS NULL)
          AND lower(COALESCE(m.status,'active')) <> 'inactive'
          AND p.suppressed_at IS NULL AND p.suppression_reason IS NULL
          AND COALESCE(m.indexable, TRUE) IS TRUE
          AND s.suppressed_at IS NULL AND s.suppression_reason IS NULL
      )
      SELECT c.brand, c.title, c.merchant_name, c.pdp_lifecycle_stage, c.category_path, c.rank_score
      FROM matched c JOIN catalog_offers o ON o.sku_key = c.sku_key AND o.suppressed_at IS NULL
      ORDER BY c.rank_score DESC, c.title LIMIT 25
    """, {"ql": "%stila stay all day liquid lipstick%", "cp": "beauty/makeup/lip/%"})

    await q("category_door_size", """
      SELECT count(*) AS sku_rows, count(DISTINCT p.product_key) AS products
      FROM catalog_products p
      JOIN catalog_skus s ON s.product_key = p.product_key
      WHERE p.category_path LIKE :cp AND p.sync_status='live'
        AND p.suppressed_at IS NULL AND p.suppression_reason IS NULL
        AND s.suppressed_at IS NULL AND s.suppression_reason IS NULL
    """, {"cp": "beauty/makeup/lip/%"})

    await database.disconnect()
    print("<<<JSON>>>" + json.dumps(OUT, default=str) + "<<<END>>>")

asyncio.run(main())
