"""`/agent/v2/storefront-cart-referrals`: the referral for a store cart the gateway's checkout door
builds (services/storefront_cart_referral.py).

DARK BY DEFAULT. While UCP_STOREFRONT_CART_REFERRAL_ENABLED is off every call, whatever its method,
its shape and whoever sends it, answers the 404 this service gives a path it does not have, byte for
byte, and nothing is read or written: the flag is checked by a router dependency that runs FIRST,
before authentication, before the body is read and before any partner assertion is looked at (so no
nonce is burned and no rate limit charged). A method other than POST is that same 404 with the flag
on too.

With the flag on, the order is: the caller's key (401), the body (400, before any assertion is
looked at), the caller must be one of our own services (409 `service_caller_required`: an agent's
own key cannot mint here), then the partner assertion (409 on a bad one, the partner's own 429s),
then the write.

One call, made by the GATEWAY with its own service key:

  POST /agent/v2/storefront-cart-referrals   mint one referral (op `ucp_storefront_cart.referral`)

WHO OWNS THE ROW. With PARTNER_IDENTITY_FORWARDING_ENABLED on and a REQUEST-BOUND (v2)
X-Pivota-Issuing-Agent from the gateway, the partner it names (adopted by
routes.agent_forwarded_owner exactly as on the hand-off record routes; a v2 token that fails any
check is 409 PARTNER_ASSERTION_REJECTED). With no partner adopted (forwarding off, no header, a v1
token, a caller that is nobody's partner) the row has NO agent: it is our evidence of the hand-off
and no partner's. The partner is never taken from the body.

The answer is `{click_id, surface, attribution}`; `attribution` holds only tracking members
(`utm_*`, `pvt_*`). The click id is not logged here.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

import routes.agent_auth as agent_auth
from routes.agent_auth import AgentContext, get_agent_context
from routes.agent_forwarded_owner import adopt_forwarded_owner
from routes.agent_storefront_handoffs import REFUSED_STATUS, _adopted_partner, _anchored, _json_body
from services import storefront_cart_referral as referrals
from services import storefront_handoff_record as handoffs

logger = logging.getLogger(__name__)


#: The detail of the framework's own 404 for an unknown path: dark is indistinguishable from it.
_NOT_FOUND = "Not Found"
_OTHER_METHODS = ["GET", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"]


def _dark_unless_enabled() -> None:
    """Runs before every other dependency of this route: dark is one 404 for every request."""
    if not referrals.storefront_cart_referral_enabled():
        raise HTTPException(status_code=404, detail=_NOT_FOUND)


router = APIRouter(
    prefix="/agent/v2/storefront-cart-referrals",
    tags=["agent-storefront-cart-referrals"],
    dependencies=[Depends(_dark_unless_enabled)],
    # Not in the published schema: with the flag off this service says nothing new anywhere.
    include_in_schema=False,
)


def _refuse(status: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code})


class ReferralBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seller_host: str = Field(max_length=253, pattern=_anchored(handoffs.SELLER_HOST_RE.pattern))
    market: Optional[str] = Field(default=None, pattern=_anchored(handoffs.MARKET_RE.pattern))
    merchant_id: Optional[str] = Field(default=None, max_length=50, pattern=_anchored(r"[A-Za-z0-9_.:-]+"))


@router.post("")
async def mint_storefront_cart_referral(
    request: Request,
    caller: AgentContext = Depends(get_agent_context),
    body: ReferralBody = Depends(_json_body(ReferralBody)),
):
    # Only our own services mint here. Checked before the assertion, so a refused caller burns no nonce.
    if not await agent_auth._is_service_caller(agent_auth.request_api_key(request)):
        return _refuse(REFUSED_STATUS, "service_caller_required")
    context = await adopt_forwarded_owner(request, caller, ops=(referrals.REFERRAL_OP,))
    partner = _adopted_partner(context)
    try:
        return await referrals.mint_referral(
            seller_host=body.seller_host,
            market=body.market,
            merchant_id=body.merchant_id,
            partner_agent_id=partner,
            via_agent_id=getattr(context, "forwarded_by_agent_id", None) if partner else None,
        )
    except Exception as exc:  # noqa: BLE001 -- the door builds its cart without a referral; say so
        logger.warning("storefront_cart_referral.mint_failed error_type=%s", type(exc).__name__)
        return _refuse(503, "referral_store_unavailable")


@router.api_route("", methods=_OTHER_METHODS)
async def _no_other_method():
    """Only POST exists here. Any other method is the unknown-path 404, never a 405 that would say
    the path is there."""
    raise HTTPException(status_code=404, detail=_NOT_FOUND)
