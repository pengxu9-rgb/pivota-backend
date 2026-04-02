from __future__ import annotations

import argparse
import asyncio
import json

from db.database import database
from services.canonical_commerce_service import canonical_cache_parity


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Compare products_cache and canonical commerce coverage for one merchant.")
    parser.add_argument("--merchant-id", required=True)
    parser.add_argument("--platform", default="shopify")
    parser.add_argument("--include-expired", action="store_true")
    args = parser.parse_args()

    await database.connect()
    try:
        result = await canonical_cache_parity(
            merchant_id=args.merchant_id,
            platform=args.platform,
            include_expired=args.include_expired,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    finally:
        await database.disconnect()


if __name__ == "__main__":
    asyncio.run(_main())
