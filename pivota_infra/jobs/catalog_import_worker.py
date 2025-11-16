"""
Catalog Import Worker - EPIC‑2 Shopify Phase 1 & 2

This worker processes ImportTasks for Platform merchants.

For Shopify connector tasks we:
- Fetch a small batch of products from Shopify Admin API
- Normalize them into a minimal DTO
- Write them into the products_cache table only (no core tables)
- Record counts and basic timing in the ImportTask
"""

from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import os

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


async def _fetch_shopify_products(shop_domain: str, access_token: str, limit: int) -> List[Dict[str, Any]]:
    """Fetch a small batch of products from Shopify."""
    url = f"https://{shop_domain}/admin/api/{SHOPIFY_API_VERSION}/products.json"
    params = {"limit": limit}

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
    return data.get("products", []) or []


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

            started_at = datetime.utcnow()
            products = await _fetch_shopify_products(shop_domain, access_token, SHOPIFY_IMPORT_LIMIT)
            duration = (datetime.utcnow() - started_at).total_seconds()

            # Phase 2: write products into cache layer only (products_cache)
            succeeded = 0
            failed = 0
            for p in products:
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

            counts["total"] = len(products)
            counts["succeeded"] = succeeded
            counts["failed"] = failed
            counts["duration_sec"] = duration

            logger.info(
                "Shopify import completed",
                extra={
                    "task_id": task_id,
                    "merchant_id": merchant_id,
                    "products_fetched": len(products),
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
            "Shopify import rate limited; scheduling retry",
            extra={"task_id": task_id, "merchant_id": merchant_id, "attempt": attempt},
        )

        if connector == "shopify":
            counts["error_type"] = "shopify_import"
            counts["error_category"] = "rate_limit"
        else:
            counts["error_type"] = "generic"

        # Simple exponential backoff with upper bound (in seconds).
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
