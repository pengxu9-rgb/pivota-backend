#!/usr/bin/env python3
"""Backfill storefront URLs for already-synced Wix products.

The attributed-redirect lane (P2b, routes/agent_shop_gateway.py
_attach_connected_product_redirects) derives a connected card's outbound
destination from the TOP-LEVEL ``online_store_url`` on the cached
StandardProduct payload. Wix rows synced before the adapter captured
``productPageUrl``/``slug`` carry neither that field nor
platform_metadata.permalink/slug, so their cards mint no /r link and every
agent click-out to the Wix store is unattributed.

This script re-fetches each active Wix store's catalog through the SAME
adapter the organic sync uses (WixProductAdapter.fetch_products) and
re-upserts products_cache rows, so backfilled rows are byte-identical to what
the next organic sync would write. Idempotent: safe to re-run; organic resyncs
converge to the same shape. products_cache only — canonical catalog ingest is
left to the next organic sync (the redirect lane reads products_cache).

Dry-run is the default and does NOT write:
  python scripts/backfill_wix_storefront_urls.py

Apply (staging first, production only with explicit user authorization):
  DATABASE_URL=... python scripts/backfill_wix_storefront_urls.py --apply

Optional scoping: --merchant-id / --store-id.
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

from adapters.product_adapters import WixProductAdapter  # noqa: E402
from db.database import database  # noqa: E402
from db.products import upsert_product_cache  # noqa: E402
from services.wix_connection import (  # noqa: E402
    WixConnectionValidationError,
    extract_wix_site_id,
    normalize_wix_api_key,
)

logger = logging.getLogger("backfill_wix_storefront_urls")

CACHE_TTL_SECONDS = 604800  # 7 days — same TTL as universal_product_sync

STORES_SQL = """
    SELECT store_id, merchant_id, name, domain, api_key
    FROM merchant_stores
    WHERE platform = 'wix'
      AND status IN ('active', 'connected')
"""


async def _fetch_wix_stores(
    merchant_id: Optional[str], store_id: Optional[str]
) -> List[Dict[str, Any]]:
    sql = STORES_SQL
    params: Dict[str, Any] = {}
    if merchant_id:
        sql += " AND merchant_id = :merchant_id"
        params["merchant_id"] = merchant_id
    if store_id:
        sql += " AND store_id = :store_id"
        params["store_id"] = store_id
    rows = await database.fetch_all(sql, params)
    return [dict(r) for r in rows]


async def _resync_store(store: Dict[str, Any], *, apply: bool, page_limit: int) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "store_id": store.get("store_id"),
        "merchant_id": store.get("merchant_id"),
        "products_fetched": 0,
        "products_with_storefront_url": 0,
        "products_missing_storefront_url": 0,
        "rows_upserted": 0,
        "error": None,
    }

    api_key = normalize_wix_api_key(store.get("api_key"))
    try:
        site_id = extract_wix_site_id(store.get("domain"), store.get("api_key"))
    except WixConnectionValidationError:
        site_id = ""
    if not site_id or not api_key:
        report["error"] = "incomplete_credentials"
        return report

    merchant_id = str(store.get("merchant_id") or "").strip()
    page_token: Optional[str] = None
    while True:
        products, next_page_token, error = await WixProductAdapter.fetch_products(
            site_id=site_id,
            api_key=api_key,
            merchant_id=merchant_id,
            limit=page_limit,
            page_token=page_token,
        )
        if error and not products:
            report["error"] = error
            return report

        for product in products:
            report["products_fetched"] += 1
            if product.online_store_url:
                report["products_with_storefront_url"] += 1
            else:
                report["products_missing_storefront_url"] += 1
            if apply:
                await upsert_product_cache(
                    merchant_id=merchant_id,
                    platform="wix",
                    platform_product_id=product.id,
                    product_data=json.loads(product.json()),
                    ttl_seconds=CACHE_TTL_SECONDS,
                )
                report["rows_upserted"] += 1

        if error:
            # Partial page error: keep what we upserted, surface the error.
            report["error"] = error
            return report
        if not next_page_token:
            return report
        page_token = next_page_token


async def resync_wix_storefront_urls(
    *,
    merchant_id: Optional[str] = None,
    store_id: Optional[str] = None,
    apply: bool = False,
    page_limit: int = 100,
) -> Dict[str, Any]:
    stores = await _fetch_wix_stores(merchant_id, store_id)
    report: Dict[str, Any] = {
        "apply": apply,
        "stores_found": len(stores),
        "stores": [],
    }
    for store in stores:
        store_report = await _resync_store(store, apply=apply, page_limit=page_limit)
        report["stores"].append(store_report)
        logger.info(
            "wix storefront backfill store=%s merchant=%s fetched=%s with_url=%s missing_url=%s upserted=%s error=%s",
            store_report["store_id"],
            store_report["merchant_id"],
            store_report["products_fetched"],
            store_report["products_with_storefront_url"],
            store_report["products_missing_storefront_url"],
            store_report["rows_upserted"],
            store_report["error"],
        )
    return report


async def _main(args: argparse.Namespace) -> Dict[str, Any]:
    if not getattr(database, "is_connected", False):
        await database.connect()
    try:
        return await resync_wix_storefront_urls(
            merchant_id=args.merchant_id,
            store_id=args.store_id,
            apply=args.apply,
            page_limit=args.page_limit,
        )
    finally:
        await database.disconnect()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write to products_cache (default: dry-run)")
    parser.add_argument("--merchant-id", default=None, help="scope to one merchant")
    parser.add_argument("--store-id", default=None, help="scope to one store")
    parser.add_argument("--page-limit", type=int, default=100, help="Wix API page size (max 100)")
    cli_args = parser.parse_args()

    result = asyncio.run(_main(cli_args))
    print(json.dumps(result, indent=2, default=str))
    if not cli_args.apply:
        print("\nDRY-RUN — nothing written. Re-run with --apply to upsert products_cache.", file=sys.stderr)
