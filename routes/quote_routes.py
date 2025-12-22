from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from models.quote import QuotePreviewRequest, QuotePreviewResponse
from routes.agent_auth import AgentContext, get_agent_context
from services.quote_service import QuoteError, QuoteService, parse_decimal_money


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
        result = await service.preview_quote(
            merchant_id=req.merchant_id,
            agent_id=context.agent_id,
            items=[it.model_dump() for it in req.items],
            discount_codes=req.discount_codes,
            customer_email=req.customer_email,
            shipping_address=req.shipping_address,
            selected_delivery_option=req.selected_delivery_option,
        )
    except QuoteError as e:
        raise HTTPException(
            status_code=503 if e.code == "SHOPIFY_PRICING_UNAVAILABLE" else 400,
            detail={"error": e.code, "message": e.message, "debug_id": e.debug_id},
        )

    pricing = result["pricing"]
    return {
        "quote_id": result["quote_id"],
        "expires_at": result["expires_at"],
        "engine": "shopify_rest_checkout",
        "engine_ref": result["engine_ref"],
        "currency": result["currency"],
        "pricing": {
            "subtotal": parse_decimal_money(pricing.get("subtotal")),
            "discount_total": parse_decimal_money(pricing.get("discount_total")),
            "shipping_fee": parse_decimal_money(pricing.get("shipping_fee")),
            "tax": parse_decimal_money(pricing.get("tax")),
            "total": parse_decimal_money(pricing.get("total")),
        },
        "promotion_lines": result.get("promotion_lines") or [],
        "line_items": result.get("line_items") or [],
        "delivery_options": result.get("delivery_options"),
    }

