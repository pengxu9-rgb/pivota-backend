import argparse
import asyncio
import json
from typing import Any, Dict, Optional


async def _run(
    merchant_id: Optional[str],
    limit: int,
    dry_run: bool,
    ttl_seconds: Optional[int],
) -> int:
    from db.database import database
    from adapters.product_adapters import ShopifyProductAdapter
    from catalog.recommendation_meta import derive_recommendation_meta

    await database.connect()
    try:
        where = "WHERE platform = 'shopify'"
        params: Dict[str, Any] = {"limit": limit}
        if merchant_id:
            where += " AND merchant_id = :merchant_id"
            params["merchant_id"] = merchant_id

        rows = await database.fetch_all(
            f"""
            SELECT id, merchant_id, platform_product_id, product_data, expires_at
            FROM products_cache
            {where}
              AND expires_at > NOW()
              AND (
                (product_data::jsonb ? 'platform') = false
                OR (product_data::jsonb ->> 'platform') != 'shopify'
                OR (product_data::jsonb ? 'id') = false
              )
            ORDER BY cached_at DESC
            LIMIT :limit
            """,
            params,
        )

        updated = 0
        skipped = 0

        for row in rows:
            row = dict(row)
            product_data = row.get("product_data") or {}
            if isinstance(product_data, str):
                try:
                    product_data = json.loads(product_data)
                except Exception:
                    product_data = {}

            raw = product_data.get("raw")
            if not isinstance(raw, dict) or not raw.get("id"):
                skipped += 1
                continue

            platform_product_id = str(row.get("platform_product_id") or raw.get("id") or "").strip()
            if not platform_product_id:
                skipped += 1
                continue

            sp = ShopifyProductAdapter.convert_to_standard(raw, row["merchant_id"])
            new_data: Dict[str, Any] = json.loads(sp.json())
            new_data["raw"] = raw
            new_data["shopify_id"] = platform_product_id
            new_data["handle"] = raw.get("handle") or (sp.platform_metadata or {}).get("handle") or ""

            existing_meta = product_data.get("recommendation_meta")
            if isinstance(existing_meta, dict) and existing_meta.get("version") == 1:
                new_data["recommendation_meta"] = existing_meta
            else:
                new_data["recommendation_meta"] = derive_recommendation_meta(
                    standard_product=sp,
                    raw_shopify_product=raw,
                )

            if dry_run:
                updated += 1
                continue

            if ttl_seconds is not None:
                await database.execute(
                    """
                    UPDATE products_cache
                    SET product_data = :product_data,
                        ttl_seconds = CAST(:ttl_seconds AS integer),
                        expires_at = NOW() + make_interval(secs => CAST(:ttl_seconds AS integer)),
                        cached_at = NOW(),
                        cache_status = 'fresh'
                    WHERE id = :id
                    """,
                    {
                        "id": row["id"],
                        # see the note in backfill_attached_seed_runtime_evidence
                        "product_data": json.dumps(new_data),
                        "ttl_seconds": int(ttl_seconds),
                    },
                )
            else:
                await database.execute(
                    """
                    UPDATE products_cache
                    SET product_data = :product_data,
                        cached_at = NOW(),
                        cache_status = 'fresh'
                    WHERE id = :id
                    """,
                    {"id": row["id"], "product_data": json.dumps(new_data)},
                )

            updated += 1

        print(json.dumps({"updated": updated, "skipped": skipped, "dry_run": dry_run}, ensure_ascii=False))
        return 0
    finally:
        await database.disconnect()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill Shopify products_cache rows into StandardProduct-shaped JSON (price/sku/inventory/images)."
    )
    parser.add_argument("--merchant-id", help="Optional merchant id filter.")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--ttl-seconds",
        type=int,
        default=None,
        help="If set, also update ttl_seconds/expires_at to this value.",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.merchant_id, args.limit, args.dry_run, args.ttl_seconds))


if __name__ == "__main__":
    raise SystemExit(main())

