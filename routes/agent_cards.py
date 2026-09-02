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
    mark_failed_with_orphan,
    mark_issued,
    mint_card_id,
)
from routes.agent_auth import AgentContext, get_agent_context
from services.agent_card_issuance import (
    MerchantQuoteError,
    cap_for_quote,
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


def _snapshot_with_headroom(quote: Dict[str, Any], cap: Dict[str, Any]) -> Dict[str, Any]:
    """The merchant's quote record, plus why the cap differs from it.

    TOLERATES A NON-DICT SNAPSHOT. `resolve_merchant_quote` always builds a dict, but the
    previous line spread it — so a `None`, a list, or a string reached `json.dumps` as a
    TypeError and a 500. `cap_for_quote`, two lines earlier in the same request, explicitly
    fail-closes against exactly this ("an older cached shape, a hand-built dict, a future
    refactor"); this had no business being stricter than its own neighbour.

    OUR KEY WINS. The snapshot carries merchant-controlled VALUES (`totals` is arbitrary
    merchant JSON), and while it does not today carry merchant-controlled KEYS, the audit field
    must not be shadowable if that ever changes — so `headroom` is applied last, deliberately.
    """
    base = quote.get("quote_snapshot")
    if not isinstance(base, dict):
        base = {"unexpected_snapshot_shape": repr(base)[:200]}
    return {**base, "headroom": cap}


def _card_view(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "card_id": row["card_id"],
        "status": row["status"],
        "merchant_domain": row["merchant_domain"],
        "checkout_id": row["checkout_id"],
        "amount_cap": {"amount_minor": row["amount_cap_minor"], "currency": row["currency"]},
        # THE OTHER HALF OF THE DELTA. `amount_cap` is what the card may spend; this is what the
        # merchant actually quoted. Since #1923 they differ by policy headroom, and an agent shown
        # only the cap cannot tell a $23.17 order with $17.78 of headroom from a $40.95 order — it
        # would quote the cap to the buyer as if it were the price. `get_card` already SELECTs
        # this column; the view simply dropped it.
        "merchant_quote": {"amount_minor": row["quote_total_minor"], "currency": row["currency"]},
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

    try:
        issuer = resolve_issuer()
    except CardIssuerError:
        # A MISCONFIGURED issuer (reap without keys, mock in production) must present exactly
        # like an absent one: 503, rail unavailable. Letting the constructor's raise escape
        # turned fail-closed into a 500 — the review's F1.
        issuer = None
    if issuer is None:
        raise HTTPException(status_code=503, detail="no card issuer is configured")

    try:
        quote = await resolve_merchant_quote(body.merchant_domain, body.checkout_id)
    except MerchantQuoteError as err:
        # THREE parties, not two. Their input was bad (422->400 house mapping); the merchant was
        # unreachable or refused (502); or WE are misconfigured — our agent profile is dead, our
        # discovery handshake was refused — which is also a 502 and used to be a 422. Telling an
        # agent its request was invalid because of OUR configuration sends it debugging a request
        # that was fine. The exception carries the verdict; no string matching on messages.
        if err.our_fault:
            # Generic detail on purpose: the specific cause names our env vars and internal
            # config, which is our operational surface and nothing the caller can act on. The
            # log is where the cause belongs.
            logger.error("card issuance blocked by OUR merchant-negotiation config: %s", err)
            raise HTTPException(
                status_code=502, detail="merchant negotiation is not configured"
            )
        raise HTTPException(status_code=422 if err.caller_fault else 502, detail=str(err))

    policy = issuance_policy()
    # ONE cap, computed ONCE, used by BOTH the row and the issuer. Deriving it twice — or letting
    # the row keep the quote while the issuer gets the headroom — would make the audit trail
    # describe a card that was never minted, and `amount_cap_minor` is the number the whole
    # migration-201 design leans on being true.
    cap = cap_for_quote(quote)
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
            # THE VISIBLE DELTA migration 201 asked for: headroom is the difference between two
            # audited columns, never a silent multiplier. Zero when the merchant already quoted a
            # landed total; otherwise it covers the shipping and tax a pre-address checkout
            # cannot carry (B7), without which the card declines the moment Reap enters an
            # address — the one action that flow exists to perform.
            "amount_cap_minor": cap["amount_cap_minor"],
            "currency": quote["currency"],
            # WHY, not just how much. `amount_cap_minor - quote_total_minor` recovers the size of
            # the headroom but not its reason, and the reasons are not interchangeable:
            # `quote_is_landed` and `currency_not_calibrated` both yield zero, and a zero that
            # means "nothing to cover" is a healthy quote while a zero that means "we could not
            # tell" is a decline waiting to happen. `ceiling` vs `flat_plus_bps` is the signal for
            # whether the ceiling ever engages at real order sizes — the question #1923 left open
            # about its own defaults, and the one this record exists to answer.
            "quote_snapshot": json.dumps(_snapshot_with_headroom(quote, cap)),
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
                amount_cap_minor=cap["amount_cap_minor"],
                currency=quote["currency"],
                merchant_domain=body.merchant_domain.strip().lower(),
                single_use=True,
                expires_at=expires_at,
                metadata={"recommendation_id": body.recommendation_id or ""},
            )
        )
    except CardIssuerError as err:
        if err.issuer_card_ref:
            # A card EXISTS at the issuer that we refused to accept (constraints unconfirmed or
            # contradicted). Persisting the ref on the failed row is what puts it in front of
            # jobs/agent_card_revocation_sweep.py — without it the orphan is unreachable and the
            # only record is a log line, which nothing sweeps.
            await mark_failed_with_orphan(card_id, err.code, err.issuer_card_ref)
            logger.error(
                "card issuance failed with an ORPHAN card_id=%s code=%s — queued for revocation",
                card_id,
                err.code,
            )
        else:
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
