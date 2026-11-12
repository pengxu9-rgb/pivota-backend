"""`/agent/v2/storefront-cart-referrals`: the referral for a store cart the gateway's checkout door
builds (services/storefront_cart_referral.py).

DARK BY DEFAULT. While UCP_STOREFRONT_CART_REFERRAL_ENABLED is off every call, whatever its method,
its shape and whoever sends it, answers the 404 this service gives a path it does not have, byte for
byte, and nothing is read or written: the flag is checked by a router dependency that runs FIRST,
before authentication, before the body is read and before any partner assertion is looked at (so no
nonce is burned and no rate limit charged). A method other than POST is that same 404 with the flag
on too.

With the flag on, the order is: the caller's key (401); the caller must be one of our own services
(409 `service_caller_required`: an agent's own key cannot mint here, and is told nothing else,
whatever its body); the body (400, before any assertion is looked at, so a refused body burns no
nonce); then the partner assertion (409 on a bad one, the partner's own 429s); then the write.

THE MARKET IS REQUIRED and never defaulted (see services/storefront_cart_referral.py). A body with
no `market`, a null or a blank one is 400 `market_required`; one that is not two capital letters is
400 `market_invalid` (a lower-case code included: case is not folded); a well-formed one the market
registry (services/market_registry.py) does not list is 400 `market_unknown`. Checked in that order.
Listed is enough, whatever the market's rollout status: this route records a referral for a cart
the gateway already built for that market, it does not decide serving. Named, so the gateway's log
says why its cart carries no referral. Nothing is written and no assertion is looked at.

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
from fastapi.exceptions import RequestValidationError
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
    # REQUIRED, and never defaulted: it is stamped on the seller's cart link as `utm_campaign`.
    market: str = Field(pattern=_anchored(handoffs.MARKET_RE.pattern))
    merchant_id: Optional[str] = Field(default=None, max_length=50, pattern=_anchored(r"[A-Za-z0-9_.:-]+"))


#: The status of a body this route refuses by name (the same one a body that fails validation gets).
BAD_BODY_STATUS = 400

_read_body = _json_body(ReferralBody)


def _market_refusal(exc: RequestValidationError) -> Optional[str]:
    """`market_required` (absent, null or blank) or `market_invalid` when the body's `market` is why it
    failed validation, else None. A market problem is named even beside another field's."""
    for err in exc.errors():
        if tuple(err.get("loc") or ())[:2] != ("body", "market"):
            continue
        value = err.get("input")
        if err.get("type") == "missing" or value is None or (isinstance(value, str) and not value.strip()):
            return "market_required"
        return "market_invalid"
    return None


@router.post("")
async def mint_storefront_cart_referral(
    request: Request,
    caller: AgentContext = Depends(get_agent_context),
):
    # Only our own services mint here. Checked FIRST after the key: before the body is read, so any
    # other caller gets this one answer whatever it sent and learns nothing of the body's shape, and
    # before the assertion, so a refused caller burns no nonce.
    if not await agent_auth._is_service_caller(agent_auth.request_api_key(request)):
        return _refuse(REFUSED_STATUS, "service_caller_required")
    # Read here, not as a dependency: a dependency would be resolved before the check above.
    try:
        body = await _read_body(request)
    except RequestValidationError as exc:
        code = _market_refusal(exc)
        if code is None:
            raise
        return _refuse(BAD_BODY_STATUS, code)
    # Required and well-formed by now; then KNOWN: listed in the market registry, whatever its rollout
    # status. Before the assertion, so an unknown market burns no nonce.
    if not referrals.market_is_known(body.market):
        return _refuse(BAD_BODY_STATUS, "market_unknown")
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
    except referrals.ReferralMarketRequired:  # unreachable past the checks above; never a default
        return _refuse(BAD_BODY_STATUS, "market_required")
    except referrals.ReferralMarketUnknown:  # reachable only if the registry changed between the two reads
        return _refuse(BAD_BODY_STATUS, "market_unknown")
    except Exception as exc:  # noqa: BLE001 -- the door builds its cart without a referral; say so
        logger.warning("storefront_cart_referral.mint_failed error_type=%s", type(exc).__name__)
        return _refuse(503, "referral_store_unavailable")


@router.api_route("", methods=_OTHER_METHODS)
async def _no_other_method():
    """Only POST exists here. Any other method is the unknown-path 404, never a 405 that would say
    the path is there."""
    raise HTTPException(status_code=404, detail=_NOT_FOUND)
