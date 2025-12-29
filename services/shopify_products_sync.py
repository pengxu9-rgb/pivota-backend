"""
Internal service to sync Shopify products into products_cache.

Why this exists:
- Creator Agent relies on products_cache-backed search to serve real products.
- Merchant-facing sync endpoints require user auth (JWT). For operations/debug,
  we expose an admin-key protected internal endpoint that calls this service.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Optional

from db.database import database
from db.products import upsert_product_cache
from adapters.product_adapters import fetch_merchant_products


class ShopifyProductsSyncError(Exception):
    """Base error for Shopify product sync."""


class ShopifyProductsSyncConfigError(ShopifyProductsSyncError):
    """Missing store connection / credentials."""


class ShopifyProductsSyncAuthError(ShopifyProductsSyncError):
    """Shopify returned 401/403 or token invalid."""


class ShopifyProductsSyncRateLimitError(ShopifyProductsSyncError):
    """Shopify rate limiting."""


async def _get_shopify_store_credentials(merchant_id: str) -> Dict[str, str]:
    row = await database.fetch_one(
        """
        SELECT domain, api_key, status
        FROM merchant_stores
        WHERE merchant_id = :merchant_id
          AND platform = 'shopify'
          AND status IN ('active', 'connected')
        ORDER BY connected_at DESC NULLS LAST
        LIMIT 1
        """,
        {"merchant_id": merchant_id},
    )

    if not row:
        raise ShopifyProductsSyncConfigError("No Shopify store connected for this merchant")

    status = (row.get("status") or "").lower()
    if status != "active":
        raise ShopifyProductsSyncConfigError(f"Shopify store is {row.get('status')}; reconnect required")

    shop_domain = row.get("domain")
    api_key_raw = row.get("api_key")

    access_token: Optional[str] = None
    if isinstance(api_key_raw, str) and api_key_raw.strip().startswith("{"):
        try:
            token_data = json.loads(api_key_raw)
            access_token = token_data.get("access_token") or token_data.get("token")
        except Exception:
            access_token = None
    elif isinstance(api_key_raw, str):
        access_token = api_key_raw

    if not shop_domain or not access_token:
        raise ShopifyProductsSyncConfigError("Shopify credentials missing (domain/access_token)")

    return {"shop_domain": shop_domain, "access_token": access_token}


async def sync_shopify_products_for_merchant(
    *,
    merchant_id: str,
    limit: int = 500,
    ttl_seconds: int = 7 * 24 * 60 * 60,
    per_page: int = 250,
    max_pages: int = 20,
) -> Dict[str, Any]:
    """
    Fetch Shopify products and upsert into products_cache.

    Returns a summary dict safe for logging and API responses.
    """
    if limit <= 0:
        return {
            "merchantId": merchant_id,
            "productsFetched": 0,
            "productsUpserted": 0,
            "pagesFetched": 0,
            "nextPageToken": None,
            "syncedAt": datetime.utcnow().isoformat(),
        }

    credentials = await _get_shopify_store_credentials(merchant_id)

    fetched = 0
    upserted = 0
    pages = 0
    page_token: Optional[str] = None
    last_error: Optional[str] = None

    # Fetch in pages to avoid huge payloads and to stay within timeouts.
    while fetched < limit and pages < max_pages:
        batch_limit = min(per_page, limit - fetched)
        products, next_token, error = await fetch_merchant_products(
            merchant_id=merchant_id,
            platform="shopify",
            credentials=credentials,
            limit=batch_limit,
            page_token=page_token,
        )

        pages += 1
        if error:
            last_error = error
            lower = str(error).lower()
            if "rate limit" in lower or "429" in lower:
                raise ShopifyProductsSyncRateLimitError(error)
            if "401" in lower or "403" in lower or "unauthorized" in lower or "forbidden" in lower:
                raise ShopifyProductsSyncAuthError(error)
            raise ShopifyProductsSyncError(error)

        if not products:
            page_token = next_token
            if not next_token:
                break
            continue

        for sp in products:
            # StandardProduct provides product_id; fall back to id if needed.
            prod = sp.dict()
            platform_product_id = str(
                prod.get("product_id") or prod.get("id") or ""
            ).strip()
            if not platform_product_id:
                continue

            await upsert_product_cache(
                merchant_id=merchant_id,
                platform="shopify",
                platform_product_id=platform_product_id,
                product_data=prod,
                ttl_seconds=ttl_seconds,
            )
            upserted += 1

        fetched += len(products)
        page_token = next_token
        if not page_token:
            break

    return {
        "merchantId": merchant_id,
        "shopDomain": credentials.get("shop_domain"),
        "productsFetched": fetched,
        "productsUpserted": upserted,
        "pagesFetched": pages,
        "nextPageToken": page_token,
        "ttlSeconds": ttl_seconds,
        "limit": limit,
        "perPage": per_page,
        "maxPages": max_pages,
        "syncedAt": datetime.utcnow().isoformat(),
        "lastError": last_error,
    }

