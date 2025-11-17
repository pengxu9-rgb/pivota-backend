"""
Admin Shopify Order POC

EPIC‑5 Mini POC:
Create a Shopify order from a product imported into products_cache.
"""

from datetime import datetime
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, EmailStr

from utils.auth import require_admin
from db.merchant_onboarding import get_merchant_onboarding
from db.products import get_product_cache_row
from jobs.catalog_import_worker import _get_shopify_config_for_merchant, SHOPIFY_API_VERSION

import logging

router = APIRouter(
    prefix="/admin/platform-onboarding",
    tags=["Admin - Shopify Order POC"],
)

logger = logging.getLogger(__name__)


class ShopifyOrderPOCRequest(BaseModel):
    platform_product_id: str
    variant_id: Optional[int] = None
    quantity: int = Field(1, ge=1, le=10)
    buyer_email: Optional[EmailStr] = None


class ShopifyOrderPOCResponse(BaseModel):
    status: str
    order_id: Optional[int]
    order_name: Optional[str]
    shop_domain: str
    variant_info: Optional[Dict[str, Any]] = None
    shopify_order_url: Optional[str] = None


@router.post(
    "/{onboarding_id}/shopify/order-poc",
    response_model=ShopifyOrderPOCResponse,
)
async def create_shopify_order_poc(
    onboarding_id: str,
    payload: ShopifyOrderPOCRequest,
    current_admin: Dict[str, Any] = Depends(require_admin),
) -> ShopifyOrderPOCResponse:
    """
    Create a minimal Shopify order for a product imported into products_cache.

    - Admin-only POC endpoint.
    - Uses per-merchant Shopify credentials if available, otherwise global config.
    - Uses the first variant by default; optional variant_id can override.
    """

    logger.info(
        "Shopify Order POC - Starting",
        extra={
            "onboarding_id": onboarding_id,
            "platform_product_id": payload.platform_product_id,
            "quantity": payload.quantity,
        },
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
            if int(v.get("id")) == payload.variant_id:
                chosen_variant = v
                break
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
            "note": f"Pivota EPIC-5 Shopify Order POC for {onboarding_id}",
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
    if resp.status_code == 401 or resp.status_code == 403:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Shopify credentials invalid or unauthorized",
        )
    elif resp.status_code == 404:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shopify resource not found",
        )
    elif resp.status_code == 422:
        # Shopify validation error (e.g., inventory, variant issues)
        try:
            error_detail = resp.json()
        except Exception:
            error_detail = {"message": "Validation failed"}
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Order validation failed: {error_detail}",
        )
    elif resp.status_code >= 500:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Shopify API unavailable",
        )
    elif resp.status_code != 201:
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
        "Shopify Order POC - Success",
        extra={
            "onboarding_id": onboarding_id,
            "order_id": order.get("id"),
            "order_name": order.get("name"),
            "variant_id": variant_id,
            "duration_ms": duration_ms,
        },
    )

    return ShopifyOrderPOCResponse(
        status="success",
        order_id=order.get("id"),
        order_name=order.get("name"),
        shop_domain=shop_domain,
        variant_info=variant_info,
        shopify_order_url=f"https://{shop_domain}/admin/orders/{order.get('id')}" if order.get("id") else None,
    )

