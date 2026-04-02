from __future__ import annotations

import argparse
import asyncio
import json

from db.database import database
from services.canonical_commerce_service import backfill_canonical_products_from_cache


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Backfill Shopify-first canonical commerce rows from products_cache.")
    parser.add_argument("--merchant-id", required=True)
    parser.add_argument("--platform", default="shopify")
    parser.add_argument("--include-expired", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    await database.connect()
    try:
        result = await backfill_canonical_products_from_cache(
            merchant_id=args.merchant_id,
            platform=args.platform,
            include_expired=args.include_expired,
            limit=args.limit,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    finally:
        await database.disconnect()


if __name__ == "__main__":
    asyncio.run(_main())
