"""Blast radius of the ancestor-taxonomy door, on prod. READ-ONLY.

The two queries this answers were dropped from stila_fix_verify.py by the very hazard the
fix comments call out: they bound :ql without referencing it, and text() refuses an unused
bind. Re-run here with only the binds each statement uses.
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
      AND EXISTS (SELECT 1 FROM catalog_offers o
                   WHERE o.sku_key = s.sku_key AND o.suppressed_at IS NULL)
"""


async def q(name, sql, params):
    try:
        OUT[name] = [dict(r) for r in await database.fetch_all(sql, params)]
    except Exception as e:
        OUT[name] = {"error": f"{type(e).__name__}: {e}"[:500]}


async def main():
    await database.connect()

    # Dialect sanity for SUBSTR/LENGTH/|| on real stored paths.
    await q("ancestor_predicate_by_path", """
      SELECT COALESCE(p.category_path,'(null)') AS category_path, count(*) AS n,
             bool_or(SUBSTR(:cp_exact, 1, LENGTH(p.category_path) + 1)
                     = p.category_path || '/') AS is_ancestor
      FROM catalog_products p
      WHERE p.category_path IN ('beauty','beauty/makeup','beauty/makeup/lip',
                                'beauty/makeup/lip/lipstick','beauty/makeup/lip/lip_oil',
                                'beauty/skincare','beauty/makeup/eye')
      GROUP BY 1 ORDER BY 1
    """, {"cp_exact": "beauty/makeup/lip/"})

    # Exactly what the new door adds that the OLD category door did not already have.
    await q("widening_brands", """
      SELECT COALESCE(p.brand,'(null)') AS brand,
             COALESCE(p.category_path,'(null)') AS path,
             COALESCE(p.source_system,'(null)') AS source_system,
             count(*) AS sku_rows, count(DISTINCT p.product_key) AS products
      FROM catalog_products p
      JOIN catalog_skus s ON s.product_key = p.product_key
      LEFT JOIN catalog_merchants m ON m.merchant_id = p.merchant_id
      WHERE """ + ANCESTOR + """
        AND NOT (p.category_path IS NOT NULL AND p.category_path LIKE :cp)
        """ + GATES + """
      GROUP BY 1,2,3 ORDER BY sku_rows DESC LIMIT 40
    """, {"cp_exact": "beauty/makeup/lip/", "cp": "beauty/makeup/lip/%"})

    # The glyph fix alone, at catalog scale: rows the 4-term anchor now scores.
    await q("anchor_match_after_glyph_fix", """
      SELECT count(*) FILTER (WHERE lower(COALESCE(p.brand,'')) LIKE '%stila%') AS stila_rows,
             count(*) AS all_rows
      FROM catalog_products p
      JOIN catalog_skus s ON s.product_key = p.product_key
      LEFT JOIN catalog_merchants m ON m.merchant_id = p.merchant_id
      WHERE """ + ANCHOR, {})

    await database.disconnect()
    print("<<<JSON>>>" + json.dumps(OUT, default=str) + "<<<END>>>")

asyncio.run(main())
