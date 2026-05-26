from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from routes.billing_routes import require_approved_merchant
from services import partner_invite_token_service


router = APIRouter(tags=["Partner Invite Redeem"])


class RedeemInviteTokenRequest(BaseModel):
    token: str = Field(..., min_length=1)


@router.post(
    "/api/onboarding/redeem-invite-token",
    response_model=None,
)
async def redeem_invite_token(
    body: RedeemInviteTokenRequest,
    merchant: dict = Depends(require_approved_merchant),
) -> dict[str, Any] | JSONResponse:
    """Attribute the authenticated merchant to the partner invite token."""

    merchant_id = str(merchant.get("merchant_id") or "")
    if not merchant_id:
        raise HTTPException(
            status_code=400,
            detail="Authenticated merchant is missing merchant_id",
        )
    try:
        attribution_id = await partner_invite_token_service.consume(
            raw_token=body.token,
            merchant_id=merchant_id,
        )
    except partner_invite_token_service.TokenInvalidError:
        return JSONResponse(
            status_code=404,
            content={"error": "invite_token_not_found"},
        )
    except partner_invite_token_service.TokenNotRedeemableError as exc:
        return JSONResponse(
            status_code=409,
            content={
                "error": "invite_token_not_redeemable",
                "message": str(exc),
            },
        )
    return {"attribution_id": attribution_id, "status": "attributed"}
