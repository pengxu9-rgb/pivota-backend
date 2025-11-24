"""
Shopping AI Gateway

High-level, LLM/Agent-friendly operations on top of the existing product/order APIs.

Currently supports:
- find_products
- get_product_detail
- create_order       (proxied to Agent API)
- submit_payment     (proxied to Agent API)

Path: POST /agent/shop/v1/invoke
"""

import os
from typing import Any, Dict, List, Optional

from datetime import datetime

import httpx
from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field

from services.product_query_service import get_products_hybrid
from models.standard_product import StandardProduct

AGENT_API_BASE = os.getenv("AGENT_API_BASE", "https://web-production-fedb.up.railway.app").rstrip("/")
AGENT_API_KEY = os.getenv("SHOP_GATEWAY_AGENT_API_KEY") or os.getenv("PIVOTA_API_KEY") or os.getenv("AGENT_API_KEY")


router = APIRouter(prefix="/agent/shop/v1", tags=["Shopping Gateway"])


class SearchFilters(BaseModel):
    merchant_id: str = Field(..., description="Merchant ID")
    query: str = Field("", description="Search query, empty string means 'all products'")
    category: Optional[str] = Field(None, description="Optional category filter")
    price_min: Optional[float] = Field(None, description="Minimum price filter")
    price_max: Optional[float] = Field(None, description="Maximum price filter")
    page: int = Field(1, ge=1, description="Page number (1-based)")
    limit: int = Field(20, ge=1, le=100, description="Page size (max 100)")


class FindProductsPayload(BaseModel):
    search: SearchFilters


class ProductRef(BaseModel):
    merchant_id: str
    product_id: str


class GetProductDetailPayload(BaseModel):
    product: ProductRef


class OrderItem(BaseModel):
    merchant_id: str
    product_id: str
    product_title: str
    quantity: int
    unit_price: float
    subtotal: float


class ShippingAddress(BaseModel):
    name: str
    address_line1: str
    address_line2: Optional[str] = ""
    city: str
    country: str
    postal_code: str
    phone: Optional[str] = None


class OrderPayloadBody(BaseModel):
    merchant_id: str
    customer_email: str
    items: List[OrderItem]
    shipping_address: ShippingAddress
    customer_notes: Optional[str] = None


class CreateOrderPayload(BaseModel):
    order: OrderPayloadBody


class PaymentPayloadBody(BaseModel):
    order_id: str
    expected_amount: float
    currency: str
    payment_method_hint: Optional[str] = None


class SubmitPaymentPayload(BaseModel):
    payment: PaymentPayloadBody


class ShopGatewayRequest(BaseModel):
    operation: str
    payload: Dict[str, Any]


def _standard_to_shop_product(p: StandardProduct) -> Dict[str, Any]:
    """
    Map internal StandardProduct to Shopping AI product contract.
    """
    # Prefer explicit image_url, then first image in list
    image_url = p.image_url or (p.images[0] if p.images else None)

    return {
        "id": p.product_id or p.id,
        "merchant_id": p.merchant_id,
        "title": p.title,
        "description": p.description or "",
        "price": p.price,
        "currency": p.currency,
        "image_url": image_url,
        "product_type": p.product_type,
        "inventory_quantity": p.inventory_quantity,
        "sku": p.sku,
        "platform": p.platform,
    }


async def _handle_find_products(
    filters: SearchFilters,
    background_tasks: BackgroundTasks,
) -> Dict[str, Any]:
    """
    Implementation of the find_products operation.

    Contract (simplified):
    - Input: { search: { merchant_id, query, category?, price_min?, price_max?, page?, limit? } }
    - Output: { products: [...], total, page, page_size }
    """
    merchant_id = filters.merchant_id
    page = filters.page or 1
    limit = min(filters.limit or 20, 100)

    # To support pagination, fetch up to page * limit items (capped)
    # and slice in-memory. For now we cap to 500 for safety.
    raw_limit = min(page * limit, 500)

    # Use a fixed agent_id for logging/metrics
    agent_id = "shopping_ai_frontend"

    products, query_source, error = await get_products_hybrid(
        merchant_id=merchant_id,
        limit=raw_limit,
        agent_id=agent_id,
        background_tasks=background_tasks,
    )

    if error and not products:
        # Hybrid layer completely failed
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch products for merchant {merchant_id}: {error}",
        )

    # In-memory filtering based on query/category/price
    filtered: List[StandardProduct] = products

    q = (filters.query or "").strip().lower()
    if q:
        def matches_query(prod: StandardProduct) -> bool:
            title = (prod.title or "").lower()
            desc = (prod.description or "").lower()
            ptype = (prod.product_type or "").lower()
            return q in title or q in desc or q in ptype

        filtered = [p for p in filtered if matches_query(p)]

    if filters.category:
        cat = filters.category.lower()

        def matches_category(prod: StandardProduct) -> bool:
            ptype = (prod.product_type or "").lower()
            return cat in ptype

        filtered = [p for p in filtered if matches_category(p)]

    if filters.price_min is not None:
        filtered = [p for p in filtered if p.price >= filters.price_min]

    if filters.price_max is not None:
        filtered = [p for p in filtered if p.price <= filters.price_max]

    total = len(filtered)

    # Pagination slice
    start_idx = (page - 1) * limit
    end_idx = start_idx + limit
    page_items = filtered[start_idx:end_idx]

    return {
        "products": [_standard_to_shop_product(p) for p in page_items],
        "total": total,
        "page": page,
        "page_size": len(page_items),
        "metadata": {
            "query_source": query_source,
            "fetched_at": datetime.utcnow().isoformat(),
        },
    }


async def _handle_get_product_detail(
    ref: ProductRef,
    background_tasks: BackgroundTasks,
) -> Dict[str, Any]:
    """
    Implementation of the get_product_detail operation.

    Contract (simplified):
    - Input: { product: { merchant_id, product_id } }
    - Output: { product: {...same shape as find_products item, with optional attributes} }
    """
    merchant_id = ref.merchant_id
    product_id = ref.product_id

    # Fetch a reasonably large slice of the catalog to locate the product.
    # For typical merchants this is sufficient and keeps latency low.
    agent_id = "shopping_ai_frontend"
    products, query_source, error = await get_products_hybrid(
        merchant_id=merchant_id,
        limit=500,
        agent_id=agent_id,
        background_tasks=background_tasks,
    )

    if error and not products:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch products for merchant {merchant_id}: {error}",
        )

    match: Optional[StandardProduct] = None
    for p in products:
        if p.product_id == product_id or p.id == product_id:
            match = p
            break

    if not match:
        # Strong contract: this should not happen if product comes from find_products,
        # so treat it as PRODUCT_NOT_FOUND.
        raise HTTPException(
            status_code=404,
            detail="PRODUCT_NOT_FOUND",
        )

    base = _standard_to_shop_product(match)

    # Optional attributes bag for LLM/Agent use; keep it simple for now.
    attributes: Dict[str, Any] = {}
    if match.platform_metadata:
        attributes.update(match.platform_metadata)

    # Include variants summary if available
    if match.variants:
        attributes["variants"] = [
            {
                "variant_id": v.variant_id or v.id,
                "title": v.title,
                "price": v.price,
                "sku": v.sku,
                "inventory_quantity": v.inventory_quantity,
                "options": v.options or {},
            }
            for v in match.variants
        ]

    return {
        "product": {
            **base,
            "attributes": attributes or None,
        },
        "metadata": {
            "query_source": query_source,
            "fetched_at": datetime.utcnow().isoformat(),
        },
    }


async def _proxy_agent_api(method: str, path: str, json_body: Dict[str, Any]) -> Dict[str, Any]:
    """Forward a request to the Agent API using a server-side API key."""
    if not AGENT_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="SHOP_GATEWAY_AGENT_API_KEY / PIVOTA_API_KEY is not configured for agent payments",
        )

    url = f"{AGENT_API_BASE}{path}"
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": AGENT_API_KEY,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.request(method, url, json=json_body, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Upstream agent API error: {exc}") from exc

    if resp.status_code >= 400:
        # Propagate upstream error detail when available
        try:
            err_json = resp.json()
        except Exception:
            err_json = {"detail": resp.text}
        raise HTTPException(status_code=resp.status_code, detail=err_json)

    try:
        return resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Invalid JSON from agent API")


async def _handle_create_order(order: OrderPayloadBody) -> Dict[str, Any]:
    """Proxy create_order to Agent API (/agent/v1/orders/create)."""
    body = {
        "merchant_id": order.merchant_id,
        "customer_email": order.customer_email,
        "items": [
            {
                "merchant_id": item.merchant_id,
                "product_id": item.product_id,
                "product_title": item.product_title,
                "quantity": item.quantity,
                "unit_price": item.unit_price,
                "subtotal": item.subtotal,
            }
            for item in order.items
        ],
        "shipping_address": {
            "recipient_name": order.shipping_address.name,
            "address_line1": order.shipping_address.address_line1,
            "address_line2": order.shipping_address.address_line2 or "",
            "city": order.shipping_address.city,
            "country": order.shipping_address.country,
            "postal_code": order.shipping_address.postal_code,
            "phone": order.shipping_address.phone or "",
        },
        "customer_notes": order.customer_notes or "",
    }

    return await _proxy_agent_api("POST", "/agent/v1/orders/create", body)


async def _handle_submit_payment(payment: PaymentPayloadBody) -> Dict[str, Any]:
    """Proxy submit_payment to Agent API (/agent/v1/payments)."""
    body = {
        "order_id": payment.order_id,
        "payment_method": payment.payment_method_hint or "card",
        "total_amount": payment.expected_amount,
        "currency": payment.currency,
    }

    return await _proxy_agent_api("POST", "/agent/v1/payments", body)


@router.post("/invoke")
async def invoke_shop_operation(
    request: ShopGatewayRequest,
    background_tasks: BackgroundTasks,
) -> Dict[str, Any]:
    """
    Unified entrypoint for Shopping AI frontend & LLM agents.

    Supported operations:
    - find_products
    - get_product_detail
    - create_order       (demo-only)
    - submit_payment     (demo-only)
    """
    operation = (request.operation or "").strip()

    if operation == "find_products":
        payload = FindProductsPayload(**request.payload)
        return await _handle_find_products(payload.search, background_tasks)

    if operation == "get_product_detail":
        payload = GetProductDetailPayload(**request.payload)
        return await _handle_get_product_detail(payload.product, background_tasks)

    if operation == "create_order":
        payload = CreateOrderPayload(**request.payload)
        return await _handle_create_order(payload.order)

    if operation == "submit_payment":
        payload = SubmitPaymentPayload(**request.payload)
        return await _handle_submit_payment(payload.payment)

    # For now we only support product operations here.
    raise HTTPException(
        status_code=400,
        detail=f"Unsupported operation: {operation}",
    )
