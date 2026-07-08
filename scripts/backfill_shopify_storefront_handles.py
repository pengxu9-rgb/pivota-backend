#!/usr/bin/env python3
"""Backfill top-level storefront handles for already-synced Shopify products.

The attributed-redirect lane (P2b, routes/agent_shop_gateway.py
_attach_connected_product_redirects) derives a connected-Shopify card's
outbound destination from the connected shop domain + the TOP-LEVEL ``handle``
on the cached StandardProduct payload. Rows synced before
ShopifyProductAdapter.convert_to_standard set the top-level handle (it lived
only in platform_metadata, which the agent gateway never lifts) mint no /r
link, so every agent click-out to a connected Shopify store is unattributed.
Measured in prod 2026-07-08: 777/777 cached rows across all connected-active
Shopify merchants had top-level handle=null.

This script re-runs the SAME sync the organic path uses
(services.shopify_products_sync.sync_shopify_products_for_merchant, with
ingest_catalog=False → products_cache only) for each merchant with an active
Shopify store, so backfilled rows are byte-identical to what the next organic
sync would write. Idempotent: safe to re-run; organic resyncs converge to the
same shape. Canonical catalog ingest is left to the next organic sync (the
redirect lane reads products_cache).

Dry-run is the default and does NOT write — it fetches the first page through
the adapter and reports handle coverage:
  python scripts/backfill_shopify_storefront_handles.py

Apply (staging first, production only with explicit user authorization):
  DATABASE_URL=... python scripts/backfill_shopify_storefront_handles.py --apply

Optional scoping: --merchant-id.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402

logger = logging.getLogger("backfill_shopify_storefront_handles")

MERCHANTS_SQL = """
    SELECT DISTINCT merchant_id
    FROM merchant_stores
    WHERE platform = 'shopify'
      AND status IN ('active', 'connected')
"""


async def _fetch_shopify_merchants(merchant_id: Optional[str]) -> List[str]:
    sql = MERCHANTS_SQL
    params: Dict[str, Any] = {}
    if merchant_id:
        sql += " AND merchant_id = :merchant_id"
        params["merchant_id"] = merchant_id
    rows = await database.fetch_all(sql, params)
    return [str(r["merchant_id"]) for r in rows]


async def _dry_run_merchant(merchant_id: str, page_limit: int) -> Dict[str, Any]:
    """Fetch one page through the same adapter the sync uses and report what a
    resync WOULD write (handle coverage), without touching products_cache."""
    from adapters.product_adapters import ShopifyProductAdapter
    from services.shopify_products_sync import (
        ShopifyProductsSyncConfigError,
        _get_shopify_store_credentials,
    )

    report: Dict[str, Any] = {
        "merchant_id": merchant_id,
        "products_sampled": 0,
        "products_with_handle": 0,
        "products_missing_handle": 0,
        "error": None,
    }
    try:
        credentials = await _get_shopify_store_credentials(merchant_id)
    except ShopifyProductsSyncConfigError as e:
        report["error"] = str(e)
        return report

    products, _next_token, error = await ShopifyProductAdapter.fetch_products(
        shop_domain=credentials["shop_domain"],
        access_token=credentials["access_token"],
        merchant_id=merchant_id,
        limit=page_limit,
    )
    if error and not products:
        report["error"] = error
        return report

    for product in products:
        report["products_sampled"] += 1
        if product.handle:
            report["products_with_handle"] += 1
        else:
            report["products_missing_handle"] += 1
    if error:
        report["error"] = error
    return report


async def _apply_merchant(merchant_id: str, limit: int) -> Dict[str, Any]:
    from services.shopify_products_sync import (
        ShopifyProductsSyncError,
        sync_shopify_products_for_merchant,
    )

    try:
        summary = await sync_shopify_products_for_merchant(
            merchant_id=merchant_id,
            limit=limit,
            ingest_catalog=False,
        )
        return {
            "merchant_id": merchant_id,
            "products_fetched": summary.get("productsFetched"),
            "rows_upserted": summary.get("productsUpserted"),
            "truncated": summary.get("truncated"),
            "error": summary.get("lastError"),
        }
    except ShopifyProductsSyncError as e:
        return {"merchant_id": merchant_id, "rows_upserted": 0, "error": str(e)}


async def resync_shopify_storefront_handles(
    *,
    merchant_id: Optional[str] = None,
    apply: bool = False,
    limit: int = 2000,
    page_limit: int = 100,
) -> Dict[str, Any]:
    merchants = await _fetch_shopify_merchants(merchant_id)
    report: Dict[str, Any] = {
        "apply": apply,
        "merchants_found": len(merchants),
        "merchants": [],
    }
    for mid in merchants:
        if apply:
            m_report = await _apply_merchant(mid, limit)
        else:
            m_report = await _dry_run_merchant(mid, page_limit)
        report["merchants"].append(m_report)
        logger.info("shopify handle backfill %s", json.dumps(m_report, default=str))
    return report


async def _main(args: argparse.Namespace) -> Dict[str, Any]:
    if not getattr(database, "is_connected", False):
        await database.connect()
    try:
        return await resync_shopify_storefront_handles(
            merchant_id=args.merchant_id,
            apply=args.apply,
            limit=args.limit,
            page_limit=args.page_limit,
        )
    finally:
        await database.disconnect()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="resync products_cache (default: dry-run sample)")
    parser.add_argument("--merchant-id", default=None, help="scope to one merchant")
    parser.add_argument("--limit", type=int, default=2000, help="max products per merchant on --apply")
    parser.add_argument("--page-limit", type=int, default=100, help="dry-run sample page size (max 250)")
    cli_args = parser.parse_args()

    result = asyncio.run(_main(cli_args))
    print(json.dumps(result, indent=2, default=str))
    if not cli_args.apply:
        print("\nDRY-RUN — nothing written. Re-run with --apply to resync products_cache.", file=sys.stderr)
