"""StyleKorean single-brand intake door — crawl one brand, match each SKU to our
canonical catalog, and produce an attach/mint PLAN. Dry-run by default.

    # dry-run: crawl COSRX, emit the plan, write nothing
    DATABASE_URL=<prod-proxy> python3 scripts/ingest_stylekorean_brand.py --brand cosrx --out /tmp/cosrx_plan.jsonl

    # apply the ATTACH lane only (retailer offers on matched canonicals):
    DATABASE_URL=<prod-proxy> python3 scripts/ingest_stylekorean_brand.py --brand cosrx --apply-offers

LANES:
  ATTACH — a `retailer_match_key` (size/pack/promo-normalized) match to an
           existing canonical → attach a StyleKorean US offer via the sanctioned
           scripts/attach_retailer_offer path. This is what --apply-offers writes.
  MINT   — no match → PROPOSE-only. Emitted for review; minting a new canonical +
           brand-official enrichment is a deliberate follow-up (never auto-run
           here, because the pilot showed naive minting duplicates products).

No DB writes happen without --apply-offers. MINT never writes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from scripts.attach_retailer_offer import (  # noqa: E402
    attach_retailer_offer,
    build_retailer_offer_row,
)
from services.pdp_matcher.retailer_match import (  # noqa: E402
    build_match_index,
    match_record,
    retailer_match_key,
)
from services.retailer_ingest import stylekorean as sk  # noqa: E402
from services.retailer_ingest.sitemap_crawler import crawl_products  # noqa: E402


def _brand_tokens(records: List[Dict[str, Any]]) -> List[str]:
    return sorted({str(r.get("brand")).strip() for r in records if r.get("brand")})


async def _load_our_rows(brands: List[str]) -> List[Dict[str, Any]]:
    """Canonical rows for the crawled brands (brand-token ILIKE, drift-tolerant)."""
    if not brands:
        return []
    clauses = " OR ".join(f"lower(brand) LIKE :b{i}" for i in range(len(brands)))
    params = {f"b{i}": f"%{b.lower()}%" for i, b in enumerate(brands)}
    rows = await database.fetch_all(
        f"SELECT product_key, content_key, brand, title, pdp_scope "
        f"FROM catalog_products WHERE title IS NOT NULL AND ({clauses})",
        params,
    )
    return [dict(r) for r in rows]


async def _drive(args: argparse.Namespace) -> int:
    # 1) enumerate this brand's PDP URLs from the sitemaps
    print(f"[1/4] enumerating StyleKorean sitemaps for brand '{args.brand}' ...", flush=True)
    index_xml = sk.fetch_text(sk.SITEMAP_INDEX)
    product_sitemaps = sk.product_sitemap_urls(index_xml)
    sitemap_texts = [sk.fetch_text(u) for u in product_sitemaps]
    urls = sk.brand_product_urls(args.brand, sitemap_texts)
    if args.limit:
        urls = urls[: args.limit]
    print(f"      {len(urls)} product URLs across {len(product_sitemaps)} sitemaps")
    if not urls:
        print("no product URLs — check the brand slug against sitemap-brands.xml")
        return 1

    # 2) crawl + extract (deterministic JSON-LD)
    print(f"[2/4] crawling {len(urls)} PDPs (concurrency={args.concurrency}) ...", flush=True)
    crawled = crawl_products(
        urls, concurrency=args.concurrency,
        on_progress=lambda i, n: (i % 25 == 0) and print(f"      {i}/{n}", flush=True),
    )
    records = crawled["records"]
    print(f"      extracted {len(records)} ({len(crawled['failures'])} failed/no-product)")

    # 3) match against our canonical catalog
    print("[3/4] matching to canonical catalog ...", flush=True)
    await database.connect()
    try:
        our_rows = await _load_our_rows(_brand_tokens(records))
        index = build_match_index(our_rows)
        plan: List[Dict[str, Any]] = []
        for r in records:
            rec = sk.to_validated_record(r)
            brand, title = rec["pdp"]["brand"], rec["pdp"]["product_name"]
            m = match_record(index, brand, title)
            off = rec["offers"][0]
            plan.append({
                "url": r["url"],
                "brand": brand,
                "title": title,
                "price": off["list_price"],
                "currency": off["currency"],
                "decision": "attach" if m else "mint",
                "match_key": retailer_match_key(brand, title),
                "matched_product_key": (m or {}).get("product_key"),
                "matched_content_key": (m or {}).get("content_key"),
                "record": rec,
            })
        attach = [p for p in plan if p["decision"] == "attach"]
        mint = [p for p in plan if p["decision"] == "mint"]
        print(f"      ATTACH {len(attach)}  |  MINT(new) {len(mint)}  |  our {args.brand} rows {len(our_rows)}")

        if args.out:
            with open(args.out, "w") as f:
                for p in plan:
                    f.write(json.dumps(p, ensure_ascii=False, default=str) + "\n")
            print(f"      wrote plan -> {args.out}")

        # 4) optionally apply the ATTACH lane (offers only)
        print("[4/4] " + ("applying ATTACH offers ..." if args.apply_offers else "dry-run — no writes (use --apply-offers)"), flush=True)
        if args.apply_offers:
            applied, skipped = 0, 0
            for p in attach:
                if p["price"] in (None, ""):
                    skipped += 1
                    continue
                row = build_retailer_offer_row(
                    product_key=p["matched_product_key"],
                    merchant_id=sk.MERCHANT_ID, merchant_name=sk.MERCHANT_NAME,
                    retailer_url=p["url"], market=sk.MARKET, currency=p["currency"] or "USD",
                    price=float(p["price"]), availability=p["record"]["offers"][0]["availability"],
                )
                await attach_retailer_offer(row)
                applied += 1
            print(f"      attached {applied} offers ({skipped} skipped: no price). MINT lane untouched.")
        else:
            for p in mint[:20]:
                print(f"      MINT  {p['title'][:56]:56} {p['currency']} {p['price']}")
    finally:
        await database.disconnect()
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--brand", required=True, help="StyleKorean brand slug, e.g. cosrx (see sitemap-brands.xml)")
    p.add_argument("--limit", type=int, default=0, help="cap number of PDPs (0 = all)")
    p.add_argument("--concurrency", type=int, default=6)
    p.add_argument("--out", default=None, help="write the attach/mint plan as JSONL")
    p.add_argument("--apply-offers", action="store_true", help="write the ATTACH lane (retailer offers). MINT never writes.")
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_drive(_parse_args())))
