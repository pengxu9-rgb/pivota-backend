#!/usr/bin/env python3
"""
Backfill recommendation_meta into products_cache.product_data for Shopify products.

Usage:
  python scripts/backfill_recommendation_meta.py --merchant-id merch_x --limit 500

Options:
  --merchant-id   Target merchant_id (required).
  --platform      Platform filter (default: shopify).
  --limit         Max rows to process in this run (default: 500).
  --include-expired  Include expired rows (default: false).
  --dry-run       Do not write changes, only log planned updates.

Behavior:
  - Only updates rows where product_data.recommendation_meta is missing or
    has version < 1.
  - Uses catalog.recommendation_meta.derive_recommendation_meta based on
    existing product_data (prefers raw Shopify payload when available).
"""

import argparse
import asyncio
import json
from typing import Any, Dict


async def backfill_recommendation_meta(
    merchant_id: str,
    platform: str = "shopify",
    limit: int = 500,
    include_expired: bool = False,
    dry_run: bool = False,
) -> None:
    from db.database import database
    from catalog.recommendation_meta import derive_recommendation_meta

    expiry_clause = "" if include_expired else "AND expires_at > NOW()"
    query = """
    SELECT id, product_data
    FROM products_cache
    WHERE merchant_id = :merchant_id
      AND platform = :platform
      {expiry_clause}
      AND (cache_status IS NULL OR cache_status = 'fresh')
      AND (
        product_data::jsonb -> 'recommendation_meta' IS NULL
        OR COALESCE(
          (product_data::jsonb -> 'recommendation_meta' ->> 'version')::int,
          0
        ) < 1
      )
    ORDER BY cached_at DESC
    LIMIT :limit
    """.format(expiry_clause=expiry_clause)
    rows = await database.fetch_all(
        query,
        {"merchant_id": merchant_id, "platform": platform, "limit": limit},
    )

    if not rows:
        print("No products require recommendation_meta backfill.")
        return

    print(f"Found {len(rows)} products requiring backfill (merchant_id={merchant_id}, platform={platform}).")

    updated = 0
    for row in rows:
        row = dict(row)
        rid = row["id"]
        pdata = row.get("product_data") or {}
        if isinstance(pdata, str):
            try:
                pdata = json.loads(pdata)
            except Exception:
                pdata = {}

        raw = (pdata or {}).get("raw") or None
        meta = derive_recommendation_meta(
            standard_product=None,
            raw_shopify_product=raw,
        )
        pdata["recommendation_meta"] = meta

        if dry_run:
            print(f"[DRY-RUN] Would update products_cache.id={rid} with recommendation_meta={meta!r}")
            continue

        await database.execute(
            """
            UPDATE products_cache
            SET product_data = :product_data
            WHERE id = :id
            """,
            {
                "id": rid,
                "product_data": json.dumps(pdata),
            },
        )
        updated += 1

    if dry_run:
        print("Dry-run complete; no rows were updated.")
    else:
        print(f"Backfill complete. Updated {updated} rows.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill recommendation_meta for products_cache.")
    parser.add_argument("--merchant-id", required=True, help="Target merchant_id")
    parser.add_argument("--platform", default="shopify", help="Platform (default: shopify)")
    parser.add_argument("--limit", type=int, default=500, help="Max rows to process")
    parser.add_argument(
        "--include-expired",
        action="store_true",
        help="Include expired rows (default: only unexpired).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not persist changes.")

    args = parser.parse_args()

    async def _runner():
        from db.database import database

        await database.connect()
        try:
            await backfill_recommendation_meta(
                merchant_id=args.merchant_id,
                platform=args.platform,
                limit=args.limit,
                include_expired=bool(args.include_expired),
                dry_run=args.dry_run,
            )
        finally:
            await database.disconnect()

    asyncio.run(_runner())


if __name__ == "__main__":
    main()
