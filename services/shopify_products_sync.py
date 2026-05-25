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
from db.products import delete_missing_products_from_cache, upsert_product_cache
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
    try:
        row = await database.fetch_one(
            """
            SELECT store_id, domain, api_key, status
            FROM merchant_stores
            WHERE merchant_id = :merchant_id
              AND platform = 'shopify'
              AND status IN ('active', 'connected')
            ORDER BY is_primary DESC, connected_at DESC NULLS LAST
            LIMIT 1
            """,
            {"merchant_id": merchant_id},
        )
    except Exception:
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
    ingest_catalog: bool = True,
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
    synced_platform_ids = set()
    catalog_payloads = []

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
            synced_platform_ids.add(platform_product_id)
            catalog_payloads.append(prod)

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

    deleted_stale = 0
    cache_prune_skipped_reason: Optional[str] = None
    if synced_platform_ids:
        if truncated:
            cache_prune_skipped_reason = truncated_reason or "sync_truncated"
        else:
            deleted_stale = await delete_missing_products_from_cache(
                merchant_id=merchant_id,
                platform="shopify",
                valid_platform_product_ids=list(synced_platform_ids),
            )

    catalog_stats: Optional[Dict[str, Any]] = None
    catalog_deleted_stale: Optional[Dict[str, int]] = None
    catalog_prune_skipped_reason: Optional[str] = None
    if ingest_catalog and catalog_payloads:
        from services.catalog_sync_service import (
            ingest_standard_products,
            prune_missing_catalog_products_for_source,
        )

        catalog_stats = await ingest_standard_products(
            merchant_id=merchant_id,
            platform="shopify",
            product_payloads=catalog_payloads,
            source_system="shopify_products_sync",
            source_ref=f"shopify_products_sync:{merchant_id}:{datetime.utcnow().isoformat()}",
            source_domain=credentials.get("shop_domain"),
        )
        if truncated:
            catalog_prune_skipped_reason = truncated_reason or "sync_truncated"
        elif synced_platform_ids:
            catalog_deleted_stale = await prune_missing_catalog_products_for_source(
                merchant_id=merchant_id,
                platform="shopify",
                valid_source_product_ids=list(synced_platform_ids),
                source_system="shopify_products_sync",
            )
        else:
            catalog_prune_skipped_reason = "no_synced_product_ids"

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
        "deletedStale": deleted_stale,
        "cachePruneSkippedReason": cache_prune_skipped_reason,
        "catalogIngested": bool(catalog_stats),
        "catalogStats": catalog_stats,
        "catalogDeletedStale": catalog_deleted_stale,
        "catalogPruneSkippedReason": catalog_prune_skipped_reason,
        "syncedAt": datetime.utcnow().isoformat(),
        "lastError": last_error,
    }


# ---------------------------------------------------------------------------
# Lightweight presence refresh — Stage 2a follow-up.
# ---------------------------------------------------------------------------
#
# The full sync above (sync_shopify_products_for_merchant) runs through
# the entire catalog ingest pipeline: taxonomy derivation, SKU upserts,
# offer writes, beauty profiles, field facts. That's appropriate when
# the merchant's catalog content has changed. It's also slow — minutes
# for a few hundred products — which is what made codex's Stage 2a
# bootstrap fail (proxy timeouts on /sync/{merchant_id}, SSH lifetime
# on direct service calls, unreliable nohup).
#
# For the sync-hygiene loop (Stage 2a's sweep), we only need ONE
# signal: "which source_product_ids currently exist on the upstream
# Shopify store?" Everything else can be derived from existing rows.
# So we fetch IDs only, bump last_seen_in_sync_at + sync_status='live'
# for matching rows, bump catalog_merchants.last_full_sync_at, and
# return. No taxonomy, no SKU writes, no offer writes — completes in
# seconds for catalogs up to ~5000 products.
#
# Usage pattern (per merchant, nightly or on-demand):
#   1. POST /admin/sync/refresh-presence/{merchant_id}
#   2. python3 scripts/sweep_stale_catalog_products.py --merchant-id M
#   3. (optionally) --apply once dry-run looks right

async def refresh_merchant_presence(
    *,
    merchant_id: str,
    per_page: int = 250,
    max_pages: int = 50,
) -> Dict[str, Any]:
    """Lightweight: fetches current Shopify product IDs for the merchant,
    bumps last_seen_in_sync_at + sync_status='live' on matching
    catalog_products rows, bumps catalog_merchants.last_full_sync_at.

    Raises ShopifyProductsSyncError subclasses on credential / auth /
    rate-limit failures (same exception hierarchy as the full sync, so
    routes can map them to the same HTTP statuses).

    Note: this DOES NOT detect stale rows — that's the sweep's job
    (scripts/sweep_stale_catalog_products.py). This function only
    establishes "what's currently live"; the sweep computes "what's
    been live for the last N hours" from last_seen_in_sync_at."""
    credentials = await _get_shopify_store_credentials(merchant_id)

    live_ids: set[str] = set()
    pages = 0
    page_token: Optional[str] = None
    truncated = False
    truncated_reason: Optional[str] = None

    while pages < max_pages:
        products, next_token, error = await fetch_merchant_products(
            merchant_id=merchant_id,
            platform="shopify",
            credentials=credentials,
            limit=per_page,
            page_token=page_token,
        )
        pages += 1

        if error:
            lower = str(error).lower()
            if "rate limit" in lower or "429" in lower:
                raise ShopifyProductsSyncRateLimitError(error)
            if "401" in lower or "403" in lower or "unauthorized" in lower or "forbidden" in lower:
                raise ShopifyProductsSyncAuthError(error)
            raise ShopifyProductsSyncError(error)

        if next_token == SHOPIFY_NEXT_PAGE_TOKEN_UNPARSEABLE:
            truncated = True
            truncated_reason = "next_page_token_unparseable"
            next_token = None

        for sp in products or []:
            # StandardProduct.product_id is the platform id; fall back
            # to .id if the adapter didn't fill product_id. Both are
            # how Path A's _upsert_by_pk keys source_product_id.
            pid = str(sp.product_id or sp.id or "").strip()
            if pid:
                live_ids.add(pid)

        if not next_token:
            break
        page_token = next_token
    else:
        # Loop exited via `pages < max_pages` becoming false WITHOUT
        # hitting the `if not next_token: break` exit. We capped before
        # exhausting the cursor — Shopify still has more products
        # downstream. Mark truncated; do NOT bump last_full_sync_at
        # below. If we did, the sweep would tombstone the un-scanned
        # tail (products beyond page max_pages would have stale
        # last_seen_in_sync_at and the sweep would archive them).
        # Caught by codex review 2026-05-12: silent data loss risk
        # at scale (≥max_pages × per_page products).
        if page_token is not None:
            truncated = True
            truncated_reason = "max_pages_reached_with_more_pages_available"

    # Bump last_seen_in_sync_at + sync_status for matching rows. Done
    # in chunks because asyncpg's parameter limit caps array binds
    # around ~32k for some Postgres versions; chunk to be safe.
    #
    # Counter via fetch_all on RETURNING product_key, not via
    # database.execute()'s rowcount (which asyncpg returns as None
    # for bulk UPDATEs through the `databases` lib — caused the
    # cosmetic `rows_touched: 0` report on 2026-05-12 even when 741
    # rows were actually updated).
    rows_touched = 0
    if live_ids:
        ids_list = list(live_ids)
        chunk_size = 1000
        async with database.transaction():
            for start in range(0, len(ids_list), chunk_size):
                chunk = ids_list[start:start + chunk_size]
                returned = await database.fetch_all(
                    """
                    UPDATE catalog_products
                    SET last_seen_in_sync_at = NOW(),
                        sync_status = 'live',
                        updated_at = NOW()
                    WHERE merchant_id = :merchant_id
                      AND platform = 'shopify'
                      AND source_product_id = ANY(:ids)
                    RETURNING product_key
                    """,
                    {"merchant_id": merchant_id, "ids": chunk},
                )
                rows_touched += len(returned or [])
            # Bump last_full_sync_at ONLY when the scan was complete.
            # Truncated scans (max_pages exhausted with more pages
            # available) MUST NOT bump — the sweep would otherwise
            # tombstone the un-scanned tail of the catalog. Caught by
            # codex review 2026-05-12.
            if not truncated:
                await database.execute(
                    """
                    UPDATE catalog_merchants
                    SET last_full_sync_at = NOW(),
                        updated_at = NOW()
                    WHERE merchant_id = :merchant_id
                    """,
                    {"merchant_id": merchant_id},
                )
    else:
        # No live IDs returned. Two cases:
        #   (a) merchant deleted everything — empty Shopify catalog,
        #       sweep should tombstone all existing rows. Bump.
        #   (b) scan errored before any product was fetched — but in
        #       that case we'd have raised above, so we wouldn't reach
        #       here. So this branch always means (a).
        # Still gated on `not truncated` for symmetry / safety.
        if not truncated:
            await database.execute(
                """
                UPDATE catalog_merchants
                SET last_full_sync_at = NOW(),
                    updated_at = NOW()
                WHERE merchant_id = :merchant_id
                """,
                {"merchant_id": merchant_id},
            )

    return {
        "merchant_id": merchant_id,
        "shop_domain": credentials.get("shop_domain"),
        "live_ids_fetched": len(live_ids),
        "rows_touched": rows_touched,
        "pages_fetched": pages,
        "truncated": truncated,
        "truncated_reason": truncated_reason,
        "refreshed_at": datetime.utcnow().isoformat(),
    }
