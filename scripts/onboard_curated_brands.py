"""Onboard a curated list of brand storefronts into the commerce index — the
CLEAN primary catalog-coverage feed.

For each curated brand (domain + category), enumerate its products via Shopify's
public /products.json and ingest them as depositable canonical anchors (brand-direct,
official_url = the brand's own storefront — no Gemini needed). Reuses the same
FK-order executor as run_catalog_enrichment / the Path-C runner (no SQL drift).

Input — a JSONL on --file (one brand per line) OR a single --domain:
  {"domain": "kosas.com", "category_path": "beauty/makeup", "brand": "Kosas"}

Usage:
  python -m scripts.onboard_curated_brands --domain kosas.com --category beauty/makeup
  python -m scripts.onboard_curated_brands --file data/catalog_enrichment/curated_brands.jsonl --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.catalog_enrichment_agent.apply import apply_ingest_plan  # noqa: E402
from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl  # noqa: E402
from services.curated_brand_feed import records_for_brand  # noqa: E402


def _read_brand_list(args: argparse.Namespace) -> List[Dict[str, Any]]:
    if args.domain:
        return [{"domain": args.domain, "category_path": args.category, "brand": args.brand}]
    brands: List[Dict[str, Any]] = []
    with open(args.file, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("domain") and (row.get("category_path") or args.category):
                row.setdefault("category_path", args.category)
                brands.append(row)
    return brands


async def _run(args: argparse.Namespace) -> int:
    brands = _read_brand_list(args)
    if not brands:
        print("no brands to onboard (need --domain or --file rows with domain+category)", file=sys.stderr)
        return 2

    all_records: List[Dict[str, Any]] = []
    for b in brands:
        recs = await records_for_brand(
            domain=b["domain"],
            category_path=b.get("category_path") or args.category or "",
            brand=b.get("brand"),
            max_products=args.max_products,
            base_listings_only=args.base_listings_only,
        )
        print(f"  {b['domain']}: {len(recs)} products")
        all_records.extend(recs)

    if not all_records:
        print("no products enumerated (non-Shopify storefronts return none).")
        return 0

    plan = ingest_validated_jsonl(all_records)
    print(
        f"plan: pdps={len(plan.get('pdps') or [])} skus={len(plan.get('skus') or [])} "
        f"offers={len(plan.get('offers') or [])} seeds={len(plan.get('seeds') or [])} "
        f"skipped={plan.get('skipped')}"
    )
    if not args.apply:
        pdps = plan.get("pdps") or []
        for p in pdps[:5]:
            print("   ", {k: p.get(k) for k in ("product_key", "brand", "title", "content_key")})
        print("  DRY-RUN — re-run with --apply to ingest as depositable anchors.")
        return 0

    from db.database import database  # noqa: E402
    if not getattr(database, "is_connected", False):
        await database.connect()
    try:
        counts = await apply_ingest_plan(plan, batch_label=f"curated_brands:{len(brands)}", db=database)
        print(f"applied: {counts}")
    finally:
        if getattr(database, "is_connected", False):
            await database.disconnect()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--domain", help="single brand storefront domain (e.g. kosas.com)")
    g.add_argument("--file", help="JSONL of {domain, category_path, brand?} rows")
    p.add_argument("--category", help="category_path (default/override for rows without one)")
    p.add_argument("--brand", help="brand name override (single --domain mode)")
    p.add_argument("--max-products", type=int, default=500, help="cap products per brand")
    p.add_argument(
        "--base-listings-only",
        action="store_true",
        help=(
            "drop single-variant '<base> - <shade>' listings whose base listing is also in the "
            "feed (maccosmetics.com publishes one product per shade; as-is that mints one PDP per shade)"
        ),
    )
    p.add_argument("--apply", action="store_true", help="ingest (else dry-run plan)")
    args = p.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
