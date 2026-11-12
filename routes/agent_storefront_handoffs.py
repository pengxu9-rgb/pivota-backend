"""`/agent/v2/storefront-handoffs`: the UCP storefront hand-off record (FIX_PLAN O-02).

DARK BY DEFAULT. While UCP_STOREFRONT_HANDOFF_RECORD_ENABLED is off every call answers the same
404 `not_available`, whatever its shape and whoever sends it, and nothing is read or written: the
flag is checked by a router dependency that runs FIRST, before authentication, before the body is
read or validated, and before the partner assertion is adopted (so no nonce is burned and no
partner rate limit or quota is charged; O-02 review B-3, status review P2-11).

With the flag on, the order is: the caller's key (401), the body (400/422, before any assertion is
looked at, so a malformed body burns no nonce), then the partner assertion (409 on a bad one, the
partner's own 429s), then the route.

Two calls, both made by the GATEWAY with its own service key and a REQUEST-BOUND (v2)
X-Pivota-Issuing-Agent naming the partner (the O-04 assertion, adopted by
routes.agent_forwarded_owner, so PARTNER_IDENTITY_FORWARDING_ENABLED must be on too). The token
must verify for the route's op, `POST`, the raw path, the exact body bytes, a fresh single-use
nonce within 60 s, and `rid` = the body's `record_id` (services/bound_issuing_assertion.py). A v2
token that fails any check is 409 PARTNER_ASSERTION_REJECTED; a v1 token is never adopted:

  POST /agent/v2/storefront-handoffs        record a hand-off the gateway just issued
                                            (op `ucp_storefront_handoff.record`)
  POST /agent/v2/storefront-handoffs/read   read one back for get_order (op `ucp_storefront_handoff.read`)

A call whose partner was NOT adopted (no assertion, a v1 token, a caller that may not vouch, the
forwarding flag off) is refused with 409 `partner_identity_required`, and a read of another
partner's hand-off with 409 `not_owner`: a hand-off row is only worth having with its partner, and a
read is only answered to its owner. 409, not 403, for the reason routes.agent_forwarded_owner
gives (REJECTED_STATUS): the gateway's private backend hop turns any backend 401/403 into a
transport failure, which would hide the refusal and make the agent retry (O-02 review G-1).
The partner is never taken from the body. See services/storefront_handoff_record.py for the row.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Literal, Optional, Type, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from routes.agent_auth import AgentContext, get_agent_context
from routes.agent_forwarded_owner import adopt_forwarded_owner
from services import storefront_handoff_record as handoffs

logger = logging.getLogger(__name__)


def _dark_unless_enabled() -> None:
    """Runs before every other dependency of these routes: dark is one 404 for every request."""
    if not handoffs.storefront_handoff_record_enabled():
        raise HTTPException(status_code=404, detail={"error": "not_available"})


router = APIRouter(
    prefix="/agent/v2/storefront-handoffs",
    tags=["agent-storefront-handoffs"],
    dependencies=[Depends(_dark_unless_enabled)],
)

#: Who refuses a request that is not this partner's (see the module docstring).
REFUSED_STATUS = 409


def _anchored(pattern: str) -> str:
    """pydantic's `pattern` SEARCHES; every field here must match whole."""
    return f"^(?:{pattern})$"


def _refuse(status: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code})


class HandoffRecordBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str = Field(pattern=_anchored(handoffs.RECORD_ID_RE.pattern))
    binding: str = Field(pattern=_anchored(handoffs.BINDING_RE.pattern))
    seller_host: str = Field(max_length=253, pattern=_anchored(handoffs.SELLER_HOST_RE.pattern))
    currency: str = Field(pattern=_anchored(handoffs.CURRENCY_RE.pattern))
    quoted_total_minor: int = Field(ge=0, le=10**15)
    quote_source: Literal["seller_cart", "catalogue_fallback"]
    buyer_market: Optional[str] = Field(default=None, pattern=_anchored(handoffs.MARKET_RE.pattern))
    referral_click_id: Optional[str] = Field(default=None, pattern=_anchored(handoffs.REFERRAL_CLICK_ID_RE.pattern))
    price_as_of: Optional[str] = Field(default=None, max_length=40, pattern=_anchored(r"\d{4}-\d{2}-\d{2}T[0-9:.]+(Z|[+-]\d{2}:\d{2})"))
    merchant_id: Optional[str] = Field(default=None, max_length=50, pattern=_anchored(r"[A-Za-z0-9_.:-]+"))


class HandoffReadBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str = Field(pattern=_anchored(handoffs.RECORD_ID_RE.pattern))
    binding: str = Field(pattern=_anchored(handoffs.BINDING_RE.pattern))


Body = TypeVar("Body", bound=BaseModel)


def _json_body(model: Type[Body]) -> Callable[..., Any]:
    """The body, validated as a dependency (not a FastAPI body parameter, which is parsed before
    ANY dependency runs, the dark gate included). A failure is the same RequestValidationError,
    with the same `("body", ...)` locations, that a body parameter raises."""

    async def _dependency(request: Request) -> Body:
        raw = await request.body()
        try:
            return model.model_validate_json(raw or b"")
        except ValidationError as exc:
            errors = []
            for err in exc.errors(include_url=False):
                loc = tuple(err.get("loc") or ())
                errors.append({**err, "loc": ("body", *loc)})
            raise RequestValidationError(errors) from None

    return _dependency


def _body_schema(model: Type[BaseModel]) -> Dict[str, Any]:
    return {"requestBody": {"required": True, "content": {"application/json": {"schema": model.model_json_schema()}}}}


def _adopted_partner(context: AgentContext) -> Optional[str]:
    """The partner the gateway vouched for, or None when this request acts as its own caller."""
    if not getattr(context, "forwarded_by_agent_id", None):
        return None
    return str(context.agent_id or "").strip() or None


@router.post("", openapi_extra=_body_schema(HandoffRecordBody))
async def record_storefront_handoff(
    request: Request,
    caller: AgentContext = Depends(get_agent_context),
    body: HandoffRecordBody = Depends(_json_body(HandoffRecordBody)),
):
    # rid = the hand-off record id: the one resource both calls act on (the gateway signs the same).
    context = await adopt_forwarded_owner(request, caller, ops=(handoffs.RECORD_OP,), resource=("body", "record_id"))
    partner = _adopted_partner(context)
    if partner is None:
        return _refuse(REFUSED_STATUS, "partner_identity_required")
    try:
        outcome = await handoffs.record_handoff(handoffs.HandoffRecord(
            record_id=body.record_id,
            binding=body.binding,
            partner_agent_id=partner,
            via_agent_id=getattr(context, "forwarded_by_agent_id", None),
            seller_host=body.seller_host,
            currency=body.currency,
            quoted_total_minor=body.quoted_total_minor,
            quote_source=body.quote_source,
            buyer_market=body.buyer_market,
            referral_click_id=body.referral_click_id,
            price_as_of=body.price_as_of,
            merchant_id=body.merchant_id,
        ))
    except Exception as exc:  # noqa: BLE001 -- the gateway hands the id out unrecorded; say so
        logger.warning("storefront_handoff.record_failed error_type=%s", type(exc).__name__)
        return _refuse(503, "handoff_store_unavailable")
    if outcome != "recorded":
        return _refuse(409, "handoff_conflict")
    return {"record_id": body.record_id, "status": handoffs.STATUS_HANDED_OFF}


@router.post("/read", openapi_extra=_body_schema(HandoffReadBody))
async def read_storefront_handoff(
    request: Request,
    caller: AgentContext = Depends(get_agent_context),
    body: HandoffReadBody = Depends(_json_body(HandoffReadBody)),
):
    context = await adopt_forwarded_owner(request, caller, ops=(handoffs.READ_OP,), resource=("body", "record_id"))
    partner = _adopted_partner(context)
    if partner is None:
        return _refuse(REFUSED_STATUS, "partner_identity_required")
    try:
        answered = await handoffs.read_handoff(body.record_id, body.binding, partner)
    except Exception as exc:  # noqa: BLE001
        logger.warning("storefront_handoff.read_failed error_type=%s", type(exc).__name__)
        return _refuse(503, "handoff_store_unavailable")
    if answered["outcome"] == "not_found":
        return _refuse(404, "handoff_not_found")
    if answered["outcome"] == "not_owner":
        return _refuse(REFUSED_STATUS, "not_owner")
    return answered["handoff"]
