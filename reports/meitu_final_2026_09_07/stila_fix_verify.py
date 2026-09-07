"""Prove the fix on the PRODUCTION dialect and on production data. READ-ONLY (SELECT only).

The unit tests run on the SQLite harness. This runs the EXACT new candidate WHERE — the
ancestor-taxonomy door and the glyph-folded brand anchor — as a plain SELECT against prod
Postgres, so the PR carries dialect evidence (SUBSTR/LENGTH/|| behave as intended on real
`category_path` values) and outcome evidence (how many Stila rows the query now admits, and
where they land against the Fenty/Clarins rows that own the page today).
"""
import asyncio, json, sys
sys.path.insert(0, "/app")
from db.database import database

OUT = {}
SEPS = [".", ",", "-", "'", "+", "/", "&", "(", ")", "®", "™", "©", "·"]


def pad(col):
    expr = "LOWER(COALESCE(%s, ''))" % col
    for sep in SEPS:
        lit = "''''" if sep == "'" else "'" + sep + "'"
        expr = "replace(%s, %s, ' ')" % (expr, lit)
    return "(' ' || " + expr + " || ' ')"


def anchor(term):
    return "(%s LIKE '%% %s %%' OR %s LIKE '%% %s %%' OR %s LIKE '%% %s %%')" % (
        pad("p.brand"), term, pad("m.merchant_name"), term, pad("p.title"), term)


ANCHOR = " AND ".join(anchor(t) for t in ("stila", "stay", "all", "day"))
EVID = " OR ".join(
    "%s LIKE '%% lipstick %%'" % pad(f) for f in ("p.title", "p.product_type", "s.title"))
# The new door, verbatim in shape.
ANCESTOR = (
    "(NULLIF(TRIM(COALESCE(p.category_path, '')), '') IS NOT NULL"
    " AND SUBSTR(:cp_exact, 1, LENGTH(p.category_path) + 1) = p.category_path || '/'"
    " AND (" + EVID + "))"
)
GATES = """
      AND p.sync_status='live'
      AND (p.pdp_lifecycle_stage IN ('validated','published') OR p.pdp_lifecycle_stage IS NULL)
      AND lower(COALESCE(m.status,'active')) <> 'inactive'
      AND p.suppressed_at IS NULL AND p.suppression_reason IS NULL
      AND COALESCE(m.indexable, TRUE) IS TRUE
      AND s.suppressed_at IS NULL AND s.suppression_reason IS NULL
"""
SCORE = """
   (CASE WHEN p.pdp_scope='multi_merchant_canonical' THEN 200 ELSE 0 END
    + CASE WHEN lower(COALESCE(p.title,'')) LIKE :ql THEN 90 ELSE 0 END
    + CASE WHEN lower(COALESCE(p.brand,'')) LIKE :ql THEN 70 ELSE 0 END
    + CASE WHEN p.pdp_lifecycle_stage='published' THEN 60 ELSE 0 END
    + CASE WHEN p.pdp_lifecycle_stage='validated' THEN 20 ELSE 0 END
    + CASE WHEN p.category_path IS NOT NULL AND p.category_path LIKE :cp THEN 90 ELSE 0 END
    + CASE WHEN (""" + ANCHOR + ") THEN 180 ELSE 0 END) "
DOORS = """(lower(COALESCE(p.title,'')) LIKE :ql OR lower(COALESCE(p.brand,'')) LIKE :ql
        OR lower(COALESCE(m.merchant_name,'')) LIKE :ql OR lower(COALESCE(s.sku,'')) LIKE :ql
        OR lower(COALESCE(s.title,'')) LIKE :ql OR lower(COALESCE(s.source_variant_id,'')) LIKE :ql
        OR lower(COALESCE(p.source_product_id,'')) LIKE :ql
        OR (p.category_path IS NOT NULL AND p.category_path LIKE :cp)
        OR """ + ANCESTOR + ")"
P = {"ql": "%stila stay all day liquid lipstick%", "cp": "beauty/makeup/lip/%",
     "cp_exact": "beauty/makeup/lip/"}


async def q(name, sql, params=None):
    try:
        OUT[name] = [dict(r) for r in await database.fetch_all(sql, params or P)]
    except Exception as e:
        OUT[name] = {"error": f"{type(e).__name__}: {e}"[:500]}


async def main():
    await database.connect()

    # 1. dialect sanity: the ancestor test on real category_path values.
    await q("ancestor_predicate_by_path", """
      SELECT COALESCE(p.category_path,'(null)') AS category_path, count(*) AS n,
             bool_or(SUBSTR(:cp_exact, 1, LENGTH(p.category_path) + 1)
                     = p.category_path || '/') AS is_ancestor
      FROM catalog_products p
      WHERE p.category_path IN ('beauty/makeup','beauty/makeup/lip/lipstick',
                                'beauty/makeup/lip/lip_oil','beauty/skincare','beauty')
      GROUP BY 1 ORDER BY n DESC
    """)

    # 2. does the new door admit Stila, and how many rows does it widen overall?
    await q("admitted_counts", """
      SELECT count(*) FILTER (WHERE lower(COALESCE(p.brand,'')) LIKE '%stila%') AS stila_rows,
             count(*) AS all_rows,
             count(DISTINCT p.product_key) AS all_products
      FROM catalog_products p
      JOIN catalog_skus s ON s.product_key = p.product_key
      LEFT JOIN catalog_merchants m ON m.merchant_id = p.merchant_id
      WHERE """ + DOORS + GATES + """
        AND EXISTS (SELECT 1 FROM catalog_offers o
                     WHERE o.sku_key = s.sku_key AND o.suppressed_at IS NULL)
    """)
    await q("widening_only", """
      SELECT count(*) AS rows_added_by_ancestor_door,
             count(DISTINCT p.product_key) AS products_added,
             count(DISTINCT COALESCE(p.brand,'?')) AS brands_added
      FROM catalog_products p
      JOIN catalog_skus s ON s.product_key = p.product_key
      LEFT JOIN catalog_merchants m ON m.merchant_id = p.merchant_id
      WHERE """ + ANCESTOR + """
        AND NOT (p.category_path IS NOT NULL AND p.category_path LIKE :cp)
        AND NOT (lower(COALESCE(p.title,'')) LIKE :ql)
        """ + GATES + """
        AND EXISTS (SELECT 1 FROM catalog_offers o
                     WHERE o.sku_key = s.sku_key AND o.suppressed_at IS NULL)
    """)
    await q("widening_brands", """
      SELECT COALESCE(p.brand,'(null)') AS brand, COALESCE(p.category_path,'(null)') AS path,
             count(*) AS n
      FROM catalog_products p
      JOIN catalog_skus s ON s.product_key = p.product_key
      LEFT JOIN catalog_merchants m ON m.merchant_id = p.merchant_id
      WHERE """ + ANCESTOR + """
        AND NOT (p.category_path IS NOT NULL AND p.category_path LIKE :cp)
        """ + GATES + """
        AND EXISTS (SELECT 1 FROM catalog_offers o
                     WHERE o.sku_key = s.sku_key AND o.suppressed_at IS NULL)
      GROUP BY 1,2 ORDER BY n DESC LIMIT 30
    """)

    # 3. the served page, after the fix, in the lane's own order.
    await q("top_after_fix", """
      SELECT p.brand, p.title, p.category_path, """ + SCORE + """ AS rank_score
      FROM catalog_products p
      JOIN catalog_skus s ON s.product_key = p.product_key
      LEFT JOIN catalog_merchants m ON m.merchant_id = p.merchant_id
      JOIN catalog_offers o ON o.sku_key = s.sku_key AND o.suppressed_at IS NULL
      WHERE """ + DOORS + GATES + """
      ORDER BY rank_score DESC, p.updated_at DESC LIMIT 20
    """)

    # 4. the glyph fix on its own: how many rows does the anchor now match?
    await q("anchor_match_after_glyph_fix", """
      SELECT count(*) FILTER (WHERE lower(COALESCE(p.brand,'')) LIKE '%stila%') AS stila,
             count(*) AS all_rows
      FROM catalog_products p
      JOIN catalog_skus s ON s.product_key = p.product_key
      LEFT JOIN catalog_merchants m ON m.merchant_id = p.merchant_id
      WHERE """ + ANCHOR)

    await database.disconnect()
    print("<<<JSON>>>" + json.dumps(OUT, default=str) + "<<<END>>>")

asyncio.run(main())
