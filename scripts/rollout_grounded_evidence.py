"""Roll out grounded evidence across the catalog from already-crawled INCI.

The crawled product_payload carries a clean `inci_list` for most of the K-beauty
catalog (~69%). This extracts it and feeds the PRODUCTION ingest
(services.crawled_inci_ingest.ingest_crawled_inci_items) → beauty_sku_ingredients →
enrich_and_persist_product → substantiated claims → agent_pdp_view auto-refresh
(#1093). No LLM, no crawl — pure extraction of data we already hold.

Dry-run by default. Resumable: skips products that already have raw_inci.

  DATABASE_URL=... python -m scripts.rollout_grounded_evidence --limit 200            # dry-run
  DATABASE_URL=... python -m scripts.rollout_grounded_evidence --limit 200 --apply    # ingest + serve
  DATABASE_URL=... python -m scripts.rollout_grounded_evidence --apply --backfill-category
"""
from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any, Dict, List, Optional

from db.database import database
from services.crawled_inci_ingest import ingest_crawled_inci_items

# K-beauty first — hero-ingredient brands give the strongest substantiation, and the
# evidence vocab (snail/centella/propolis/niacinamide/panthenol) is pre-tuned for them.
DEFAULT_BRANDS = (
    "(cosrx|round lab|anua|beauty of joseon|skin1004|tirtir|torriden|medicube|isntree|"
    "mixsoon|biodance|haruharu|some by mi|purito|klairs|iunik|pyunkang yul|abib|"
    "numbuzin|goodal|axis-?y|laneige|innisfree|missha|skinfood)"
)

_SELECT = """
    SELECT product_key, sku_key, inci FROM (
      SELECT DISTINCT ON (cp.product_key)
             cp.product_key, s.sku_key, cp.product_payload->>'inci_list' AS inci
      FROM catalog_products cp
      JOIN catalog_skus s ON s.product_key = cp.product_key
      LEFT JOIN beauty_sku_ingredients bsi
        ON bsi.product_key = cp.product_key AND coalesce(bsi.raw_inci, '') <> ''
      WHERE cp.merchant_id = 'external_seed'
        AND cp.category_kind = 'skincare'
        AND cp.brand ~* :brand
        AND coalesce(cp.product_payload->>'inci_list', '') <> ''
        -- Only REAL INCI: has a delimiter (excludes crawler keyword-blobs) and isn't
        -- benefit/marketing prose. Coarse pre-filter mirroring _looks_like_inci; runtime
        -- _is_skippable_inci stays authoritative.
        AND position(',' in cp.product_payload->>'inci_list') > 0
        AND cp.product_payload->>'inci_list' !~* '\\y(promot|improv|reduc|boost|helps|soothe|calm|nourish|brighten|clinically|dermatologist|visibly|wrinkle)\\y|anti-?aging'
        AND bsi.product_key IS NULL            -- not yet ingested → resumable
      ORDER BY cp.product_key, (s.sku_key LIKE '%::canonical') DESC
    ) t
    -- Randomize AFTER dedup so the residual skippable sources the pre-filter misses
    -- (Python's _PROSE_RE is broader than SQL can cheaply mirror) can't clog a fixed
    -- ORDER-BY prefix and starve valid products — each pass samples fresh.
    ORDER BY random()
    LIMIT :limit
"""

# Null category_kind blocks enrichment (skipped_non_beauty). These are skincare brands,
# so backfill null → skincare where an inci_list is present. Conservative + gated.
_BACKFILL_CATEGORY = """
    UPDATE catalog_products SET category_kind = 'skincare', updated_at = NOW()
    WHERE merchant_id = 'external_seed' AND category_kind IS NULL AND brand ~* :brand
      AND coalesce(product_payload->>'inci_list', '') <> ''
"""
_BACKFILL_COUNT = """
    SELECT count(*) AS n FROM catalog_products
    WHERE merchant_id = 'external_seed' AND category_kind IS NULL AND brand ~* :brand
      AND coalesce(product_payload->>'inci_list', '') <> ''
"""


def _norm_inci(raw: Optional[str]) -> str:
    """inci_list is usually a comma-string but sometimes a JSON-array string."""
    raw = (raw or "").strip()
    if raw.startswith("["):
        try:
            arr = json.loads(raw)
            if isinstance(arr, list):
                return ", ".join(str(x).strip() for x in arr if str(x).strip())
        except (ValueError, TypeError):
            pass
    return raw


async def _run(args: argparse.Namespace) -> int:
    own = not getattr(database, "is_connected", False)
    if own:
        await database.connect()
    try:
        if args.backfill_category:
            if args.apply:
                await database.execute(_BACKFILL_CATEGORY, {"brand": args.brand})
                print("category_kind backfill: null → skincare applied for the brand set")
            else:
                n = await database.fetch_val(_BACKFILL_COUNT, {"brand": args.brand})
                print(f"category_kind backfill: would set {n} null rows → skincare (dry-run)")

        rows = await database.fetch_all(_SELECT, {"brand": args.brand, "limit": args.limit})
        items: List[Dict[str, Any]] = [
            {"product_key": r["product_key"], "sku_key": r["sku_key"], "raw_inci": _norm_inci(r["inci"])}
            for r in rows
        ]
        print(f"products with inci_list to process: {len(items)}")
        if not items:
            print("nothing to do (all caught up, or none match)")
            return 0

        agg = {"n": 0, "inci_written": 0, "actives_filled": 0, "claims_written": 0,
               "skipped": 0, "skipped_outranked": 0}
        for i in range(0, len(items), args.batch_size):
            batch = items[i:i + args.batch_size]
            rep = await ingest_crawled_inci_items(batch, dry_run=not args.apply, db=database)
            for k in agg:
                agg[k] += int(rep.get(k, 0) or 0)
            print(f"  batch {i // args.batch_size + 1}: {rep['n']} items, {rep['claims_written']} claims written")

        tag = "" if args.apply else "  (DRY-RUN — no writes)"
        print(f"TOTAL{tag}: {agg}")
        return 0
    finally:
        if own:
            await database.disconnect()


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Roll out grounded evidence from crawled inci_list.")
    p.add_argument("--brand", default=DEFAULT_BRANDS, help="brand regex (default: K-beauty set)")
    p.add_argument("--limit", type=int, default=200, help="max products per run")
    p.add_argument("--batch-size", type=int, default=25, help="products per ingest call")
    p.add_argument("--backfill-category", action="store_true",
                   help="set null category_kind → skincare for the brand set (assumes skincare brands)")
    p.add_argument("--apply", action="store_true", help="ingest + serve (else dry-run)")
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
