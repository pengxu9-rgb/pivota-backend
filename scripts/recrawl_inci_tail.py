"""Re-crawl clean INCI for the junk-source tail.

Products whose stored inci_list is prose/blob junk (unenrichable) but that have a
canonical_url: re-fetch the PDP, extract + validate a clean INCI via
canonical_inci_resolver (PDP text + Shopify-JSON auto-fallback), and feed the production
ingest (ingest_crawled_inci_items → enrich → substantiated claims → agent_pdp_view
auto-refresh). The ingest's _is_skippable_inci is the authoritative gate, so an occasional
prose false-positive from the resolver is rejected, not served.

Dry-run by default; resumable (skips products that already have raw_inci).

  DATABASE_URL=... python -m scripts.recrawl_inci_tail --limit 100            # measure yield
  DATABASE_URL=... python -m scripts.recrawl_inci_tail --limit 100 --apply    # ingest + serve
"""
from __future__ import annotations

import argparse
import asyncio
from typing import List, Optional

from db.database import database
from services.canonical_inci_resolver import http_fetch, resolve_inci_from_urls
from services.crawled_inci_ingest import ingest_crawled_inci_items

_SELECT = """
    SELECT product_key, sku_key, canonical_url FROM (
      SELECT DISTINCT ON (cp.product_key)
             cp.product_key, s.sku_key, cp.canonical_url
      FROM catalog_products cp
      JOIN catalog_skus s ON s.product_key = cp.product_key
      LEFT JOIN beauty_sku_ingredients bsi
        ON bsi.product_key = cp.product_key AND coalesce(bsi.raw_inci,'') <> ''
      WHERE cp.merchant_id='external_seed' AND cp.category_kind='skincare'
        AND cp.brand ~* :brand
        AND coalesce(cp.product_payload->>'inci_list','') <> ''
        -- junk source: keyword-blob (no delimiter) or marketing prose
        AND (position(',' in cp.product_payload->>'inci_list')=0
             OR cp.product_payload->>'inci_list' ~* '\\y(promot|improv|reduc|boost|helps|soothe|calm|nourish|brighten|clinically|dermatologist|visibly|wrinkle)\\y|anti-?aging')
        AND cp.canonical_url IS NOT NULL
        AND bsi.product_key IS NULL            -- not yet has clean INCI → resumable
      ORDER BY cp.product_key, (s.sku_key LIKE '%::canonical') DESC
    ) t
    ORDER BY random()
    LIMIT :limit
"""


async def _run(args: argparse.Namespace) -> int:
    own = not getattr(database, "is_connected", False)
    if own:
        await database.connect()
    try:
        rows = await database.fetch_all(_SELECT, {"brand": args.brand, "limit": args.limit})
        print(f"tail products to re-crawl: {len(rows)}")
        items: List[dict] = []
        misses = 0
        for r in rows:
            try:
                res = await resolve_inci_from_urls([r["canonical_url"]], fetch=http_fetch)
            except Exception:
                res = None
            if res and res.raw_inci:
                items.append({"product_key": r["product_key"], "sku_key": r["sku_key"], "raw_inci": res.raw_inci})
            else:
                misses += 1
        print(f"resolved clean INCI: {len(items)} | misses: {misses}")
        if not items:
            return 0
        if args.apply:
            rep = await ingest_crawled_inci_items(items, source_system="pdp_recrawl", db=database)
            print(f"INGEST: {{'n': {rep['n']}, 'inci_written': {rep['inci_written']}, "
                  f"'claims_written': {rep['claims_written']}, 'skipped': {rep['skipped']}}}")
        else:
            print(f"(DRY-RUN — would ingest {len(items)} resolved INCI)")
        return 0
    finally:
        if own:
            await database.disconnect()


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Re-crawl clean INCI for the junk-source tail.")
    p.add_argument("--brand", default=".*", help="brand regex (default: all)")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--apply", action="store_true", help="ingest + serve (else resolve-only dry-run)")
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
