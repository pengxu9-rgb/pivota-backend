from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from routes.agent_auth import AgentContext, get_agent_context


router = APIRouter(prefix="/agent/v1/checkout", tags=["agent-checkout"])


def _base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _checkout_token_secret() -> str:
    return (os.getenv("CHECKOUT_TOKEN_SECRET") or os.getenv("AGENT_CHECKOUT_TOKEN_SECRET") or "").strip()


def mint_checkout_token(payload: Dict[str, Any], ttl_seconds: int = 60 * 60) -> str:
    secret = _checkout_token_secret()
    if not secret:
        raise HTTPException(status_code=500, detail="Checkout token secret is not configured")

    now = int(time.time())
    exp = now + int(ttl_seconds)
    body = {
        "v": 1,
        "iat": now,
        "exp": exp,
        **payload,
    }

    payload_b64 = _base64url_encode(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    sig = _base64url_encode(hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest())
    return f"v1.{payload_b64}.{sig}"


def _checkout_ui_base() -> str:
    return (os.getenv("CHECKOUT_UI_BASE_URL") or "https://agent.pivota.cc").rstrip("/")


class CheckoutIntentItem(BaseModel):
    product_id: str = Field(..., description="Platform product id")
    variant_id: Optional[str] = Field(None, description="Platform variant id (preferred when available)")
    sku: Optional[str] = None
    merchant_id: str = Field(..., description="Merchant id")
    title: Optional[str] = None
    quantity: int = Field(1, ge=1)
    unit_price: Optional[float] = None
    currency: Optional[str] = None
    image_url: Optional[str] = None


class CreateCheckoutIntentRequest(BaseModel):
    items: List[CheckoutIntentItem]
    return_url: Optional[str] = None
    buyer_ref: Optional[str] = None
    job_id: Optional[str] = None
    market: Optional[str] = None
    locale: Optional[str] = None
    source: Optional[str] = None


@router.post("/intents")
async def create_checkout_intent(
    req: CreateCheckoutIntentRequest,
    context: AgentContext = Depends(get_agent_context),
):
    if not req.items:
        raise HTTPException(status_code=400, detail={"error": "INVALID_REQUEST", "message": "items[] is required"})

    merchant_ids = sorted({str(it.merchant_id).strip() for it in req.items if str(it.merchant_id or "").strip()})
    if not merchant_ids:
        raise HTTPException(status_code=400, detail={"error": "INVALID_REQUEST", "message": "items[] must include merchant_id"})
    if len(merchant_ids) > 1:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "MULTI_MERCHANT_NOT_SUPPORTED",
                "message": "Create one checkout intent per merchant_id (split the cart by merchant).",
                "merchant_ids": merchant_ids,
            },
        )

    for mid in merchant_ids:
        if not context.can_access_merchant(mid):
            raise HTTPException(status_code=403, detail="Not authorized for this merchant")

    buyer_ref = (req.buyer_ref or "").strip() or None
    job_id = (req.job_id or "").strip() or None
    market = (req.market or "").strip().upper() or None
    locale = (req.locale or "").strip().lower() or None
    source = (req.source or "").strip().lower() or None

    token = mint_checkout_token(
        {
            "agent_id": context.agent_id,
            "buyer_ref": buyer_ref,
            "job_id": job_id,
            "market": market,
            "locale": locale,
            "merchant_ids": merchant_ids,
            "scopes": ["checkout"],
            # Bind items to the token (merchant-scoped enforcement is applied at auth;
            # item-level enforcement can be added later if needed).
            "items": [it.model_dump() for it in req.items],
        },
        ttl_seconds=60 * 60,
    )

    checkout_ui = _checkout_ui_base()
    # Keep items in query for backward compatibility (UI can still parse without decoding the token).
    items_param = json.dumps([it.model_dump() for it in req.items], ensure_ascii=False)
    query = {
        "checkout_token": token,
        "items": items_param,
    }
    if req.return_url:
        query["return"] = str(req.return_url)
    if market:
        query["market"] = market
    if locale:
        query["locale"] = locale
    if source:
        query["source"] = source
    if buyer_ref:
        query["buyer_ref"] = buyer_ref
    if job_id:
        query["job_id"] = job_id

    checkout_url = f"{checkout_ui}/order?{urlencode(query)}"

    return {
        "checkout_token": token,
        "checkout_url": checkout_url,
        "expires_at": int(time.time()) + 60 * 60,
    }
