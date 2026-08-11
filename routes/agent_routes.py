"""
⚠️ DEPRECATED - 已弃用 (2025-11-06)

此模块包含旧版 Agent API 端点，已被 /agent/v1/* 取代。

迁移指南：
- /agent/pay → /agent/v1/orders/create
- /agent/pay-simple → /agent/v1/orders/create

Both endpoints answer 410 GONE with a pointer at the quote-first flow. The
fabrication belt that used to sit behind the 410s — an in-memory "AI PSP
selector" over invented metrics, a coin-flip `simulate_payment_processing`
(random latency + `random.random() < success_rate` verdicts), and a
platform-key `create_payment_intent` call (Pivota-as-MoR) — was unreachable
dead code and was deleted in the 2026-08-11 Tier-2 cleanup. The routes remain
mounted so old callers get an actionable 410, not a 404.
"""

import warnings

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from models.schemas import AgentPayRequest
from utils.logger import logger

router = APIRouter(
    prefix="/agent",
    tags=["agent-deprecated"],
    deprecated=True,
    responses={
        303: {
            "description": "Redirect to new API version",
            "headers": {
                "Location": {
                    "description": "New API endpoint location",
                    "schema": {"type": "string"}
                }
            }
        }
    }
)

_DEPRECATION_DETAIL = {
    "error": "QUOTE_REQUIRED_BEFORE_PURCHASE",
    "message": (
        "Deprecated direct payment endpoint is disabled. Use /agent/v1/quotes/preview "
        "then /agent/v1/orders/create with quote_id."
    ),
}


@router.post("/pay")
async def agent_pay(req: AgentPayRequest):
    """
    ⚠️ DEPRECATED: Use POST /agent/v1/orders/create instead.
    Always answers 410 GONE.
    """
    warnings.warn(
        "POST /agent/pay is deprecated. Use POST /agent/v1/orders/create instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    logger.warning("⚠️ DEPRECATED API CALL: POST /agent/pay - Use /agent/v1/orders/create")
    raise HTTPException(status_code=410, detail=_DEPRECATION_DETAIL)


class SimpleAgentPaymentRequest(BaseModel):
    agent_id: str
    order_id: str
    amount: float
    currency: str


@router.post("/pay-simple")
async def agent_pay_simple(req: SimpleAgentPaymentRequest):
    """
    ⚠️ DEPRECATED: Use POST /agent/v1/orders/create instead.
    Always answers 410 GONE.
    """
    warnings.warn(
        "POST /agent/pay-simple is deprecated. Use POST /agent/v1/orders/create instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    logger.warning("⚠️ DEPRECATED API CALL: POST /agent/pay-simple - Use /agent/v1/orders/create")
    raise HTTPException(status_code=410, detail=_DEPRECATION_DETAIL)
