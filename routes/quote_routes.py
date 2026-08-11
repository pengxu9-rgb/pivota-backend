from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from models.quote import QuotePreviewRequest, QuotePreviewResponse
from routes.agent_auth import AgentContext, get_agent_context
from services.quote_service import QuoteError, QuoteService, parse_decimal_money
from mvp.constants import EVENT_OFFER_GENERATED, SURFACE_BACKEND
from mvp.events import emit_best_effort
from utils.database_readiness import (
    DatabaseUnavailableError,
    database_unavailable_http_exception,
    ensure_database_ready,
)
from utils.transient_errors import db_busy_http_exception, is_asyncpg_busy_error


router = APIRouter(prefix="/agent/v1/quotes", tags=["agent-quotes"])


@router.post("/preview", response_model=QuotePreviewResponse)
async def preview_quote(
    req: QuotePreviewRequest,
    context: AgentContext = Depends(get_agent_context),
):
    # Merchant authorization guard
    if not context.can_access_merchant(req.merchant_id):
        raise HTTPException(status_code=403, detail="Not authorized for this merchant")

    service = QuoteService()
    try:
        await ensure_database_ready()
        result = await service.preview_quote(
            merchant_id=req.merchant_id,
            agent_id=context.agent_id,
            items=[it.model_dump() for it in req.items],
            discount_codes=req.discount_codes,
            customer_email=req.customer_email,
            shipping_address=req.shipping_address,
            selected_delivery_option=req.selected_delivery_option,
            payment_context=req.payment_context,
            brief_id=req.brief_id,
            brief_schema_version=req.brief_schema_version,
        )
    except QuoteError as e:
        raise HTTPException(
            status_code=503 if e.code == "SHOPIFY_PRICING_UNAVAILABLE" else 400,
            headers={"Retry-After": "1"} if e.code == "SHOPIFY_PRICING_UNAVAILABLE" else None,
            detail={
                "error": e.code,
                "message": e.message,
                "debug_id": e.debug_id,
                "details": getattr(e, "details", {}),
            },
        )
    except DatabaseUnavailableError:
        raise database_unavailable_http_exception()
    except Exception as e:
        if is_asyncpg_busy_error(e):
            raise db_busy_http_exception()
        raise

    pricing = result["pricing"]
    presentment_currency = result.get("presentment_currency") or result["currency"]
    charge_currency = result.get("charge_currency") or presentment_currency
    settlement_currency = result.get("settlement_currency")
    response = {
        "quote_id": result["quote_id"],
        "expires_at": result["expires_at"],
        "engine": result.get("engine") or "shopify_rest_checkout",
        "engine_ref": result["engine_ref"],
        "checkout_url": result.get("checkout_url"),
        "currency": result["currency"],
        "presentment_currency": presentment_currency,
        # Non-MoR: charge currency is currently the same as the platform quote currency.
        # When we introduce FX/PSP conversion, this will diverge.
        "charge_currency": charge_currency,
        # Settlement currency is determined by merchant/PSP contract and can be configured
        # via employee settlement rules; it may not be known at quote time.
        "settlement_currency": settlement_currency,
        "availability_status": result.get("availability_status") or "available_confirmed",
        "available_quantity": result.get("available_quantity"),
        "is_final": bool(result.get("is_final", True)),
        "source_updated_at": result.get("source_updated_at"),
        "warnings": result.get("warnings") or [],
        "pricing": {
            "subtotal": parse_decimal_money(pricing.get("subtotal")),
            "discount_total": parse_decimal_money(pricing.get("discount_total")),
            "shipping_fee": parse_decimal_money(pricing.get("shipping_fee")),
            "tax": parse_decimal_money(pricing.get("tax")),
            "total": parse_decimal_money(pricing.get("total")),
        },
        "promotion_lines": result.get("promotion_lines") or [],
        "discount_evidence": result.get("discount_evidence") or {},
        "payment_offer_evidence": result.get("payment_offer_evidence") or {},
        "payment_pricing": result.get("payment_pricing") or {},
        "savings_presentation": result.get("savings_presentation") or {},
        "line_items": result.get("line_items") or [],
        "delivery_options": result.get("delivery_options"),
        "debug_id": result.get("debug_id"),
        "attempts": (result.get("attempts") or None),
        "brief_id": req.brief_id,
        "brief_schema_version": req.brief_schema_version,
    }

    # MVP measurement scaffolding (best-effort): offer/quote generated.
    # Note: do not emit PII; keep only minimal geo fields + quote refs.
    geo = None
    try:
        addr = req.shipping_address or {}
        if isinstance(addr, dict):
            geo = {
                "country": (addr.get("country") or "").upper() or None,
                "postal_code": addr.get("postal_code") or addr.get("zip"),
                "city": addr.get("city"),
                "state": addr.get("state") or addr.get("province"),
            }
    except Exception:
        geo = None

    emit_best_effort(
        event_type=EVENT_OFFER_GENERATED,
        payload={
            "merchant_id": req.merchant_id,
            "quote_id": response["quote_id"],
            "expires_at": str(response["expires_at"]),
            "engine": response["engine"],
            "engine_ref": response["engine_ref"],
            "currency": response["currency"],
            "pricing": response["pricing"],
            "items_count": len(req.items or []),
            "delivery_options_count": len(response.get("delivery_options") or []),
            **({"brief_id": req.brief_id} if req.brief_id else {}),
            **({"brief_schema_version": req.brief_schema_version} if req.brief_schema_version else {}),
        },
        merchant_id=req.merchant_id,
        geo=geo,
        surface=SURFACE_BACKEND,
        adapter="quote_preview",
        risk_tier="unknown",
        idempotency_key=str(response["quote_id"]),
    )

    return response
