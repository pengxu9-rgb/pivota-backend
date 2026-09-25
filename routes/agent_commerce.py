from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select

from db.canonical_commerce import canonical_offers, canonical_products, canonical_variants
from db.database import database
from db.orders import get_order
from models.order import CreateOrderRequest, OrderItem, ShippingAddress
from routes.agent_api import agent_create_order
from routes.agent_auth import AgentContext, get_agent_context
from routes.agent_user_auth import AgentUserContext, get_agent_user_context
from routes.refund_api import RefundRequest, process_refund as process_refund_route
from services.commerce_ledger_provenance import ledger_provenance
from services.commerce_order_ref import pivota_order_ref
from services.commerce_interaction_service import (
    find_interaction_by_order_id,
    record_commerce_event,
    trace_interaction,
)
from services.merchant_commerce_readiness_service import upsert_merchant_commerce_readiness_state
from services.merchant_store_service import get_primary_store
from services.shopify_returns_service import (
    probe_shopify_return_eligibility_best_effort,
    sync_shopify_returns_best_effort,
)


router = APIRouter(prefix="/agent/v2/commerce", tags=["agent-commerce"])


class CommerceCheckoutItem(BaseModel):
    product_id: Optional[str] = None
    variant_id: Optional[str] = None
    canonical_product_id: Optional[str] = None
    canonical_variant_id: Optional[str] = None
    sku: Optional[str] = None
    quantity: int = Field(1, ge=1, le=20)
    title: Optional[str] = None
    unit_price: Optional[float] = Field(default=None, gt=0)
    currency: Optional[str] = None


class CommerceShippingAddress(BaseModel):
    name: str = Field(..., min_length=1)
    address_line1: str = Field(..., min_length=1)
    city: str = Field(..., min_length=1)
    postal_code: str = Field(..., min_length=1)
    country: str = Field(..., min_length=2, max_length=2)
    address_line2: Optional[str] = None
    state: Optional[str] = None
    phone: Optional[str] = None


class CreateCommerceCheckoutRequest(BaseModel):
    merchant_id: str = Field(..., min_length=1)
    interaction_id: str = Field(..., min_length=1)
    customer_email: EmailStr
    customer_name: Optional[str] = None
    buyer_ref: Optional[str] = None
    brief_id: Optional[str] = None
    brief_schema_version: Optional[str] = None
    source: Optional[str] = None
    currency: str = Field(default="USD", min_length=3, max_length=8)
    preferred_psp: Optional[str] = None
    idempotency_key: Optional[str] = Field(default=None, max_length=255)
    shipping_address: CommerceShippingAddress
    items: List[CommerceCheckoutItem]
    metadata: Optional[Dict[str, Any]] = None


class PaymentIntentRequest(BaseModel):
    preferred_psps: Optional[List[str]] = None


class CheckoutRefundBody(BaseModel):
    amount: Optional[float] = Field(default=None, gt=0)
    reason: Optional[str] = None
    restore_inventory: bool = True
    idempotency_key: Optional[str] = None


class CheckoutReturnBody(BaseModel):
    api_version: Optional[str] = Field(default="2025-10", max_length=16)
    limit: int = Field(20, ge=1, le=100)


def _model_dump(model: Any) -> Dict[str, Any]:
    if isinstance(model, dict):
        return dict(model)
    dump = getattr(model, "model_dump", None)
    if callable(dump):
        return dump()
    return model.dict()


def _surface_from_source(source: Optional[str]) -> str:
    normalized = str(source or "").strip().lower()
    return normalized or "agent_v2_commerce"


async def _ensure_execute_ready(merchant_id: str) -> Dict[str, Any]:
    readiness = await upsert_merchant_commerce_readiness_state(merchant_id)
    if str(readiness.get("execute_status") or "").strip().lower() != "ready":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MERCHANT_EXECUTE_NOT_READY",
                "merchant_id": merchant_id,
                "execute_status": readiness.get("execute_status"),
                "blockers": readiness.get("execute_blockers") or [],
            },
        )
    return readiness


async def _resolve_checkout_item(merchant_id: str, item: CommerceCheckoutItem) -> Dict[str, Any]:
    resolved_product_id = str(item.product_id or "").strip() or None
    resolved_variant_id = str(item.variant_id or "").strip() or None
    canonical_product_id = str(item.canonical_product_id or "").strip() or None
    canonical_variant_id = str(item.canonical_variant_id or "").strip() or None
    title = str(item.title or "").strip() or None
    unit_price = item.unit_price
    currency = str(item.currency or "").strip() or None

    product_row: Optional[Dict[str, Any]] = None
    variant_row: Optional[Dict[str, Any]] = None

    if canonical_variant_id:
        variant = await database.fetch_one(
            select(canonical_variants).where(
                canonical_variants.c.merchant_id == merchant_id,
                canonical_variants.c.canonical_variant_id == canonical_variant_id,
            )
        )
        if not variant:
            raise HTTPException(status_code=400, detail=f"Unknown canonical_variant_id '{canonical_variant_id}'")
        variant_row = dict(variant)
        resolved_product_id = resolved_product_id or str(variant_row.get("platform_product_id") or "").strip() or None
        resolved_variant_id = resolved_variant_id or str(variant_row.get("platform_variant_id") or "").strip() or None
        canonical_product_id = canonical_product_id or str(variant_row.get("canonical_product_id") or "").strip() or None

    if canonical_product_id:
        product = await database.fetch_one(
            select(canonical_products).where(
                canonical_products.c.merchant_id == merchant_id,
                canonical_products.c.canonical_product_id == canonical_product_id,
            )
        )
        if product:
            product_row = dict(product)
            resolved_product_id = resolved_product_id or str(product_row.get("platform_product_id") or "").strip() or None
            title = title or str(product_row.get("title") or "").strip() or None
            currency = currency or str(product_row.get("currency") or "").strip() or None

    if canonical_variant_id and unit_price is None:
        offer = await database.fetch_one(
            select(canonical_offers).where(
                canonical_offers.c.merchant_id == merchant_id,
                canonical_offers.c.canonical_variant_id == canonical_variant_id,
            )
        )
        if offer:
            offer_row = dict(offer)
            try:
                unit_price = float(offer_row.get("amount"))
            except Exception:
                unit_price = unit_price
            currency = currency or str(offer_row.get("currency") or "").strip() or None

    if not resolved_product_id:
        raise HTTPException(status_code=400, detail="Each checkout item must resolve to a platform product_id")
    if unit_price is None:
        raise HTTPException(status_code=400, detail="Each checkout item must resolve to a unit_price")

    return {
        "product_id": resolved_product_id,
        "variant_id": resolved_variant_id,
        "canonical_product_id": canonical_product_id,
        "canonical_variant_id": canonical_variant_id,
        "product_title": title or resolved_product_id,
        "sku": item.sku,
        "quantity": item.quantity,
        "unit_price": unit_price,
        "currency": currency or "USD",
    }


def _build_payment_action_from_order(order: Dict[str, Any]) -> Dict[str, Any]:
    client_secret = str(order.get("client_secret") or "").strip() or None
    if client_secret and client_secret.startswith("http"):
        return {"type": "redirect_url", "url": client_secret, "client_secret": None}
    if client_secret:
        return {"type": "client_secret", "client_secret": client_secret, "url": None}
    return {"type": None, "client_secret": None, "url": None}


def _ensure_order_access(order: Optional[Dict[str, Any]], context: AgentContext) -> Dict[str, Any]:
    if not order:
        raise HTTPException(status_code=404, detail="Checkout not found")
    merchant_id = str(order.get("merchant_id") or "").strip()
    if not merchant_id or not context.can_access_merchant(merchant_id):
        raise HTTPException(status_code=403, detail="Not authorized for this merchant")
    return order


@router.post("/checkouts")
async def create_commerce_checkout(
    body: CreateCommerceCheckoutRequest,
    background_tasks: BackgroundTasks,
    context: AgentContext = Depends(get_agent_context),
    agent_user: Optional[AgentUserContext] = Depends(get_agent_user_context),
):
    if not context.can_access_merchant(body.merchant_id):
        raise HTTPException(status_code=403, detail="Not authorized for this merchant")

    readiness = await _ensure_execute_ready(body.merchant_id)
    store = await get_primary_store(body.merchant_id) or {}
    platform = str(store.get("platform") or readiness.get("primary_platform") or "").strip().lower() or "shopify"
    resolved_items = [await _resolve_checkout_item(body.merchant_id, item) for item in body.items]

    order_request = CreateOrderRequest(
        merchant_id=body.merchant_id,
        customer_email=body.customer_email,
        customer_name=body.customer_name,
        brief_id=body.brief_id,
        brief_schema_version=body.brief_schema_version,
        items=[
            OrderItem(
                product_id=item["product_id"],
                product_title=item["product_title"],
                variant_id=item["variant_id"],
                sku=item["sku"],
                quantity=item["quantity"],
                unit_price=item["unit_price"],
            )
            for item in resolved_items
        ],
        shipping_address=ShippingAddress(**_model_dump(body.shipping_address)),
        currency=body.currency,
        metadata={
            **(body.metadata or {}),
            "interaction_id": body.interaction_id,
            "surface": _surface_from_source(body.source),
            "platform": platform,
            "buyer_ref": body.buyer_ref,
            "canonical_product_ids": [item.get("canonical_product_id") for item in resolved_items if item.get("canonical_product_id")],
            "canonical_variant_ids": [item.get("canonical_variant_id") for item in resolved_items if item.get("canonical_variant_id")],
        },
        preferred_psp=body.preferred_psp,
        idempotency_key=body.idempotency_key,
    )
    created = await agent_create_order(
        order_request=order_request,
        background_tasks=background_tasks,
        context=context,
        agent_user=agent_user,
        x_buyer_ref=body.buyer_ref,
    )
    order_payload = _model_dump(created)
    checkout_id = str(order_payload.get("order_id") or "").strip()
    payment_action = _model_dump(order_payload.get("payment_action")) if order_payload.get("payment_action") else _build_payment_action_from_order(order_payload)
    first_item = resolved_items[0] if resolved_items else {}

    await record_commerce_event(
        event_type="checkout.created",
        metadata={
            # The identity the ingress authenticated, carried in metadata so the
            # interaction-level merge treats it as verified (see _authenticated_agent_id).
            "agent_id": context.agent_id,
            "agent_identity_confidence": "verified",
            "merchant_id": body.merchant_id,
            "interaction_id": body.interaction_id,
            "platform": platform,
            "surface": _surface_from_source(body.source),
            "checkout_id": checkout_id,
            "order_id": checkout_id,
            "order_ref": pivota_order_ref(checkout_id),
            "buyer_id": body.buyer_ref,
            "brief_id": body.brief_id,
            "canonical_product_id": first_item.get("canonical_product_id"),
            "canonical_variant_id": first_item.get("canonical_variant_id"),
            "payment_action": payment_action,
        },
        source="agent_v2_commerce",
        upstream_idempotency_key=body.idempotency_key or f"checkout:{checkout_id}",
        actor_type="agent",
        actor_id=context.agent_id,
        **ledger_provenance("agent_commerce_api", "verified"),
    )
    await record_commerce_event(
        event_type="payment.intent.created",
        metadata={
            # The identity the ingress authenticated, carried in metadata so the
            # interaction-level merge treats it as verified (see _authenticated_agent_id).
            "agent_id": context.agent_id,
            "agent_identity_confidence": "verified",
            "merchant_id": body.merchant_id,
            "interaction_id": body.interaction_id,
            "platform": platform,
            "surface": _surface_from_source(body.source),
            "checkout_id": checkout_id,
            "order_id": checkout_id,
            "order_ref": pivota_order_ref(checkout_id),
            "buyer_id": body.buyer_ref,
            "canonical_product_id": first_item.get("canonical_product_id"),
            "canonical_variant_id": first_item.get("canonical_variant_id"),
            "payment_action": payment_action,
        },
        source="agent_v2_commerce",
        upstream_idempotency_key=f"payment-intent:{checkout_id}",
        actor_type="agent",
        actor_id=context.agent_id,
        **ledger_provenance("agent_commerce_api", "verified"),
    )

    return {
        "status": "success",
        "merchant_id": body.merchant_id,
        "platform": platform,
        "checkout_id": checkout_id,
        "order_id": checkout_id,
        "interaction_id": body.interaction_id,
        "payment_url": payment_action.get("url"),
        "client_secret": payment_action.get("client_secret"),
        "payment_action": payment_action,
        "readiness_state": {
            "execute_status": readiness.get("execute_status"),
            "discover_status": readiness.get("discover_status"),
            "signals_status": readiness.get("signals_status"),
        },
        "order": order_payload,
    }


@router.post("/checkouts/{checkout_id}/payment-intent")
async def get_checkout_payment_intent(
    checkout_id: str,
    body: PaymentIntentRequest,
    context: AgentContext = Depends(get_agent_context),
):
    order = _ensure_order_access(await get_order(checkout_id), context)
    payment_action = _build_payment_action_from_order(order)
    if not payment_action.get("url") and not payment_action.get("client_secret"):
        raise HTTPException(status_code=409, detail="Checkout does not have an active payment action")

    merchant_id = str(order.get("merchant_id") or "").strip()
    interaction = await find_interaction_by_order_id(checkout_id, merchant_id=merchant_id)
    await record_commerce_event(
        event_type="payment.intent.viewed",
        metadata={
            # The identity the ingress authenticated, carried in metadata so the
            # interaction-level merge treats it as verified (see _authenticated_agent_id).
            "agent_id": context.agent_id,
            "agent_identity_confidence": "verified",
            "merchant_id": order.get("merchant_id"),
            "interaction_id": (interaction or {}).get("interaction_id"),
            "platform": (await get_primary_store(str(order.get("merchant_id") or "")) or {}).get("platform"),
            "surface": (interaction or {}).get("surface") or "agent_v2_commerce",
            "checkout_id": checkout_id,
            "order_id": checkout_id,
            "order_ref": pivota_order_ref(checkout_id),
            "payment_action": payment_action,
            "preferred_psps": body.preferred_psps or [],
        },
        source="agent_v2_commerce",
        upstream_idempotency_key=f"payment-intent-view:{checkout_id}",
        actor_type="agent",
        actor_id=context.agent_id,
        **ledger_provenance("agent_commerce_api", "verified"),
    )
    return {
        "status": "success",
        "checkout_id": checkout_id,
        "order_id": checkout_id,
        "payment_url": payment_action.get("url"),
        "client_secret": payment_action.get("client_secret"),
        "payment_action": payment_action,
        "payment_status": order.get("payment_status"),
    }


@router.get("/checkouts/{checkout_id}/status")
async def get_checkout_status(
    checkout_id: str,
    context: AgentContext = Depends(get_agent_context),
):
    order = _ensure_order_access(await get_order(checkout_id), context)
    merchant_id = str(order.get("merchant_id") or "").strip()
    interaction = await find_interaction_by_order_id(checkout_id, merchant_id=merchant_id)
    trace = await trace_interaction(str(interaction.get("interaction_id"))) if interaction else {"interaction": None, "events": []}
    store = await get_primary_store(str(order.get("merchant_id") or "")) or {}
    return {
        "status": "success",
        "checkout_id": checkout_id,
        "order_id": checkout_id,
        "merchant_id": order.get("merchant_id"),
        "platform": store.get("platform"),
        "order_status": order.get("status"),
        "payment_status": order.get("payment_status"),
        "fulfillment_status": order.get("fulfillment_status"),
        "payment_action": _build_payment_action_from_order(order),
        "interaction_trace": trace,
    }


@router.post("/checkouts/{checkout_id}/refunds")
async def create_checkout_refund(
    checkout_id: str,
    body: CheckoutRefundBody,
    background_tasks: BackgroundTasks,
    context: AgentContext = Depends(get_agent_context),
):
    order = _ensure_order_access(await get_order(checkout_id), context)
    response = await process_refund_route(
        order_id=checkout_id,
        refund_request=RefundRequest(
            order_id=checkout_id,
            amount=body.amount,
            reason=body.reason,
            restore_inventory=body.restore_inventory,
            idempotency_key=body.idempotency_key,
        ),
        background_tasks=background_tasks,
        current_user={"role": "agent", "agent_id": context.agent_id},
    )
    merchant_id = str(order.get("merchant_id") or "").strip()
    interaction = await find_interaction_by_order_id(checkout_id, merchant_id=merchant_id)
    await record_commerce_event(
        event_type="refund.requested",
        metadata={
            # The identity the ingress authenticated, carried in metadata so the
            # interaction-level merge treats it as verified (see _authenticated_agent_id).
            "agent_id": context.agent_id,
            "agent_identity_confidence": "verified",
            "merchant_id": order.get("merchant_id"),
            "interaction_id": (interaction or {}).get("interaction_id"),
            "platform": (await get_primary_store(str(order.get("merchant_id") or "")) or {}).get("platform"),
            "surface": (interaction or {}).get("surface") or "agent_v2_commerce",
            "checkout_id": checkout_id,
            "order_id": checkout_id,
            "order_ref": pivota_order_ref(checkout_id),
            "refund_id": response.get("refund_id"),
            "amount": response.get("refund_amount"),
            "reason": body.reason,
        },
        source="agent_v2_commerce",
        upstream_idempotency_key=body.idempotency_key or f"refund:{checkout_id}:{response.get('refund_id')}",
        actor_type="agent",
        actor_id=context.agent_id,
        **ledger_provenance("agent_commerce_api", "verified"),
    )
    return {
        "status": "success",
        "checkout_id": checkout_id,
        "order_id": checkout_id,
        "refund": response,
    }


@router.post("/checkouts/{checkout_id}/returns")
async def sync_checkout_returns(
    checkout_id: str,
    body: CheckoutReturnBody,
    context: AgentContext = Depends(get_agent_context),
):
    order = _ensure_order_access(await get_order(checkout_id), context)
    merchant_id = str(order.get("merchant_id") or "").strip()
    store = await get_primary_store(merchant_id) or {}
    platform = str(store.get("platform") or "").strip().lower()
    interaction = await find_interaction_by_order_id(checkout_id, merchant_id=merchant_id)

    if platform != "shopify":
        await record_commerce_event(
            event_type="return.sync.pending",
            metadata={
                # The identity the ingress authenticated, carried in metadata so the
                # interaction-level merge treats it as verified (see _authenticated_agent_id).
                "agent_id": context.agent_id,
                "agent_identity_confidence": "verified",
                "merchant_id": merchant_id,
                "interaction_id": (interaction or {}).get("interaction_id"),
                "platform": platform,
                "surface": (interaction or {}).get("surface") or "agent_v2_commerce",
                "checkout_id": checkout_id,
                "order_id": checkout_id,
                "order_ref": pivota_order_ref(checkout_id),
            },
            source="agent_v2_commerce",
            upstream_idempotency_key=f"return-sync-pending:{checkout_id}:{platform or 'unknown'}",
            actor_type="agent",
            actor_id=context.agent_id,
            **ledger_provenance("agent_commerce_api", "verified"),
        )
        return {
            "status": "pending_external_platform",
            "checkout_id": checkout_id,
            "order_id": checkout_id,
            "platform": platform or "unknown",
            "message": "Return sync is only automated for Shopify at the moment.",
        }

    shop_domain = str(store.get("domain") or "").strip()
    access_token = str(store.get("api_key") or "").strip()
    if not shop_domain or not access_token:
        raise HTTPException(status_code=409, detail="Shopify store credentials are incomplete for return sync")

    sync_result = await sync_shopify_returns_best_effort(
        merchant_id=merchant_id,
        shop_domain=shop_domain,
        access_token=access_token,
        api_version=body.api_version or "2025-10",
        limit=body.limit,
    )
    eligibility = None
    shopify_order_id = str(order.get("shopify_order_id") or "").strip()
    if shopify_order_id:
        eligibility = await probe_shopify_return_eligibility_best_effort(
            shop_domain=shop_domain,
            access_token=access_token,
            api_version=body.api_version or "2025-10",
            shopify_order_id=shopify_order_id,
        )

    await record_commerce_event(
        event_type="return.sync.completed",
        metadata={
            # The identity the ingress authenticated, carried in metadata so the
            # interaction-level merge treats it as verified (see _authenticated_agent_id).
            "agent_id": context.agent_id,
            "agent_identity_confidence": "verified",
            "merchant_id": merchant_id,
            "interaction_id": (interaction or {}).get("interaction_id"),
            "platform": platform,
            "surface": (interaction or {}).get("surface") or "agent_v2_commerce",
            "checkout_id": checkout_id,
            "order_id": checkout_id,
            "order_ref": pivota_order_ref(checkout_id),
            "return_sync": sync_result,
            "return_eligibility": eligibility,
        },
        source="agent_v2_commerce",
        upstream_idempotency_key=f"return-sync:{checkout_id}:{body.api_version}:{body.limit}",
        actor_type="agent",
        actor_id=context.agent_id,
        **ledger_provenance("agent_commerce_api", "verified"),
    )
    return {
        "status": "success",
        "checkout_id": checkout_id,
        "order_id": checkout_id,
        "platform": platform,
        "return_sync": sync_result,
        "return_eligibility": eligibility,
    }
