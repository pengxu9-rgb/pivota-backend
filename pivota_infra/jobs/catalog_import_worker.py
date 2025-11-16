"""
Catalog Import Worker - EPIC‑2 Shopify Phase 1 & 2 & EPIC‑3 Pagination

This worker processes ImportTasks for Platform merchants.

For Shopify connector tasks we:
- Fetch one or more pages of products from Shopify Admin API
- Normalize them into a minimal DTO
- Write them into the products_cache table only (no core tables)
- Record counts, pagination stats, and basic timing in the ImportTask
"""

from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import os
import re
from urllib.parse import urlparse, parse_qs

import httpx

from config.settings import settings
from services.platform_import_service import (
    get_next_ready_task,
    mark_import_task_running,
    mark_import_task_succeeded,
    mark_import_task_failed,
    mark_import_task_retry_scheduled,
)
from db.connector_credentials import (
    get_latest_connector_credential_for_merchant,
    mark_credential_used,
)
from services.crypto_service import crypto_service
from db.products import upsert_product_cache

logger = logging.getLogger(__name__)

SHOPIFY_API_VERSION = "2024-07"
SHOPIFY_IMPORT_LIMIT = 50
SHOPIFY_MAX_RETRY_ATTEMPTS = int(os.getenv("SHOPIFY_MAX_RETRY_ATTEMPTS", "5"))


class ShopifyAPIError(Exception):
    """Base exception for Shopify API errors."""


class ShopifyConfigError(ShopifyAPIError):
    """Raised when Shopify configuration is missing or invalid."""


class ShopifyAuthError(ShopifyAPIError):
    """Raised when Shopify rejects our credentials (401/403)."""


class ShopifyRateLimitError(ShopifyAPIError):
    """Raised when Shopify rate limits our requests (429)."""


@dataclass
class ShopifyProductDTO:
    """Minimal normalized representation of a Shopify product."""

    shopify_id: str
    title: str
    handle: str
    status: str
    created_at: str
    updated_at: str
    variants_count: int
    images_count: int
    vendor: Optional[str] = None
    product_type: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None

    @classmethod
    def from_shopify_api(cls, data: Dict[str, Any]) -> "ShopifyProductDTO":
        """Build DTO from raw Shopify product payload."""
        return cls(
            shopify_id=str(data.get("id")),
            title=data.get("title") or "",
            handle=data.get("handle") or "",
            status=data.get("status") or "",
            created_at=data.get("created_at") or "",
            updated_at=data.get("updated_at") or "",
            variants_count=len(data.get("variants", []) or []),
            images_count=len(data.get("images", []) or []),
            vendor=data.get("vendor"),
            product_type=data.get("product_type"),
            raw=data,
        )

    def to_cache_payload(self) -> Dict[str, Any]:
        """Return payload shape stored in products_cache.product_data."""
        payload: Dict[str, Any] = {
            "shopify_id": self.shopify_id,
            "title": self.title,
            "handle": self.handle,
            "status": self.status,
            "vendor": self.vendor,
            "product_type": self.product_type,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "variants_count": self.variants_count,
            "images_count": self.images_count,
        }
        if self.raw is not None:
            payload["raw"] = self.raw
        return payload


def _parse_shopify_next_page_info(link_header: Optional[str]) -> Optional[str]:
    """
    Parse Shopify Link header to extract next page_info cursor.

    Example Link header:
    <https://shop.myshopify.com/admin/api/2024-07/products.json?limit=50&page_info=XYZ>; rel="next"
    """
    if not link_header:
        return None

    parts = link_header.split(",")
    for part in parts:
        if 'rel="next"' not in part and "rel='next'" not in part:
            continue
        match = re.search(r"<([^>]+)>", part)
        if not match:
            continue
        url = match.group(1)
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        page_info_values = params.get("page_info")
        if page_info_values:
            return page_info_values[0]
    return None


async def _fetch_shopify_products_page(
    shop_domain: str,
    access_token: str,
    limit: int,
    page_info: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Fetch a single page of products from Shopify and return (products, next_page_info)."""
    url = f"https://{shop_domain}/admin/api/{SHOPIFY_API_VERSION}/products.json"
    params: Dict[str, Any] = {"limit": limit}
    if page_info:
        params["page_info"] = page_info

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params, headers={"X-Shopify-Access-Token": access_token})
    except httpx.RequestError as exc:
        raise ShopifyAPIError(f"Shopify products request error: {exc}") from exc

    # 429: explicit rate limiting
    if resp.status_code == 429:
        raise ShopifyRateLimitError("Shopify rate limit exceeded (status=429)")

    # 401/403: credentials / permissions issue for product fetch
    if resp.status_code in (401, 403):
        raise ShopifyAuthError(f"Shopify products auth failed (status={resp.status_code})")

    if resp.status_code != 200:
        raise ShopifyAPIError(f"Shopify products fetch failed (status={resp.status_code})")

    data = resp.json()
    products = data.get("products", []) or []
    next_page_info = _parse_shopify_next_page_info(resp.headers.get("Link"))
    return products, next_page_info


async def _fetch_shopify_products(shop_domain: str, access_token: str, limit: int) -> List[Dict[str, Any]]:
    """
    Backwards-compatible helper: fetch a single page of products.

    Retained for compatibility; new code should prefer _fetch_shopify_products_page.
    """
    products, _ = await _fetch_shopify_products_page(shop_domain, access_token, limit, page_info=None)
    return products


def _get_shopify_config() -> Dict[str, Any]:
    """Resolve Shopify configuration from settings/env."""
    shop_domain = (
        getattr(settings, "shopify_store_url", None)
        or os.getenv("SHOPIFY_STORE_URL")
        or os.getenv("SHOPIFY_SHOP_DOMAIN")
        or ""
    ).strip()
    access_token = (
        getattr(settings, "shopify_access_token", None)
        or os.getenv("SHOPIFY_ACCESS_TOKEN")
        or ""
    ).strip()
    return {"shop_domain": shop_domain, "access_token": access_token}


async def _get_shopify_config_for_merchant(merchant_id: str) -> Dict[str, Any]:
    """
    Resolve Shopify configuration for a specific merchant.

    Order of precedence:
    1) Per-merchant encrypted connector_credentials (if available and decryptable).
    2) Global settings/env fallback via _get_shopify_config().
    """
    # Try per-merchant encrypted credentials first.
    try:
        credential = await get_latest_connector_credential_for_merchant(merchant_id, "shopify")
    except Exception as e:
        logger.error(
            "Failed to load connector credentials for merchant",
            extra={"merchant_id": merchant_id, "connector": "shopify", "error": str(e)},
        )
        credential = None

    if credential:
        try:
            decrypted = crypto_service.decrypt_json_secret(credential["credentials_encrypted"])
            shop_domain = (decrypted.get("shop_domain") or "").strip()
            access_token = (decrypted.get("access_token") or "").strip()
            if shop_domain and access_token:
                await mark_credential_used(credential["id"])
                return {"shop_domain": shop_domain, "access_token": access_token}
            logger.warning(
                "Connector credentials missing required Shopify fields; falling back to env",
                extra={"merchant_id": merchant_id, "credential_id": credential["id"]},
            )
        except Exception as e:
            logger.error(
                "Failed to decrypt connector credentials; falling back to env",
                extra={"merchant_id": merchant_id, "credential_id": credential.get('id'), "error": str(e)},
            )

    # Fallback to global configuration.
    return _get_shopify_config()


async def process_next_import_task() -> Dict[str, Any]:
    """
    Process the next ready ImportTask, if any.

    - Picks the oldest `pending` task
    - For Shopify connector tasks, fetches a small batch of products and records counts
    - For other tasks, simply marks as succeeded with zero counts
    """

    task = await get_next_ready_task()
    if not task:
        return {"processed": False, "reason": "no_pending_tasks"}

    task_id = task["id"]
    attempt = int(task.get("attempt", 0)) + 1
    merchant_id = task.get("merchant_id")
    source_type = task.get("source_type")
    connector = task.get("connector")

    logger.info(
        "Processing catalog import task",
        extra={
            "task_id": task_id,
            "merchant_id": merchant_id,
            "source_type": source_type,
            "connector": connector,
        },
    )

    await mark_import_task_running(task_id, attempt)

    # Default counts
    counts: Dict[str, Any] = {"total": 0}

    try:
        # Only handle Shopify connector for now; other tasks are no-ops.
        if source_type == "connector" and connector == "shopify":
            cfg = await _get_shopify_config_for_merchant(merchant_id)
            shop_domain = cfg["shop_domain"]
            access_token = cfg["access_token"]
            if not shop_domain or not access_token:
                raise ShopifyConfigError("Shopify configuration missing (SHOPIFY_STORE_URL/SHOPIFY_ACCESS_TOKEN)")

            # Pagination-aware import: fetch up to SHOPIFY_MAX_PAGES_PER_RUN pages.
            # NOTE: We keep the public constant SHOPIFY_IMPORT_LIMIT as the default page size.
            page_size = SHOPIFY_IMPORT_LIMIT
            max_pages = int(os.getenv("SHOPIFY_MAX_PAGES_PER_RUN", "5"))
            if max_pages < 1:
                logger.warning("Invalid SHOPIFY_MAX_PAGES_PER_RUN=%s, falling back to 5", max_pages)
                max_pages = 5

            max_products = int(os.getenv("SHOPIFY_MAX_PRODUCTS_PER_RUN", "500"))
            if max_products < 1:
                logger.warning("Invalid SHOPIFY_MAX_PRODUCTS_PER_RUN=%s, falling back to 500", max_products)
                max_products = 500

            started_at = datetime.utcnow()
            all_products: List[Dict[str, Any]] = []
            page_info: Optional[str] = None
            pages_fetched = 0
            succeeded = 0
            failed = 0

            while pages_fetched < max_pages and len(all_products) < max_products:
                logger.info(
                    "Fetching Shopify products page",
                    extra={
                        "task_id": task_id,
                        "merchant_id": merchant_id,
                        "page": pages_fetched + 1,
                        "page_info_present": bool(page_info),
                    },
                )

                products_page, next_page_info = await _fetch_shopify_products_page(
                    shop_domain=shop_domain,
                    access_token=access_token,
                    limit=page_size,
                    page_info=page_info,
                )

                if not products_page:
                    break

                for p in products_page:
                    try:
                        dto = ShopifyProductDTO.from_shopify_api(p)
                        platform_product_id = dto.shopify_id

                        await upsert_product_cache(
                            merchant_id=merchant_id,
                            platform="shopify",
                            platform_product_id=platform_product_id,
                            product_data=dto.to_cache_payload(),
                            ttl_seconds=3600,
                        )
                        succeeded += 1
                    except Exception as cache_exc:
                        failed += 1
                        logger.error(
                            "Failed to cache Shopify product",
                            extra={
                                "task_id": task_id,
                                "merchant_id": merchant_id,
                                "product_id": p.get("id"),
                                "error": str(cache_exc),
                            },
                        )

                all_products.extend(products_page)
                pages_fetched += 1

                logger.info(
                    "Shopify page imported",
                    extra={
                        "task_id": task_id,
                        "merchant_id": merchant_id,
                        "page": pages_fetched,
                        "products_in_page": len(products_page),
                        "total_so_far": len(all_products),
                        "has_next_page": next_page_info is not None,
                    },
                )

                if not next_page_info:
                    break

                page_info = next_page_info

                if len(all_products) >= max_products:
                    logger.info(
                        "Reached SHOPIFY_MAX_PRODUCTS_PER_RUN limit; stopping pagination",
                        extra={
                            "task_id": task_id,
                            "merchant_id": merchant_id,
                            "max_products": max_products,
                        },
                    )
                    break

            duration = (datetime.utcnow() - started_at).total_seconds()

            counts["total"] = len(all_products)
            counts["succeeded"] = succeeded
            counts["failed"] = failed
            counts["duration_sec"] = duration
            counts["pages_fetched"] = pages_fetched
            counts["page_size"] = page_size
            counts["max_pages_limit"] = max_pages

            logger.info(
                "Shopify import completed",
                extra={
                    "task_id": task_id,
                    "merchant_id": merchant_id,
                    "products_fetched": len(all_products),
                    "pages_fetched": pages_fetched,
                    "duration_sec": duration,
                },
            )

        await mark_import_task_succeeded(task_id, counts=counts)

        return {
            "processed": True,
            "task_id": task_id,
            "status": "succeeded",
            "attempt": attempt,
            "counts": counts,
        }
    except ShopifyRateLimitError as exc:
        logger.warning(
            "Shopify import rate limited; handling retry decision",
            extra={
                "task_id": task_id,
                "merchant_id": merchant_id,
                "attempt": attempt,
                "max_attempts": SHOPIFY_MAX_RETRY_ATTEMPTS,
            },
        )

        if connector == "shopify":
            counts["error_type"] = "shopify_import"
            counts["error_category"] = "rate_limit"
        else:
            counts["error_type"] = "generic"

        # If we've reached the maximum retry attempts, mark as failed instead of rescheduling.
        if attempt >= SHOPIFY_MAX_RETRY_ATTEMPTS:
            logger.warning(
                "Max retry attempts reached for Shopify import; marking task as failed",
                extra={
                    "task_id": task_id,
                    "merchant_id": merchant_id,
                    "attempt": attempt,
                    "max_attempts": SHOPIFY_MAX_RETRY_ATTEMPTS,
                },
            )
            await mark_import_task_failed(
                task_id,
                error=str(exc),
                counts=counts,
            )
            return {
                "processed": True,
                "task_id": task_id,
                "status": "failed",
                "attempt": attempt,
                "error": str(exc),
                "counts": counts,
            }

        # Otherwise schedule a retry with exponential backoff (bounded).
        backoff_seconds = min(60 * (2 ** max(attempt - 1, 0)), 3600)
        next_run_at = datetime.utcnow() + timedelta(seconds=backoff_seconds)

        await mark_import_task_retry_scheduled(
            task_id,
            error=str(exc),
            counts=counts,
            next_run_at=next_run_at,
        )
        return {
            "processed": True,
            "task_id": task_id,
            "status": "retry_scheduled",
            "attempt": attempt,
            "error": str(exc),
            "counts": counts,
            "next_run_at": next_run_at.isoformat(),
        }
    except Exception as exc:
        logger.exception("Catalog import task failed: task_id=%s", task_id)
        # Coarse error type kept for compatibility; add finer category for Shopify.
        if connector == "shopify":
            counts["error_type"] = "shopify_import"
            if isinstance(exc, ShopifyAuthError):
                counts["error_category"] = "auth"
            elif isinstance(exc, ShopifyRateLimitError):
                counts["error_category"] = "rate_limit"
            elif isinstance(exc, ShopifyConfigError):
                counts["error_category"] = "config"
            elif isinstance(exc, ShopifyAPIError):
                counts["error_category"] = "upstream"
            else:
                counts["error_category"] = "unknown"
        else:
            counts["error_type"] = "generic"

        await mark_import_task_failed(
            task_id,
            error=str(exc),
            counts=counts,
        )
        return {
            "processed": True,
            "task_id": task_id,
            "status": "failed",
            "attempt": attempt,
            "error": str(exc),
            "counts": counts,
        }
