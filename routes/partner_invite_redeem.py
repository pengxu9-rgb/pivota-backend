from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from db.merchant_onboarding import get_merchant_by_api_key
from services import partner_invite_token_service
from utils.auth import decode_token


router = APIRouter(tags=["Partner Invite Redeem"])
security = HTTPBearer(auto_error=False)


class RedeemInviteTokenRequest(BaseModel):
    token: str = Field(..., min_length=1)


async def _resolve_merchant_for_redeem(
    x_merchant_api_key: Optional[str] = Header(None, alias="X-Merchant-API-Key"),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict[str, Any]:
    """Resolve the calling merchant via X-Merchant-API-Key or Bearer JWT.

    Unlike `require_approved_merchant`, this does NOT gate on
    `merchant.status == 'approved'`. The redeem call happens during onboarding,
    before auto-KYC has flipped the merchant to approved and before the
    merchant has minted an API key, so we accept either credential and any
    status.
    """

    if x_merchant_api_key:
        merchant = await get_merchant_by_api_key(x_merchant_api_key)
        if merchant and merchant.get("merchant_id"):
            return {"merchant_id": str(merchant["merchant_id"])}

    if credentials and credentials.credentials:
        try:
            payload = decode_token(credentials.credentials)
        except HTTPException:
            payload = None
        except Exception:
            payload = None
        if payload:
            role = str(payload.get("role") or "")
            merchant_id = payload.get("merchant_id")
            if role == "merchant" and merchant_id:
                return {"merchant_id": str(merchant_id)}

    raise HTTPException(
        status_code=401,
        detail="Missing or invalid merchant credentials",
    )


@router.post(
    "/api/onboarding/redeem-invite-token",
    response_model=None,
)
async def redeem_invite_token(
    body: RedeemInviteTokenRequest,
    merchant: dict = Depends(_resolve_merchant_for_redeem),
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
