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
from adapters.product_adapters import (
    SHOPIFY_NEXT_PAGE_TOKEN_UNPARSEABLE,
    fetch_merchant_products,
)
from services.attached_seed_runtime_evidence import (
    hydrate_product_payloads_from_attached_seed_runtime_evidence,
)
from services.shopify_access_token_service import resolve_shopify_admin_access_token
from utils.rich_text import rich_text_to_plain_text


class ShopifyProductsSyncError(Exception):
    """Base error for Shopify product sync."""


class ShopifyProductsSyncConfigError(ShopifyProductsSyncError):
    """Missing store connection / credentials."""


class ShopifyProductsSyncAuthError(ShopifyProductsSyncError):
    """Shopify returned 401/403 or token invalid."""


class ShopifyProductsSyncRateLimitError(ShopifyProductsSyncError):
    """Shopify rate limiting."""


def _inject_description_text_fields(product_payload: Dict[str, Any]) -> Dict[str, Any]:
    raw = product_payload.get("raw") if isinstance(product_payload.get("raw"), dict) else {}
    description_text = rich_text_to_plain_text(
        product_payload.get("description_text")
        or product_payload.get("description")
        or product_payload.get("body_html")
        or raw.get("description_text")
        or raw.get("body_html")
        or raw.get("description")
        or raw.get("description_html")
        or ""
    )
    if description_text:
        product_payload["description_text"] = description_text
        if raw:
            raw.setdefault("description_text", description_text)
            product_payload["raw"] = raw
    return product_payload


async def _get_shopify_store_credentials(merchant_id: str) -> Dict[str, str]:
    row = await database.fetch_one(
        """
        SELECT store_id, domain, api_key, status
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

    data = dict(row)

    status = (data.get("status") or "").lower()
    if status != "active":
        raise ShopifyProductsSyncConfigError(f"Shopify store is {data.get('status')}; reconnect required")

    shop_domain = str(data.get("domain") or "").strip()
    api_key_raw = data.get("api_key")
    store_id = str(data.get("store_id") or "").strip() or None

    access_token, _ = await resolve_shopify_admin_access_token(
        shop_domain=shop_domain,
        api_key_raw=api_key_raw,
        store_id=store_id,
    )

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
    next_page_token: Optional[str] = None
    last_error: Optional[str] = None
    truncated = False
    truncated_reason: Optional[str] = None

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

        if next_token == SHOPIFY_NEXT_PAGE_TOKEN_UNPARSEABLE:
            truncated = True
            truncated_reason = "next_page_token_unparseable"
            next_page_token = None
            next_token = None

        if not products:
            page_token = next_token
            next_page_token = next_token
            if not next_token:
                break
            continue

        payloads = []
        for sp in products:
            # StandardProduct provides product_id; fall back to id if needed.
            # Ensure JSON-serializable payload for products_cache JSONB.
            # StandardProduct contains datetimes; `.json()` converts them to ISO strings.
            prod = json.loads(sp.json())
            # Defensive: some nested/raw fields may still contain datetimes;
            # normalize anything JSON can't encode into strings.
            prod = json.loads(json.dumps(prod, default=str))
            prod = _inject_description_text_fields(prod)
            payloads.append(prod)

        if not payloads:
            fetched += len(products)
            page_token = next_token
            next_page_token = next_token
            if not page_token:
                break
            continue

        hydrated_payloads = await hydrate_product_payloads_from_attached_seed_runtime_evidence(
            merchant_id=merchant_id,
            platform="shopify",
            product_payloads=payloads,
        )

        for prod in hydrated_payloads:
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
        next_page_token = next_token
        if not page_token:
            break

    if not truncated and fetched >= limit and page_token:
        truncated = True
        truncated_reason = "limit_reached_with_next_page"
    if not truncated and pages >= max_pages and page_token:
        truncated = True
        truncated_reason = "max_pages_reached_with_next_page"

    return {
        "merchantId": merchant_id,
        "shopDomain": credentials.get("shop_domain"),
        "productsFetched": fetched,
        "productsUpserted": upserted,
        "pagesFetched": pages,
        "nextPageToken": next_page_token,
        "nextPageTokenPresent": bool(next_page_token),
        "truncated": truncated,
        "truncatedReason": truncated_reason,
        "ttlSeconds": ttl_seconds,
        "limit": limit,
        "perPage": per_page,
        "maxPages": max_pages,
        "syncedAt": datetime.utcnow().isoformat(),
        "lastError": last_error,
    }
