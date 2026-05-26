from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from services import partner_invite_token_service
from utils.auth import require_admin


router = APIRouter(tags=["Admin - Partner Invite Tokens"])


class IssueInviteTokenRequest(BaseModel):
    expires_in_days: int = Field(90, ge=1)
    notes: str | None = None


class RevokeInviteTokenRequest(BaseModel):
    reason: str | None = None


@router.post(
    "/admin/partners/{channel_partner_id}/invite-tokens",
    status_code=201,
    response_model=None,
)
async def issue_invite_token(
    channel_partner_id: int,
    body: IssueInviteTokenRequest,
    current_admin: dict = Depends(require_admin),
) -> dict[str, Any] | JSONResponse:
    """Issue a new invite token. Returns the raw token exactly once."""

    try:
        result = await partner_invite_token_service.issue(
            channel_partner_id=channel_partner_id,
            issued_by=str(current_admin.get("email") or ""),
            expires_in_days=body.expires_in_days,
            notes=body.notes,
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=404,
            content={"error": "partner_not_found", "message": str(exc)},
        )
    return {
        "token_id": result.token_id,
        "raw_token": result.raw_token,
        "signup_url": result.signup_url,
        "expires_at": result.expires_at,
    }


@router.get("/admin/partners/{channel_partner_id}/invite-tokens")
async def list_invite_tokens(
    channel_partner_id: int,
    current_admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """List invite-token metadata for a partner audit view."""

    tokens = await partner_invite_token_service.list_for_partner(
        channel_partner_id=channel_partner_id,
    )
    return {"tokens": tokens}


@router.post(
    "/admin/partners/{channel_partner_id}/invite-tokens/{token_id}/revoke",
    response_model=None,
)
async def revoke_invite_token(
    channel_partner_id: int,
    token_id: int,
    body: RevokeInviteTokenRequest,
    current_admin: dict = Depends(require_admin),
) -> dict[str, Any] | JSONResponse:
    """Revoke an active invite token."""

    try:
        await partner_invite_token_service.revoke(
            token_id=token_id,
            channel_partner_id=channel_partner_id,
            revoked_by=str(current_admin.get("email") or ""),
            reason=body.reason,
        )
    except partner_invite_token_service.PartnerInviteTokenError as exc:
        return JSONResponse(
            status_code=409,
            content={
                "error": "invite_token_not_revokable",
                "message": str(exc),
            },
        )
    return {"token_id": token_id, "status": "revoked"}
