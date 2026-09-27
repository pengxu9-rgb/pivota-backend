"""Retailers as enrichment: fill a brand product's MISSING INCI from the retailer listings linked to it.

A content_key groups one product's listings; the brand store's row is the served canonical
(pick_canonical, #2384). Measured 2026-09-27 over the 386 groups where it serves: 12 brand products
have no INCI while a linked retailer listing carries one. Ratings and evidence profiles showed no such
gap, and descriptions are never replaced (the brand's own copy stands), so INCI is the one field filled.

Rules -- a fill only ever lands in an empty slot:
  * only a product whose category carries a formula (not tools, devices, false lashes, press-on nails,
    gift sets; not an uncategorised row);
  * only the served, non-retailer row is filled, and only when it has NO INCI at all;
  * every linked retailer INCI must parse to the SAME ingredient list; if they disagree, nothing is
    filled (which retailer is right is not ours to guess);
  * the write goes through services.canonical_inci_intake at `reseller_listing` rank (1), so any
    brand-official, supplier or later brand-store crawl INCI outranks and replaces it.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Tuple

from services.beauty_enrichment import parse_inci
from services.canonical_inci_intake import INCI_SOURCE_RESELLER, ingest_canonical_inci, is_valid_inci

RETAILER_LISTING_PREFIX = "ext:retailer:"

# Categories whose products have no formula: an "INCI" copied onto them is a materials list (a retailer's
# "Synthetic fibres, Wood, Aluminium" on a Westman Atelier brush, measured 2026-09-27), and a gift set's
# list belongs to none of its products. Fills are refused here; so is a product with no category.
NO_FORMULA_PREFIXES = ("beauty/tools/", "beauty/devices/", "beauty/makeup/eye/false-lashes",
                       "beauty/makeup/nails/press-on-nails", "beauty/sets/")

# One row per (served brand product, linked retailer listing that carries INCI), for brand products with
# no INCI of their own. The served row is agent_pdp_view's signature, so a group a retailer still serves
# is never touched.
CANDIDATES_SQL = """
SELECT w.product_key AS brand_product_key, w.content_key, w.source_domain AS brand_host,
       w.category_path AS brand_category_path,
       r.product_key AS listing_product_key, r.source_domain AS listing_host, i.raw_inci
FROM agent_pdp_view apv
JOIN catalog_products w ON w.pivota_signature_id = apv.pivota_signature_id
JOIN catalog_products r ON r.content_key = w.content_key
JOIN beauty_sku_ingredients i ON i.product_key = r.product_key
WHERE w.product_key NOT LIKE 'ext:retailer:%' AND w.suppressed_at IS NULL
  AND r.product_key LIKE 'ext:retailer:%' AND r.suppressed_at IS NULL
  AND coalesce(trim(i.raw_inci), '') <> ''
  AND NOT EXISTS (SELECT 1 FROM beauty_sku_ingredients own
                  WHERE own.product_key = w.product_key AND coalesce(trim(own.raw_inci), '') <> '')
"""


def _ingredients(raw_inci: str) -> Tuple[str, ...]:
    return tuple(str(x).strip().lower() for x in parse_inci(raw_inci) if str(x).strip())


def plan_fills(rows: Iterable[Mapping[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Pure: group candidate rows by brand product and decide each one."""
    by_product: Dict[str, List[Mapping[str, Any]]] = {}
    for row in rows:
        if str(row.get("brand_product_key") or "").startswith(RETAILER_LISTING_PREFIX):
            continue  # never fill a retailer listing: it is not the brand's product
        by_product.setdefault(row["brand_product_key"], []).append(row)
    fills: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {}

    def note(reason: str) -> None:
        counts[reason] = counts.get(reason, 0) + 1

    for pk, cands in sorted(by_product.items()):
        category = str(cands[0].get("brand_category_path") or "").strip().lower()
        if not category.startswith("beauty/") or category.startswith(NO_FORMULA_PREFIXES):
            note("category_has_no_formula")
            continue
        valid = [c for c in cands if is_valid_inci(c.get("raw_inci"))]
        if not valid:
            note("no_valid_listing_inci")
            continue
        lists = {_ingredients(c["raw_inci"]) for c in valid}
        if len(lists) != 1:
            note("listings_disagree")
            continue
        chosen = sorted(valid, key=lambda c: str(c.get("listing_product_key")))[0]
        fills.append({"brand_product_key": pk, "content_key": chosen.get("content_key"),
                      "brand_host": chosen.get("brand_host"), "raw_inci": chosen["raw_inci"],
                      "from_listings": sorted({str(c.get("listing_host")) for c in valid}),
                      "ingredient_count": len(next(iter(lists)))})
        note("fill")
    return fills, counts


async def fill(db: Any, *, apply: bool) -> Dict[str, Any]:
    """Plan, and with apply=True write each fill through the canonical intake at reseller rank."""
    rows = [dict(r) for r in await db.fetch_all(CANDIDATES_SQL)]
    fills, counts = plan_fills(rows)
    results = []
    for f in fills:
        out = await ingest_canonical_inci(f["brand_product_key"], f["raw_inci"], INCI_SOURCE_RESELLER,
                                          db=db, dry_run=not apply)
        results.append({**{k: v for k, v in f.items() if k != "raw_inci"},
                        "status": out.get("status"), "written_skus": len(out.get("written_skus") or []),
                        "skipped_outranked_skus": len(out.get("skipped_outranked_skus") or []),
                        "serving_eligible": out.get("serving_eligible")})
    return {"apply": apply, "counts": counts, "fills": results}
