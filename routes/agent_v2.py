from __future__ import annotations

import json
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from db.database import database
from db.orders import get_order
from db.quotes import expire_quote_if_needed, get_quote
from models.order import CreateOrderRequest, OrderItem, ShippingAddress
from models.catalog import PivotPaymentContext
from models.quote import QuotePreviewRequest
from routes.agent_api import agent_create_order as agent_v1_create_order
from routes.agent_api import agent_search_products as agent_v1_search_products
from routes.agent_api import agent_track_order as agent_v1_track_order
from routes.agent_auth import AgentContext, get_agent_context
from routes.agent_checkout_intents import (
    CheckoutIntentItem,
    CreateCheckoutIntentRequest,
    create_checkout_intent as create_checkout_intent_route,
)
from routes.agent_user_auth import AgentUserContext, get_agent_user_context
from routes.quote_routes import preview_quote as agent_v1_preview_quote
from routes.refund_api import RefundRequest, process_refund as process_refund_route
from services.pcs_tier_service import get_merchant_pcs_tier
from services.agent_governance import validate_request_compat
from services.platform_capabilities import get_store_platform_capabilities
from services.psp_capabilities import get_psp_capabilities
from services.quote_service import QuoteError, QuoteService
from services.refund_observability import build_order_refund_tracking_payload
from services.traffic_taxonomy_service import attach_traffic_taxonomy, build_traffic_taxonomy


router = APIRouter(prefix="/agent/v2", tags=["agent-v2"])
logger = logging.getLogger(__name__)


class RequestContext(BaseModel):
    tenant_id: Optional[str] = None
    request_id: Optional[str] = None
    channel: Optional[str] = None
    locale: Optional[str] = None
    currency: Optional[str] = None
    country: Optional[str] = None
    delivery_region: Optional[str] = None
    user_constraints: Optional[Dict[str, Any]] = None
    consent_context: Optional[Dict[str, Any]] = None


class BuyerContext(BaseModel):
    customer_email: Optional[str] = None
    customer_name: Optional[str] = None
    shipping_address: Optional[Dict[str, Any]] = None
    buyer_ref: Optional[str] = None
    agent_user_ref: Optional[str] = None


class SearchProductsRequest(BaseModel):
    merchant_id: Optional[str] = None
    merchant_ids: Optional[List[str]] = None
    search_all_merchants: bool = False
    query: Optional[str] = None
    category: Optional[str] = None
    catalog_surface: Optional[str] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    in_stock_only: bool = True
    limit: int = Field(default=20, ge=1, le=200)
    offset: int = Field(default=0, ge=0)
    allow_external_seed: bool = True
    external_seed_only: bool = False
    allow_stale_cache: bool = True
    external_seed_strategy: str = "legacy"
    fast_mode: bool = False
    payment_context: Optional[PivotPaymentContext] = None
    request_context: Optional[RequestContext] = None


class QuoteOfferRef(BaseModel):
    offer_id: Optional[str] = None
    product_id: str
    variant_id: str
    quantity: int = Field(default=1, ge=1)


class QuotePreviewBody(BaseModel):
    merchant_id: str
    offer_refs: List[QuoteOfferRef]
    discount_codes: Optional[List[str]] = None
    buyer_context: Optional[BuyerContext] = None
    selected_delivery_option: Optional[Dict[str, Any]] = None
    payment_context: Optional[PivotPaymentContext] = None
    request_context: Optional[RequestContext] = None
    metadata: Optional[Dict[str, Any]] = None


class CreateOrderBody(BaseModel):
    quote_id: str
    buyer_context: BuyerContext
    request_context: Optional[RequestContext] = None
    metadata: Optional[Dict[str, Any]] = None
    preferred_psp: Optional[str] = None
    selected_payment_offer_id: Optional[str] = None
    payment_method_evidence: Optional[Dict[str, Any]] = None
    idempotency_key: Optional[str] = None


class CreateCheckoutSessionBody(BaseModel):
    order_id: str
    return_url: Optional[str] = None
    buyer_ref: Optional[str] = None
    requested_scopes: Optional[List[str]] = None
    market: Optional[str] = None
    locale: Optional[str] = None
    source: Optional[str] = None
    customer_email: Optional[str] = None
    shipping_address: Optional[Dict[str, Any]] = None
    request_context: Optional[RequestContext] = None


class RefundCreateBody(BaseModel):
    amount: Optional[float] = Field(default=None, gt=0)
    reason: Optional[str] = None
    restore_inventory: bool = True
    idempotency_key: Optional[str] = None


def _utc_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return str(value)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _money_str(value: Any) -> str:
    if value is None:
        return "0"
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _decimal_or_zero(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _decode_json_like(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _decode_json_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _order_amounts_from_row(order: Dict[str, Any]) -> Dict[str, Any]:
    metadata = _decode_json_like(order.get("metadata"))
    pricing_quote = _decode_json_like(metadata.get("pricing_quote"))
    pricing = _decode_json_like(pricing_quote.get("pricing"))

    subtotal = (
        _decimal_or_zero(order.get("subtotal"))
        or _decimal_or_zero(pricing.get("subtotal"))
    )
    discount_total = (
        _decimal_or_zero(order.get("discount_total"))
        or _decimal_or_zero(pricing.get("discount_total"))
    )
    shipping_fee = (
        _decimal_or_zero(order.get("shipping_fee"))
        or _decimal_or_zero(pricing.get("shipping_fee"))
    )
    tax = _decimal_or_zero(order.get("tax")) or _decimal_or_zero(pricing.get("tax"))
    total = (
        _decimal_or_zero(order.get("total"))
        or _decimal_or_zero(pricing.get("total"))
        or max(Decimal("0"), subtotal - discount_total) + shipping_fee + tax
    )

    return {
        "subtotal": _money_str(subtotal),
        "discount_total": _money_str(discount_total),
        "shipping_fee": _money_str(shipping_fee),
        "tax": _money_str(tax),
        "total": _money_str(total),
        "currency": order.get("currency") or pricing_quote.get("currency") or "USD",
    }


def _event(event_type: str, **payload: Any) -> Dict[str, Any]:
    return {
        "event_id": f"evt_{uuid.uuid4().hex}",
        "type": event_type,
        "occurred_at": _now_iso(),
        **payload,
    }


def _quote_state_from_status(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized == "expired":
        return "expired"
    if normalized == "consumed":
        return "superseded"
    return "previewed"


def _order_state_from_row(order: Dict[str, Any]) -> str:
    status = str(order.get("status") or "").strip().lower()
    payment_status = str(order.get("payment_status") or "").strip().lower()
    fulfillment_status = str(order.get("fulfillment_status") or "").strip().lower()

    total = Decimal(str(order.get("total") or "0"))
    total_refunded = Decimal(str(order.get("total_refunded") or "0"))

    if status == "cancelled":
        return "cancelled"
    if total > 0 and total_refunded >= total:
        return "refunded"
    if total_refunded > 0:
        return "refund_pending"
    if fulfillment_status == "delivered":
        return "fulfilled"
    if fulfillment_status in {"shipped", "in_transit"}:
        return "partially_fulfilled"
    if payment_status in {"failed", "payment_failed"}:
        return "payment_failed"
    if payment_status in {"awaiting_payment", "pending", "unpaid"}:
        return "awaiting_checkout"
    if payment_status in {"paid", "completed", "succeeded"}:
        if order.get("shopify_order_id") or str(status) in {"processing", "confirmed"}:
            return "confirmed"
        return "merchant_confirming"
    return "draft"


def _quote_id_from_order(order: Dict[str, Any]) -> Optional[str]:
    metadata = _decode_json_like(order.get("metadata"))
    pricing_quote = _decode_json_like(metadata.get("pricing_quote"))
    return (
        pricing_quote.get("quote_id")
        or metadata.get("quote_id")
        or _decode_json_like(metadata.get("agent_v2")).get("quote_id")
    )


def _normalize_image_refs(product: Dict[str, Any]) -> List[str]:
    refs: List[str] = []
    for key in ("image_url", "image", "featured_image"):
        value = product.get(key)
        if isinstance(value, str) and value.strip():
            refs.append(value.strip())
    images = product.get("images")
    if isinstance(images, list):
        for item in images:
            if isinstance(item, str) and item.strip():
                refs.append(item.strip())
            elif isinstance(item, dict):
                src = item.get("src") or item.get("url")
                if isinstance(src, str) and src.strip():
                    refs.append(src.strip())
    deduped: List[str] = []
    for ref in refs:
        if ref not in deduped:
            deduped.append(ref)
    return deduped


def _normalize_variant_attributes(variant: Dict[str, Any]) -> Dict[str, Any]:
    attrs: Dict[str, Any] = {}
    for key in ("option1", "option2", "option3", "title", "sku"):
        value = variant.get(key)
        if value not in (None, ""):
            attrs[key] = value
    selected = variant.get("selected_options") or variant.get("selectedOptions")
    if isinstance(selected, list):
        normalized_selected: List[Dict[str, Any]] = []
        for item in selected:
            if isinstance(item, dict):
                normalized_selected.append(
                    {"name": item.get("name"), "value": item.get("value")}
                )
        if normalized_selected:
            attrs["selected_options"] = normalized_selected
    return attrs


def _capability_flags_from_product(product: Dict[str, Any]) -> List[str]:
    flags = ["catalog_search"]
    source = str(product.get("source") or "").strip().lower()
    if source != "external_seed":
        flags.extend(["quote_preview", "hosted_checkout", "order_create"])
    return flags


def _shipping_summary_from_product(product: Dict[str, Any]) -> Dict[str, Any]:
    shipping = product.get("shipping")
    if isinstance(shipping, dict):
        return shipping
    summary: Dict[str, Any] = {}
    if product.get("estimated_delivery"):
        summary["estimated_delivery"] = product.get("estimated_delivery")
    if product.get("shipping_fee") is not None:
        summary["shipping_fee"] = product.get("shipping_fee")
    return summary


def _canonicalize_search_product(product: Dict[str, Any]) -> Dict[str, Any]:
    merchant_id = str(product.get("merchant_id") or "").strip()
    product_id = str(product.get("product_id") or product.get("id") or "").strip()
    variants = product.get("variants")
    normalized_variants: List[Dict[str, Any]] = []
    if isinstance(variants, list) and variants:
        for raw_variant in variants:
            if not isinstance(raw_variant, dict):
                continue
            variant_id = str(
                raw_variant.get("variant_id")
                or raw_variant.get("id")
                or raw_variant.get("sku")
                or product_id
            ).strip()
            if not variant_id:
                continue
            normalized_variants.append(
                {
                    "variant_id": variant_id,
                    "variant_attributes": _normalize_variant_attributes(raw_variant),
                }
            )
    else:
        normalized_variants.append(
            {
                "variant_id": str(product.get("variant_id") or product_id or "variant_default"),
                "variant_attributes": {},
            }
        )

    offers: List[Dict[str, Any]] = []
    for variant in normalized_variants:
        variant_id = variant["variant_id"]
        offer_id = str(
            product.get("offer_id") or f"offer::{merchant_id or 'merchant_unknown'}::{variant_id}"
        )
        offers.append(
            {
                "offer_id": offer_id,
                "merchant_id": merchant_id,
                "variant_id": variant_id,
                "merchant_sku": product.get("sku"),
                "price": _money_str(product.get("price")),
                "currency": product.get("currency") or "USD",
                "availability": {
                    "in_stock": bool(product.get("in_stock", True)),
                    "inventory_quantity": product.get("inventory_quantity"),
                },
                "shipping_summary": _shipping_summary_from_product(product),
                "source_type": product.get("source") or "catalog_cache",
                "connector": product.get("platform"),
                "freshness_ts": _utc_iso(product.get("cached_at") or product.get("updated_at")),
                "confidence": product.get("score") or product.get("confidence") or 1.0,
                "capability_flags": _capability_flags_from_product(product),
                "payment_offer_evidence": product.get("payment_offer_evidence") or {},
                "payment_offer_summary": product.get("payment_offer_summary") or {},
                "payment_offer_badges": product.get("payment_offer_badges") or [],
                "savings_presentation": product.get("savings_presentation") or {},
            }
        )

    return {
        "product_id": product_id,
        "canonical_title": product.get("title") or product.get("name"),
        "canonical_category": product.get("category") or product.get("product_type"),
        "brand": product.get("brand") or product.get("vendor"),
        "normalized_attributes": _decode_json_like(product.get("attributes")),
        "image_refs": _normalize_image_refs(product),
        "dedupe_group_id": str(product.get("dedupe_group_id") or product_id or merchant_id),
        "recommendation_reason": product.get("recommendation_reason")
        or product.get("agent_reason")
        or "merchant-network search candidate",
        "ranking_features_summary": product.get("ranking_features_summary")
        or {"score": product.get("score")},
        "variants": normalized_variants,
        "offers": offers,
        "payment_offer_evidence": product.get("payment_offer_evidence") or {},
        "payment_offer_summary": product.get("payment_offer_summary") or {},
        "payment_offer_badges": product.get("payment_offer_badges") or [],
        "savings_presentation": product.get("savings_presentation") or {},
        "provenance": {
            "merchant_id": merchant_id,
            "merchant_name": product.get("merchant_name"),
            "connector": product.get("platform"),
            "source_type": product.get("source") or "catalog_cache",
            "freshness_ts": _utc_iso(product.get("cached_at") or product.get("updated_at")),
        },
    }


def _quote_offer_refs_from_row(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    request_json = _decode_json_like(row.get("request_json"))
    snapshot_json = _decode_json_like(row.get("snapshot_json"))
    request_items = request_json.get("items") or []
    line_items = snapshot_json.get("line_items") or []
    results: List[Dict[str, Any]] = []
    for index, item in enumerate(request_items):
        if not isinstance(item, dict):
            continue
        product_id = str(item.get("product_id") or "").strip()
        variant_id = str(item.get("variant_id") or "").strip()
        quantity = int(item.get("quantity") or 0)
        if not product_id or not variant_id or quantity <= 0:
            continue
        line_item = line_items[index] if index < len(line_items) and isinstance(line_items[index], dict) else {}
        results.append(
            {
                "offer_id": f"offer::{row.get('merchant_id')}::{variant_id}",
                "product_id": product_id,
                "variant_id": variant_id,
                "quantity": quantity,
                "unit_price_original": _money_str(line_item.get("unit_price_original")),
                "unit_price_effective": _money_str(line_item.get("unit_price_effective")),
                "line_discount_total": _money_str(line_item.get("line_discount_total")),
            }
        )
    return results


def _quote_response_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    snapshot_json = _decode_json_like(row.get("snapshot_json"))
    pricing = _decode_json_like(snapshot_json.get("pricing"))
    delivery_options = snapshot_json.get("delivery_options")
    policy_snapshot = {
        "quote_hash_sha256": row.get("quote_hash_sha256"),
        "engine": row.get("engine"),
        "engine_ref": row.get("engine_ref"),
        "status": row.get("status"),
    }
    return {
        "quote_id": row.get("quote_id"),
        "merchant_id": row.get("merchant_id"),
        "state": _quote_state_from_status(str(row.get("status") or "")),
        "offer_refs": _quote_offer_refs_from_row(row),
        "price_breakdown": {
            "subtotal": _money_str(pricing.get("subtotal")),
            "discount_total": _money_str(pricing.get("discount_total")),
            "total": _money_str(pricing.get("total")),
            "currency": snapshot_json.get("currency") or "USD",
        },
        "shipping_breakdown": {
            "shipping_fee": _money_str(pricing.get("shipping_fee")),
            "delivery_options": delivery_options or [],
        },
        "tax_breakdown": {
            "tax": _money_str(pricing.get("tax")),
        },
        "expires_at": _utc_iso(row.get("expires_at")),
        "merchant_terms_ref": snapshot_json.get("merchant_terms_ref"),
        "policy_snapshot": policy_snapshot,
        "currency": snapshot_json.get("currency") or "USD",
        "presentment_currency": snapshot_json.get("presentment_currency")
        or snapshot_json.get("currency")
        or "USD",
        "charge_currency": snapshot_json.get("charge_currency")
        or snapshot_json.get("currency")
        or "USD",
        "settlement_currency": snapshot_json.get("settlement_currency"),
        "line_items": snapshot_json.get("line_items") or [],
        "promotion_lines": snapshot_json.get("promotion_lines") or [],
        "discount_evidence": snapshot_json.get("discount_evidence") or {},
        "payment_offer_evidence": snapshot_json.get("payment_offer_evidence") or {},
        "payment_pricing": snapshot_json.get("payment_pricing") or {},
        "savings_presentation": snapshot_json.get("savings_presentation") or {},
        "provenance": {
            "engine": row.get("engine"),
            "engine_ref": row.get("engine_ref"),
            "merchant_id": row.get("merchant_id"),
        },
    }


def _coerce_shipping_address(raw_address: Any) -> ShippingAddress:
    if not isinstance(raw_address, dict):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "INVALID_BUYER_CONTEXT",
                "message": "buyer_context.shipping_address is required for order creation",
            },
        )
    required_fields = {
        "name": raw_address.get("name"),
        "address_line1": raw_address.get("address_line1"),
        "city": raw_address.get("city"),
        "postal_code": raw_address.get("postal_code") or raw_address.get("zip"),
        "country": raw_address.get("country"),
    }
    missing = [key for key, value in required_fields.items() if not str(value or "").strip()]
    if missing:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "INVALID_BUYER_CONTEXT",
                "message": "shipping address is missing required fields",
                "missing_fields": missing,
            },
        )
    return ShippingAddress(
        name=str(required_fields["name"]).strip(),
        address_line1=str(required_fields["address_line1"]).strip(),
        address_line2=(str(raw_address.get("address_line2") or "").strip() or None),
        city=str(required_fields["city"]).strip(),
        state=(str(raw_address.get("state") or raw_address.get("province") or "").strip() or None),
        postal_code=str(required_fields["postal_code"]).strip(),
        country=str(required_fields["country"]).strip(),
        phone=(str(raw_address.get("phone") or "").strip() or None),
    )


def _order_items_from_quote_request(request_json: Dict[str, Any]) -> List[OrderItem]:
    items: List[OrderItem] = []
    for raw_item in request_json.get("items") or []:
        if not isinstance(raw_item, dict):
            continue
        product_id = str(raw_item.get("product_id") or "").strip()
        variant_id = str(raw_item.get("variant_id") or "").strip()
        quantity = int(raw_item.get("quantity") or 0)
        if not product_id or quantity <= 0:
            continue
        items.append(
            OrderItem(
                product_id=product_id,
                variant_id=variant_id or None,
                quantity=quantity,
            )
        )
    return items


def _payment_summary_from_create_response(payment: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(payment, dict):
        return None
    return {
        "psp": payment.get("psp"),
        "client_secret": payment.get("client_secret"),
        "payment_intent_id": payment.get("payment_intent_id"),
        "payment_action": payment.get("payment_action"),
        "instructions": payment.get("instructions"),
    }


def _order_response_from_row(
    order: Dict[str, Any],
    *,
    payment: Optional[Dict[str, Any]] = None,
    fallback_quote_id: Optional[str] = None,
) -> Dict[str, Any]:
    shipping_address = order.get("shipping_address") if isinstance(order.get("shipping_address"), dict) else {}
    metadata = _decode_json_like(order.get("metadata"))
    amounts = _order_amounts_from_row(order)
    return {
        "order_id": order.get("order_id"),
        "quote_id": _quote_id_from_order(order) or fallback_quote_id,
        "merchant_id": order.get("merchant_id"),
        "state": _order_state_from_row(order),
        "line_items": order.get("items") or [],
        "buyer_context": {
            "customer_email": order.get("customer_email"),
            "customer_name": order.get("customer_name"),
            "shipping_address": shipping_address,
            "buyer_ref": metadata.get("buyer_ref"),
            "agent_user_ref": metadata.get("agent_user_ref"),
        },
        "payment_status": str(order.get("payment_status") or ""),
        "payment_summary": payment or {
            "psp": order.get("psp_used"),
            "payment_intent_id": order.get("payment_intent_id"),
            "client_secret": order.get("client_secret"),
        },
        "fulfillment_summary": {
            "fulfillment_status": order.get("fulfillment_status"),
            "tracking_number": order.get("tracking_number"),
            "carrier": order.get("carrier"),
            "shipped_at": _utc_iso(order.get("shipped_at")),
            "delivered_at": _utc_iso(order.get("delivered_at")),
        },
        "amounts": {
            "subtotal": amounts["subtotal"],
            "discount_total": amounts["discount_total"],
            "shipping_fee": amounts["shipping_fee"],
            "tax": amounts["tax"],
            "total": amounts["total"],
            "currency": amounts["currency"],
        },
        "refund_summary": build_order_refund_tracking_payload(
            order,
            psp_used=order.get("psp_used"),
        ),
        "audit": {
            "agent_id": order.get("agent_id"),
            "created_at": _utc_iso(order.get("created_at")),
            "updated_at": _utc_iso(order.get("updated_at")),
        },
    }


def _merchant_capability_state(onboarding_status: str, last_checked_at: Any) -> Dict[str, str]:
    normalized_status = str(onboarding_status or "").strip().lower()
    if normalized_status == "deleted":
        return {"verification_state": "retired", "degradation_state": "retired"}
    if normalized_status in {"rejected", "suspended"}:
        return {"verification_state": "suspended", "degradation_state": "suspended"}
    if last_checked_at:
        return {"verification_state": "verified", "degradation_state": "active"}
    return {"verification_state": "declared", "degradation_state": "degraded"}


def _freshness_score(last_checked_at: Any) -> int:
    if not isinstance(last_checked_at, datetime):
        return 0
    dt = last_checked_at if last_checked_at.tzinfo else last_checked_at.replace(tzinfo=timezone.utc)
    age_hours = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0)
    if age_hours <= 24:
        return 100
    if age_hours <= 72:
        return 80
    if age_hours <= 168:
        return 60
    return 30


def _health_score(row: Dict[str, Any], scopes_json: Dict[str, Any]) -> int:
    score = 100
    if not row.get("psp_connected"):
        score -= 30
    if not row.get("mcp_connected"):
        score -= 20
    if scopes_json.get("missing_required_scopes"):
        score -= 25
    if not row.get("last_checked_at"):
        score -= 15
    return max(0, min(100, score))


@router.post("/products/search")
async def search_products_v2(
    req: Request,
    body: SearchProductsRequest,
    background_tasks: BackgroundTasks,
    context: AgentContext = Depends(get_agent_context),
):
    search_all_merchants = body.search_all_merchants or (
        not body.merchant_id and not body.merchant_ids
    )
    result = await agent_v1_search_products(
        req=req,
        background_tasks=background_tasks,
        merchant_id=body.merchant_id,
        merchant_ids=body.merchant_ids,
        search_all_merchants=search_all_merchants,
        query=body.query,
        category=body.category,
        catalog_surface=body.catalog_surface,
        min_price=body.min_price,
        max_price=body.max_price,
        in_stock_only=body.in_stock_only,
        limit=body.limit,
        offset=body.offset,
        allow_external_seed=body.allow_external_seed,
        external_seed_only=body.external_seed_only,
        allow_stale_cache=body.allow_stale_cache,
        external_seed_strategy=body.external_seed_strategy,
        fast_mode=body.fast_mode,
        market=body.request_context.country if body.request_context and body.request_context.country else None,
        psp=body.payment_context.psp if body.payment_context else None,
        payment_method_type=body.payment_context.payment_method_type if body.payment_context else None,
        card_network=body.payment_context.card_network if body.payment_context else None,
        issuer_name=body.payment_context.issuer_name if body.payment_context else None,
        wallet_type=body.payment_context.wallet_type if body.payment_context else None,
        installment_provider=body.payment_context.installment_provider if body.payment_context else None,
        context=context,
    )
    products = [
        _canonicalize_search_product(product)
        for product in (result.get("products") or [])
        if isinstance(product, dict)
    ]
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    metadata = dict(metadata or {})
    decision_id = str(uuid.uuid4())
    metadata["decision_id"] = decision_id
    metadata["decision_layer"] = {
        "decision_id": decision_id,
        "correlation_source": "agent_v2.products.search",
    }
    try:
        from services.agent_decision_event_store import (
            record_decision_candidates,
            record_decision_event,
            record_exposure_events,
        )
        from services.protocols import derive_protocol_for_surface

        raw_products = [p for p in (result.get("products") or []) if isinstance(p, dict)]
        rows = []
        for idx, product in enumerate(raw_products):
            rows.append(
                {
                    "content_key": product.get("content_key") or product.get("product_key"),
                    "catalog_offer_id": product.get("catalog_offer_id") or product.get("offer_id"),
                    "position": idx,
                    "eligibility_flags": {
                        "merchant_id": product.get("merchant_id"),
                        "in_stock": product.get("in_stock"),
                        "source": product.get("source"),
                        "ranking_score": product.get("ranking_score") or product.get("score"),
                    },
                    "slot": "search_result",
                }
            )
        async def _record_search_decision() -> None:
            try:
                await record_decision_event(
                    decision_id=decision_id,
                    merchant_id=body.merchant_id,
                    surface="agent_v2.products.search",
                    channel=body.request_context.channel if body.request_context else None,
                    # Phase 0: derive the agentic-commerce protocol from the
                    # request channel instead of hardcoding pdp_direct —
                    # conservative mapper, unknown channels stay default.
                    protocol=derive_protocol_for_surface(
                        body.request_context.channel if body.request_context else None
                    ),
                    agent_context={
                        "agent_id": getattr(context, "agent_id", None),
                        "session_id": getattr(context, "session_id", None),
                        "query": body.query,
                        "category": body.category,
                        "merchant_ids": body.merchant_ids,
                        "search_all_merchants": search_all_merchants,
                        "limit": body.limit,
                        "offset": body.offset,
                        "request_context": body.request_context.model_dump(exclude_none=True)
                        if body.request_context
                        else None,
                    },
                )
                await record_decision_candidates(decision_id, rows)
                await record_exposure_events(decision_id, rows)
            except Exception:
                logger.debug("agent_v2 decision event enqueue failed", exc_info=True)

        asyncio.create_task(_record_search_decision())
    except Exception:
        logger.debug("agent_v2 decision event scheduling failed", exc_info=True)
    return {
        "status": str(result.get("status") or "success"),
        "products": products,
        "pagination": result.get("pagination") or {},
        "metadata": metadata,
        "request_context": body.request_context.model_dump(exclude_none=True)
        if body.request_context
        else None,
    }


@router.post("/quotes/preview")
async def preview_quote_v2(
    body: QuotePreviewBody,
    context: AgentContext = Depends(get_agent_context),
):
    request_context = body.request_context.model_dump(exclude_none=True) if body.request_context else {}
    buyer_context = body.buyer_context or BuyerContext()
    v1_req = QuotePreviewRequest(
        merchant_id=body.merchant_id,
        items=[
            {
                "product_id": ref.product_id,
                "variant_id": ref.variant_id,
                "quantity": ref.quantity,
            }
            for ref in body.offer_refs
        ],
        discount_codes=body.discount_codes,
        customer_email=buyer_context.customer_email,
        shipping_address=buyer_context.shipping_address,
        selected_delivery_option=body.selected_delivery_option,
        payment_context=body.payment_context,
        brief_id=request_context.get("request_id"),
        brief_schema_version="agent_v2",
    )
    result = await agent_v1_preview_quote(req=v1_req, context=context)
    row = await get_quote(str(result.get("quote_id")))
    quote = _quote_response_from_row(row) if row else {
        "quote_id": result.get("quote_id"),
        "merchant_id": body.merchant_id,
        "state": "previewed",
        "offer_refs": [ref.model_dump() for ref in body.offer_refs],
        "price_breakdown": {
            "subtotal": _money_str((result.get("pricing") or {}).get("subtotal")),
            "discount_total": _money_str((result.get("pricing") or {}).get("discount_total")),
            "total": _money_str((result.get("pricing") or {}).get("total")),
            "currency": result.get("currency") or "USD",
        },
        "shipping_breakdown": {
            "shipping_fee": _money_str((result.get("pricing") or {}).get("shipping_fee")),
            "delivery_options": result.get("delivery_options") or [],
        },
        "tax_breakdown": {"tax": _money_str((result.get("pricing") or {}).get("tax"))},
        "expires_at": _utc_iso(result.get("expires_at")),
        "merchant_terms_ref": None,
        "policy_snapshot": {},
        "currency": result.get("currency") or "USD",
        "presentment_currency": result.get("presentment_currency") or result.get("currency") or "USD",
        "charge_currency": result.get("charge_currency") or result.get("currency") or "USD",
        "settlement_currency": result.get("settlement_currency"),
        "line_items": result.get("line_items") or [],
        "promotion_lines": result.get("promotion_lines") or [],
        "discount_evidence": result.get("discount_evidence") or {},
        "payment_offer_evidence": result.get("payment_offer_evidence") or {},
        "payment_pricing": result.get("payment_pricing") or {},
        "savings_presentation": result.get("savings_presentation") or {},
        "provenance": {
            "engine": result.get("engine"),
            "engine_ref": result.get("engine_ref"),
            "merchant_id": body.merchant_id,
        },
    }
    return {
        "status": "success",
        "quote": quote,
        "events": [
            _event(
                "quote.created",
                tenant_id=request_context.get("tenant_id"),
                merchant_id=body.merchant_id,
                quote_id=quote.get("quote_id"),
            )
        ],
    }


@router.get("/quotes/{quote_id}")
async def get_quote_v2(
    quote_id: str,
    context: AgentContext = Depends(get_agent_context),
):
    await expire_quote_if_needed(quote_id)
    row = await get_quote(quote_id)
    if not row:
        raise HTTPException(status_code=404, detail="Quote not found")
    if not context.can_access_merchant(str(row.get("merchant_id") or "")):
        raise HTTPException(status_code=403, detail="Not authorized for this merchant")
    quote = _quote_response_from_row(row)
    event_type = "quote.expired" if quote.get("state") == "expired" else "quote.created"
    return {
        "status": "success",
        "quote": quote,
        "events": [
            _event(
                event_type,
                merchant_id=row.get("merchant_id"),
                quote_id=quote_id,
            )
        ],
    }


@router.post("/orders")
async def create_order_v2(
    body: CreateOrderBody,
    background_tasks: BackgroundTasks,
    context: AgentContext = Depends(get_agent_context),
    agent_user: Optional[AgentUserContext] = Depends(get_agent_user_context),
):
    service = QuoteService()
    try:
        quote = await service.load_active_quote_or_raise(quote_id=body.quote_id)
    except QuoteError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": exc.code, "message": exc.message, "details": exc.details},
        )

    request_json = quote.request_json if isinstance(quote.request_json, dict) else {}
    items = _order_items_from_quote_request(request_json)
    if not items:
        raise HTTPException(
            status_code=400,
            detail={"error": "INVALID_QUOTE", "message": "quote snapshot does not contain orderable items"},
        )

    customer_email = str(body.buyer_context.customer_email or request_json.get("customer_email") or "").strip()
    if not customer_email:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "INVALID_BUYER_CONTEXT",
                "message": "buyer_context.customer_email is required for order creation",
            },
        )

    raw_shipping = body.buyer_context.shipping_address or request_json.get("shipping_address")
    shipping_address = _coerce_shipping_address(raw_shipping)

    metadata = dict(body.metadata or {})
    agent_v2_meta = dict(metadata.get("agent_v2") or {}) if isinstance(metadata.get("agent_v2"), dict) else {}
    agent_v2_meta.update({
        "contract_version": "merchant-network-middleware-v1",
        "quote_id": body.quote_id,
    })
    metadata["agent_v2"] = agent_v2_meta
    if body.request_context:
        metadata["request_context"] = body.request_context.model_dump(exclude_none=True)
    if body.selected_payment_offer_id:
        metadata["selected_payment_offer_id"] = body.selected_payment_offer_id
    if isinstance(body.payment_method_evidence, dict):
        metadata["payment_method_evidence"] = body.payment_method_evidence
    metadata = attach_traffic_taxonomy(
        metadata,
        build_traffic_taxonomy(
            metadata,
            authenticated_agent_id=context.agent_id,
            caller_id=context.agent_id,
            default_source_channel=(
                body.request_context.channel
                if body.request_context and body.request_context.channel
                else None
            ),
            default_query_source=str(metadata.get("query_source") or "").strip() or None,
            default_protocol_name="rest",
            default_commerce_surface=str(metadata.get("commerce_surface") or "agent_api").strip() or "agent_api",
        ),
    )

    order_request = CreateOrderRequest(
        merchant_id=quote.merchant_id,
        customer_email=customer_email,
        customer_name=body.buyer_context.customer_name,
        quote_id=body.quote_id,
        items=items,
        shipping_address=shipping_address,
        currency=str(
            body.request_context.currency
            if body.request_context and body.request_context.currency
            else (quote.snapshot_json or {}).get("currency")
            or "USD"
        ),
        discount_codes=request_json.get("discount_codes") or [],
        selected_delivery_option=request_json.get("selected_delivery_option"),
        agent_session_id=(body.request_context.request_id if body.request_context else None),
        metadata=metadata,
        preferred_psp=body.preferred_psp,
        selected_payment_offer_id=body.selected_payment_offer_id,
        payment_method_evidence=body.payment_method_evidence,
        idempotency_key=body.idempotency_key,
    )

    response = await agent_v1_create_order(
        order_request=order_request,
        background_tasks=background_tasks,
        context=context,
        agent_user=agent_user,
        x_buyer_ref=body.buyer_context.buyer_ref,
    )
    order_id = str((response or {}).get("order_id") or "").strip()
    if not order_id:
        raise HTTPException(status_code=502, detail="Order creation did not return an order_id")
    order_row = await get_order(order_id)
    if not order_row:
        raise HTTPException(status_code=502, detail="Created order could not be reloaded")

    payment_summary = _payment_summary_from_create_response((response or {}).get("payment"))
    canonical_order = _order_response_from_row(
        order_row,
        payment=payment_summary,
        fallback_quote_id=body.quote_id,
    )
    return {
        "status": "success",
        "order": canonical_order,
        "payment": payment_summary,
        "events": [
            _event(
                "order.created",
                tenant_id=(body.request_context.tenant_id if body.request_context else None),
                merchant_id=order_row.get("merchant_id"),
                order_id=order_id,
                quote_id=body.quote_id,
            )
        ],
    }


@router.get("/orders/{order_id}")
async def get_order_v2(
    order_id: str,
    context: AgentContext = Depends(get_agent_context),
):
    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if str(order.get("agent_id") or "") != str(context.agent_id):
        raise HTTPException(status_code=403, detail="Not authorized for this order")
    return {
        "status": "success",
        "order": _order_response_from_row(order),
    }


@router.post("/payments/checkout-sessions")
async def create_checkout_session_v2(
    body: CreateCheckoutSessionBody,
    context: AgentContext = Depends(get_agent_context),
    agent_user: Optional[AgentUserContext] = Depends(get_agent_user_context),
):
    from services.agent_governance import agent_governance

    order = await get_order(body.order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if str(order.get("agent_id") or "") != str(context.agent_id):
        raise HTTPException(status_code=403, detail="Not authorized for this order")

    await validate_request_compat(agent_governance, context.agent_id, fail_closed=True)

    order_state = _order_state_from_row(order)
    if order_state not in {"awaiting_checkout", "draft"}:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "CHECKOUT_SESSION_NOT_ALLOWED",
                "message": "checkout sessions can only be created for orders awaiting checkout",
                "order_state": order_state,
            },
        )

    raw_items = order.get("items") or []
    items: List[CheckoutIntentItem] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        items.append(
            CheckoutIntentItem(
                product_id=str(raw_item.get("product_id") or "").strip() or None,
                variant_id=str(raw_item.get("variant_id") or "").strip() or None,
                sku=raw_item.get("sku"),
                merchant_id=str(order.get("merchant_id") or ""),
                title=raw_item.get("product_title") or raw_item.get("title"),
                quantity=int(raw_item.get("quantity") or 1),
                unit_price=float(raw_item.get("unit_price") or 0) if raw_item.get("unit_price") is not None else None,
                currency=order.get("currency"),
            )
        )

    intent_request = CreateCheckoutIntentRequest(
        items=items,
        return_url=body.return_url,
        buyer_ref=body.buyer_ref,
        agent_user_ref=agent_user.agent_user_ref if agent_user else None,
        requested_scopes=body.requested_scopes,
        market=body.market or (body.request_context.country if body.request_context else None),
        locale=body.locale or (body.request_context.locale if body.request_context else None),
        source=body.source or (body.request_context.channel if body.request_context else None),
        customer_email=body.customer_email or order.get("customer_email"),
        shipping_address=body.shipping_address or order.get("shipping_address"),
        order_id=body.order_id,
    )
    response = await create_checkout_intent_route(
        req=intent_request,
        context=context,
        agent_user=agent_user,
    )
    checkout_session_id = (
        response.get("checkout_session_id")
        or response.get("intent_id")
        or response.get("checkout_token")
    )
    return {
        "status": "success",
        "checkout_session": {
            "checkout_session_id": checkout_session_id,
            "order_id": body.order_id,
            "state": "created",
            "hosted_url": response.get("checkout_url"),
            "expires_at": response.get("expires_at"),
            "provider": "pivota_hosted_checkout",
            "checkout_token": response.get("checkout_token"),
        },
        "events": [
            _event(
                "checkout.session.created",
                tenant_id=(body.request_context.tenant_id if body.request_context else None),
                merchant_id=order.get("merchant_id"),
                order_id=body.order_id,
                checkout_session_id=checkout_session_id,
            )
        ],
    }


@router.get("/merchants/capabilities")
async def list_merchant_capabilities_v2(
    merchant_id: Optional[str] = Query(default=None),
    context: AgentContext = Depends(get_agent_context),
):
    if merchant_id and not context.can_access_merchant(merchant_id):
        raise HTTPException(status_code=403, detail="Not authorized for this merchant")

    rows = await database.fetch_all(
        """
        SELECT
          mo.merchant_id,
          mo.business_name,
          mo.status,
          mo.mcp_connected,
          mo.mcp_platform,
          mo.psp_connected,
          mo.psp_type,
          pmc.shopify_api_version,
          pmc.scopes_json,
          pmc.has_shopify_payments,
          pmc.has_returns_api,
          pmc.last_checked_at
        FROM merchant_onboarding mo
        LEFT JOIN pcs_merchant_capabilities pmc
          ON pmc.merchant_id = mo.merchant_id
        WHERE (CAST(:merchant_id AS TEXT) IS NULL OR mo.merchant_id = CAST(:merchant_id AS TEXT))
          AND mo.status != 'deleted'
        ORDER BY mo.updated_at DESC NULLS LAST, mo.created_at DESC NULLS LAST
        """,
        {"merchant_id": merchant_id},
    )
    merchants: List[Dict[str, Any]] = []
    for raw_row in rows or []:
        row = dict(raw_row)
        if not context.can_access_merchant(str(row.get("merchant_id") or "")):
            continue
        scopes_json = _decode_json_like(row.get("scopes_json"))
        access_scopes = sorted(
            {
                str(scope).strip()
                for scope in (scopes_json.get("access_scopes") or [])
                if str(scope or "").strip()
            }
        )
        access_scope_set = {scope.lower() for scope in access_scopes}
        state = _merchant_capability_state(str(row.get("status") or ""), row.get("last_checked_at"))
        connector = row.get("mcp_platform") or "unknown"
        platform_capabilities = get_store_platform_capabilities(str(connector))
        psp_capabilities = get_psp_capabilities(str(row.get("psp_type") or "unknown"))
        connector_ready = bool(row.get("mcp_connected"))
        psp_ready = bool(row.get("psp_connected"))
        platform_checkout_ready = connector_ready and platform_capabilities.supports_platform_checkout
        pivota_direct_checkout_ready = connector_ready and psp_ready and platform_capabilities.supports_live_quote
        merchants.append(
            {
                "merchant_id": row.get("merchant_id"),
                "merchant_name": row.get("business_name"),
                "connector": connector,
                "capability_state": state["verification_state"],
                "verification_state": state["verification_state"],
                "degradation_state": state["degradation_state"],
                "supported_flows": {
                    "catalog_search": connector_ready,
                    "quote_refresh": connector_ready and platform_capabilities.supports_live_quote,
                    "hosted_checkout": platform_checkout_ready or pivota_direct_checkout_ready,
                    "external_platform_checkout": platform_checkout_ready,
                    "pivota_direct_checkout": pivota_direct_checkout_ready,
                    "tracking": True,
                    "refunds": bool(row.get("has_returns_api")),
                    "payment_refunds": psp_ready and psp_capabilities.supports_auto_refund,
                    "live_connector": connector_ready and platform_capabilities.supports_live_quote,
                    "ucp": connector_ready and platform_capabilities.supports_live_quote,
                },
                "commerce_capabilities": platform_capabilities.as_dict(),
                "psp_capabilities": psp_capabilities.as_dict(),
                "policy_flags": {
                    "missing_required_scopes": scopes_json.get("missing_required_scopes") or [],
                    "missing_optional_scopes": scopes_json.get("missing_optional_scopes") or [],
                    "access_scopes": access_scopes,
                    "has_read_discounts": "read_discounts" in access_scope_set,
                    "has_write_discounts": "write_discounts" in access_scope_set,
                    "has_read_customers": "read_customers" in access_scope_set,
                },
                "health_score": _health_score(row, scopes_json),
                "freshness_score": _freshness_score(row.get("last_checked_at")),
                "pcs_tier": await get_merchant_pcs_tier(merchant_id=str(row.get("merchant_id") or "")),
                "checked_at": _utc_iso(row.get("last_checked_at")),
                "psp_type": row.get("psp_type"),
                "shopify_api_version": row.get("shopify_api_version"),
                "has_shopify_payments": bool(row.get("has_shopify_payments")),
            }
        )
    return {
        "status": "success",
        "merchants": merchants,
    }


@router.get("/orders/{order_id}/tracking")
async def track_order_v2(
    order_id: str,
    context: AgentContext = Depends(get_agent_context),
):
    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if str(order.get("agent_id") or "") != str(context.agent_id):
        raise HTTPException(status_code=403, detail="Not authorized for this order")
    tracking_response = await agent_v1_track_order(order_id=order_id, context=context)
    amounts = _order_amounts_from_row(order)
    return {
        "status": "success",
        "tracking": {
            **(tracking_response.get("tracking") if isinstance(tracking_response, dict) else {}),
            "payment_status": str(order.get("payment_status") or ""),
            "fulfillment_status": str(order.get("fulfillment_status") or ""),
            "currency": amounts["currency"],
            "pricing": amounts,
            "subtotal": amounts["subtotal"],
            "discount_total": amounts["discount_total"],
            "shipping_fee": amounts["shipping_fee"],
            "tax": amounts["tax"],
            "total": amounts["total"],
        },
        "capability": "optional",
    }


@router.post("/orders/{order_id}/refunds")
async def create_refund_v2(
    order_id: str,
    body: RefundCreateBody,
    background_tasks: BackgroundTasks,
    context: AgentContext = Depends(get_agent_context),
):
    from services.agent_governance import agent_governance

    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if str(order.get("agent_id") or "") != str(context.agent_id):
        raise HTTPException(status_code=403, detail="Not authorized for this order")
    await validate_request_compat(agent_governance, context.agent_id, fail_closed=True)
    response = await process_refund_route(
        order_id=order_id,
        refund_request=RefundRequest(
            order_id=order_id,
            amount=body.amount,
            reason=body.reason,
            restore_inventory=body.restore_inventory,
            idempotency_key=body.idempotency_key,
        ),
        background_tasks=background_tasks,
        current_user={"role": "agent", "agent_id": context.agent_id},
    )
    return {
        "status": "success",
        "refund": {
            "refund_id": response.get("refund_id"),
            "order_id": response.get("order_id"),
            "refund_amount": response.get("refund_amount"),
            "currency": order.get("currency"),
            "state": "requested",
            "is_partial": response.get("is_partial"),
            "remaining_refundable": response.get("remaining_refundable"),
        },
        "beta": True,
        "events": [
            _event(
                "refund.requested",
                merchant_id=order.get("merchant_id"),
                order_id=order_id,
                refund_id=response.get("refund_id"),
            )
        ],
    }
