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
    from services.attached_seed_runtime_evidence import hydrate_product_payloads_from_attached_seed_runtime_evidence

    await database.connect()
    try:
        params: Dict[str, Any] = {"limit": limit}
        where = ["platform = 'shopify'", "expires_at > NOW()"]
        if merchant_id:
            where.append("merchant_id = :merchant_id")
            params["merchant_id"] = merchant_id

        rows = await database.fetch_all(
            f"""
            SELECT id, merchant_id, platform, platform_product_id, product_data, ttl_seconds
            FROM products_cache
            WHERE {' AND '.join(where)}
            ORDER BY cached_at DESC
            LIMIT :limit
            """,
            params,
        )

        if not rows:
            print(json.dumps({"updated": 0, "dry_run": dry_run, "reason": "no_rows"}, ensure_ascii=False))
            return 0

        row_dicts = [dict(row) for row in rows]
        merchant_groups: Dict[str, List[Dict[str, Any]]] = {}
        for row in row_dicts:
            merchant_groups.setdefault(str(row["merchant_id"]), []).append(row)

        updated = 0
        for grouped_merchant_id, merchant_rows in merchant_groups.items():
            payloads = []
            for row in merchant_rows:
                product_data = row.get("product_data") or {}
                if isinstance(product_data, str):
                    try:
                        product_data = json.loads(product_data)
                    except Exception:
                        product_data = {}
                payloads.append(product_data if isinstance(product_data, dict) else {})

            hydrated_payloads = await hydrate_product_payloads_from_attached_seed_runtime_evidence(
                merchant_id=grouped_merchant_id,
                platform="shopify",
                product_payloads=payloads,
            )

            for row, hydrated_payload, original_payload in zip(merchant_rows, hydrated_payloads, payloads):
                if hydrated_payload == original_payload:
                    continue
                updated += 1
                if dry_run:
                    continue

                update_params: Dict[str, Any] = {
                    "id": row["id"],
                    # json.dumps, not the dict: a raw-SQL bind carries no
                    # SQLAlchemy type, so asyncpg gets a dict where the json
                    # codec wants str and raises DataError at encode time.
                    "product_data": json.dumps(hydrated_payload),
                }
                if ttl_seconds is not None:
                    update_params["ttl_seconds"] = int(ttl_seconds)
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
                        update_params,
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
                        update_params,
                    )

        print(json.dumps({"updated": updated, "dry_run": dry_run}, ensure_ascii=False))
        return 0
    finally:
        await database.disconnect()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill attached external-seed reviewed ingredient/shade evidence into Shopify products_cache rows."
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
