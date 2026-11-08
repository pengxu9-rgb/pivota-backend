"""`/agent/v2/storefront-handoffs`: the UCP storefront hand-off record (FIX_PLAN O-02).

DARK BY DEFAULT. While UCP_STOREFRONT_HANDOFF_RECORD_ENABLED is off every call answers the same
404 `not_available`, whatever its shape, and nothing is read or written.

Two calls, both made by the GATEWAY with its own service key and a verified
X-Pivota-Issuing-Agent naming the partner (the O-04 assertion, adopted by
routes.agent_forwarded_owner, so PARTNER_IDENTITY_FORWARDING_ENABLED must be on too):

  POST /agent/v2/storefront-handoffs        record a hand-off the gateway just issued
                                            (op `ucp_storefront_handoff.record`)
  POST /agent/v2/storefront-handoffs/read   read one back for get_order (op `ucp_storefront_handoff.read`)

A call whose partner was NOT adopted (no assertion, one that does not verify for the op, a caller
that may not vouch, the forwarding flag off) is refused with 403 `partner_identity_required`: a
hand-off row is only worth having with its partner, and a read is only answered to its owner.
The partner is never taken from the body. See services/storefront_handoff_record.py for the row.
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from routes.agent_auth import AgentContext
from routes.agent_forwarded_owner import forwarded_owner_context
from services import storefront_handoff_record as handoffs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent/v2/storefront-handoffs", tags=["agent-storefront-handoffs"])


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


def _adopted_partner(context: AgentContext) -> Optional[str]:
    """The partner the gateway vouched for, or None when this request acts as its own caller."""
    if not getattr(context, "forwarded_by_agent_id", None):
        return None
    return str(context.agent_id or "").strip() or None


@router.post("")
async def record_storefront_handoff(
    body: HandoffRecordBody,
    context: AgentContext = Depends(forwarded_owner_context(handoffs.RECORD_OP)),
):
    if not handoffs.storefront_handoff_record_enabled():
        return _refuse(404, "not_available")
    partner = _adopted_partner(context)
    if partner is None:
        return _refuse(403, "partner_identity_required")
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


@router.post("/read")
async def read_storefront_handoff(
    body: HandoffReadBody,
    context: AgentContext = Depends(forwarded_owner_context(handoffs.READ_OP)),
):
    if not handoffs.storefront_handoff_record_enabled():
        return _refuse(404, "not_available")
    partner = _adopted_partner(context)
    if partner is None:
        return _refuse(403, "partner_identity_required")
    try:
        answered = await handoffs.read_handoff(body.record_id, body.binding, partner)
    except Exception as exc:  # noqa: BLE001
        logger.warning("storefront_handoff.read_failed error_type=%s", type(exc).__name__)
        return _refuse(503, "handoff_store_unavailable")
    if answered["outcome"] == "not_found":
        return _refuse(404, "handoff_not_found")
    if answered["outcome"] == "not_owner":
        return _refuse(403, "not_owner")
    return answered["handoff"]
