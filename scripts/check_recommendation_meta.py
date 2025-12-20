import argparse
import asyncio
import json
from typing import Optional


async def _run(merchant_id: Optional[str], limit: int) -> int:
    from db.database import database

    await database.connect()
    try:
        params = {"limit": limit}
        where = "WHERE platform = 'shopify'"
        if merchant_id:
            where += " AND merchant_id = :merchant_id"
            params["merchant_id"] = merchant_id

        totals = await database.fetch_one(
            f"""
            SELECT
              COUNT(*)::int AS total,
              SUM(CASE WHEN expires_at > NOW() THEN 1 ELSE 0 END)::int AS unexpired,
              SUM(CASE WHEN product_data::jsonb ? 'recommendation_meta' THEN 1 ELSE 0 END)::int AS with_meta,
              SUM(CASE WHEN (product_data::jsonb ? 'recommendation_meta') AND expires_at > NOW() THEN 1 ELSE 0 END)::int AS with_meta_unexpired
            FROM products_cache
            {where}
            """,
            {"merchant_id": merchant_id} if merchant_id else None,
        )
        totals = dict(totals) if totals else {}
        print(f"shopify_total={totals.get('total', 0)}")
        print(f"shopify_unexpired={totals.get('unexpired', 0)}")
        print(f"rows_with_meta={totals.get('with_meta', 0)}")
        print(f"rows_with_meta_unexpired={totals.get('with_meta_unexpired', 0)}")

        rows = await database.fetch_all(
            f"""
            SELECT merchant_id, platform_product_id,
                   product_data::jsonb -> 'recommendation_meta' AS recommendation_meta
            FROM products_cache
            {where}
              AND product_data::jsonb ? 'recommendation_meta'
            ORDER BY cached_at DESC
            LIMIT :limit
            """,
            params,
        )
        for r in rows:
            meta = r["recommendation_meta"]
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    pass
            print(
                json.dumps(
                    {
                        "merchant_id": r["merchant_id"],
                        "platform_product_id": r["platform_product_id"],
                        "recommendation_meta": meta,
                    },
                    ensure_ascii=False,
                    default=str,
                )
            )
        return 0
    finally:
        await database.disconnect()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check products_cache rows containing product_data.recommendation_meta."
    )
    parser.add_argument("--merchant-id", help="Optional merchant id filter.")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.merchant_id, args.limit))


if __name__ == "__main__":
    raise SystemExit(main())
