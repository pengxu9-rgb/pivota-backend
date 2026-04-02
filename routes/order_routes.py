"""
订单处理 API 路由
Pivota 核心业务流程：Agent 下单 → 支付 → 履约
"""

from services.merchant_store_service import get_merchant_active_stores, get_primary_store, get_store_by_id
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Response, Header, Query, status
from typing import Optional, List, Dict, Any, Tuple
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta
from urllib.parse import urlencode
import asyncio
import time
import hashlib
import httpx
import os
import json
from contextlib import asynccontextmanager
from sqlalchemy import and_, or_, select

from models.order import (
    CreateOrderRequest, OrderResponse, PaymentConfirmRequest, 
    OrderListResponse, OrderItem, OrderStatus
)
from db.orders import (
    create_order, get_order, get_orders_by_merchant, get_orders_by_customer,
    update_order_status, update_payment_info, mark_order_paid, 
    update_fulfillment_info, mark_order_shipped, get_order_stats, update_order as update_order_row
)
from db.orders import orders as orders_table
from db.merchant_onboarding import get_merchant_onboarding
from db.products import log_order_event
from db.database import database, IS_POSTGRES
from utils.auth import require_admin, require_admin_or_key, get_current_user
from adapters.psp_adapter import get_psp_adapter
from adapters.multi_psp_orchestrator import create_payment_with_failover
from utils.logger import logger
from services.payment_routing_service import PaymentRoutingService
from services.merchant_payment_initiation_service import build_payment_action
from services.merchant_psp_config_service import (
    build_runtime_adapter_kwargs,
    fetch_active_runtime_merchant_psp,
    infer_runtime_provider,
)
from services.promotions_service import list_promotions, PromotionStatus
from services.commerce_attribution_service import (
    PVT_CLICK_ID,
    PVT_PRODUCT_ID,
    PVT_PROMPT_CLUSTER,
    PVT_SURFACE,
    PVT_VARIANT_ID,
    has_attribution_signal,
    materialize_attribution_context,
    upsert_order_attribution_edge,
)
from services.quote_service import (
    QuoteError,
    QuoteService,
    compute_request_fingerprint,
    normalize_discount_codes,
    normalize_items_for_fingerprint,
    normalize_shipping_for_fingerprint,
    parse_decimal_money,
)
from services.shopify_transactions_service import (
    extract_shopify_access_token,
    ensure_external_payment_transaction_best_effort,
)
from services.shopify_access_token_service import resolve_shopify_admin_access_token
from services.merchant_webhook_service import emit_merchant_webhook_event
from adapters.woocommerce_adapter import normalize_woocommerce_store_url
from adapters.bigcommerce_adapter import (
    build_bigcommerce_domain,
    build_bigcommerce_headers,
    normalize_bigcommerce_store_hash,
)
from services.traffic_taxonomy_service import attach_traffic_taxonomy, build_traffic_taxonomy
from routes.reviews_invitation_issuer import (
    SendInvitationEmailFromOrderRequest,
    send_invitation_email_from_order,
    _internal_key as _reviews_invitation_internal_key,
    _invitation_send_delay_seconds as _reviews_invitation_send_delay_seconds,
    enqueue_invitation_email_send_job_from_order,
)
from services.reviews_invitation_send_jobs_service import (
    enqueue_invitation_send_job_from_order as enqueue_reviews_invitation_send_job_from_order,
)


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


router = APIRouter(prefix="/orders", tags=["orders"])
_PG_SHOPIFY_LOCK_SUPPORTED: Optional[bool] = None
_SUPPORTED_ORDER_PROVIDER_HINTS = {"stripe", "adyen", "checkout", "paypal"}


def _shopify_order_lock_key(order_id: str) -> int:
    digest = hashlib.sha256(f"shopify_order:{order_id}".encode("utf-8")).hexdigest()
    # Keep within signed int64 range for pg advisory lock.
    return int(digest[:16], 16) & 0x7FFFFFFFFFFFFFFF


async def _try_acquire_shopify_order_lock(order_id: str) -> Tuple[bool, Optional[int]]:
    global _PG_SHOPIFY_LOCK_SUPPORTED

    if _PG_SHOPIFY_LOCK_SUPPORTED is False:
        return True, None

    lock_key = _shopify_order_lock_key(order_id)
    try:
        row = await database.fetch_one(
            "SELECT pg_try_advisory_lock(:lock_key) AS locked",
            {"lock_key": lock_key},
        )
        _PG_SHOPIFY_LOCK_SUPPORTED = True
        locked = False
        if row is not None:
            try:
                locked = bool(row["locked"])
            except Exception:
                locked = bool(getattr(row, "locked", False))
        return locked, lock_key
    except Exception:
        _PG_SHOPIFY_LOCK_SUPPORTED = False
        return True, None


async def _release_shopify_order_lock(lock_key: Optional[int], *, lock_acquired: bool) -> None:
    if not lock_acquired or lock_key is None or _PG_SHOPIFY_LOCK_SUPPORTED is not True:
        return
    try:
        await database.execute(
            "SELECT pg_advisory_unlock(:lock_key)",
            {"lock_key": lock_key},
        )
    except Exception:
        pass


def _normalize_order_provider_hint(
    selected_psp: Optional[str], preferred_psp: Optional[str]
) -> Optional[str]:
    for candidate in (selected_psp, preferred_psp):
        provider = str(candidate or "").strip().lower()
        if provider in _SUPPORTED_ORDER_PROVIDER_HINTS:
            return provider
    return None


def _finalize_order_psp_used(psp_used: Optional[str], fallback_provider: Optional[str]) -> str:
    value = str(psp_used or fallback_provider or "unknown").strip().lower()
    return value or "unknown"


async def _resolve_active_order_psp(
    merchant_id: str, provider_hint: Optional[str]
) -> Tuple[str, str]:
    psp_row = await fetch_active_runtime_merchant_psp(
        merchant_id=merchant_id,
        provider=provider_hint,
    )

    if not psp_row:
        raise HTTPException(
            status_code=400,
            detail="No active PSP configuration found for this merchant",
        )

    provider = str(psp_row["provider"] or "").strip().lower()
    psp_id = str(psp_row["psp_id"] or "").strip()
    if not provider or not psp_id:
        raise HTTPException(
            status_code=500,
            detail="Active PSP configuration is incomplete for this merchant",
        )
    return provider, psp_id


async def _resolve_order_psp_adapter(order: Dict[str, Any]) -> Tuple[str, Any]:
    merchant_id = str(order.get("merchant_id") or "").strip()
    order_psp_id = str(order.get("psp_id") or "").strip()
    provider_hint = infer_runtime_provider(
        psp_used=order.get("psp_used"),
        psp_id=order_psp_id,
        payment_reference=order.get("payment_intent_id"),
    )
    psp_row = await fetch_active_runtime_merchant_psp(
        merchant_id=merchant_id,
        provider=provider_hint,
        psp_id=order_psp_id,
    )

    if not psp_row:
        raise ValueError("Canonical merchant_psps configuration is missing for this order")

    row_dict = dict(psp_row)
    provider = str(row_dict.get("provider") or "").strip().lower()
    api_key = str(row_dict.get("api_key") or "").strip()
    if not provider or not api_key:
        raise ValueError("Canonical merchant_psps configuration is incomplete for this order")

    adapter = get_psp_adapter(
        provider,
        api_key,
        **build_runtime_adapter_kwargs(
            provider,
            api_key=api_key,
            account_id=row_dict.get("account_id"),
            provider_config=row_dict.get("provider_config"),
            environment=row_dict.get("environment"),
            secret_key=row_dict.get("secret_key"),
        ),
    )
    return provider, adapter


# ============================================================================
# 促销折扣应用（多件折扣）
# ============================================================================

def _normalize_shopify_domain(domain: str) -> str:
    d = (domain or "").strip()
    if not d:
        return d
    d = d.replace("https://", "").replace("http://", "").strip().rstrip("/")
    if d.endswith(".myshopify.com"):
        return d
    return f"{d}.myshopify.com"


def _normalize_storefront_base_url(domain: str) -> str:
    d = (domain or "").strip()
    if not d:
        return ""
    if not d.startswith(("http://", "https://")):
        d = f"https://{d}"
    return d.rstrip("/")


def _shopify_order_create_lock_key(order_id: str) -> int:
    """
    Stable advisory-lock key for a given order_id.

    Postgres advisory locks accept signed bigint keys; derive one from sha256 to avoid
    collisions across different lock namespaces and order ids.
    """
    raw = f"pivota:shopify_order_create:{order_id}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big", signed=True)


@asynccontextmanager
async def _pg_advisory_lock_best_effort(*, lock_key: int):
    """
    Best-effort Postgres advisory lock.

    - Yields `True` when the lock is acquired (or locking is unavailable).
    - Yields `False` when the lock is available but currently held by someone else.
    """
    if not IS_POSTGRES or not getattr(database, "is_connected", False):
        yield True
        return

    try:
        async with database.connection() as conn:
            acquired = bool(
                await conn.fetch_val(
                    "SELECT pg_try_advisory_lock(:lock_key)",
                    {"lock_key": int(lock_key)},
                )
            )
            if not acquired:
                yield False
                return
            try:
                yield True
            finally:
                try:
                    await conn.execute(
                        "SELECT pg_advisory_unlock(:lock_key)",
                        {"lock_key": int(lock_key)},
                    )
                except Exception:
                    pass
    except Exception:
        # If advisory locks aren't available for any reason, proceed without blocking order creation.
        yield True


def _build_shopify_cart_permalink_best_effort(
    *,
    shop_domain: str,
    items: List[OrderItem],
    discount_codes: Optional[List[str]] = None,
) -> Optional[str]:
    """
    Build a Shopify cart permalink checkout fallback:
      https://{shop}/cart/{variant_id}:{qty},{variant_id}:{qty}?discount=CODE1,CODE2

    Note: this does not guarantee pricing match with our quote; final total is computed by Shopify checkout.
    """
    domain = _normalize_shopify_domain(shop_domain)
    if not domain:
        return None

    parts: List[str] = []
    for item in items or []:
        variant_id = getattr(item, "variant_id", None) or None
        try:
            qty = int(getattr(item, "quantity", 0) or 0)
        except Exception:
            qty = 0
        if not variant_id or qty <= 0:
            continue
        try:
            variant_numeric = str(int(str(variant_id)))
        except Exception:
            continue
        parts.append(f"{variant_numeric}:{qty}")

    if not parts:
        return None

    base = f"https://{domain}/cart/" + ",".join(parts)

    codes = []
    for c in (discount_codes or []):
        if isinstance(c, str) and c.strip():
            codes.append(c.strip())
    if codes:
        # Shopify supports `discount=CODE` and typically accepts comma-delimited codes.
        q = urlencode({"discount": ",".join(codes[:5])})
        return f"{base}?{q}"
    return base


def _build_woocommerce_checkout_permalink_best_effort(
    *,
    store_url: str,
    items: List[OrderItem],
) -> Optional[str]:
    """
    Best-effort WooCommerce hosted checkout fallback.

    We only generate a URL for a single simple product because variable and multi-product
    carts require extra form state that we do not persist in OrderItem today.
    """
    base = _normalize_storefront_base_url(store_url)
    if not base:
        return None

    valid_items: List[OrderItem] = []
    for item in items or []:
        try:
            qty = int(getattr(item, "quantity", 0) or 0)
        except Exception:
            qty = 0
        if qty <= 0:
            continue
        valid_items.append(item)

    if len(valid_items) != 1:
        return None

    item = valid_items[0]
    if getattr(item, "variant_id", None):
        return None

    try:
        product_id = str(int(str(getattr(item, "product_id", "") or "")))
        quantity = int(getattr(item, "quantity", 0) or 0)
    except Exception:
        return None

    if quantity <= 0:
        return None

    query = urlencode({"add-to-cart": product_id, "quantity": quantity})
    return f"{base}/checkout/?{query}"


def _build_bigcommerce_checkout_permalink_best_effort(
    *,
    store_domain: str,
    items: List[OrderItem],
) -> Optional[str]:
    """
    Best-effort BigCommerce hosted checkout fallback.

    BigCommerce's storefront add-to-cart redirect is only reliable here for a single
    product line item without option reconstruction.
    """
    base = _normalize_storefront_base_url(store_domain)
    if not base:
        return None

    valid_items: List[OrderItem] = []
    for item in items or []:
        try:
            qty = int(getattr(item, "quantity", 0) or 0)
        except Exception:
            qty = 0
        if qty <= 0:
            continue
        valid_items.append(item)

    if len(valid_items) != 1:
        return None

    item = valid_items[0]
    if getattr(item, "variant_id", None):
        return None

    try:
        product_id = str(int(str(getattr(item, "product_id", "") or "")))
        quantity = int(getattr(item, "quantity", 0) or 0)
    except Exception:
        return None

    if quantity <= 0:
        return None

    query = urlencode({"action": "buy", "product_id": product_id, "qty": quantity})
    return f"{base}/cart.php?{query}"


def _platform_order_create_lock_key(platform: str, order_id: str) -> int:
    raw = f"pivota:{platform}_order_create:{order_id}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big", signed=True)


def _coerce_order_metadata(order: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    raw = (order or {}).get("metadata")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _get_linked_platform_order(order: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not order:
        return None

    metadata = _coerce_order_metadata(order)
    linked = metadata.get("merchant_order")
    if isinstance(linked, dict):
        platform_order_id = str(linked.get("platform_order_id") or "").strip()
        if platform_order_id:
            linked_copy = dict(linked)
            linked_copy["platform_order_id"] = platform_order_id
            return linked_copy

    shopify_order_id = str((order or {}).get("shopify_order_id") or "").strip()
    if shopify_order_id:
        return {
            "platform": "shopify",
            "platform_order_id": shopify_order_id,
            "platform_order_url": None,
        }
    return None


def _name_parts_from_order(order: Dict[str, Any]) -> Tuple[str, str]:
    shipping_address = order.get("shipping_address") or {}
    raw_name = str(shipping_address.get("name") or order.get("customer_name") or "").strip()
    email = str(order.get("customer_email") or "").strip()
    if not raw_name and email and "@" in email:
        raw_name = email.split("@", 1)[0].strip()
    if not raw_name:
        return "Customer", ""
    parts = [part for part in raw_name.split() if part.strip()]
    if not parts:
        return "Customer", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _build_woocommerce_address(order: Dict[str, Any]) -> Dict[str, Any]:
    shipping_address = order.get("shipping_address") or {}
    first_name, last_name = _name_parts_from_order(order)
    return {
        "first_name": first_name,
        "last_name": last_name,
        "address_1": str(shipping_address.get("address_line1") or "").strip(),
        "address_2": str(shipping_address.get("address_line2") or "").strip(),
        "city": str(shipping_address.get("city") or "").strip(),
        "state": str(shipping_address.get("state") or "").strip(),
        "postcode": str(shipping_address.get("postal_code") or "").strip(),
        "country": str(shipping_address.get("country") or "US").strip(),
        "email": str(order.get("customer_email") or "").strip(),
        "phone": str(shipping_address.get("phone") or "").strip(),
    }


def _build_bigcommerce_address(order: Dict[str, Any]) -> Dict[str, Any]:
    shipping_address = order.get("shipping_address") or {}
    first_name, last_name = _name_parts_from_order(order)
    country = str(shipping_address.get("country") or "US").strip().upper() or "US"
    return {
        "first_name": first_name,
        "last_name": last_name,
        "street_1": str(shipping_address.get("address_line1") or "").strip(),
        "street_2": str(shipping_address.get("address_line2") or "").strip(),
        "city": str(shipping_address.get("city") or "").strip(),
        "state": str(shipping_address.get("state") or "").strip(),
        "zip": str(shipping_address.get("postal_code") or "").strip(),
        "country": country,
        "country_iso2": country,
        "email": str(order.get("customer_email") or "").strip(),
        "phone": str(shipping_address.get("phone") or "").strip(),
    }


def _as_order_items(raw_items: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if not isinstance(raw_items, list):
        return items
    for raw_item in raw_items:
        if isinstance(raw_item, dict):
            items.append(dict(raw_item))
    return items


def _merge_linked_platform_order_metadata(
    order: Dict[str, Any],
    *,
    platform: str,
    platform_order_id: str,
    platform_order_name: Optional[str],
    platform_order_url: Optional[str],
    store: Dict[str, Any],
) -> Dict[str, Any]:
    metadata = _coerce_order_metadata(order)
    metadata["merchant_order"] = {
        "platform": platform,
        "platform_order_id": platform_order_id,
        "platform_order_name": platform_order_name,
        "platform_order_url": platform_order_url,
        "store_id": str((store or {}).get("store_id") or "").strip() or None,
        "domain": str((store or {}).get("domain") or "").strip() or None,
        "linked_at": datetime.utcnow().isoformat() + "Z",
    }
    return metadata


async def _candidate_platform_stores(
    order: Dict[str, Any],
    *,
    platform: str,
) -> List[Dict[str, Any]]:
    stores = await get_merchant_active_stores(str(order.get("merchant_id") or "").strip())
    platform_stores = [s for s in (stores or []) if str((s or {}).get("platform") or "").strip().lower() == platform]
    if not platform_stores:
        return []

    bound_store_id = str(order.get("store_id") or "").strip() or None
    candidates: List[Dict[str, Any]] = []
    if bound_store_id:
        for store in platform_stores:
            if str((store or {}).get("store_id") or "").strip() == bound_store_id:
                candidates.append(store)
                break

    for store in platform_stores:
        if store not in candidates:
            candidates.append(store)
    return candidates


def _parse_woocommerce_store_credentials(store: Dict[str, Any]) -> Tuple[str, str, str]:
    credentials = dict((store or {}).get("api_credentials") or {})
    raw_api_key = str((store or {}).get("api_key_raw") or (store or {}).get("api_key") or "").strip()
    consumer_key = str(credentials.get("consumer_key") or "").strip()
    consumer_secret = str(credentials.get("consumer_secret") or "").strip()

    if not consumer_key and ":" in raw_api_key:
        consumer_key = raw_api_key.split(":", 1)[0].strip()
    if not consumer_secret and ":" in raw_api_key:
        consumer_secret = raw_api_key.split(":", 1)[1].strip()

    store_url = normalize_woocommerce_store_url((store or {}).get("domain"))
    return store_url, consumer_key, consumer_secret


def _parse_bigcommerce_store_credentials(store: Dict[str, Any]) -> Tuple[str, str, str, str]:
    credentials = dict((store or {}).get("api_credentials") or {})
    store_hash = normalize_bigcommerce_store_hash(
        credentials.get("store_hash") or (store or {}).get("domain")
    )
    access_token = str(credentials.get("access_token") or (store or {}).get("api_key") or "").strip()
    client_id = str(credentials.get("client_id") or "").strip()
    store_domain = str((store or {}).get("domain") or "").strip() or build_bigcommerce_domain(store_hash)
    return store_hash, access_token, client_id, store_domain


async def _resolve_bigcommerce_status_id(
    *,
    client: httpx.AsyncClient,
    store_hash: str,
    headers: Dict[str, str],
) -> Optional[int]:
    try:
        response = await client.get(
            f"https://api.bigcommerce.com/stores/{store_hash}/v2/order_statuses",
            headers=headers,
            timeout=12.0,
        )
    except Exception:
        return None

    if response.status_code != 200:
        return None

    try:
        payload = response.json()
    except Exception:
        return None

    if not isinstance(payload, list):
        return None

    for row in payload:
        if not isinstance(row, dict):
            continue
        names = {
            str(row.get("name") or "").strip().lower(),
            str(row.get("label") or "").strip().lower(),
            str(row.get("system_label") or "").strip().lower(),
            str(row.get("custom_label") or "").strip().lower(),
        }
        if "awaiting fulfillment" in names:
            try:
                return int(row.get("id"))
            except Exception:
                return None
    return None


def _normalize_bigcommerce_option_value_id(raw_value: Dict[str, Any]) -> Optional[int]:
    for key in ("id", "option_value_id", "value_id"):
        candidate = raw_value.get(key)
        try:
            return int(candidate)
        except Exception:
            continue
    return None


async def _fetch_bigcommerce_variant_product_options(
    *,
    client: httpx.AsyncClient,
    store_hash: str,
    headers: Dict[str, str],
    product_id: int,
    variant_id: int,
) -> List[Dict[str, int]]:
    variant_resp = await client.get(
        f"https://api.bigcommerce.com/stores/{store_hash}/v3/catalog/products/{product_id}/variants/{variant_id}",
        headers=headers,
        timeout=12.0,
    )
    if variant_resp.status_code != 200:
        raise ValueError(f"BigCommerce variant lookup failed: HTTP {variant_resp.status_code}")

    variant_payload = variant_resp.json() or {}
    variant = variant_payload.get("data") or {}
    option_values = variant.get("option_values") or []
    assignments: List[Dict[str, int]] = []
    missing_mapping = False

    for option_value in option_values:
        if not isinstance(option_value, dict):
            continue
        option_id = option_value.get("option_id")
        value_id = _normalize_bigcommerce_option_value_id(option_value)
        try:
            option_id_int = int(option_id)
        except Exception:
            option_id_int = None
        if option_id_int is not None and value_id is not None:
            assignments.append({"id": option_id_int, "value": value_id})
        else:
            missing_mapping = True

    if assignments and not missing_mapping:
        return assignments

    options_resp = await client.get(
        f"https://api.bigcommerce.com/stores/{store_hash}/v3/catalog/products/{product_id}/options",
        headers=headers,
        timeout=12.0,
    )
    if options_resp.status_code != 200:
        raise ValueError(f"BigCommerce option lookup failed: HTTP {options_resp.status_code}")

    options_payload = options_resp.json() or {}
    options_rows = options_payload.get("data") or []
    option_map: Dict[Tuple[str, str], Dict[str, int]] = {}
    for row in options_rows:
        if not isinstance(row, dict):
            continue
        try:
            option_id = int(row.get("id"))
        except Exception:
            continue
        display_name = str(row.get("display_name") or row.get("name") or "").strip().lower()
        for value_row in row.get("option_values") or []:
            if not isinstance(value_row, dict):
                continue
            value_id = _normalize_bigcommerce_option_value_id(value_row)
            label = str(value_row.get("label") or value_row.get("name") or "").strip().lower()
            if value_id is None or not display_name or not label:
                continue
            option_map[(display_name, label)] = {"id": option_id, "value": value_id}

    mapped_assignments: List[Dict[str, int]] = []
    for option_value in option_values:
        if not isinstance(option_value, dict):
            continue
        key = (
            str(option_value.get("option_display_name") or option_value.get("display_name") or "").strip().lower(),
            str(option_value.get("label") or option_value.get("option_label") or "").strip().lower(),
        )
        if not key[0] or not key[1]:
            continue
        mapped = option_map.get(key)
        if mapped:
            mapped_assignments.append(mapped)

    if mapped_assignments:
        return mapped_assignments
    raise ValueError("BigCommerce variant option mapping unavailable")


async def _get_platform_checkout_fallback_url_best_effort(
    *,
    merchant_id: str,
    items: List[OrderItem],
    discount_codes: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Platform-hosted checkout fallback when we cannot create an external PSP payment intent.
    Returns {url, platform, method} or None.
    """
    try:
        store = await get_primary_store(merchant_id)
    except Exception:
        store = None

    platform = (store or {}).get("platform")
    domain = (store or {}).get("domain")
    if not platform or not domain:
        return None

    if str(platform).lower() == "shopify":
        url = _build_shopify_cart_permalink_best_effort(
            shop_domain=str(domain),
            items=items,
            discount_codes=discount_codes,
        )
        if url:
            return {"url": url, "platform": "shopify", "method": "cart_permalink"}
    elif str(platform).lower() == "woocommerce":
        url = _build_woocommerce_checkout_permalink_best_effort(
            store_url=str(domain),
            items=items,
        )
        if url:
            return {"url": url, "platform": "woocommerce", "method": "checkout_add_to_cart"}
    elif str(platform).lower() == "bigcommerce":
        url = _build_bigcommerce_checkout_permalink_best_effort(
            store_domain=str(domain),
            items=items,
        )
        if url:
            return {"url": url, "platform": "bigcommerce", "method": "cart_buy_now"}

    return None


async def compute_order_discount_from_promotions(
    merchant_id: str,
    items: List[OrderItem],
    channel: str = "creator_agents",
) -> Tuple[Decimal, List[Dict[str, Any]]]:
    """
    根据当前订单和促销配置计算订单级折扣金额。

    当前 v0 仅支持：
    - type = MULTI_BUY_DISCOUNT
    - scope.global = true 或 scope.productIds 精确匹配 product_id
    - channel 包含 creator_agents
    """
    discount_total = Decimal("0")
    applied: List[Dict[str, Any]] = []

    try:
        promotions, _ = await list_promotions(
            merchant_id=merchant_id,
            status=PromotionStatus.ACTIVE,
            channel=channel,
        )
    except Exception as e:
        logger.warning(
            f"[OrderRoutes] Failed to load promotions for merchant {merchant_id}: {e}"
        )
        return discount_total, applied

    if not promotions:
        return discount_total, applied

    for promo in promotions:
        try:
            if promo.type != "MULTI_BUY_DISCOUNT":
                continue

            scope = promo.scope or {}
            cfg = promo.config or {}

            threshold = int(
                cfg.get("thresholdQuantity")
                or cfg.get("threshold_quantity")
                or 0
            )
            discount_percent_raw = (
                cfg.get("discountPercent") or cfg.get("discount_percent") or 0
            )
            discount_percent = Decimal(str(discount_percent_raw))

            if threshold <= 0 or discount_percent <= 0:
                continue

            # 收集满足 scope 的每一件商品的单价（按件展开）
            unit_prices: List[Decimal] = []
            for item in items:
                eligible = False
                product_id = item.product_id
                if scope.get("global"):
                    eligible = True
                else:
                    product_ids = scope.get("productIds") or scope.get("product_ids") or []
                    if product_id in product_ids:
                        eligible = True

                if not eligible:
                    continue

                # 将每件商品按数量展开成单价列表
                for _ in range(item.quantity):
                    unit_prices.append(Decimal(item.unit_price))

            total_qty = len(unit_prices)
            if total_qty < threshold:
                continue

            # 优先对价格较高的商品进行折扣
            unit_prices.sort(reverse=True)
            discountable_qty = (total_qty // threshold) * threshold
            discount_base = sum(unit_prices[:discountable_qty])

            if discount_base <= 0:
                continue

            promo_discount = (
                discount_base * discount_percent / Decimal("100")
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            if promo_discount <= 0:
                continue

            discount_total += promo_discount
            applied.append(
                {
                    "id": promo.id,
                    "label": promo.humanReadableRule,
                    "type": promo.type,
                    "thresholdQuantity": threshold,
                    "discountPercent": float(discount_percent),
                    "discountAmount": float(promo_discount),
                }
            )
        except Exception as promo_err:
            logger.warning(
                f"[OrderRoutes] Failed to apply promotion {getattr(promo, 'id', None)}: {promo_err}"
            )
            continue

    return discount_total, applied


# ============================================================================
# 库存检查
# ============================================================================

async def check_inventory_availability(
    merchant_id: str,
    items: List[OrderItem]
) -> Tuple[bool, Dict[str, Any]]:
    """
    检查 Shopify 库存是否充足
    
    返回: (是否有库存, 库存详情)
    """
    try:
        # 获取主店铺信息（Shopify/Wix/...），用于后续判断
        store_info = await get_primary_store(merchant_id)
        if not store_info:
            return True, {"message": "No store connected, skipping inventory check"}

        if store_info.get("platform") != "shopify":
            # 非 Shopify 平台，暂不检查库存
            return True, {"message": f"Platform {store_info.get('platform')} inventory check not implemented"}
        
        shop_domain = store_info.get("domain")
        access_token, _ = await resolve_shopify_admin_access_token(
            shop_domain=shop_domain,
            api_key_raw=store_info.get("api_key_raw") or store_info.get("api_key"),
            store_id=str(store_info.get("store_id") or "").strip() or None,
        )
        
        if not shop_domain or not access_token:
            return True, {"message": "Shop credentials missing, skipping inventory check"}
        
        # 获取所有产品和变体
        url = f"https://{shop_domain}/admin/api/2024-01/products.json"
        headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json"
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, timeout=10.0)
            
            if response.status_code != 200:
                return True, {"message": "Failed to fetch products, allowing order"}
            
            products = response.json().get("products", [])
            
            # 建立 variant_id -> inventory 的映射
            inventory_map = {}
            for product in products:
                for variant in product.get("variants", []):
                    variant_id = str(variant["id"])
                    inventory_map[variant_id] = {
                        "available": variant.get("inventory_quantity", 0),
                        "tracked": variant.get("inventory_management") == "shopify",
                        "sku": variant.get("sku"),
                        "title": f"{product['title']} - {variant.get('title', '')}"
                    }
            
            # 检查每个订单项的库存
            insufficient_items = []
            inventory_details = {}
            
            for item in items:
                if not item.variant_id:
                    # 如果没有 variant_id，跳过检查
                    continue
                
                variant_id = str(item.variant_id)
                if variant_id in inventory_map:
                    inv = inventory_map[variant_id]
                    inventory_details[variant_id] = inv
                    
                    if inv["tracked"] and inv["available"] < item.quantity:
                        insufficient_items.append({
                            "product": item.product_title,
                            "requested": item.quantity,
                            "available": inv["available"]
                        })
            
            if insufficient_items:
                return False, {
                    "message": "Insufficient inventory",
                    "items": insufficient_items
                }
            
            return True, {
                "message": "Inventory check passed",
                "details": inventory_details
            }
            
    except Exception as e:
        # 库存检查失败时，默认允许订单（fail-open）
        logger.error(f"Inventory check failed: {e}")
        return True, {"message": f"Inventory check error: {str(e)}, allowing order"}


def _extract_delivery_option_identifier(selected_delivery_option: Any) -> Optional[str]:
    """
    Best-effort extraction of a stable delivery option identifier for drift diagnostics.

    Do not include full delivery option payload in responses/events (may contain extra data),
    only a stable identifier-like string.
    """
    if not selected_delivery_option:
        return None

    if isinstance(selected_delivery_option, str):
        value = selected_delivery_option.strip()
        return value or None

    if not isinstance(selected_delivery_option, dict):
        return None

    for key in (
        "id",
        "identifier",
        "handle",
        "code",
        "shipping_rate_id",
        "rate_id",
        "title",
        "name",
    ):
        raw = selected_delivery_option.get(key)
        if raw is None:
            continue
        value = str(raw).strip()
        if value:
            return value
    return None


def _build_quote_drift_normalized_request(
    *,
    items: List[Dict[str, Any]],
    discount_codes: List[str],
    shipping_address: Optional[Dict[str, Any]],
    selected_delivery_option: Any,
) -> Dict[str, Any]:
    return {
        "items": normalize_items_for_fingerprint(items),
        "discount_codes": normalize_discount_codes(discount_codes),
        "shipping_geo": normalize_shipping_for_fingerprint(shipping_address),
        "selected_delivery_option": _extract_delivery_option_identifier(selected_delivery_option),
    }


# ============================================================================
# 订单创建（Agent 调用）
# ============================================================================

@router.post("/create", response_model=OrderResponse)
async def create_new_order(
    order_request: CreateOrderRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(require_admin)  # Agent 需要管理员权限
):
    """
    **创建新订单（Agent → Pivota）**
    
    流程：
    1. 验证商户存在且已连接 PSP
    2. 计算订单总价
    3. 创建订单记录
    4. 创建 Stripe Payment Intent
    5. 返回订单详情和支付密钥
    
    防御性设计：
    - 订单创建后立即记录事件日志
    - 金额使用 Decimal 精确计算
    - 支付信息与订单解耦，失败不影响订单创建
    """
    try:
        # 1. 验证商户
        merchant = await get_merchant_onboarding(order_request.merchant_id)
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")

        # Quote-first enforcement (PCS v0.2-a): dual guard to prevent bypass.
        from services.quote_first_enforcement import should_require_quote_for_order_create

        require_quote, require_ctx = await should_require_quote_for_order_create(merchant_id=order_request.merchant_id)
        if require_quote and not order_request.quote_id:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "QUOTE_REQUIRED",
                    "message": "quote_id is required",
                    "context": require_ctx,
                },
            )

        # 2. 检查库存（如果商户连接了 Shopify）
        has_inventory, inventory_info = await check_inventory_availability(
            order_request.merchant_id,
            order_request.items
        )
        if not has_inventory:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "Insufficient inventory",
                    "items": inventory_info.get("items", [])
                }
            )

        # 3. 计算订单金额
        # Quote-first path: if quote_id is provided, amounts come from quote snapshot.
        pricing_quote_meta: Optional[Dict[str, Any]] = None
        if order_request.quote_id:
            quote_service = QuoteService()
            try:
                quote = await quote_service.load_active_quote_or_raise(
                    quote_id=order_request.quote_id
                )

                order_items_for_fingerprint = [
                    {
                        "product_id": it.product_id,
                        "variant_id": it.variant_id or "",
                        "quantity": it.quantity,
                    }
                    for it in (order_request.items or [])
                ]
                order_discount_codes = normalize_discount_codes(order_request.discount_codes)
                order_shipping_geo = (
                    {
                        "country": order_request.shipping_address.country,
                        "postal_code": order_request.shipping_address.postal_code,
                        "city": order_request.shipping_address.city,
                        "state": order_request.shipping_address.state,
                    }
                    if order_request.shipping_address
                    else None
                )

                order_request_fingerprint = compute_request_fingerprint(
                    merchant_id=order_request.merchant_id,
                    items=order_items_for_fingerprint,
                    discount_codes=order_discount_codes,
                    shipping_address=order_shipping_geo,
                    selected_delivery_option=order_request.selected_delivery_option,
                )

                order_request_normalized = _build_quote_drift_normalized_request(
                    items=order_items_for_fingerprint,
                    discount_codes=order_discount_codes,
                    shipping_address=order_shipping_geo,
                    selected_delivery_option=order_request.selected_delivery_option,
                )

                quote_request_json = quote.request_json if isinstance(quote.request_json, dict) else {}
                quote_request_normalized = _build_quote_drift_normalized_request(
                    items=quote_request_json.get("items") or [],
                    discount_codes=quote_request_json.get("discount_codes") or [],
                    shipping_address=quote_request_json.get("shipping_address"),
                    selected_delivery_option=quote_request_json.get("selected_delivery_option"),
                )

                drift_fields: List[str] = []
                if quote.merchant_id != order_request.merchant_id:
                    drift_fields.append("merchant_id")
                if quote_request_normalized.get("items") != order_request_normalized.get("items"):
                    drift_fields.append("items")
                if quote_request_normalized.get("discount_codes") != order_request_normalized.get(
                    "discount_codes"
                ):
                    drift_fields.append("discount_codes")
                if quote_request_normalized.get("shipping_geo") != order_request_normalized.get("shipping_geo"):
                    drift_fields.append("shipping_geo")
                if quote_request_normalized.get("selected_delivery_option") != order_request_normalized.get(
                    "selected_delivery_option"
                ):
                    drift_fields.append("selected_delivery_option")

                drift_details = {
                    "quote_id": quote.quote_id,
                    "quote_expires_at": quote.expires_at.isoformat() if quote.expires_at else None,
                    "quote_hash_sha256": quote.quote_hash_sha256,
                    "quote_request_fingerprint": quote.request_fingerprint,
                    "order_request_fingerprint": order_request_fingerprint,
                    "drift_fields": (
                        drift_fields
                        if drift_fields
                        else ["selected_delivery_option"]
                        if order_request_fingerprint != quote.request_fingerprint
                        else []
                    ),
                    "quote_request_normalized": quote_request_normalized,
                    "order_request_normalized": order_request_normalized,
                }

                if quote.merchant_id != order_request.merchant_id:
                    raise QuoteError(
                        "QUOTE_MISMATCH",
                        "quote merchant_id mismatch",
                        debug_id=quote.debug_id,
                        details=drift_details,
                    )

                if order_request_fingerprint != quote.request_fingerprint:
                    raise QuoteError(
                        "QUOTE_MISMATCH",
                        "order request does not match quote snapshot",
                        debug_id=quote.debug_id,
                        details=drift_details,
                    )

                snap = quote.snapshot_json or {}
                pricing = (snap.get("pricing") or {}) if isinstance(snap, dict) else {}
                quote_currency = None
                try:
                    quote_currency = str(snap.get("currency") or "").strip().upper() if isinstance(snap, dict) else None
                except Exception:
                    quote_currency = None
                if quote_currency:
                    # Quote-first: currency is locked by the quote snapshot, not by the request payload.
                    # This prevents mismatches where amounts are from EUR but currency is defaulted to USD.
                    order_request.currency = quote_currency

                settlement_currency = None
                try:
                    settlement_currency = str(snap.get("settlement_currency") or "").strip().upper() if isinstance(snap, dict) else None
                except Exception:
                    settlement_currency = None

                checkout_url = None
                try:
                    if isinstance(snap, dict):
                        checkout_url = snap.get("checkout_url") or (snap.get("metadata") or {}).get("checkout_url")
                except Exception:
                    checkout_url = None

                subtotal = parse_decimal_money(pricing.get("subtotal"))
                discount_total = parse_decimal_money(pricing.get("discount_total"))
                shipping_fee = parse_decimal_money(pricing.get("shipping_fee"))
                tax = parse_decimal_money(pricing.get("tax"))
                total = parse_decimal_money(pricing.get("total"))

                if total <= 0:
                    fallback_subtotal = Decimal("0")
                    try:
                        raw_line_items = (snap.get("line_items") or []) if isinstance(snap, dict) else []
                        if isinstance(raw_line_items, list) and raw_line_items:
                            for li in raw_line_items:
                                if not isinstance(li, dict):
                                    continue
                                try:
                                    qty = int(li.get("quantity") or 0)
                                except Exception:
                                    qty = 0
                                if qty <= 0:
                                    continue
                                unit = (
                                    li.get("unit_price_effective")
                                    or li.get("unit_price_original")
                                    or li.get("price")
                                    or 0
                                )
                                fallback_subtotal += parse_decimal_money(unit) * Decimal(qty)
                    except Exception:
                        fallback_subtotal = Decimal("0")

                    if fallback_subtotal > 0 and subtotal <= 0:
                        logger.warning(
                            "[QuoteFirst] Quote snapshot pricing subtotal/total is zero; falling back to quote line_items",
                            extra={"merchant_id": order_request.merchant_id, "quote_id": quote.quote_id},
                        )
                        subtotal = fallback_subtotal
                        total = max(Decimal("0"), subtotal - discount_total) + shipping_fee + tax

                pricing_quote_meta = {
                    "quote_id": quote.quote_id,
                    "expires_at": quote.expires_at.isoformat() if quote.expires_at else None,
                    "engine": quote.engine,
                    "engine_ref": quote.engine_ref,
                    "currency": quote_currency,
                    "settlement_currency": settlement_currency,
                    "checkout_url": checkout_url,
                    "request_fingerprint": quote.request_fingerprint,
                    "quote_hash_sha256": quote.quote_hash_sha256,
                    "pricing": pricing,
                    "promotion_lines": snap.get("promotion_lines") or [],
                    "line_items": snap.get("line_items") or [],
                }
            except QuoteError as e:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": e.code,
                        "message": e.message,
                        "debug_id": e.debug_id,
                        **({"details": e.details} if getattr(e, "details", None) else {}),
                    },
                )

        else:
            subtotal = sum(item.subtotal for item in order_request.items)

            # Legacy promotions (multi-buy) for non-quote orders.
            discount_total = Decimal("0")
            applied_promos: List[Dict[str, Any]] = []
            try:
                discount_total, applied_promos = await compute_order_discount_from_promotions(
                    merchant_id=order_request.merchant_id,
                    items=order_request.items,
                    channel="creator_agents",
                )
            except Exception as promo_err:
                logger.warning(
                    f"[OrderRoutes] Failed to compute promotions for order: {promo_err}"
                )
                discount_total = Decimal("0")
                applied_promos = []

            if discount_total > 0:
                logger.info(
                    f"[OrderRoutes] Applied promotions for merchant {order_request.merchant_id}: "
                    f"discount_total={discount_total}"
                )
                subtotal = max(Decimal("0"), subtotal - discount_total)

            shipping_fee = Decimal("0")
            tax = Decimal("0")
            total = subtotal + shipping_fee + tax

        # 4. 创建订单
        # Extract agent_id from metadata if present
        agent_id = None
        if order_request.metadata:
            agent_id = order_request.metadata.get("agent_id")

        # Determine PSP using PaymentRoutingService (merchant routing UI),
        # falling back to legacy hints only if routing config is missing.
        routing_service = PaymentRoutingService(database)
        selected_psp = None
        route_config: Dict[str, Any] = {}
        try:
            selected_psp, route_config = await routing_service.select_psp(
                agent_id=agent_id or "",
                merchant_id=order_request.merchant_id,
                amount=float(total),
                currency=order_request.currency or "USD",
            )
            logger.info(
                f"[OrderRoutes] Routing selected PSP '{selected_psp}' for order "
                f"{order_request.merchant_id} via payment_routes config"
            )
        except Exception as e:
            logger.error(f"[OrderRoutes] Routing selection failed, falling back to legacy PSP: {e}")
            selected_psp = None

        # Source of truth is canonical merchant_psps. Route selection and an explicit
        # provider preference can hint which active provider row to choose, but we do
        # not fall back to merchant_onboarding.psp_type for live runtime decisions.
        provider_hint = _normalize_order_provider_hint(
            selected_psp,
            order_request.preferred_psp,
        )

        # Always get psp_id for PSP metrics tracking (even if psp_type is known)
        psp_id_value = None
        try:
            psp_type, psp_id_value = await _resolve_active_order_psp(
                order_request.merchant_id,
                provider_hint,
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get PSP configuration: {e}")
            raise HTTPException(
                status_code=500, detail=f"Failed to determine PSP: {str(e)}"
            )
        
        # Ensure psp_type is lowercase for consistency
        if psp_type:
            psp_type = psp_type.lower()
        
        # Validate PSP fields are set
        if not psp_type or not psp_id_value:
            logger.error(f"PSP fields incomplete: psp_type={psp_type}, psp_id={psp_id_value}")
            raise HTTPException(
                status_code=500,
                detail="Failed to determine complete PSP configuration"
            )
        
        logger.info(f"✅ PSP determined: {psp_type} (ID: {psp_id_value})")
        
        # 合并订单元数据并记录促销信息（如果有）
        order_metadata: Dict[str, Any] = dict(order_request.metadata or {})
        if pricing_quote_meta:
            order_metadata["pricing_quote"] = pricing_quote_meta
        elif discount_total > 0:
            promo_meta = {
                "discount_total": float(discount_total),
                "applied_promotions": applied_promos,
            }
            existing_promos = order_metadata.get("promotions") or {}
            # 促销信息统一挂在 metadata.promotions 下
            order_metadata["promotions"] = {**existing_promos, **promo_meta}

        order_taxonomy = build_traffic_taxonomy(
            order_metadata,
            authenticated_agent_id=_clean_text(order_metadata.get("agent_id")) if isinstance(order_metadata, dict) else None,
            caller_id=_clean_text(order_metadata.get("caller_id")) if isinstance(order_metadata, dict) else None,
            default_source_channel=_clean_text(order_metadata.get("source_channel") or order_metadata.get("source")),
            default_query_source=_clean_text(order_metadata.get("query_source")),
            default_protocol_name=_clean_text(order_metadata.get("protocol_name") or order_metadata.get("protocol")),
            default_commerce_surface=_clean_text(order_metadata.get("commerce_surface") or order_metadata.get("surface")),
        )
        order_metadata = attach_traffic_taxonomy(order_metadata, order_taxonomy)

        if has_attribution_signal(order_metadata):
            attribution_context = materialize_attribution_context(
                order_metadata,
                default_surface=str(order_metadata.get(PVT_SURFACE) or order_metadata.get("surface") or "merchant_native"),
                merchant_id=order_request.merchant_id,
            )
            for key in (
                PVT_SURFACE,
                PVT_CLICK_ID,
                PVT_PRODUCT_ID,
                PVT_VARIANT_ID,
                PVT_PROMPT_CLUSTER,
            ):
                if attribution_context.get(key):
                    order_metadata[key] = attribution_context[key]

        # Bind order to the current store connection (if any) so downstream Shopify sync
        # does not accidentally use a different store after a merchant connects another store.
        store_id_value: Optional[str] = None
        try:
            primary_store = await get_primary_store(order_request.merchant_id)
            if primary_store and primary_store.get("store_id"):
                store_id_value = str(primary_store.get("store_id"))
        except Exception:
            store_id_value = None

        order_data = {
            "merchant_id": order_request.merchant_id,
            "customer_email": order_request.customer_email,
            "items": [json.loads(item.json()) for item in order_request.items],
            "shipping_address": json.loads(order_request.shipping_address.json()),
            "subtotal": float(subtotal),
            "shipping_fee": float(shipping_fee),
            "tax": float(tax),
            "total": float(total),
            # "amount" field removed - use "total" instead
            "currency": order_request.currency,
            "agent_id": agent_id,  # Extract from metadata
            "agent_session_id": order_request.agent_session_id,
            "metadata": order_metadata,
            # Buyer Vault linkage (internal-only). These are nullable and may be backfilled later.
            "intent_id": str(order_metadata.get("intent_id") or "").strip() or None,
            "agent_user_ref": str(order_metadata.get("agent_user_ref") or order_metadata.get("agentUserRef") or "").strip() or None,
            "buyer_id": str(order_metadata.get("buyer_id") or "").strip() or None,
            "agent_scoped_buyer_ref": str(order_metadata.get("agent_scoped_buyer_ref") or "").strip() or None,
            "psp_used": psp_type,  # Record which PSP provider is used (lowercase)
            # Legacy fields (optional, can be null)
            "store_id": store_id_value,
            "psp_id": psp_id_value,  # Include actual PSP ID for metrics tracking
            "payment_method": None
        }
        order_id = await create_order(order_data)
        try:
            await upsert_order_attribution_edge(
                order_id=str(order_id),
                merchant_id=order_request.merchant_id,
                metadata=order_metadata,
            )
        except Exception as attribution_exc:
            logger.warning(
                "[OrderRoutes] Failed to persist commerce attribution edge for %s: %s",
                order_id,
                attribution_exc,
            )

        try:
            await emit_merchant_webhook_event(
                order_request.merchant_id,
                event_type="order.created",
                payload={
                    "order_id": str(order_id),
                    "merchant_id": str(order_request.merchant_id),
                    "customer_email": order_request.customer_email,
                    "total": float(total),
                    "currency": order_request.currency,
                    "item_count": len(order_request.items or []),
                    "psp_used": psp_type,
                },
            )
        except Exception as exc:
            logger.warning(
                "Failed to emit merchant order.created webhook for %s: %s",
                order_request.merchant_id,
                exc,
            )

        # Consume quote best-effort after order creation succeeds.
        if order_request.quote_id:
            try:
                quote_service = QuoteService()
                await quote_service.consume_quote_best_effort(order_request.quote_id, order_id=str(order_id))
            except Exception:
                pass

        # 5. 同步创建 Payment Intent（立即返回结果）
        payment_intent_id = None
        client_secret = None
        # For future monitoring: track a single payment_attempt row per order
        # without changing routing or PSP behavior.
        payment_attempt_id = None
        route_id_for_attempt = route_config.get("route_id") if isinstance(route_config, dict) else None
        # Unified payment action for frontends (optional, best-effort)
        payment_action: Dict[str, Any] = {}
        
        try:
            # Build preferred PSP ordering from routing config (if available)
            preferred_psps: Optional[List[str]] = None
            try:
                if isinstance(route_config, dict):
                    raw_priority = route_config.get("psp_priority") or []
                    if isinstance(raw_priority, str):
                        try:
                            raw_priority = json.loads(raw_priority)
                        except Exception:
                            raw_priority = []
                    if isinstance(raw_priority, list) and raw_priority:
                        preferred_psps = [
                            str(entry.get("psp", "")).lower()
                            for entry in sorted(
                                raw_priority, key=lambda e: e.get("priority", 999)
                            )
                            if entry.get("psp")
                        ]
            except Exception as pref_err:
                logger.warning(
                    f"[OrderRoutes] Failed to build preferred_psps list from route_config: {pref_err}"
                )
                preferred_psps = None

            # Attempt-level logging is handled inside MultiPSPOrchestrator (best-effort),
            # so we don't create a single aggregated payment_attempt row here.
            payment_attempt_id = None

            # 使用 MultiPSPOrchestrator，按路由配置的优先级（preferred_psps）
            # 自动在 adyen → stripe → checkout 之间切换。
            start_ts = time.monotonic()
            # Agent / 对话场景下，如果前端传了 preferred_psp = "stripe_checkout"，
            # 则通过 metadata.psp_mode 告诉 Stripe 适配器走 Checkout Session 流程，
            # 但 PSP provider 仍然是 "stripe"（由 routing 决定）。
            psp_mode = None
            if (order_request.preferred_psp or "").lower() == "stripe_checkout":
                psp_mode = "stripe_checkout"

            success, payment_intent, error, psp_used = await create_payment_with_failover(
                merchant_id=order_request.merchant_id,
                amount=total,
                currency=order_request.currency,
                metadata={
                    "order_id": order_id,
                    "merchant_id": order_request.merchant_id,
                    "customer_email": order_request.customer_email,
                    "route_id": route_id_for_attempt,
                    "agent_id": agent_id,
                    **(
                        {
                            PVT_SURFACE: order_metadata.get(PVT_SURFACE),
                            PVT_CLICK_ID: order_metadata.get(PVT_CLICK_ID),
                            PVT_PRODUCT_ID: order_metadata.get(PVT_PRODUCT_ID),
                            PVT_VARIANT_ID: order_metadata.get(PVT_VARIANT_ID),
                            PVT_PROMPT_CLUSTER: order_metadata.get(PVT_PROMPT_CLUSTER),
                        }
                        if has_attribution_signal(order_metadata)
                        else {}
                    ),
                    **({"psp_mode": psp_mode} if psp_mode else {}),
                },
                preferred_psps=preferred_psps,
                canonical_psp_required=True,
                enforce_live_readiness=True,
            )
            response_ms = int((time.monotonic() - start_ts) * 1000)

            final_psp = _finalize_order_psp_used(psp_used, psp_type)
            logger.info(
                f"[OrderRoutes] Payment intent result via MultiPSPOrchestrator: "
                f"success={success}, psp_used={final_psp}, has_intent={payment_intent is not None}, error={error}"
            )

            if success and payment_intent:
                payment_intent_id = payment_intent.id
                client_secret = getattr(payment_intent, "client_secret", None)
                psp_type = final_psp
                logger.info(f"✅ Payment intent created via {psp_type}: {payment_intent_id}")

                # Build unified payment_action for frontend / Agent
                try:
                    payment_action = build_payment_action(payment_intent, psp_used=psp_type)
                except Exception as pa_err:
                    logger.warning(
                        f"⚠️ Failed to build payment_action for order {order_id}: {pa_err}"
                    )

                # MultiPSPOrchestrator logs each PSP attempt; no aggregated update here.

                # Log redirect URL when available（Checkout / PayPal / Stripe Checkout）
                redirect_url = getattr(payment_intent, "redirect_url", None)
                if (
                    not redirect_url
                    and psp_type in ["checkout", "paypal"]
                    and client_secret
                    and isinstance(client_secret, str)
                    and client_secret.startswith("http")
                ):
                    redirect_url = client_secret
                if redirect_url:
                    logger.info(f"🔗 {psp_type.capitalize()} redirect URL: {redirect_url}")

                await update_payment_info(
                    order_id=order_id,
                    payment_intent_id=payment_intent_id,
                    client_secret=client_secret or "",
                    payment_status="awaiting_payment",
                    psp_used=final_psp,
                )
                await log_order_event(
                    event_type="order_created",
                    order_id=order_id,
                    merchant_id=order_request.merchant_id,
                    metadata={
                        "total": float(total),
                        "currency": order_request.currency,
                        "items_count": len(order_request.items),
                        "payment_intent_id": payment_intent_id,
                        "psp_type": psp_type,
                    },
                )
            else:
                logger.error(f"Payment intent creation failed via MultiPSP: {error}")
                # Long-term fallback: if we have a platform checkout URL from the quote snapshot,
                # return a redirect_url action so the client can continue on the store platform.
                fallback_checkout_url = None
                try:
                    if isinstance(pricing_quote_meta, dict):
                        fallback_checkout_url = pricing_quote_meta.get("checkout_url")
                except Exception:
                    fallback_checkout_url = None

                platform_checkout = None
                if not fallback_checkout_url:
                    platform_checkout = await _get_platform_checkout_fallback_url_best_effort(
                        merchant_id=order_request.merchant_id,
                        items=order_request.items,
                        discount_codes=order_request.discount_codes,
                    )

                if (fallback_checkout_url or platform_checkout) and not payment_action:
                    psp_type = "checkout"
                    client_secret = str(fallback_checkout_url or (platform_checkout or {}).get("url"))
                    payment_action = {
                        "type": "redirect_url",
                        "url": str(fallback_checkout_url or (platform_checkout or {}).get("url")),
                        "raw": {
                            "reason": "psp_unavailable",
                            "error": error,
                            **({"platform": platform_checkout.get("platform"), "method": platform_checkout.get("method")} if platform_checkout else {}),
                        },
                    }
                    await log_order_event(
                        event_type="payment_fallback_platform_checkout",
                        order_id=order_id,
                        merchant_id=order_request.merchant_id,
                        metadata={"checkout_url": str(fallback_checkout_url or (platform_checkout or {}).get("url"))},
                    )
                # MultiPSPOrchestrator logs each PSP attempt; no aggregated update here.
                await log_order_event(
                    event_type="payment_intent_failed",
                    order_id=order_id,
                    merchant_id=order_request.merchant_id,
                    metadata={"error": error, "psp_type": final_psp},
                )
        except Exception as e:
            logger.error(f"Payment intent creation error: {e}")
            fallback_checkout_url = None
            try:
                if isinstance(pricing_quote_meta, dict):
                    fallback_checkout_url = pricing_quote_meta.get("checkout_url")
            except Exception:
                fallback_checkout_url = None

            platform_checkout = None
            if not fallback_checkout_url:
                platform_checkout = await _get_platform_checkout_fallback_url_best_effort(
                    merchant_id=order_request.merchant_id,
                    items=order_request.items,
                    discount_codes=order_request.discount_codes,
                )

            if (fallback_checkout_url or platform_checkout) and not payment_action:
                psp_type = "checkout"
                client_secret = str(fallback_checkout_url or (platform_checkout or {}).get("url"))
                payment_action = {
                    "type": "redirect_url",
                    "url": str(fallback_checkout_url or (platform_checkout or {}).get("url")),
                    "raw": {
                        "reason": "psp_error",
                        "error": str(e),
                        **({"platform": platform_checkout.get("platform"), "method": platform_checkout.get("method")} if platform_checkout else {}),
                    },
                }
                await log_order_event(
                    event_type="payment_fallback_platform_checkout",
                    order_id=order_id,
                    merchant_id=order_request.merchant_id,
                    metadata={"checkout_url": str(fallback_checkout_url or (platform_checkout or {}).get("url"))},
                )
            await log_order_event(
                event_type="payment_intent_error",
                order_id=order_id,
                merchant_id=order_request.merchant_id,
                metadata={"error": str(e)},
            )

        # 6. 返回订单信息（支付已同步创建）
        return OrderResponse(
            order_id=order_id,
            merchant_id=order_request.merchant_id,
            customer_email=order_request.customer_email,
            items=order_request.items,
            shipping_address=order_request.shipping_address,
            subtotal=float(subtotal),
            shipping_fee=float(shipping_fee),
            tax=float(tax),
            total=float(total),
            currency=order_request.currency,
            status="pending",
            payment_status="awaiting_payment" if payment_intent_id else "pending",
            payment_intent_id=payment_intent_id,
            client_secret=client_secret,
             psp=psp_type,
             payment_action=payment_action or None,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Order creation internal error: {e}")
        raise HTTPException(status_code=500, detail=f"Order creation internal error: {str(e)}")


# ============================================================================
# 支付处理
# ============================================================================

@router.post("/payment/confirm")
async def confirm_payment(
    payment_request: PaymentConfirmRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(require_admin)
):
    """
    **确认支付（Agent 调用）**
    
    流程：
    1. 验证订单存在
    2. 确认 Stripe Payment Intent
    3. 更新订单状态为已支付
    4. 触发履约流程（创建 Shopify 订单）
    """
    
    order = await get_order(payment_request.order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    if order["payment_status"] == "paid":
        return {"status": "success", "message": "Order already paid"}
    
    # 获取商户信息
    merchant = await get_merchant_onboarding(order["merchant_id"])
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")
    
    try:
        psp_type, psp_adapter = await _resolve_order_psp_adapter(order)
        
        # 确认支付
        success, status, error = await psp_adapter.confirm_payment(
            payment_intent_id=order["payment_intent_id"],
            payment_method_id=payment_request.payment_method_id
        )
        
        if not success:
            raise HTTPException(status_code=400, detail=f"Payment confirmation failed: {error}")
        
        if status == "succeeded":
            # 标记订单已支付
            await mark_order_paid(payment_request.order_id)
            
            # 记录支付成功事件
            await log_order_event(
                event_type="payment_succeeded",
                order_id=payment_request.order_id,
                merchant_id=order["merchant_id"],
                metadata={
                    "payment_intent_id": order["payment_intent_id"],
                    "amount": float(order["total"]),
                    "currency": order["currency"],
                    "psp_type": psp_type
                }
            )
            
            # 后台任务：创建 Shopify 订单
            async def create_shopify_order_task():
                """创建 Shopify 订单通知商户发货"""
                try:
                    logger.info(f"Creating Shopify order for {payment_request.order_id}")
                    success = await create_shopify_order(payment_request.order_id)
                    if success:
                        logger.info(f"Shopify order created successfully for {payment_request.order_id}")
                    else:
                        logger.error(f"Failed to create Shopify order for {payment_request.order_id}")
                except Exception as e:
                    logger.error(f"Error in Shopify order creation task: {e}")
            
            background_tasks.add_task(create_shopify_order_task)
            
            # 后台任务：计算订单佣金（Phase 6 - Commission Automation）
            async def calculate_commission_task():
                """自动计算订单佣金并记录"""
                try:
                    from services.order_commission_service import process_order_commission
                    logger.info(f"Calculating commission for order {payment_request.order_id}")
                    result = await process_order_commission(payment_request.order_id, database)
                    if result.get("status") == "success":
                        logger.info(
                            f"Commission calculated: ${result.get('commission_amount', 0):.2f} "
                            f"at {result.get('commission_rate', 0) * 100}%"
                        )
                    elif result.get("status") == "skipped":
                        logger.info(f"Commission skipped: {result.get('reason')}")
                    else:
                        logger.error(f"Commission calculation failed: {result.get('message')}")
                except Exception as e:
                    logger.error(f"Error in commission calculation task: {e}")
            
            background_tasks.add_task(calculate_commission_task)
            
            return {
                "status": "success",
                "message": "Payment confirmed successfully",
                "order_id": payment_request.order_id,
                "payment_intent_id": order["payment_intent_id"],
                "psp_type": psp_type
            }
        else:
            return {
                "status": "pending",
                "message": f"Payment status: {status}",
                "payment_intent_id": order["payment_intent_id"]
            }
            
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Payment failed: {str(e)}")


# ============================================================================
# 订单查询
# ============================================================================

@router.get("/{order_id}", response_model=OrderResponse)
async def get_order_details(
    order_id: str,
    current_user: dict = Depends(require_admin)
):
    """获取订单详情"""
    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    return OrderResponse(
        order_id=order["order_id"],
        merchant_id=order["merchant_id"],
        customer_email=order["customer_email"],
        items=[OrderItem(**item) for item in order["items"]],
        shipping_address=order["shipping_address"],
        subtotal=order["subtotal"],
        shipping_fee=order["shipping_fee"],
        tax=order["tax"],
        total=order["total"],
        currency=order["currency"],
        status=order["status"],
        payment_status=order["payment_status"],
        fulfillment_status=order.get("fulfillment_status"),
        payment_intent_id=order.get("payment_intent_id"),
        shopify_order_id=order.get("shopify_order_id"),
        tracking_number=order.get("tracking_number"),
        created_at=order["created_at"],
        updated_at=order["updated_at"],
        paid_at=order.get("paid_at"),
        shipped_at=order.get("shipped_at"),
        agent_session_id=order.get("agent_session_id"),
        metadata=order.get("metadata")
    )


@router.get("/merchant/{merchant_id}", response_model=OrderListResponse)
async def get_merchant_orders(
    merchant_id: str,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    current_user: dict = Depends(get_current_user)  # Allow authenticated users
):
    """获取商户的订单列表"""
    orders_list = await get_orders_by_merchant(merchant_id, status, limit, offset)
    
    return OrderListResponse(
        status="success",
        total=len(orders_list),
        orders=[
            OrderResponse(
                order_id=o["order_id"],
                merchant_id=o["merchant_id"],
                customer_email=o["customer_email"],
                items=[OrderItem(**item) for item in o["items"]],
                shipping_address=o["shipping_address"],
                subtotal=o["subtotal"],
                shipping_fee=o["shipping_fee"],
                tax=o["tax"],
                total=o["total"],
                currency=o["currency"],
                status=o["status"],
                payment_status=o["payment_status"],
                fulfillment_status=o.get("fulfillment_status"),
                payment_intent_id=o.get("payment_intent_id"),
                shopify_order_id=o.get("shopify_order_id"),
                tracking_number=o.get("tracking_number"),
                created_at=o["created_at"],
                updated_at=o["updated_at"],
                paid_at=o.get("paid_at"),
                shipped_at=o.get("shipped_at"),
                agent_session_id=o.get("agent_session_id"),
                metadata=o.get("metadata")
            ) for o in orders_list
        ]
    )


@router.get("/merchant/{merchant_id}/stats")
async def get_merchant_order_stats(
    merchant_id: str,
    current_user: dict = Depends(require_admin)
):
    """获取商户订单统计"""
    stats = await get_order_stats(merchant_id)
    return stats


# ============================================================================
# Shopify 订单创建（履约集成）
# ============================================================================

async def create_woocommerce_order(order_id: str) -> bool:
    lock_key = _platform_order_create_lock_key("woocommerce", order_id)
    async with _pg_advisory_lock_best_effort(lock_key=lock_key) as lock_acquired:
        if not lock_acquired:
            logger.info("[WooCommerce] Create already in progress; skipping: order_id=%s", order_id)
            return True

        order = await get_order(order_id)
        if not order:
            logger.error("[WooCommerce] Order %s not found", order_id)
            return False
        if _get_linked_platform_order(order):
            return True
        if order.get("payment_status") != "paid":
            logger.warning(
                "[WooCommerce] Skip create (not paid): order_id=%s payment_status=%s",
                order_id,
                order.get("payment_status"),
            )
            return False

        candidates = await _candidate_platform_stores(order, platform="woocommerce")
        if not candidates:
            await log_order_event(
                event_type="merchant_order_failed",
                order_id=order_id,
                merchant_id=order["merchant_id"],
                metadata={"platform": "woocommerce", "error": "active_woocommerce_store_missing"},
            )
            return False

        order_items = _as_order_items(order.get("items"))
        billing_address = _build_woocommerce_address(order)
        shipping_address = dict(billing_address)
        last_error: Optional[str] = None

        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            for store in candidates:
                store_url, consumer_key, consumer_secret = _parse_woocommerce_store_credentials(store)
                if not store_url or not consumer_key or not consumer_secret:
                    continue

                line_items: List[Dict[str, Any]] = []
                for item in order_items:
                    try:
                        product_id = int(str(item.get("product_id") or "").strip())
                        quantity = int(item.get("quantity") or 0)
                    except Exception:
                        last_error = "WooCommerce order item is missing a numeric product_id or quantity"
                        line_items = []
                        break
                    if quantity <= 0:
                        last_error = "WooCommerce order item quantity must be > 0"
                        line_items = []
                        break
                    line_item: Dict[str, Any] = {"product_id": product_id, "quantity": quantity}
                    variant_id = str(item.get("variant_id") or "").strip()
                    if variant_id:
                        try:
                            line_item["variation_id"] = int(variant_id)
                        except Exception:
                            last_error = "WooCommerce variation_id must be numeric"
                            line_items = []
                            break
                    unit_price = item.get("unit_price")
                    if unit_price is not None:
                        try:
                            total = Decimal(str(unit_price)) * Decimal(quantity)
                            line_item["subtotal"] = str(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
                            line_item["total"] = line_item["subtotal"]
                        except Exception:
                            pass
                    line_items.append(line_item)

                if not line_items:
                    continue

                payload = {
                    "status": "processing",
                    "set_paid": True,
                    "payment_method": "pivota_external",
                    "payment_method_title": "Pivota External Payment",
                    "customer_note": f"Pivota Order ID: {order_id}",
                    "billing": billing_address,
                    "shipping": shipping_address,
                    "line_items": line_items,
                }
                response = await client.post(
                    f"{store_url}/wp-json/wc/v3/orders",
                    params={"consumer_key": consumer_key, "consumer_secret": consumer_secret},
                    json=payload,
                )
                if response.status_code not in (200, 201):
                    last_error = f"WooCommerce API error {response.status_code}: {(response.text or '')[:500]}"
                    continue

                data = response.json() or {}
                platform_order_id = str(data.get("id") or "").strip()
                if not platform_order_id:
                    last_error = "WooCommerce response missing order id"
                    continue

                metadata = _merge_linked_platform_order_metadata(
                    order,
                    platform="woocommerce",
                    platform_order_id=platform_order_id,
                    platform_order_name=str(data.get("number") or platform_order_id),
                    platform_order_url=f"{store_url}/wp-admin/post.php?post={platform_order_id}&action=edit",
                    store=store,
                )
                store_id_used = str((store or {}).get("store_id") or "").strip() or None
                await update_fulfillment_info(order_id=order_id, fulfillment_status="processing")
                await update_order_row(
                    order_id,
                    {
                        "metadata": metadata,
                        **({"store_id": store_id_used} if store_id_used else {}),
                    },
                )
                await log_order_event(
                    event_type="merchant_order_created",
                    order_id=order_id,
                    merchant_id=order["merchant_id"],
                    metadata={
                        "platform": "woocommerce",
                        "platform_order_id": platform_order_id,
                        "store_id": store_id_used,
                        "domain": str((store or {}).get("domain") or "").strip() or None,
                    },
                )
                logger.info(
                    "[WooCommerce] ✅ Order linked: order_id=%s platform_order_id=%s store_id=%s",
                    order_id,
                    platform_order_id,
                    store_id_used,
                )
                return True

        if last_error:
            await log_order_event(
                event_type="merchant_order_failed",
                order_id=order_id,
                merchant_id=order["merchant_id"],
                metadata={"platform": "woocommerce", "error": last_error},
            )
        return False


async def create_bigcommerce_order(order_id: str) -> bool:
    lock_key = _platform_order_create_lock_key("bigcommerce", order_id)
    async with _pg_advisory_lock_best_effort(lock_key=lock_key) as lock_acquired:
        if not lock_acquired:
            logger.info("[BigCommerce] Create already in progress; skipping: order_id=%s", order_id)
            return True

        order = await get_order(order_id)
        if not order:
            logger.error("[BigCommerce] Order %s not found", order_id)
            return False
        if _get_linked_platform_order(order):
            return True
        if order.get("payment_status") != "paid":
            logger.warning(
                "[BigCommerce] Skip create (not paid): order_id=%s payment_status=%s",
                order_id,
                order.get("payment_status"),
            )
            return False

        candidates = await _candidate_platform_stores(order, platform="bigcommerce")
        if not candidates:
            await log_order_event(
                event_type="merchant_order_failed",
                order_id=order_id,
                merchant_id=order["merchant_id"],
                metadata={"platform": "bigcommerce", "error": "active_bigcommerce_store_missing"},
            )
            return False

        order_items = _as_order_items(order.get("items"))
        billing_address = _build_bigcommerce_address(order)
        shipping_address = dict(billing_address)
        last_error: Optional[str] = None

        async with httpx.AsyncClient(timeout=20.0) as client:
            for store in candidates:
                store_hash, access_token, client_id, store_domain = _parse_bigcommerce_store_credentials(store)
                if not store_hash or not access_token:
                    continue

                headers = build_bigcommerce_headers(access_token, client_id)
                status_id = await _resolve_bigcommerce_status_id(
                    client=client,
                    store_hash=store_hash,
                    headers=headers,
                )

                products_payload: List[Dict[str, Any]] = []
                try:
                    for item in order_items:
                        product_id = int(str(item.get("product_id") or "").strip())
                        quantity = int(item.get("quantity") or 0)
                        if quantity <= 0:
                            raise ValueError("BigCommerce order item quantity must be > 0")
                        line_item: Dict[str, Any] = {"product_id": product_id, "quantity": quantity}
                        variant_id = str(item.get("variant_id") or "").strip()
                        if variant_id:
                            product_options = await _fetch_bigcommerce_variant_product_options(
                                client=client,
                                store_hash=store_hash,
                                headers=headers,
                                product_id=product_id,
                                variant_id=int(variant_id),
                            )
                            if product_options:
                                line_item["product_options"] = product_options
                        products_payload.append(line_item)
                except Exception as exc:
                    last_error = f"BigCommerce item mapping failed: {exc}"
                    continue

                payload = {
                    "billing_address": billing_address,
                    "shipping_addresses": [
                        {
                            **shipping_address,
                            "shipping_method": "Pivota External Shipping",
                        }
                    ],
                    "products": products_payload,
                    "customer_message": f"Pivota Order ID: {order_id}",
                    "staff_notes": f"Pivota external payment reference: {order.get('payment_intent_id')}",
                }
                if status_id is not None:
                    payload["status_id"] = status_id

                response = await client.post(
                    f"https://api.bigcommerce.com/stores/{store_hash}/v2/orders",
                    headers=headers,
                    json=payload,
                )
                if response.status_code not in (200, 201):
                    last_error = f"BigCommerce API error {response.status_code}: {(response.text or '')[:500]}"
                    continue

                data = response.json() or {}
                platform_order_id = str(data.get("id") or "").strip()
                if not platform_order_id:
                    last_error = "BigCommerce response missing order id"
                    continue

                metadata = _merge_linked_platform_order_metadata(
                    order,
                    platform="bigcommerce",
                    platform_order_id=platform_order_id,
                    platform_order_name=str(data.get("id") or platform_order_id),
                    platform_order_url=None,
                    store=store,
                )
                store_id_used = str((store or {}).get("store_id") or "").strip() or None
                await update_fulfillment_info(order_id=order_id, fulfillment_status="processing")
                await update_order_row(
                    order_id,
                    {
                        "metadata": metadata,
                        **({"store_id": store_id_used} if store_id_used else {}),
                    },
                )
                await log_order_event(
                    event_type="merchant_order_created",
                    order_id=order_id,
                    merchant_id=order["merchant_id"],
                    metadata={
                        "platform": "bigcommerce",
                        "platform_order_id": platform_order_id,
                        "store_id": store_id_used,
                        "domain": store_domain,
                    },
                )
                logger.info(
                    "[BigCommerce] ✅ Order linked: order_id=%s platform_order_id=%s store_id=%s",
                    order_id,
                    platform_order_id,
                    store_id_used,
                )
                return True

        if last_error:
            await log_order_event(
                event_type="merchant_order_failed",
                order_id=order_id,
                merchant_id=order["merchant_id"],
                metadata={"platform": "bigcommerce", "error": last_error},
            )
        return False


async def sync_order_to_connected_store(order_id: str) -> bool:
    order = await get_order(order_id)
    if not order:
        return False
    if _get_linked_platform_order(order):
        return True

    bound_store_id = str(order.get("store_id") or "").strip() or None
    store_info = None
    if bound_store_id:
        store_info = await get_store_by_id(bound_store_id, merchant_id=str(order.get("merchant_id") or "").strip())
    if not store_info:
        store_info = await get_primary_store(str(order.get("merchant_id") or "").strip())
    platform = str((store_info or {}).get("platform") or "").strip().lower()
    if platform == "shopify":
        return await _create_shopify_order_impl(order_id)
    if platform == "woocommerce":
        return await create_woocommerce_order(order_id)
    if platform == "bigcommerce":
        return await create_bigcommerce_order(order_id)
    logger.info("[MerchantSync] No supported store connected for order_id=%s platform=%s", order_id, platform or None)
    return False


async def create_shopify_order(order_id: str) -> bool:
    """
    Legacy entrypoint retained for webhook/payment callers.

    This now dispatches to the connected merchant platform instead of assuming Shopify-only.
    """
    return await sync_order_to_connected_store(order_id)


async def _create_shopify_order_impl(order_id: str) -> bool:
    """
    在 Shopify 中创建订单（通知商户发货）
    
    防御性设计：
    - 失败不影响 Pivota 订单状态
    - 记录事件日志用于后续重试
    """
    lock_key: Optional[int] = None
    lock_acquired = True
    try:
        lock_acquired, lock_key = await _try_acquire_shopify_order_lock(order_id)
        if not lock_acquired:
            logger.info(
                "[Shopify] Duplicate create suppressed by advisory lock: order_id=%s",
                order_id,
            )
            return True

        logger.info("[Shopify] Starting order creation for %s", order_id)

        order = await get_order(order_id)
        if not order:
            logger.error("[Shopify] Order %s not found", order_id)
            return False

        existing_shopify_order_id = str(order.get("shopify_order_id") or "").strip()
        if existing_shopify_order_id:
            logger.info(
                "[Shopify] Order already linked: order_id=%s shopify_order_id=%s",
                order_id,
                existing_shopify_order_id,
            )
            return True

        if order.get("payment_status") != "paid":
            logger.warning(
                "[Shopify] Skip create (not paid): order_id=%s payment_status=%s",
                order_id,
                order.get("payment_status"),
            )
            return False
        logger.info(
            "[Shopify] Order data: merchant_id=%s items_count=%s has_email=%s",
            order.get("merchant_id"),
            len(order.get("items", []) or []),
            bool(str(order.get("customer_email") or "").strip()),
        )

        from services.shopify_graphql_client import shopify_admin_graphql
        from db.orders import update_order as update_order_row

        pivota_tag = f"pivota_order_id:{order_id}"

        def _token_fingerprint(token: Optional[str]) -> Optional[str]:
            if not token:
                return None
            return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]

        async def _find_existing_order_id_best_effort(
            *, shop_domain: str, access_token: str
        ) -> Optional[str]:
            query = """
            query($query: String!) {
              orders(first: 1, query: $query) {
                edges {
                  node {
                    legacyResourceId
                  }
                }
              }
            }
            """
            try:
                data = await shopify_admin_graphql(
                    shop_domain=shop_domain,
                    access_token=access_token,
                    query=query,
                    variables={"query": f"tag:{pivota_tag}"},
                    api_version="2024-07",
                    timeout_s=10.0,
                )
                orders_node = data.get("orders") if isinstance(data, dict) else None
                edges = orders_node.get("edges") if isinstance(orders_node, dict) else None
                if isinstance(edges, list) and edges:
                    node = (edges[0] or {}).get("node") or {}
                    legacy = node.get("legacyResourceId")
                    legacy_str = str(legacy).strip() if legacy is not None else ""
                    return legacy_str or None
            except Exception:
                return None
            return None

        # Choose candidate stores:
        # - Prefer the order.store_id if it points at a Shopify store row
        # - Fall back to any active Shopify store for the merchant
        stores = await get_merchant_active_stores(order["merchant_id"])
        shopify_stores = [s for s in (stores or []) if (s or {}).get("platform") == "shopify"]
        if not shopify_stores:
            logger.error("[Shopify] No active Shopify store for merchant %s", order["merchant_id"])
            return False

        bound_store_id = str(order.get("store_id") or "").strip() or None
        bound_store = None
        if bound_store_id:
            for s in shopify_stores:
                if str((s or {}).get("store_id") or "") == bound_store_id:
                    bound_store = s
                    break

        candidates: List[Dict[str, Any]] = []
        if bound_store:
            candidates.append(bound_store)

        bound_domain = (bound_store or {}).get("domain") if bound_store else None
        for s in shopify_stores:
            if s in candidates:
                continue
            if bound_domain and (s or {}).get("domain") == bound_domain:
                candidates.append(s)
        for s in shopify_stores:
            if s in candidates:
                continue
            candidates.append(s)

        # 构造 Shopify 订单数据
        # Priority: Use variant_id if available (from real Shopify products)
        # Fallback: Use title-based custom line items (for testing/manual orders)
        line_items = []
        for item in order["items"]:
            # Check if item has a real Shopify variant_id
            has_variant = False
            if item.get("variant_id"):
                try:
                    variant_id = int(item["variant_id"])
                    # Real Shopify variant IDs are typically > 10000000000
                    # Use variant_id for proper inventory management
                    line_item = {
                        "variant_id": variant_id,
                        "quantity": item["quantity"]
                    }
                    line_items.append(line_item)
                    has_variant = True
                    logger.info(f"Using variant_id {variant_id} for {item.get('product_title')}")
                except (ValueError, TypeError):
                    logger.warning(f"Invalid variant_id: {item.get('variant_id')}")
            
            # Fallback to custom line item if no variant_id
            if not has_variant:
                line_item = {
                    "title": item.get("product_title", "Product"),
                    "quantity": item["quantity"],
                    "price": str(item["unit_price"]),
                    "taxable": False  # Custom items, tax already calculated
                }
                line_items.append(line_item)
                logger.info(f"Using custom line item for {item.get('product_title')}")
        
        # 转换地址格式：Pivota → Shopify
        customer_email = str(order.get("customer_email") or "").strip()
        shipping_addr = order.get("shipping_address") or {}
        raw_name = str(shipping_addr.get("name") or "").strip()
        fallback_name = str(order.get("customer_name") or "").strip()
        email_name = ""
        if customer_email and "@" in customer_email:
            email_name = customer_email.split("@", 1)[0].strip()
        full_name = raw_name or fallback_name or email_name or "Customer"
        # Shopify staff notification subjects are often customized to render the buyer identity using
        # `customer.*` and/or `billing_address.*`. If a last name is blank, Liquid templates may apply
        # a fallback like `{{ last_name | default: first_name }}` which can render duplicated names
        # (e.g. "peng peng"). We avoid this by using an invisible last name placeholder for single-token names.
        # NOTE: \u200b (ZWSP) may still be treated as "blank" by some template filters; use
        # a non-whitespace invisible character to prevent Liquid `default`/`blank` fallbacks.
        INVISIBLE_LAST_NAME = "\u2060"  # word joiner
        parts = full_name.split()
        normalized_parts = [p for p in parts if p.strip()]
        # If the name is duplicated (e.g. "peng peng"), treat it as a single-token input.
        all_same = bool(normalized_parts) and len({p.lower() for p in normalized_parts}) == 1
        include_name_field = False
        if not normalized_parts:
            first_name, last_name = "Customer", INVISIBLE_LAST_NAME
        elif len(normalized_parts) == 1 or all_same:
            first_name, last_name = normalized_parts[0], INVISIBLE_LAST_NAME
            full_name = normalized_parts[0]
        else:
            first_name, last_name = normalized_parts[0], " ".join(normalized_parts[1:])
            include_name_field = True
        shopify_shipping = {
            "first_name": first_name,
            "last_name": last_name,
            # Some templates use `billing_address.name`/`shipping_address.name` directly.
            # Avoid sending `name` for single-token inputs, as Shopify may re-parse it and
            # backfill last_name=first_name, resulting in duplicated names.
            **({"name": full_name} if include_name_field else {}),
            "address1": shipping_addr.get("address_line1", ""),
            "address2": shipping_addr.get("address_line2"),
            "city": shipping_addr.get("city", ""),
            "province": shipping_addr.get("state", ""),
            "zip": shipping_addr.get("postal_code", ""),
            "country": shipping_addr.get("country", "US"),
            "phone": shipping_addr.get("phone")
        }
        
        logger.info(
            "[Shopify] Converted address: has_name=%s country=%s",
            bool(full_name and full_name != "Customer"),
            shopify_shipping.get("country"),
        )

        # Merchant PSP payments (e.g. Stripe) complete outside Shopify. Shopify's customer order
        # confirmation email can show "Paid 0" unless a successful transaction exists at the time
        # the email is generated. We embed a best-effort external transaction during order creation
        # to keep the email accurate. We still run a post-create reconciliation (transactions API)
        # afterwards for idempotency / late-binding payment refs.
        psp_used_for_txn = infer_runtime_provider(
            psp_used=order.get("psp_used"),
            psp_id=order.get("psp_id"),
            payment_reference=order.get("payment_intent_id"),
        )
        external_payment_ref = str(order.get("payment_intent_id") or "").strip() or None
        currency_code = str(order.get("currency") or "").strip().upper() or "USD"
        try:
            order_total = Decimal(str(order.get("total") or "0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except Exception:
            order_total = Decimal("0.00")

        transactions_payload: List[Dict[str, Any]] = []
        if order_total > 0:
            txn: Dict[str, Any] = {
                "kind": "sale",
                "status": "success",
                "amount": str(order_total),
                "source_name": "external",
                # Use "manual" to avoid Shopify rejecting unknown gateway names when the merchant
                # pays via an external PSP (Stripe/Adyen/etc) outside of Shopify Payments.
                "gateway": "manual",
            }
            if currency_code and len(currency_code) == 3:
                txn["currency"] = currency_code
            if external_payment_ref:
                txn["authorization"] = external_payment_ref
            transactions_payload = [txn]

        shopify_order_data = {
            "order": {
                # Email is required for receipts; keep optional in payload in case a legacy order row is missing it.
                **({"email": customer_email} if customer_email else {}),
                # Ensure staff notification subjects that use `{{ customer.name }}` don't render empty.
                "customer": {
                    "first_name": first_name,
                    "last_name": last_name,
                    **({"email": customer_email} if customer_email else {}),
                },
                **({"transactions": transactions_payload} if transactions_payload else {}),
                "financial_status": "paid",
                "send_receipt": bool(customer_email),
                "send_fulfillment_receipt": bool(customer_email),
                "line_items": line_items,
                "shipping_address": shopify_shipping,
                # Many templates reference billing_address.* for the buyer identity.
                "billing_address": shopify_shipping,
                "note": f"Pivota Order ID: {order_id}",
                "tags": ",".join(["pivota", "agent-order", pivota_tag])
            }
        }

        async def _finalize_success(
            *,
            shopify_order_id: str,
            store_used: Dict[str, Any],
            shop_domain: str,
            access_token: str,
            event_type: str,
        ) -> bool:
            await update_fulfillment_info(
                order_id=order_id,
                shopify_order_id=shopify_order_id,
                fulfillment_status="processing",
            )
            store_id_used = str((store_used or {}).get("store_id") or "").strip() or None
            if store_id_used and store_id_used != bound_store_id:
                try:
                    await update_order_row(order_id, {"store_id": store_id_used})
                except Exception:
                    pass

            await log_order_event(
                event_type=event_type,
                order_id=order_id,
                merchant_id=order["merchant_id"],
                metadata={
                    "shopify_order_id": shopify_order_id,
                    "store_id": store_id_used,
                    "domain": shop_domain,
                    "api_key_fp": _token_fingerprint(access_token),
                },
            )

            # Best-effort reconciliation: record external PSP payment as a Shopify transaction.
            try:
                psp_used = infer_runtime_provider(
                    psp_used=order.get("psp_used"),
                    psp_id=order.get("psp_id"),
                    payment_reference=order.get("payment_intent_id"),
                )
                payment_ref = order.get("payment_intent_id") or None
                await ensure_external_payment_transaction_best_effort(
                    shop_domain=shop_domain,
                    access_token=access_token,
                    shopify_order_id=shopify_order_id,
                    psp_used=psp_used,
                    external_payment_ref=payment_ref,
                    amount=float(order.get("total") or 0),
                    currency=str(order.get("currency") or "USD"),
                    pivota_order_id=order_id,
                )
            except Exception as e:
                logger.warning(
                    "[Shopify] Payment transaction sync failed order_id=%s shopify_order_id=%s err=%s",
                    order_id,
                    shopify_order_id,
                    str(e),
                )

            logger.info(
                "[Shopify] ✅ Shopify order linked: order_id=%s shopify_order_id=%s store_id=%s domain=%s",
                order_id,
                shopify_order_id,
                store_id_used,
                shop_domain,
            )
            return True

        # Concurrency guard: confirm-payment + Stripe webhook can race and both try to create.
        # Use a Postgres advisory lock when available, otherwise proceed best-effort.
        lock_key = _shopify_order_create_lock_key(order_id)
        async with _pg_advisory_lock_best_effort(lock_key=lock_key) as lock_acquired:
            if not lock_acquired:
                # Another worker is creating the Shopify order. Wait briefly for it to finish
                # and return the observed outcome (avoid duplicate creation).
                for _ in range(60):
                    await asyncio.sleep(0.2)
                    latest = await get_order(order_id)
                    if latest and str(latest.get("shopify_order_id") or "").strip():
                        return True
                logger.info("[Shopify] Create already in progress; skipping: order_id=%s", order_id)
                return False

            # Re-check after acquiring the lock in case another path linked the order just before us.
            latest = await get_order(order_id)
            latest_shopify_order_id = str((latest or {}).get("shopify_order_id") or "").strip()
            if latest_shopify_order_id:
                logger.info(
                    "[Shopify] Order linked while waiting for lock: order_id=%s shopify_order_id=%s",
                    order_id,
                    latest_shopify_order_id,
                )
                return True

            # NOTE: Shopify REST Admin API is on a legacy track; keep as-is for v0.1,
            # but plan migration to GraphQL Admin Orders API if you intend to ship as a public app.
            async with httpx.AsyncClient() as client:
                last_error: Optional[str] = None
                for store in candidates:
                    shop_domain_raw = str((store or {}).get("domain") or "").strip()
                    shop_domain = _normalize_shopify_domain(shop_domain_raw)
                    store_id = str((store or {}).get("store_id") or "").strip() or None
                    access_token, token_meta = await resolve_shopify_admin_access_token(
                        shop_domain=shop_domain,
                        api_key_raw=(store or {}).get("api_key_raw") or (store or {}).get("api_key"),
                        store_id=store_id,
                    )

                    if not shop_domain or not access_token:
                        continue

                    token_fp = _token_fingerprint(access_token)
                    logger.info(
                        "[Shopify] Attempt create: order_id=%s store_id=%s domain=%s token_fp=%s",
                        order_id,
                        store_id,
                        shop_domain,
                        token_fp,
                    )

                    # Idempotency guardrail: if Shopify already has an order with our tag, reuse it.
                    existing_id = await _find_existing_order_id_best_effort(
                        shop_domain=shop_domain, access_token=access_token
                    )
                    if existing_id:
                        return await _finalize_success(
                            shopify_order_id=existing_id,
                            store_used=store,
                            shop_domain=shop_domain,
                            access_token=access_token,
                            event_type="shopify_order_reused",
                        )

                    url = f"https://{shop_domain}/admin/api/2024-01/orders.json"
                    headers = {"X-Shopify-Access-Token": access_token, "Content-Type": "application/json"}

                    # Retry once on transient upstream failures.
                    for attempt in range(2):
                        try:
                            response = await client.post(
                                url,
                                json=shopify_order_data,
                                headers=headers,
                                timeout=12.0,
                            )
                        except Exception as e:
                            last_error = f"{type(e).__name__}: {str(e)}"
                            if attempt == 0:
                                continue
                            await log_order_event(
                                event_type="shopify_order_error",
                                order_id=order_id,
                                merchant_id=order["merchant_id"],
                                metadata={
                                    "store_id": store_id,
                                    "domain": shop_domain,
                                    "api_key_fp": token_fp,
                                    "token_refreshed": bool((token_meta or {}).get("refreshed")),
                                    "token_refresh_error": (token_meta or {}).get("refresh_error"),
                                    "error": last_error,
                                },
                            )
                            return False

                        logger.info("[Shopify] API response: %s", response.status_code)

                        if response.status_code == 201:
                            shopify_order = response.json().get("order") or {}
                            shopify_order_id = str(shopify_order.get("id") or "").strip()
                            if not shopify_order_id:
                                last_error = "Missing Shopify order id in response"
                                break
                            return await _finalize_success(
                                shopify_order_id=shopify_order_id,
                                store_used=store,
                                shop_domain=shop_domain,
                                access_token=access_token,
                                event_type="shopify_order_created",
                            )

                        # Auth errors: try another store row (stale token recovery).
                        if response.status_code in (401, 403):
                            error_msg = (response.text or "")[:800]
                            await log_order_event(
                                event_type="shopify_order_failed",
                                order_id=order_id,
                                merchant_id=order["merchant_id"],
                                metadata={
                                    "status_code": response.status_code,
                                    "store_id": store_id,
                                    "domain": shop_domain,
                                    "api_key_fp": token_fp,
                                    "error": error_msg,
                                },
                            )
                            last_error = f"Auth failed {response.status_code}"
                            break

                        # Retryable upstream issues.
                        if response.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                            continue

                        error_msg = (response.text or "")[:800]
                        await log_order_event(
                            event_type="shopify_order_failed",
                            order_id=order_id,
                            merchant_id=order["merchant_id"],
                            metadata={
                                "status_code": response.status_code,
                                "store_id": store_id,
                                "domain": shop_domain,
                                "api_key_fp": token_fp,
                                "error": error_msg,
                            },
                        )
                        last_error = f"Shopify API error {response.status_code}"
                        break

                if last_error:
                    logger.error("[Shopify] ❌ Failed to create order_id=%s err=%s", order_id, last_error)
                return False
    except Exception as e:
        logger.error(f"[Shopify] ❌ Exception in create_shopify_order: {type(e).__name__}: {e}", exc_info=True)
        
        # 记录异常
        try:
            await log_order_event(
                event_type="shopify_order_error",
                order_id=order_id,
                merchant_id=order.get("merchant_id", "unknown") if order else "unknown",
                metadata={"error": str(e), "error_type": type(e).__name__}
            )
        except Exception as log_error:
            logger.error(f"[Shopify] Failed to log order event: {log_error}")
            
        return False
    finally:
        await _release_shopify_order_lock(lock_key, lock_acquired=lock_acquired)


# ============================================================================
# 订单状态更新（Admin/Webhook 调用）
# ============================================================================

@router.get("/{order_id}/debug")
async def debug_order_data(
    order_id: str,
    _: dict = Depends(require_admin_or_key),
):
    """调试端点：查看订单的原始数据结构和Shopify credentials"""
    try:
        order = await get_order(order_id)
        if not order:
            return {"error": "Order not found"}
        
        order_store_id = str(order.get("store_id") or "").strip() or None

        # Primary (latest active) store for this merchant.
        from services.merchant_store_service import get_primary_store
        primary_store = await get_primary_store(order["merchant_id"])

        # Bound store referenced by the order row (may be stale/inactive).
        bound_store = None
        if order_store_id:
            try:
                row = await database.fetch_one(
                    """
                    SELECT store_id, platform, domain, api_key, status, connected_at
                    FROM merchant_stores
                    WHERE store_id = :store_id
                    LIMIT 1
                    """,
                    {"store_id": order_store_id},
                )
                if row:
                    bound_store = dict(row)
                    bound_store["api_key_raw"] = bound_store.get("api_key")
                    bound_store["source"] = "merchant_stores"
            except Exception:
                bound_store = None

        def _summarize_store(store: Dict[str, Any] | None) -> Dict[str, Any]:
            if not store:
                return {}
            token = extract_shopify_access_token((store or {}).get("api_key_raw") or (store or {}).get("api_key"))
            token_fp = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12] if token else None
            return {
                "store_id": store.get("store_id"),
                "platform": store.get("platform"),
                "domain": store.get("domain"),
                "status": store.get("status"),
                "source": store.get("source"),
                "has_api_key": bool(token),
                "api_key_length": len(token) if token else 0,
                "api_key_fp": token_fp,
            }
        
        # 检查数据类型
        return {
            "order_id": order_id,
            "merchant_id": order["merchant_id"],
            "order_store_id": order_store_id,
            "bound_store": _summarize_store(bound_store),
            "primary_store": _summarize_store(primary_store),
            "data_types": {
                "items": str(type(order.get("items"))),
                "items_count": len(order.get("items", [])),
                "shipping_address": str(type(order.get("shipping_address"))),
                "has_customer_email": bool(str(order.get("customer_email") or "").strip()),
            }
        }
    except Exception as e:
        logger.error(f"Debug error: {type(e).__name__}: {e}", exc_info=True)
        return {"error": str(e), "error_type": type(e).__name__}


@router.post("/{order_id}/create-shopify")
async def trigger_shopify_order(
    order_id: str,
    _: dict = Depends(require_admin_or_key),
):
    """Manually trigger Shopify order creation for debugging"""
    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    if order.get("shopify_order_id"):
        return {"status": "already_exists", "shopify_order_id": order["shopify_order_id"]}
    
    if order.get("payment_status") != "paid":
        return {"status": "not_paid", "payment_status": order.get("payment_status")}
    
    try:
        success = await create_shopify_order(order_id)
        if success:
            updated_order = await get_order(order_id)
            return {
                "status": "success",
                "shopify_order_id": updated_order.get("shopify_order_id"),
                "message": "Shopify order created"
            }
        else:
            # 查询最近的order事件日志来获取错误
            event_query = """
                SELECT event_type, metadata, created_at
                FROM order_events
                WHERE order_id = :order_id
                ORDER BY created_at DESC
                LIMIT 5
            """
            events = await database.fetch_all(event_query, {"order_id": order_id})
            
            error_details = []
            for event in events:
                if event["event_type"] in ["shopify_order_failed", "shopify_order_error"]:
                    error_details.append({
                        "event": event["event_type"],
                        "metadata": event["metadata"],
                        "time": str(event["created_at"])
                    })
            
            return {
                "status": "failed",
                "message": "Shopify order creation failed",
                "error_details": error_details if error_details else "No error events found - check Railway logs for [Shopify] entries"
            }
    except Exception as e:
        logger.error(f"Exception in trigger_shopify_order: {type(e).__name__}: {e}", exc_info=True)
        return {"status": "error", "error": str(e), "error_type": type(e).__name__}


@router.post("/reconcile-missing-shopify")
async def reconcile_missing_shopify_orders(
    merchant_id: Optional[str] = Query(None, description="Optional merchant_id to scope reconciliation"),
    limit: int = Query(50, ge=1, le=500),
    min_age_seconds: int = Query(
        120,
        ge=0,
        le=7 * 24 * 3600,
        description="Only reconcile orders paid at least this many seconds ago",
    ),
    dry_run: bool = Query(False),
    current_user: dict = Depends(require_admin_or_key),
):
    """
    Ops endpoint: reconcile paid orders that are missing `shopify_order_id`.

    This is a guardrail against transient failures (DB busy, timeouts, stale store rows).
    Intended to be called by a cron/scheduler or manually during incidents.
    """
    cutoff = datetime.utcnow() - timedelta(seconds=int(min_age_seconds))

    conditions = [
        orders_table.c.is_deleted.is_(False),
        orders_table.c.payment_status == "paid",
        or_(orders_table.c.shopify_order_id.is_(None), orders_table.c.shopify_order_id == ""),
        or_(
            and_(orders_table.c.paid_at.isnot(None), orders_table.c.paid_at <= cutoff),
            and_(orders_table.c.paid_at.is_(None), orders_table.c.created_at <= cutoff),
        ),
    ]
    if merchant_id:
        conditions.append(orders_table.c.merchant_id == merchant_id)

    try:
        base_query = select(orders_table.c.order_id)
    except Exception:
        # SQLAlchemy 1.x compatibility
        base_query = select([orders_table.c.order_id])

    query = (
        base_query.where(and_(*conditions))
        .order_by(orders_table.c.created_at.asc())
        .limit(int(limit))
    )
    rows = await database.fetch_all(query)
    order_ids: List[str] = []
    for row in (rows or []):
        if not row:
            continue
        order_id_value = None
        try:
            # `databases` can return row wrappers that support `__getitem__` but not `.get()`.
            order_id_value = row["order_id"]
        except Exception:
            if isinstance(row, dict):
                order_id_value = row.get("order_id")
            else:
                order_id_value = getattr(row, "order_id", None)
        if order_id_value in (None, ""):
            continue
        order_ids.append(str(order_id_value))

    if dry_run:
        return {
            "status": "success",
            "dry_run": True,
            "merchant_id": merchant_id,
            "cutoff_utc": cutoff.isoformat() + "Z",
            "candidates": order_ids,
            "count": len(order_ids),
        }

    succeeded: List[str] = []
    failed: List[Dict[str, Any]] = []
    for oid in order_ids:
        try:
            ok = await create_shopify_order(oid)
            if ok:
                succeeded.append(oid)
            else:
                failed.append({"order_id": oid, "error": "create_shopify_order returned false"})
        except Exception as e:
            failed.append({"order_id": oid, "error": f"{type(e).__name__}: {str(e)}"})

    return {
        "status": "success",
        "dry_run": False,
        "merchant_id": merchant_id,
        "cutoff_utc": cutoff.isoformat() + "Z",
        "attempted": len(order_ids),
        "succeeded": len(succeeded),
        "failed": len(failed),
        "succeeded_orders": succeeded,
        "failed_orders": failed[:50],
    }


@router.post("/{order_id}/ship")
async def mark_order_as_shipped(
    order_id: str,
    tracking_number: str,
    background_tasks: BackgroundTasks,
    carrier: Optional[str] = None,
    current_user: dict = Depends(require_admin)
):
    """标记订单已发货"""
    success = await mark_order_shipped(order_id, tracking_number, carrier)
    
    if not success:
        raise HTTPException(status_code=404, detail="Order not found")
    
    order = await get_order(order_id)
    
    # 记录发货事件
    await log_order_event(
        event_type="order_shipped",
        order_id=order_id,
        merchant_id=order["merchant_id"],
        metadata={
            "tracking_number": tracking_number,
            "carrier": carrier
        }
    )

    # 后台任务：订单发货后发送评价邀请邮件
    async def send_review_invitation_task():
        try:
            internal_key = (_reviews_invitation_internal_key() or "").strip()
            if not internal_key:
                logger.info("Reviews invitation issuer disabled; skip send.")
                return
            delay = _reviews_invitation_send_delay_seconds()
            worker_enabled = (os.getenv("REVIEWS_INVITATION_WORKER_ENABLED") or "").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            if delay > 0 or worker_enabled:
                ok = await enqueue_reviews_invitation_send_job_from_order(
                    merchant_id=order["merchant_id"],
                    order_id=order_id,
                    force_reschedule=False,
                )
                logger.info(f"Reviews invitation job enqueued for order {order_id} ok={ok}")
                return
            req = SendInvitationEmailFromOrderRequest(
                merchant_id=order["merchant_id"],
                order_id=order_id,
                ttl_seconds=7 * 24 * 3600,
            )
            await send_invitation_email_from_order(
                body=req,
                response=Response(),
                x_internal_key=internal_key,
            )
            logger.info(
                f"Reviews invitation email dispatched for order {order_id}"
            )
        except HTTPException as e:
            logger.warning(
                f"Reviews invitation skipped for order {order_id}: {e.detail}"
            )
        except Exception as e:
            logger.error(
                f"Reviews invitation error for order {order_id}: {e}"
            )

    background_tasks.add_task(send_review_invitation_task)
    
    return {
        "status": "success",
        "message": "Order marked as shipped",
        "order_id": order_id,
        "tracking_number": tracking_number
    }


@router.post("/{order_id}/cancel")
async def cancel_order(
    order_id: str,
    reason: Optional[str] = None,
    current_user: dict = Depends(require_admin)
):
    """取消订单"""
    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    if order["payment_status"] == "paid":
        raise HTTPException(
            status_code=400, 
            detail="Cannot cancel paid order. Please process refund first."
        )
    
    success = await update_order_status(
        order_id=order_id,
        status="cancelled",
        cancelled_at=datetime.now(),
        metadata={**(order.get("metadata") or {}), "cancellation_reason": reason}
    )
    
    if not success:
        raise HTTPException(status_code=500, detail="Failed to cancel order")
    
    # 记录取消事件
    await log_order_event(
        event_type="order_cancelled",
        order_id=order_id,
        merchant_id=order["merchant_id"],
        metadata={"reason": reason}
    )
    
    return {
        "status": "success",
        "message": "Order cancelled",
        "order_id": order_id
    }
