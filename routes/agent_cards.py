"""POST /agent/v1/cards — mint a constrained card instrument for one merchant checkout.

THE REQUEST CARRIES NO AMOUNT, AND THAT IS THE CONTRACT. `model_config` forbids extra fields, so
a caller sending `amount`, `amount_cap_minor`, or any other invented knob gets a 4xx instead of
a silently-ignored field it might believe worked. The cap is derived server-side from the
merchant's own UCP quote for the named checkout (services/agent_card_issuance.py says why), and
`agent_id` is stamped from the authenticated context — both for the same reason as
card_rail_outcomes: a caller that could name its own numbers could spend outside its caps.

Flow: kill switch -> resolve merchant quote -> guarded insert (caps enforced in ONE SQL
statement) -> issuer mint -> issued row with the reveal handle. The issuer failing after the
insert leaves a 'failed' row on purpose: refused mints are part of the evidence trail, and the
daily cap deliberately does not count them.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

import json

from db.agent_issued_cards import (
    create_card_guarded,
    get_card,
    mark_failed,
    mark_issued,
    mint_card_id,
)
from routes.agent_auth import AgentContext, get_agent_context
from services.agent_card_issuance import (
    card_expiry,
    is_enabled,
    issuance_policy,
    resolve_merchant_quote,
)
from services.card_issuers import CardIssuerError, IssueRequest, resolve_issuer
from utils.logger import logger

router = APIRouter(prefix="/agent/v1", tags=["agent-cards"])


class CardIssueRequest(BaseModel):
    # extra='forbid' IS the no-amount contract — do not relax it to tolerate new fields.
    model_config = ConfigDict(extra="forbid")

    merchant_domain: str = Field(min_length=4, max_length=255)
    checkout_id: str = Field(min_length=1, max_length=255)
    recommendation_id: Optional[str] = Field(default=None, max_length=64)


def _card_view(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "card_id": row["card_id"],
        "status": row["status"],
        "merchant_domain": row["merchant_domain"],
        "checkout_id": row["checkout_id"],
        "amount_cap": {"amount_minor": row["amount_cap_minor"], "currency": row["currency"]},
        "single_use": row["single_use"],
        "expires_at": row["expires_at"],
        "reveal_handle": row.get("reveal_handle"),
        "recommendation_id": row.get("recommendation_id"),
    }


@router.post("/cards")
async def issue_card(
    body: CardIssueRequest,
    context: AgentContext = Depends(get_agent_context),
) -> Dict[str, Any]:
    if not is_enabled():
        # 503, not 404: the surface exists, the rail is switched off. An agent should back off,
        # not conclude it is talking to the wrong host.
        raise HTTPException(status_code=503, detail="card issuance is not enabled")

    issuer = resolve_issuer()
    if issuer is None:
        raise HTTPException(status_code=503, detail="no card issuer is configured")

    try:
        quote = await resolve_merchant_quote(body.merchant_domain, body.checkout_id)
    except ValueError as err:
        # The distinction the caller needs: their input was bad (422->400 house mapping) vs the
        # merchant was unreachable (502). "not a fetchable public hostname" is the former.
        status = 422 if "hostname" in str(err) else 502
        raise HTTPException(status_code=status, detail=str(err))

    policy = issuance_policy()
    card_id = mint_card_id()
    expires_at = card_expiry()  # once — the row and the issuer must agree on the expiry
    created = await create_card_guarded(
        {
            "card_id": card_id,
            "agent_id": context.agent_id,
            "recommendation_id": (body.recommendation_id or None),
            "merchant_domain": body.merchant_domain.strip().lower(),
            "checkout_id": body.checkout_id,
            "quote_total_minor": quote["total_minor"],
            "amount_cap_minor": quote["total_minor"],  # v1: cap == quote, exactly
            "currency": quote["currency"],
            "quote_snapshot": json.dumps(quote["quote_snapshot"]),
            "issuer": issuer.name,
            "single_use": True,
            "expires_at": expires_at,
            "max_outstanding": policy["max_outstanding"],
            "daily_cap_minor": policy["daily_cap_minor"],
        }
    )
    if created is None:
        raise HTTPException(status_code=429, detail="issuance cap reached for this agent")

    try:
        issued = await issuer.issue(
            IssueRequest(
                card_id=card_id,
                amount_cap_minor=quote["total_minor"],
                currency=quote["currency"],
                merchant_domain=body.merchant_domain.strip().lower(),
                single_use=True,
                expires_at=expires_at,
                metadata={"recommendation_id": body.recommendation_id or ""},
            )
        )
    except CardIssuerError as err:
        await mark_failed(card_id, err.code)
        logger.warning(f"card issuance failed card_id={card_id} code={err.code}")
        raise HTTPException(status_code=502, detail=f"issuer refused: {err.code}")

    await mark_issued(card_id, issued.issuer_card_ref, issued.reveal_handle)
    row = await get_card(card_id, context.agent_id)
    if row is None:  # unreachable in practice; fail loudly rather than fabricate a view
        raise HTTPException(status_code=500, detail="card row vanished after issuance")
    return {"status": "success", "card": _card_view(row)}


@router.get("/cards/{card_id}")
async def get_card_status(
    card_id: str,
    context: AgentContext = Depends(get_agent_context),
) -> Dict[str, Any]:
    row = await get_card(card_id, context.agent_id)
    if row is None:
        # 404 for both "no such card" and "not your card" — a 403 would confirm the id exists.
        raise HTTPException(status_code=404, detail="card not found")
    return {"status": "success", "card": _card_view(row)}
