"""
Admin Platform Order POC - EPIC-7/8

Unified Admin-only POC endpoint for creating platform orders from products_cache.
Phase 1: Shopify (real API).
Phase 2 (EPIC-8): Amazon/Temu stub adapters (no external API calls yet).
"""

from datetime import datetime
from typing import Any, Dict, Optional, Literal
from uuid import uuid4
import hashlib
import os

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, EmailStr

from utils.auth import require_admin
from db.merchant_onboarding import get_merchant_onboarding
from db.products import get_product_cache_row
from db.shopify_order_poc_log import get_poc_log_by_hash, insert_poc_log
from jobs.catalog_import_worker import _get_shopify_config_for_merchant, SHOPIFY_API_VERSION
from models.standard_product import StandardProduct

import logging


router = APIRouter(
    prefix="/admin/platform-onboarding",
    tags=["Admin - Platform Order POC"],
)

logger = logging.getLogger(__name__)


class PlatformOrderPOCRequest(BaseModel):
    platform: Literal["shopify", "amazon", "temu"]
    platform_product_id: str
    variant_id: Optional[str] = None
    quantity: int = Field(1, ge=1, le=10)
    buyer_email: Optional[EmailStr] = None


class PlatformOrderPOCResponse(BaseModel):
    status: str
    platform: str
    poc_mode: Optional[str] = None  # "shopify_live" | "amazon_stub" | "temu_stub" | future variants
    platform_order_id: Optional[str] = None
    platform_order_name: Optional[str] = None
    platform_order_url: Optional[str] = None
    variant_info: Optional[Dict[str, Any]] = None
    raw: Optional[Dict[str, Any]] = None


async def _place_shopify_order_from_cache(
    onboarding_id: str,
    payload: PlatformOrderPOCRequest,
) -> PlatformOrderPOCResponse:
    """
    Place a minimal Shopify order using a product imported into products_cache.

    This is a thin wrapper around the EPIC-5 logic, adapted to the unified
    PlatformOrderPOCRequest/Response shapes.
    """

    logger.info(
        "Platform Order POC (Shopify) - Starting",
        extra={
            "onboarding_id": onboarding_id,
            "platform_product_id": payload.platform_product_id,
            "quantity": payload.quantity,
        },
    )

    # 0. Safety: allowlist for Shopify live POC (optional env override).
    # If SHOPIFY_LIVE_ONBOARDINGS is set, only those onboarding_ids are allowed.
    raw_whitelist = os.getenv("SHOPIFY_LIVE_ONBOARDINGS", "")
    whitelist = {x.strip() for x in raw_whitelist.split(",") if x.strip()}
    if whitelist and onboarding_id not in whitelist:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Shopify live order POC is not enabled for this onboarding_id",
        )

    # Idempotency key based on input tuple.
    hash_input = "|".join(
        [
            onboarding_id,
            "shopify",
            payload.platform_product_id,
            str(payload.variant_id or ""),
            str(payload.quantity),
            payload.buyer_email or "",
        ]
    )
    request_hash = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()

    # 0.1 Check if we already created a POC order for this exact request.
    existing = await get_poc_log_by_hash(request_hash)
    if existing and existing.get("shopify_order_id"):
        logger.info(
            "Platform Order POC (Shopify) - Idempotent replay",
            extra={
                "onboarding_id": onboarding_id,
                "platform_product_id": payload.platform_product_id,
                "quantity": payload.quantity,
                "shopify_order_id": existing.get("shopify_order_id"),
            },
        )
        return PlatformOrderPOCResponse(
            status="success",
            platform="shopify",
            poc_mode="shopify_live",
            platform_order_id=existing.get("shopify_order_id"),
            platform_order_name=existing.get("shopify_order_name"),
            platform_order_url=existing.get("shopify_order_url"),
            variant_info=None,
            raw=existing.get("raw_order"),
        )

    # 1. Ensure onboarding exists
    record = await get_merchant_onboarding(onboarding_id)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Onboarding not found",
        )

    # 2. Resolve Shopify config (per-merchant credentials or env)
    cfg = await _get_shopify_config_for_merchant(onboarding_id)
    shop_domain = cfg.get("shop_domain") or ""
    access_token = cfg.get("access_token") or ""
    if not shop_domain or not access_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Shopify configuration missing for this merchant",
        )

    # 3. Lookup product in products_cache
    cache_row = await get_product_cache_row(
        merchant_id=onboarding_id,
        platform="shopify",
        platform_product_id=payload.platform_product_id,
        include_expired=False,
    )
    if not cache_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found in cache",
        )

    product_data = cache_row.get("product_data") or {}
    raw = product_data.get("raw") or {}
    variants = raw.get("variants") or []
    if not variants:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No variants available for this product",
        )

    # 4. Choose variant: payload.variant_id or first variant
    chosen_variant: Optional[Dict[str, Any]] = None
    if payload.variant_id is not None:
        for v in variants:
            # Shopify variant.id is numeric; payload.variant_id is string.
            try:
                if str(v.get("id")) == str(payload.variant_id):
                    chosen_variant = v
                    break
            except Exception:
                continue
        if not chosen_variant:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Variant not found for given variant_id",
            )
    else:
        chosen_variant = variants[0]

    variant_id = chosen_variant.get("id")
    if not variant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Variant id missing in raw payload",
        )

    # 5. Build Shopify order payload
    email = payload.buyer_email or "poc-buyer@example.com"
    order_payload = {
        "order": {
            "email": email,
            "send_receipt": False,
            "send_fulfillment_receipt": False,
            "financial_status": "paid",
            "line_items": [
                {"variant_id": variant_id, "quantity": payload.quantity}
            ],
            "note": f"Pivota EPIC-7 Platform Order POC (Shopify) for {onboarding_id}",
        }
    }

    # 6. Call Shopify Orders API
    started_at = datetime.utcnow()
    url = f"https://{shop_domain}/admin/api/{SHOPIFY_API_VERSION}/orders.json"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                json=order_payload,
                headers={"X-Shopify-Access-Token": access_token},
            )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Shopify order request error: {exc}",
        )

    duration_ms = int((datetime.utcnow() - started_at).total_seconds() * 1000)

    # 7. Basic error classification
    if resp.status_code in (401, 403):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Shopify credentials invalid or unauthorized",
        )
    if resp.status_code == 404:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shopify resource not found",
        )
    if resp.status_code == 422:
        try:
            error_detail = resp.json()
        except Exception:
            error_detail = {"message": "Validation failed"}
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Order validation failed: {error_detail}",
        )
    if resp.status_code >= 500:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Shopify API unavailable",
        )
    if resp.status_code != 201:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Unexpected Shopify order error: status={resp.status_code}",
        )

    order = resp.json().get("order") or {}
    variant_info = {
        "variant_id": variant_id,
        "title": chosen_variant.get("title"),
        "price": chosen_variant.get("price"),
        "sku": chosen_variant.get("sku"),
    }

    logger.info(
        "Platform Order POC (Shopify) - Success",
        extra={
            "onboarding_id": onboarding_id,
            "order_id": order.get("id"),
            "order_name": order.get("name"),
            "variant_id": variant_id,
            "duration_ms": duration_ms,
        },
    )

    # Persist basic POC log for idempotency and auditing.
    try:
        await insert_poc_log(
            onboarding_id=onboarding_id,
            request_hash=request_hash,
            platform_product_id=payload.platform_product_id,
            variant_id=str(variant_id),
            quantity=payload.quantity,
            buyer_email=payload.buyer_email or None,
            shopify_order_id=str(order.get("id")) if order.get("id") is not None else None,
            shopify_order_name=order.get("name"),
            shopify_order_url=f"https://{shop_domain}/admin/orders/{order.get('id')}" if order.get("id") else None,
            raw_order=order,
        )
    except Exception as exc:
        # Logging failures must not break the main flow.
        logger.warning(
            "Failed to persist Shopify order POC log",
            extra={
                "onboarding_id": onboarding_id,
                "platform_product_id": payload.platform_product_id,
                "error": str(exc),
            },
        )

    return PlatformOrderPOCResponse(
        status="success",
        platform="shopify",
        poc_mode="shopify_live",
        platform_order_id=str(order.get("id")) if order.get("id") is not None else None,
        platform_order_name=order.get("name"),
        platform_order_url=f"https://{shop_domain}/admin/orders/{order.get('id')}" if order.get("id") else None,
        variant_info=variant_info,
        raw=order,
    )


async def _place_amazon_order_from_cache(
    onboarding_id: str,
    payload: PlatformOrderPOCRequest,
) -> PlatformOrderPOCResponse:
    """
    Amazon Order POC adapter (stub).

    - Reads StandardProduct from products_cache (platform='amazon').
    - Requires product.orderable == True.
    - Selects variant by payload.variant_id or first variant.
    - Constructs a synthetic order id/name and logs payload; no external API call.
    """

    logger.info(
        "Platform Order POC (Amazon stub) - Starting",
        extra={
            "onboarding_id": onboarding_id,
            "platform_product_id": payload.platform_product_id,
            "quantity": payload.quantity,
        },
    )

    record = await get_merchant_onboarding(onboarding_id)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Onboarding not found",
        )

    cache_row = await get_product_cache_row(
        merchant_id=onboarding_id,
        platform="amazon",
        platform_product_id=payload.platform_product_id,
        include_expired=False,
    )
    if not cache_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found in cache",
        )

    product_data = cache_row.get("product_data") or {}
    try:
        product = StandardProduct(**product_data)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to parse StandardProduct for amazon: {exc}",
        )

    if not product.orderable:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "PRODUCT_NOT_ORDERABLE",
                "validation": product.orderable_validation,
            },
        )

    chosen_variant = None
    if payload.variant_id:
        for v in product.variants:
            if v.id == str(payload.variant_id):
                chosen_variant = v
                break
    if not chosen_variant and product.variants:
        chosen_variant = product.variants[0]
    if not chosen_variant:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No variant available for this product",
        )

    synthetic_id = f"poc-amazon-{uuid4().hex[:8]}"
    synthetic_name = f"AMZ-POC-{synthetic_id.split('-')[-1].upper()}"

    platform_payload = {
        "asin": product.id,
        "seller_sku": chosen_variant.sku or chosen_variant.id,
        "quantity": payload.quantity,
        "price": chosen_variant.price,
        "currency": product.currency,
        "buyer_email": payload.buyer_email or "poc-buyer@example.com",
        "mode": "stub",
    }

    logger.info(
        "Platform Order POC (Amazon stub) - Success",
        extra={
            "onboarding_id": onboarding_id,
            "platform_order_id": synthetic_id,
            "variant_id": chosen_variant.id,
        },
    )

    return PlatformOrderPOCResponse(
        status="success",
        platform="amazon",
        poc_mode="amazon_stub",
        platform_order_id=synthetic_id,
        platform_order_name=synthetic_name,
        platform_order_url=None,
        variant_info={
            "variant_id": chosen_variant.id,
            "title": chosen_variant.title,
            "price": chosen_variant.price,
            "sku": chosen_variant.sku,
        },
        raw={"mode": "stub", "payload": platform_payload},
    )


async def _place_temu_order_from_cache(
    onboarding_id: str,
    payload: PlatformOrderPOCRequest,
) -> PlatformOrderPOCResponse:
    """
    Temu Order POC adapter (stub).

    - Reads StandardProduct from products_cache (platform='temu').
    - Requires product.orderable == True.
    - Selects variant by payload.variant_id or first variant.
    - Constructs a synthetic order id/name and logs payload; no external API call.
    """

    logger.info(
        "Platform Order POC (Temu stub) - Starting",
        extra={
            "onboarding_id": onboarding_id,
            "platform_product_id": payload.platform_product_id,
            "quantity": payload.quantity,
        },
    )

    record = await get_merchant_onboarding(onboarding_id)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Onboarding not found",
        )

    cache_row = await get_product_cache_row(
        merchant_id=onboarding_id,
        platform="temu",
        platform_product_id=payload.platform_product_id,
        include_expired=False,
    )
    if not cache_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found in cache",
        )

    product_data = cache_row.get("product_data") or {}
    try:
        product = StandardProduct(**product_data)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to parse StandardProduct for temu: {exc}",
        )

    if not product.orderable:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "PRODUCT_NOT_ORDERABLE",
                "validation": product.orderable_validation,
            },
        )

    chosen_variant = None
    if payload.variant_id:
        for v in product.variants:
            if v.id == str(payload.variant_id):
                chosen_variant = v
                break
    if not chosen_variant and product.variants:
        chosen_variant = product.variants[0]
    if not chosen_variant:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No variant available for this product",
        )

    synthetic_id = f"poc-temu-{uuid4().hex[:8]}"
    synthetic_name = f"TEMU-POC-{synthetic_id.split('-')[-1].upper()}"

    platform_payload = {
        "product_id": product.id,
        "variant_id": chosen_variant.id,
        "quantity": payload.quantity,
        "price": chosen_variant.price,
        "currency": product.currency,
        "buyer_email": payload.buyer_email or "poc-buyer@example.com",
        "mode": "stub",
    }

    logger.info(
        "Platform Order POC (Temu stub) - Success",
        extra={
            "onboarding_id": onboarding_id,
            "platform_order_id": synthetic_id,
            "variant_id": chosen_variant.id,
        },
    )

    return PlatformOrderPOCResponse(
        status="success",
        platform="temu",
        poc_mode="temu_stub",
        platform_order_id=synthetic_id,
        platform_order_name=synthetic_name,
        platform_order_url=None,
        variant_info={
            "variant_id": chosen_variant.id,
            "title": chosen_variant.title,
            "price": chosen_variant.price,
            "sku": chosen_variant.sku,
        },
        raw={"mode": "stub", "payload": platform_payload},
    )


@router.post(
    "/{onboarding_id}/orders/poc",
    response_model=PlatformOrderPOCResponse,
)
async def create_platform_order_poc(
    onboarding_id: str,
    payload: PlatformOrderPOCRequest,
    current_admin: Dict[str, Any] = Depends(require_admin),
) -> PlatformOrderPOCResponse:
    """
    Unified Admin-only Order POC endpoint.

    Phase 1: Shopify (real API).
    Phase 2 (EPIC-8): Amazon/Temu stub adapters (no external API calls yet).
    """

    if payload.platform == "shopify":
        return await _place_shopify_order_from_cache(onboarding_id, payload)

    if payload.platform == "amazon":
        return await _place_amazon_order_from_cache(onboarding_id, payload)

    if payload.platform == "temu":
        return await _place_temu_order_from_cache(onboarding_id, payload)

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unsupported platform: {payload.platform}",
    )
