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
import asyncio
import json
from urllib.parse import urlparse, parse_qs

import httpx
import csv
import io

from config.settings import settings
from services.platform_import_service import (
    claim_next_ready_task,
    claim_ready_task_by_id,
    requeue_stale_running_tasks,
    mark_import_task_succeeded,
    mark_import_task_failed,
    mark_import_task_retry_scheduled,
)
from db.platform_import_tasks import get_import_task, update_import_task_status
from db.connector_credentials import (
    get_latest_connector_credential_for_merchant,
    mark_credential_used,
)
from services.crypto_service import crypto_service
from services.shopify_access_token_service import (
    exchange_shopify_client_credentials_token,
    resolve_shopify_admin_access_token,
)
from catalog.recommendation_meta import derive_recommendation_meta
from db.products import upsert_product_cache, touch_products_cache_ttl
from db.platform_import_reports import get_platform_report
from db.platform_orders import insert_platform_order
from db.database import database
from models.standard_product import StandardProduct, StandardProductVariant, ProductStatus, validate_orderable
from adapters.product_adapters import ShopifyProductAdapter

# Amazon SP-API imports
from services.amazon_sp_api_service import (
    get_amazon_access_token,
    fetch_amazon_orders,
    fetch_order_items,
    convert_amazon_order_to_platform_format,
)

logger = logging.getLogger(__name__)

SHOPIFY_API_VERSION = "2025-10"
SHOPIFY_IMPORT_LIMIT = 250
SHOPIFY_MAX_RETRY_ATTEMPTS = int(os.getenv("SHOPIFY_MAX_RETRY_ATTEMPTS", "5"))
ORDERS_REQUIRED_COLUMNS = {
    "amazon": {"order_id", "order_item_id", "sku", "quantity", "price", "currency"},
    "temu": {"order_id", "product_id", "variant_id", "quantity", "price", "currency"},
}


class ShopifyAPIError(Exception):
    """Base exception for Shopify API errors."""


class ShopifyConfigError(ShopifyAPIError):
    """Raised when Shopify configuration is missing or invalid."""


class ShopifyCredentialsUnavailableError(ShopifyAPIError):
    """No usable per-merchant Shopify credentials could be resolved right now.

    Deliberately NOT a ShopifyConfigError, because that class is terminal and
    this condition is not reliably distinguishable from a transient one.
    `get_merchant_active_stores` catches its own DB errors and returns `[]`, so
    a statement timeout — which this repo has a documented history of — looks
    exactly like "this merchant has no store". Treating that as terminal would
    permanently fail a fully connected merchant's import on attempt 1 over a
    momentary blip.

    Retrying is close to free here: a merchant with no store costs ZERO Shopify
    calls per attempt (the resolver returns before any fetch), so the bounded
    retry buys transient recovery for the price of a few DB reads, and a
    genuinely storeless merchant still terminates at SHOPIFY_MAX_RETRY_ATTEMPTS.
    """


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


def _build_shopify_cache_payload(
    *,
    merchant_id: str,
    raw_shopify_product: Dict[str, Any],
    currency: Optional[str] = None,
) -> Tuple[str, Dict[str, Any], StandardProduct]:
    """
    Build a StandardProduct-shaped cache payload for Shopify products.

    This is important because many downstream endpoints assume products_cache.product_data
    can be parsed as StandardProduct. We still keep `raw` and other additive keys for
    debugging and recommendation_meta derivation.
    """
    standard_product = ShopifyProductAdapter.convert_to_standard(
        raw_shopify_product,
        merchant_id,
        currency=(currency or "USD"),
    )
    product_data: Dict[str, Any] = standard_product.model_dump(mode="json")

    platform_product_id = str(raw_shopify_product.get("id") or standard_product.id or "")
    if not platform_product_id:
        raise ValueError("Shopify product missing id")

    # Additive/compat fields for easier consumption/debugging.
    # Raw Shopify payload can be large; allow disabling it to save DB cost.
    if os.getenv("SHOPIFY_CACHE_INCLUDE_RAW", "true").lower() in ("1", "true", "yes", "y"):
        product_data["raw"] = raw_shopify_product
    product_data["shopify_id"] = platform_product_id
    product_data["handle"] = (
        raw_shopify_product.get("handle")
        or (standard_product.platform_metadata or {}).get("handle")
        or ""
    )

    return platform_product_id, product_data, standard_product


def _parse_shopify_next_page_info(link_header: Optional[str]) -> Optional[str]:
    """
    Parse Shopify Link header to extract next page_info cursor.

    Example Link header:
    <https://shop.myshopify.com/admin/api/2025-10/products.json?limit=50&page_info=XYZ>; rel="next"
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


async def _best_effort_update_store_product_count(
    *,
    merchant_id: str,
    platform: str,
    product_count: int,
) -> None:
    """
    Keep merchant_stores.product_count in sync with background imports so the merchant
    portal doesn't show 0 products while products_cache is being populated.
    """
    try:
        mid = str(merchant_id or "").strip()
        plat = str(platform or "").strip().lower()
        if not mid or not plat:
            return

        row = await database.fetch_one(
            """
            SELECT store_id
            FROM merchant_stores
            WHERE merchant_id = :merchant_id
              AND platform = :platform
              AND status IN ('active', 'connected')
            ORDER BY connected_at DESC NULLS LAST
            LIMIT 1
            """,
            {"merchant_id": mid, "platform": plat},
        )
        if not row:
            return
        store_row = dict(row)
        store_id = store_row.get("store_id")
        if not store_id:
            return

        await database.execute(
            """
            UPDATE merchant_stores
            SET product_count = :count, last_sync = CURRENT_TIMESTAMP
            WHERE store_id = :store_id
            """,
            {"count": int(product_count or 0), "store_id": store_id},
        )
    except Exception:
        # Never block import completion on a dashboard-only update.
        return


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
    else:
        # Shopify does not allow published_status together with page_info pagination.
        published_status = (os.getenv("SHOPIFY_PUBLISHED_STATUS", "any") or "").strip()
        if published_status:
            params["published_status"] = published_status
        # Keep status unset by default. Some shops return empty product lists when
        # `status=any` is provided despite valid credentials and non-empty catalogs.
        product_status = (os.getenv("SHOPIFY_PRODUCT_STATUS", "") or "").strip()
        if product_status and product_status.lower() != "any":
            params["status"] = product_status

    timeout_seconds = float(os.getenv("SHOPIFY_HTTP_TIMEOUT_SECONDS", "30"))
    if timeout_seconds < 5:
        timeout_seconds = 5.0

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
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


def _map_report_row_to_standard_product(
    merchant_id: str,
    report_type: str,
    row: Dict[str, Any],
) -> StandardProduct:
    """
    Map a single CSV report row into a StandardProduct instance.

    Phase 1: Amazon report rows
      asin, seller_sku, title, price, currency, image_url, product_type, brand, quantity_available, tags

    Phase 2: Temu report rows
      product_id, variant_id, name, price, currency, image_url, category, brand, stock
    """

    # Normalize report_type to a simple platform identifier.
    normalized_type = (report_type or "").lower()
    if normalized_type.endswith("_report"):
        normalized_type = normalized_type[: -len("_report")]

    # Amazon mapping
    if normalized_type == "amazon":
        asin = (row.get("asin") or "").strip()
        seller_sku = (row.get("seller_sku") or "").strip()
        title = (row.get("title") or "").strip()
        price_raw = row.get("price")
        currency = (row.get("currency") or "USD").strip() or "USD"

        if not asin or not seller_sku or not title or price_raw in (None, ""):
            raise ValueError("Missing required Amazon report fields (asin, seller_sku, title, price)")

        try:
            price = float(price_raw)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid price value: {price_raw}")

        qty_raw = row.get("quantity_available")
        quantity = 0
        if qty_raw not in (None, ""):
            try:
                quantity = int(qty_raw)
            except (TypeError, ValueError):
                quantity = 0

        image_url = (row.get("image_url") or "").strip() or None

        raw_tags = row.get("tags") or ""
        tags: List[str] = []
        if raw_tags:
            separator = ";" if ";" in raw_tags else ","
            tags = [t.strip() for t in raw_tags.split(separator) if t.strip()]

        # Simple completeness score for EPIC-4/6 visibility.
        score = 0.0
        if title:
            score += 0.4
        if image_url:
            score += 0.2
        if price > 0:
            score += 0.2
        if quantity > 0:
            score += 0.2
        score = round(score, 2)

        variant = StandardProductVariant(
            id=seller_sku,
            title=title,
            sku=seller_sku,
            price=price,
            inventory_quantity=quantity,
            image_url=image_url,
        )

        product = StandardProduct(
            id=asin,
            platform="amazon",
            merchant_id=merchant_id,
            title=title,
            description=None,
            vendor=row.get("brand"),
            product_type=row.get("product_type"),
            tags=tags,
            price=price,
            compare_at_price=None,
            currency=currency,
            inventory_quantity=quantity,
            sku=seller_sku,
            barcode=None,
            image_url=image_url,
            images=[image_url] if image_url else [],
            variants=[variant],
            status=ProductStatus.ACTIVE,
            published_at=None,
            created_at=None,
            updated_at=None,
            data_completeness_score=score,
            platform_metadata={"raw_report_row": row},
        )

        orderable, validation = validate_orderable(product)
        product.orderable = orderable
        product.orderable_validation = validation

        return product

    # Temu mapping
    if normalized_type == "temu":
        product_id = (row.get("product_id") or "").strip()
        variant_id = (row.get("variant_id") or "").strip()
        name = (row.get("name") or "").strip()
        price_raw = row.get("price")
        currency = (row.get("currency") or "USD").strip() or "USD"

        if not product_id or not variant_id or not name or price_raw in (None, ""):
            raise ValueError("Missing required Temu report fields (product_id, variant_id, name, price)")

        try:
            price = float(price_raw)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid price value: {price_raw}")

        stock_raw = row.get("stock")
        stock = 0
        if stock_raw not in (None, ""):
            try:
                stock = int(stock_raw)
            except (TypeError, ValueError):
                stock = 0

        image_url = (row.get("image_url") or "").strip() or None

        # Simple completeness score for EPIC-4/6 visibility.
        score = 0.0
        if name:
            score += 0.4
        if image_url:
            score += 0.2
        if price > 0:
            score += 0.2
        if stock > 0:
            score += 0.2
        score = round(score, 2)

        variant = StandardProductVariant(
            id=variant_id,
            title=name,
            sku=variant_id,
            price=price,
            inventory_quantity=stock,
            image_url=image_url,
        )

        product = StandardProduct(
            id=product_id,
            platform="temu",
            merchant_id=merchant_id,
            title=name,
            description=None,
            vendor=row.get("brand"),
            product_type=row.get("category"),
            tags=[],
            price=price,
            compare_at_price=None,
            currency=currency,
            inventory_quantity=stock,
            sku=variant_id,
            barcode=None,
            image_url=image_url,
            images=[image_url] if image_url else [],
            variants=[variant],
            status=ProductStatus.ACTIVE,
            published_at=None,
            created_at=None,
            updated_at=None,
            data_completeness_score=score,
            platform_metadata={"raw_report_row": row},
        )

        orderable, validation = validate_orderable(product)
        product.orderable = orderable
        product.orderable_validation = validation

        return product

    raise ValueError(f"Unsupported report_type for mapping: {report_type}")


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


async def _get_shopify_config_for_merchant(
    merchant_id: str,
    *,
    allow_global_fallback: bool = True,
) -> Dict[str, Any]:
    """
    Resolve Shopify configuration for a specific merchant.

    Order of precedence:
    1) Per-merchant encrypted connector_credentials (if available and decryptable).
    2) Per-merchant merchant_stores primary store (domain/api_key), when available.
    3) Global settings/env fallback via _get_shopify_config() — ONLY when
       `allow_global_fallback` is true.

    THE FALLBACK ANSWERS A DIFFERENT QUESTION THAN THE ONE ASKED. Tiers 1 and 2
    resolve "what are THIS merchant's credentials"; tier 3 returns the
    platform's own env-configured store, for any merchant, and those env vars
    are set in production. So a merchant who never connected a store — or who
    detached one — still gets a usable shop_domain + access_token back.

    That has already cost the repo once: the store-detach gate in
    readiness/sources/shopify_live.py had to switch from `shopify_connected` to
    `get_primary_store` precisely because this fallback made every merchant look
    connected (readiness/tests/test_store_detach_catalog_gate.py pins it). The
    hazard was routed around there rather than closed here, because read paths
    can tolerate a wrong-but-harmless config while write paths cannot.

    Pass `allow_global_fallback=False` from any caller that WRITES merchant-owned
    data. Read-only callers keep the historical default so their behaviour is
    unchanged.
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
            if (not access_token) and shop_domain:
                client_id = (decrypted.get("client_id") or "").strip()
                client_secret = (decrypted.get("client_secret") or "").strip()
                if client_id and client_secret:
                    refreshed, _, err = await exchange_shopify_client_credentials_token(
                        shop_domain=shop_domain,
                        client_id=client_id,
                        client_secret=client_secret,
                    )
                    if refreshed:
                        access_token = refreshed
                    else:
                        logger.warning(
                            "Connector client_credentials exchange failed; falling back",
                            extra={
                                "merchant_id": merchant_id,
                                "credential_id": credential["id"],
                                "error": err,
                            },
                        )
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

    # Try per-merchant store connection (merchant_stores / legacy mcp) before env fallback.
    try:
        from services.merchant_store_service import get_merchant_active_stores

        stores = await get_merchant_active_stores(merchant_id)
        for store in stores:
            if (store.get("platform") or "").lower() != "shopify":
                continue
            shop_domain = (store.get("domain") or store.get("shop_domain") or "").strip()
            access_token, _ = await resolve_shopify_admin_access_token(
                shop_domain=shop_domain,
                api_key_raw=store.get("api_key_raw") or store.get("api_key"),
                store_id=str(store.get("store_id") or "").strip() or None,
            )
            access_token = (access_token or "").strip()
            if shop_domain and access_token:
                return {"shop_domain": shop_domain, "access_token": access_token}
    except Exception as e:
        logger.error(
            "Failed to resolve Shopify config from merchant store; falling back to env",
            extra={"merchant_id": merchant_id, "error": str(e)},
        )

    # Fallback to global configuration — the platform's own store, not this
    # merchant's. Callers that write merchant-owned data opt out.
    if not allow_global_fallback:
        logger.warning(
            "No per-merchant Shopify credentials; refusing the global env fallback",
            extra={"merchant_id": merchant_id},
        )
        return {"shop_domain": "", "access_token": ""}
    return _get_shopify_config()


async def _ingest_orders_report_csv(
    merchant_id: str,
    platform: str,
    text: str,
    task_id: int,
) -> Dict[str, Any]:
    """
    Parse orders CSV (Amazon/Temu) into platform_orders table.
    """
    required = ORDERS_REQUIRED_COLUMNS.get(platform, set())
    counts: Dict[str, Any] = {"total": 0, "succeeded": 0, "failed": 0, "error_reasons": {}}

    try:
        reader = csv.DictReader(io.StringIO(text))
    except Exception as exc:
        raise ShopifyAPIError(f"Orders CSV parse failed: {exc}") from exc

    header = set(reader.fieldnames or [])
    missing = required.difference(header)
    if missing:
        raise ShopifyAPIError(f"Orders CSV missing columns: {sorted(missing)}")

    for row in reader:
        counts["total"] += 1
        try:
            order_id = str(row.get("order_id") or "").strip()
            if not order_id:
                raise ValueError("order_id missing")
            
            # For Temu, use variant_id as order_item_id (since Temu doesn't provide order_item_id)
            # For Amazon, use order_item_id directly
            order_item_id = (row.get("order_item_id") or "").strip() or None
            variant_id = (row.get("variant_id") or "").strip() or None
            
            # If no order_item_id but has variant_id, use variant_id as order_item_id
            if not order_item_id and variant_id:
                order_item_id = variant_id
            
            sku = (row.get("sku") or "").strip() or None
            product_id = (row.get("product_id") or sku or order_item_id or "").strip()
            qty_raw = row.get("quantity") or "0"
            price_raw = row.get("price") or "0"
            quantity = int(float(qty_raw))
            price = float(price_raw)
            currency = (row.get("currency") or "").strip() or "USD"

            if quantity <= 0:
                raise ValueError("quantity must be >= 1")

            order_payload = {
                "order_id": order_id,
                "order_item_id": order_item_id,
                "platform": platform,
                "merchant_id": merchant_id,
                "status": "imported",
                "items": [
                    {
                        "product_id": product_id,
                        "variant_id": variant_id,
                        "sku": sku,
                        "quantity": quantity,
                        "price": price,
                        "currency": currency,
                    }
                ],
                "raw": row,
                "import_task_id": task_id,
            }

            inserted = await insert_platform_order(
                merchant_id=merchant_id,
                platform=platform,
                order_id=order_id,
                order_item_id=order_item_id,
                data=order_payload,
                import_task_id=task_id,
            )

            if inserted is None:
                counts["failed"] += 1
                counts["error_reasons"]["insert_failed"] = counts["error_reasons"].get("insert_failed", 0) + 1
            else:
                counts["succeeded"] += 1
        except Exception as exc:
            counts["failed"] += 1
            reason = str(exc).split(":")[0]
            counts["error_reasons"][reason] = counts["error_reasons"].get(reason, 0) + 1

    return counts


async def _process_import_task_record(task: Dict[str, Any]) -> Dict[str, Any]:
    """Run one ImportTask that the caller has ALREADY claimed.

    `task` must be the row returned by `claim_next_ready_task` /
    `claim_ready_task_by_id` — status is already `running` and `attempt` is
    already incremented, so this function must not do either again.
    """
    task_id = task["id"]
    attempt = int(task.get("attempt") or 1)
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

    # NOT marked running here: the atomic claim already did it. Re-marking would
    # re-open the double-run window the claim exists to close.

    # Counts are stored as JSON; if the task is retry_scheduled we may resume from prior progress.
    existing_counts = task.get("counts") if isinstance(task.get("counts"), dict) else {}
    counts: Dict[str, Any] = dict(existing_counts or {})
    counts.setdefault("total", 0)

    try:
        # Shopify connector import branch.
        if source_type == "connector" and connector == "shopify":
            # allow_global_fallback=False: this branch WRITES the merchant's
            # catalog. Falling back to the platform's env store would import the
            # platform's own products into this merchant's products_cache and
            # then expire their real rows in the full-sync sweep below — a
            # silent cross-merchant catalog overwrite, not a degraded import.
            # Failing here is the correct outcome: ShopifyConfigError is
            # terminal (see the retry handler), so a merchant with no
            # credentials fails fast instead of retrying five times.
            cfg = await _get_shopify_config_for_merchant(
                merchant_id, allow_global_fallback=False
            )
            shop_domain = cfg["shop_domain"]
            access_token = cfg["access_token"]
            if not shop_domain or not access_token:
                # The dominant reader of this string is a MERCHANT: the sync
                # status endpoint returns the task row verbatim, with no message
                # mapping. And the likeliest reader is someone whose Integrations
                # page shows their store as connected — the endpoint gates on a
                # merchant_stores row and never resolves a token, so a store with
                # an unusable api_key passes the gate and lands here. "You have no
                # store" would tell them to redo what they already did, and naming
                # connector_credentials points them at an internal table.
                raise ShopifyCredentialsUnavailableError(
                    "Could not resolve a Shopify access token for this merchant. "
                    "If your store shows as connected, please reconnect it in Integrations."
                )

            shop_currency: Optional[str] = None
            try:
                shop_currency = await ShopifyProductAdapter.fetch_shop_currency(
                    shop_domain=shop_domain,
                    access_token=access_token,
                    api_version=SHOPIFY_API_VERSION,
                )
            except Exception:
                shop_currency = None

            # Pagination-aware import: fetch up to SHOPIFY_MAX_PAGES_PER_RUN pages.
            page_size = int(os.getenv("SHOPIFY_IMPORT_LIMIT", str(SHOPIFY_IMPORT_LIMIT)))
            if page_size < 1 or page_size > 250:
                logger.warning("Invalid SHOPIFY_IMPORT_LIMIT=%s, falling back to %s", page_size, SHOPIFY_IMPORT_LIMIT)
                page_size = SHOPIFY_IMPORT_LIMIT
            max_pages = int(os.getenv("SHOPIFY_MAX_PAGES_PER_RUN", "50"))
            if max_pages < 1:
                logger.warning("Invalid SHOPIFY_MAX_PAGES_PER_RUN=%s, falling back to 50", max_pages)
                max_pages = 50

            max_products = int(os.getenv("SHOPIFY_MAX_PRODUCTS_PER_RUN", "5000"))
            if max_products < 1:
                logger.warning("Invalid SHOPIFY_MAX_PRODUCTS_PER_RUN=%s, falling back to 5000", max_products)
                max_products = 5000

            max_runtime_seconds = int(os.getenv("SHOPIFY_MAX_RUNTIME_SECONDS", "600"))
            if max_runtime_seconds < 10:
                logger.warning("Invalid SHOPIFY_MAX_RUNTIME_SECONDS=%s, falling back to 600", max_runtime_seconds)
                max_runtime_seconds = 600

            shopify_cache_ttl_seconds = int(os.getenv("SHOPIFY_PRODUCTS_CACHE_TTL_SECONDS", "604800"))
            min_ttl_seconds = int(os.getenv("SHOPIFY_PRODUCTS_CACHE_TTL_MIN_SECONDS", "86400"))
            if shopify_cache_ttl_seconds < min_ttl_seconds:
                logger.warning(
                    "SHOPIFY_PRODUCTS_CACHE_TTL_SECONDS=%s is below minimum=%s; using minimum",
                    shopify_cache_ttl_seconds,
                    min_ttl_seconds,
                )
                shopify_cache_ttl_seconds = min_ttl_seconds

            sync_only_orderable = os.getenv("SHOPIFY_SYNC_ONLY_ORDERABLE", "false").lower() in ("1", "true", "yes", "y")

            touch_existing_cache = os.getenv("SHOPIFY_TOUCH_EXISTING_CACHE", "true").lower() in (
                "1",
                "true",
                "yes",
                "y",
            )
            if touch_existing_cache:
                try:
                    touched = await touch_products_cache_ttl(
                        merchant_id=merchant_id,
                        platform="shopify",
                        ttl_seconds=shopify_cache_ttl_seconds,
                        # Do not revive expired rows: we explicitly expire Shopify cache on store reconnect.
                        include_expired=False,
                    )
                    counts["touched_existing_cache_rows"] = touched
                except Exception as exc:
                    logger.exception(
                        "Failed to extend TTL for existing Shopify cache rows",
                        extra={
                            "task_id": task_id,
                            "merchant_id": merchant_id,
                            "error": str(exc),
                        },
                    )

            # Resume cursor if this task previously stopped mid-pagination.
            page_info_value = counts.get("shopify_next_page_info")
            page_info: Optional[str] = str(page_info_value) if page_info_value else None

            started_at = datetime.utcnow()
            full_sync_started_at = started_at
            total = int(counts.get("total", 0) or 0)
            succeeded = int(counts.get("succeeded", 0) or 0)
            failed = int(counts.get("failed", 0) or 0)
            skipped = int(counts.get("skipped", 0) or 0)
            pages_fetched_total = int(counts.get("pages_fetched", 0) or 0)

            run_total = 0
            run_pages_fetched = 0
            stop_reason: Optional[str] = None

            # Running progress snapshot for UI/status polling.
            counts.update(
                {
                    "total": total,
                    "succeeded": succeeded,
                    "failed": failed,
                    "skipped": skipped,
                    "pages_fetched": pages_fetched_total,
                    "page_size": page_size,
                    "max_pages_limit": max_pages,
                    "max_products_limit": max_products,
                    "has_next_page": page_info is not None,
                    "shopify_next_page_info": page_info,
                }
            )

            while run_pages_fetched < max_pages and run_total < max_products:
                runtime_seconds = (datetime.utcnow() - started_at).total_seconds()
                if runtime_seconds >= max_runtime_seconds:
                    logger.info(
                        "Reached SHOPIFY_MAX_RUNTIME_SECONDS limit; stopping pagination",
                        extra={
                            "task_id": task_id,
                            "merchant_id": merchant_id,
                            "max_runtime_seconds": max_runtime_seconds,
                            "runtime_seconds": runtime_seconds,
                            "total_so_far": total,
                            "run_pages_fetched": run_pages_fetched,
                            "has_next_page": page_info is not None,
                        },
                    )
                    stop_reason = "max_runtime_seconds"
                    break

                logger.info(
                    "Fetching Shopify products page",
                    extra={
                        "task_id": task_id,
                        "merchant_id": merchant_id,
                        "page": run_pages_fetched + 1,
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
                    page_info = None
                    break

                for p in products_page:
                    try:
                        platform_product_id, product_data, standard_product = _build_shopify_cache_payload(
                            merchant_id=merchant_id,
                            raw_shopify_product=p,
                            currency=shop_currency,
                        )

                        if sync_only_orderable and not bool(getattr(standard_product, "orderable", False)):
                            skipped += 1
                            continue

                        # Enrich with recommendation_meta (best-effort; never blocks import).
                        recommendation_meta = None
                        try:
                            recommendation_meta = derive_recommendation_meta(
                                standard_product=standard_product,
                                raw_shopify_product=p,
                            )
                        except Exception as meta_exc:
                            # Parsing recommendation metadata must never break catalog
                            # imports. Log a structured error and continue with an
                            # explicit empty meta structure so downstream code can
                            # rely on the field existing.
                            logger.exception(
                                "Failed to derive recommendation_meta for Shopify product",
                                extra={
                                    "task_id": task_id,
                                    "merchant_id": merchant_id,
                                    "platform_product_id": platform_product_id,
                                    "raw_tags": p.get("tags"),
                                },
                            )
                            recommendation_meta = {
                                "version": 1,
                                "group_id": None,
                                "tags_raw": [],
                                "tags": [],
                                "facets": {},
                                "parse_error": True,
                            }

                        product_data["recommendation_meta"] = recommendation_meta

                        await upsert_product_cache(
                            merchant_id=merchant_id,
                            platform="shopify",
                            platform_product_id=platform_product_id,
                            product_data=product_data,
                            ttl_seconds=shopify_cache_ttl_seconds,
                        )
                        succeeded += 1
                    except Exception as cache_exc:
                        failed += 1
                        extra = {
                            "task_id": task_id,
                            "merchant_id": merchant_id,
                            "product_id": p.get("id"),
                            "platform_product_id": str(p.get("id") or ""),
                            "error_type": type(cache_exc).__name__,
                            "error": str(cache_exc),
                        }
                        # Avoid emitting a full traceback for every product in large catalogs.
                        if failed <= 3 or failed % 50 == 0:
                            logger.exception("Failed to cache Shopify product", extra=extra)
                        else:
                            logger.error("Failed to cache Shopify product", extra=extra)

                run_total += len(products_page)
                total += len(products_page)
                run_pages_fetched += 1
                pages_fetched_total += 1

                counts["total"] = total
                counts["succeeded"] = succeeded
                counts["failed"] = failed
                counts["skipped"] = skipped
                counts["pages_fetched"] = pages_fetched_total
                counts["run_total"] = run_total
                counts["run_pages_fetched"] = run_pages_fetched
                counts["has_next_page"] = next_page_info is not None
                counts["shopify_next_page_info"] = next_page_info or None

                # Best-effort progress update so the merchant portal can show activity.
                await update_import_task_status(
                    task_id=task_id,
                    status="running",
                    counts=counts,
                    attempt=attempt,
                )
                await _best_effort_update_store_product_count(
                    merchant_id=merchant_id,
                    platform="shopify",
                    product_count=succeeded,
                )

                logger.info(
                    "Shopify page imported",
                    extra={
                        "task_id": task_id,
                        "merchant_id": merchant_id,
                        "page": run_pages_fetched,
                        "products_in_page": len(products_page),
                        "total_so_far": total,
                        "has_next_page": next_page_info is not None,
                    },
                )

                if not next_page_info:
                    page_info = None
                    break

                page_info = next_page_info

                if run_total >= max_products:
                    logger.info(
                        "Reached SHOPIFY_MAX_PRODUCTS_PER_RUN limit; stopping pagination",
                        extra={
                            "task_id": task_id,
                            "merchant_id": merchant_id,
                            "max_products": max_products,
                        },
                    )
                    stop_reason = "max_products_per_run"
                    break

            duration = (datetime.utcnow() - started_at).total_seconds()

            counts["total"] = total
            counts["succeeded"] = succeeded
            counts["failed"] = failed
            counts["skipped"] = skipped
            counts["duration_sec"] = duration
            counts["pages_fetched"] = pages_fetched_total
            counts["run_total"] = run_total
            counts["run_pages_fetched"] = run_pages_fetched
            counts["page_size"] = page_size
            counts["max_pages_limit"] = max_pages
            counts["has_next_page"] = page_info is not None
            counts["shopify_next_page_info"] = page_info or None

            logger.info(
                "Shopify import completed",
                extra={
                    "task_id": task_id,
                    "merchant_id": merchant_id,
                    "products_fetched": total,
                    "pages_fetched": pages_fetched_total,
                    "run_products_fetched": run_total,
                    "run_pages_fetched": run_pages_fetched,
                    "duration_sec": duration,
                    "has_next_page": page_info is not None,
                },
            )

            # If we finished a full catalog run (no cursor remaining), expire any older
            # cache rows that were not touched by this sync. This prevents inflated counts
            # after store reconnects or interrupted historical imports.
            if page_info is None and stop_reason is None:
                try:
                    expired = await database.execute(
                        """
                        UPDATE products_cache
                        SET expires_at = NOW()
                        WHERE merchant_id = :merchant_id
                          AND platform = 'shopify'
                          AND cached_at < :started_at
                          AND (expires_at IS NULL OR expires_at > NOW())
                        """,
                        {"merchant_id": merchant_id, "started_at": full_sync_started_at},
                    )
                    counts["expired_stale_cache_rows"] = int(expired or 0)
                except Exception:
                    pass

            # If we still have a cursor, we didn't finish; schedule a continuation run.
            if page_info:
                if stop_reason is None:
                    if run_pages_fetched >= max_pages:
                        stop_reason = "max_pages_per_run"
                    else:
                        stop_reason = "page_limit"

                counts["partial_import"] = True
                counts["stop_reason"] = stop_reason

                continuation_delay = int(os.getenv("SHOPIFY_CONTINUATION_DELAY_SECONDS", "30"))
                if continuation_delay < 5:
                    continuation_delay = 5

                next_run_at = datetime.utcnow() + timedelta(seconds=continuation_delay)
                await update_import_task_status(
                    task_id=task_id,
                    status="retry_scheduled",
                    counts=counts,
                    attempt=attempt,
                    next_run_at=next_run_at,
                )
                await _best_effort_update_store_product_count(
                    merchant_id=merchant_id,
                    platform="shopify",
                    product_count=succeeded,
                )

                return {
                    "processed": True,
                    "task_id": task_id,
                    "status": "retry_scheduled",
                    "attempt": attempt,
                    "counts": counts,
                }

        # Amazon SP-API orders sync
        elif source_type == "amazon_orders":
            logger.info(
                "Processing Amazon SP-API orders sync task",
                extra={
                    "task_id": task_id,
                    "merchant_id": merchant_id,
                    "connector": connector,
                },
            )
            
            # Get Amazon credentials
            cred = await get_latest_connector_credential_for_merchant(
                merchant_id, "amazon_sp_api"
            )
            if not cred:
                raise Exception(f"No Amazon SP-API credentials for merchant {merchant_id}")
            
            # Decrypt credentials
            credentials = crypto_service.decrypt_json_secret(cred["credentials_encrypted"])
            refresh_token = credentials.get("refresh_token")
            marketplace_id = credentials.get("marketplace_id", "ATVPDKIKX0DER")
            region = credentials.get("region", "na")
            
            if not refresh_token:
                raise Exception("No refresh_token in credentials")
            
            # Get access token
            logger.info(f"Refreshing Amazon access token for merchant {merchant_id}")
            access_token = await get_amazon_access_token(refresh_token)
            
            # Mark credential as used
            await mark_credential_used(cred["id"])
            
            # Determine sync window (default: last 7 days)
            created_after = datetime.utcnow() - timedelta(days=7)
            created_before = datetime.utcnow()
            
            logger.info(
                f"Fetching Amazon orders for merchant {merchant_id} "
                f"(from {created_after.isoformat()} to {created_before.isoformat()})"
            )
            
            # Fetch orders
            orders = await fetch_amazon_orders(
                access_token=access_token,
                marketplace_id=marketplace_id,
                created_after=created_after,
                created_before=created_before,
                region=region,
            )
            
            logger.info(f"Fetched {len(orders)} orders from Amazon SP-API")
            
            # Process each order
            total = 0
            succeeded = 0
            skipped = 0
            failed = 0
            
            for order in orders:
                try:
                    amazon_order_id = order.get("AmazonOrderId")
                    
                    # Fetch order items
                    order_items = await fetch_order_items(
                        access_token=access_token,
                        order_id=amazon_order_id,
                        region=region,
                    )
                    
                    if not order_items:
                        logger.warning(f"No items found for order {amazon_order_id}, skipping")
                        skipped += 1
                        continue
                    
                    # Convert to platform_orders format
                    platform_records = convert_amazon_order_to_platform_format(
                        merchant_id=merchant_id,
                        order=order,
                        order_items=order_items,
                        marketplace_id=marketplace_id,
                    )
                    
                    # Insert each record (one per item)
                    for record in platform_records:
                        total += 1
                        inserted_id = await insert_platform_order(
                            merchant_id=record["merchant_id"],
                            platform=record["platform"],
                            order_id=record["order_id"],
                            order_item_id=record["order_item_id"],
                            data=record["data"],
                            import_task_id=task_id,
                        )
                        
                        if inserted_id:
                            succeeded += 1
                        else:
                            # Duplicate (unique constraint)
                            skipped += 1
                
                except Exception as order_exc:
                    failed += 1
                    logger.error(
                        "Failed to process Amazon order",
                        extra={
                            "task_id": task_id,
                            "merchant_id": merchant_id,
                            "order_id": order.get("AmazonOrderId"),
                            "error": str(order_exc),
                        },
                    )
            
            duration = (datetime.utcnow() - started_at).total_seconds()
            counts["orders_fetched"] = len(orders)
            counts["total"] = total
            counts["succeeded"] = succeeded
            counts["skipped"] = skipped
            counts["failed"] = failed
            counts["duration_sec"] = duration
            counts["created_after"] = created_after.isoformat()
            counts["created_before"] = created_before.isoformat()
            counts["marketplace_id"] = marketplace_id
            
            logger.info(
                "Amazon SP-API orders sync completed",
                extra={
                    "task_id": task_id,
                    "merchant_id": merchant_id,
                    "orders_fetched": len(orders),
                    "total_items": total,
                    "succeeded": succeeded,
                    "skipped": skipped,
                    "failed": failed,
                    "duration_sec": duration,
                },
            )
        
        # Orders report ingest (Amazon/Temu orders CSV -> platform_orders)
        elif source_type == "orders_report":
            saga_id = task.get("saga_id")
            if not saga_id:
                raise RuntimeError("orders_report task missing saga_id (report_id)")
            try:
                report_id = int(saga_id)
            except (TypeError, ValueError):
                raise RuntimeError(f"Invalid saga_id for orders_report task: {saga_id}")

            report = await get_platform_report(report_id)
            if not report:
                raise RuntimeError(f"Platform orders report not found for report_id={report_id}")

            platform = (report.get("report_type") or "").strip().lower()
            raw_content = report.get("raw_content") or ""
            if not raw_content:
                raise RuntimeError(f"Platform orders report {report_id} has empty content")

            counts = await _ingest_orders_report_csv(
                merchant_id=merchant_id,
                platform=platform,
                text=raw_content,
                task_id=task_id,
            )
            counts["report_id"] = report_id

        # Report-based import branch (EPIC-6 - Amazon/Temu CSV).
        elif source_type == "report" and connector in ("amazon_report", "temu_report"):
            started_at = datetime.utcnow()
            counts = {"total": 0, "succeeded": 0, "failed": 0}

            saga_id = task.get("saga_id")
            if not saga_id:
                raise RuntimeError("ImportTask for report is missing saga_id (report_id)")

            try:
                report_id = int(saga_id)
            except (TypeError, ValueError):
                raise RuntimeError(f"Invalid saga_id for report ImportTask: {saga_id}")

            report = await get_platform_report(report_id)
            if not report:
                raise RuntimeError(f"Platform report not found for report_id={report_id}")

            raw_content = report.get("raw_content") or ""
            if not raw_content:
                raise RuntimeError(f"Platform report {report_id} has empty content")

            try:
                reader = csv.DictReader(io.StringIO(raw_content))
            except Exception as exc:
                raise RuntimeError(f"Failed to parse report CSV: {exc}") from exc

            total = 0
            succeeded = 0
            failed = 0
            error_reasons: Dict[str, int] = {}

            # Derive platform from connector name.
            platform = "amazon" if connector == "amazon_report" else "temu"

            for row in reader:
                total += 1
                try:
                    product = _map_report_row_to_standard_product(
                        merchant_id=merchant_id,
                        report_type=report.get("report_type") or connector,
                        row=row,
                    )
                    await upsert_product_cache(
                        merchant_id=merchant_id,
                        platform=platform,
                        platform_product_id=product.id,
                        product_data=product.model_dump(),
                        ttl_seconds=3600,
                    )
                    succeeded += 1
                except Exception as row_exc:
                    failed += 1
                    reason = type(row_exc).__name__
                    error_reasons[reason] = error_reasons.get(reason, 0) + 1
                    logger.error(
                        "Failed to import platform report row",
                        extra={
                            "task_id": task_id,
                            "merchant_id": merchant_id,
                            "report_id": report_id,
                            "error": str(row_exc),
                        },
                    )

            duration = (datetime.utcnow() - started_at).total_seconds()
            counts["total"] = total
            counts["succeeded"] = succeeded
            counts["failed"] = failed
            counts["duration_sec"] = duration
            counts["report_id"] = report_id
            counts["report_type"] = report.get("report_type")
            counts["platform"] = platform
            if failed:
                counts["error_type"] = "report_import"
                counts["error_category"] = "row_mapping"
                counts["error_reasons"] = error_reasons

            logger.info(
                "Platform report import completed",
                extra={
                    "task_id": task_id,
                    "merchant_id": merchant_id,
                    "report_id": report_id,
                    "report_type": report.get("report_type"),
                    "platform": platform,
                    "rows_total": report.get("rows_total"),
                    "total": total,
                    "succeeded": succeeded,
                    "failed": failed,
                    "duration_sec": duration,
                },
            )

        await mark_import_task_succeeded(task_id, counts=counts)
        if connector == "shopify":
            await _best_effort_update_store_product_count(
                merchant_id=merchant_id,
                platform="shopify",
                product_count=int(counts.get("succeeded") or 0),
            )

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
    except ShopifyAPIError as exc:
        # Retry transient upstream failures (timeouts, 5xx, etc) but fail fast for
        # auth/config issues which won't be resolved by retries.
        logger.warning(
            "Shopify import failed; handling retry decision",
            extra={
                "task_id": task_id,
                "merchant_id": merchant_id,
                "attempt": attempt,
                "max_attempts": SHOPIFY_MAX_RETRY_ATTEMPTS,
                "error_type": type(exc).__name__,
            },
        )

        counts["error_type"] = "shopify_import"
        if isinstance(exc, ShopifyAuthError):
            counts["error_category"] = "auth"
        elif isinstance(exc, ShopifyConfigError):
            counts["error_category"] = "config"
        elif isinstance(exc, ShopifyCredentialsUnavailableError):
            # Distinct from "config" on purpose: a spike in THIS category means
            # either a credential-resolution outage or a cohort of merchants
            # whose stores no longer resolve, and both want a human. It is the
            # signal to watch when CATALOG_IMPORT_DRAIN_ENABLED is first armed.
            counts["error_category"] = "credentials_unavailable"
        else:
            counts["error_category"] = "upstream"

        if isinstance(exc, (ShopifyAuthError, ShopifyConfigError)) or attempt >= SHOPIFY_MAX_RETRY_ATTEMPTS:
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

        backoff_seconds = min(30 * (2 ** max(attempt - 1, 0)), 1800)
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
    except asyncio.CancelledError as exc:
        # Ensure we don't leave tasks stuck in `running` if the process is shutting down
        # or the coroutine is cancelled by the runtime (e.g., deploy/restart).
        logger.warning(
            "Catalog import task cancelled; scheduling retry",
            extra={
                "task_id": task_id,
                "merchant_id": merchant_id,
                "connector": connector,
                "attempt": attempt,
            },
        )

        counts["error_type"] = "cancelled"
        backoff_seconds = 60
        next_run_at = datetime.utcnow() + timedelta(seconds=backoff_seconds)

        # Shield the status update from immediate cancellation so we persist the state.
        await asyncio.shield(
            mark_import_task_retry_scheduled(
                task_id,
                error=f"cancelled: {str(exc) or 'CancelledError'}",
                counts=counts,
                next_run_at=next_run_at,
            )
        )

        return {
            "processed": True,
            "task_id": task_id,
            "status": "retry_scheduled",
            "attempt": attempt,
            "error": "cancelled",
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


def _record_import_outcome(task: Dict[str, Any], result: Dict[str, Any]) -> None:
    """Emit the terminal outcome of one import task to the metrics counter.

    Called at the two ENTRY POINTS rather than inside
    _process_import_task_record, which has a dozen return statements across five
    exception handlers — one call site per entry point covers every path,
    including ones added later, and cannot drift out of sync with a new branch.

    Only ever called with a PROCESSED result: both call sites sit after
    _process_import_task_record returns, and every one of its eight return paths
    sets processed=True. The un-processed outcomes — no_pending_tasks,
    task_not_found, task_not_ready — return earlier than this and are
    deliberately not counted: a drain tick on an empty queue fires every 30s
    forever, and counting those would swamp the series and make a real failure
    rate unreadable. A `if not result.get("processed")` guard here would be
    unreachable, so there isn't one.

    Best-effort by construction: a metrics failure must never turn a successful
    import into a failed one, so everything here is swallowed.
    """
    try:
        from observability.reliability_metrics import record_catalog_import_task

        counts = result.get("counts")
        record_catalog_import_task(
            connector=task.get("connector"),
            status=str(result.get("status") or "unknown"),
            error_category=(counts or {}).get("error_category") if isinstance(counts, dict) else None,
        )
    except Exception:  # noqa: BLE001 — observability must not break the import
        logger.debug("Failed to record catalog import metrics", exc_info=True)


async def process_next_import_task() -> Dict[str, Any]:
    """
    Claim and process the next ready ImportTask, if any.

    - Atomically claims the oldest `pending`/`retry_scheduled` task
    - For Shopify connector tasks, fetches a small batch of products and records counts
    - For other tasks, performs the corresponding import and marks status

    Returns `reason="no_pending_tasks"` both when the queue is empty and when a
    racing runner claimed the candidate first — from this runner's point of view
    those are the same outcome: there is nothing for it to do.
    """
    task = await claim_next_ready_task()
    if not task:
        return {"processed": False, "reason": "no_pending_tasks"}
    result = await _process_import_task_record(task)
    _record_import_outcome(task, result)
    return result


async def process_import_task_by_id(task_id: int) -> Dict[str, Any]:
    """
    Claim and process a specific ImportTask by ID (best-effort).

    Intended for APIs that want to schedule a task and immediately kick off processing
    without waiting for the drain tick in services/audit_scheduler.py.

    The claim is what makes it safe for BOTH to fire at once: whichever gets the
    conditional UPDATE first runs the import, and the loser reports
    `task_not_ready` instead of importing the same catalog a second time.
    """
    task = await claim_ready_task_by_id(task_id)
    if task:
        result = await _process_import_task_record(task)
        _record_import_outcome(task, result)
        return result

    # Claim failed: either the row is gone or it was not in a claimable state
    # (already running/succeeded/failed, or another runner just took it).
    existing = await get_import_task(task_id)
    if not existing:
        return {"processed": False, "reason": "task_not_found", "task_id": task_id}
    return {
        "processed": False,
        "reason": "task_not_ready",
        "task_id": task_id,
        "status": existing.get("status"),
    }


def _catalog_import_drain_enabled() -> bool:
    """Whether the scheduler drain tick and its reaper may do anything.

    DORMANT BY DEFAULT, matching the two closest precedents in
    services/audit_scheduler.py — `catalog_onboard_queue_drain` ("OFF BY DEFAULT
    ... so deploying never starts autonomous catalog writes") and
    `payment_reconcile_tick` ("DORMANT unless ... enable deliberately"). Every
    reason those give applies here and then some: this drains a queue nothing has
    ever drained, so the backlog is unmeasured and may be months old; each task
    calls a merchant's Shopify Admin API with stored credentials and rewrites
    their products_cache; and prod and staging share one Postgres.

    So arming it is a separate, deliberate act from deploying it. Measure the
    backlog first:

        SELECT status, source_type, connector, count(*), min(created_at)
        FROM platform_import_tasks GROUP BY 1, 2, 3;

    then set CATALOG_IMPORT_DRAIN_ENABLED=true. Read per-run, so flipping it
    takes effect on the next tick.
    """
    return (os.getenv("CATALOG_IMPORT_DRAIN_ENABLED", "false") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


async def run_catalog_import_drain_tick() -> Dict[str, Any]:
    """Scheduler tick: drain ONE ready ImportTask per fire.

    Registered as `catalog_import_drain_tick` in services/audit_scheduler.py.
    Before this existed the ONLY runner was the request-scoped BackgroundTask in
    routes/merchant_api_extensions.py, which dies with the process on a Cloud Run
    revision swap or scale-down — leaving the row `pending` with nobody looking
    at it, because `process_next_import_task` had zero callers anywhere.

    One task per tick (not a drain loop) so a slow Shopify import cannot hold the
    run past its deadline; `max_instances=1` + a 30s interval means the queue
    still empties steadily.
    """
    if not _catalog_import_drain_enabled():
        return {"processed": False, "reason": "disabled"}
    return await process_next_import_task()


async def run_catalog_import_stale_reaper_tick() -> Dict[str, Any]:
    """Scheduler tick: return abandoned `running` ImportTasks to the queue.

    Registered as `catalog_import_stale_reaper` in services/audit_scheduler.py.
    Separate from the drain tick on purpose: the drain tick holds
    `max_instances=1` for the length of a real import (up to
    SHOPIFY_MAX_RUNTIME_SECONDS), and recovery must not queue behind that.
    """
    if not _catalog_import_drain_enabled():
        return {"requeued": 0, "reason": "disabled"}
    requeued = await requeue_stale_running_tasks(
        stale_after_seconds=_env_int("CATALOG_IMPORT_STALE_AFTER_SECONDS", None),
        limit=_env_int("CATALOG_IMPORT_STALE_REQUEUE_LIMIT", 5) or 5,
        # Poison-pill bound. A task that kills its process never reaches the
        # worker's attempt cutoffs (they all sit in `except` handlers), so the
        # reaper has to apply one itself or an OOM-inducing row is requeued
        # forever. Same ceiling the retry handlers use.
        max_attempt=SHOPIFY_MAX_RETRY_ATTEMPTS,
    )
    if requeued:
        logger.warning("Requeued %s stale catalog import task(s)", requeued)
    return {"requeued": requeued}


def _env_int(name: str, default: Optional[int]) -> Optional[int]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r; using default %s", name, raw, default)
        return default
