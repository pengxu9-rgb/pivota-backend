"""POST /webhooks/reap — the issuer's report of what happened to a minted card.

AUTH IS THE SIGNATURE, verified over the exact raw bytes (services/reap_webhooks.py rule 1).
No REAP_WEBHOOK_SECRET configured means 503 for everything — an unset secret is a receiver
that does not exist yet, never one that accepts unsigned reports.

RESPONSE-CODE POSTURE, because webhook providers retry on non-2xx:
  503  secret unconfigured (we are not ready; retrying is correct)
  401  bad/missing signature (retrying the same forgery is useless but harmless)
  400  VALID signature, unparseable JSON (the holder of the secret sent garbage — surface it)
  200  everything else, including events we ignore (unknown card, duplicate, unusable shape):
       a 4xx would invite infinite redelivery of something that will never become processable.

MISMATCH POSTURE (services/reap_webhooks.check_card_consistency): metadata or currency
mismatches are alarmed and NOT applied — the report may not be about the card we found. A cap
breach is alarmed AND applied: the issuer approving beyond our cap means real money moved, and
refusing to record it would not unmove it — it would just blind the reconciliation this
receiver exists for.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

from db.agent_issued_cards import (
    apply_auth_approved,
    apply_auth_declined,
    apply_settlement,
    find_by_issuer_ref,
    record_event_once,
)
from db.card_rail_outcomes import record_outcome
from services.reap_webhooks import (
    ReapEvent,
    alarm,
    check_card_consistency,
    minor_to_major,
    parse_event,
    signature_header_name,
    verify_signature,
    webhook_secret,
)
from utils.logger import logger

router = APIRouter(prefix="/webhooks", tags=["reap-webhooks"])


def _outcome_values(
    card: Dict[str, Any], event: ReapEvent, outcome: str,
    failure_reason: Optional[str], auth_outcome: Optional[str],
) -> Dict[str, Any]:
    """Full bind set for record_outcome's UPSERT. agent_id comes from the CARD row — it was
    stamped from the authenticated context at mint time, which is exactly the provenance the
    outcomes table demands; a webhook body never names an agent."""
    actual = (
        minor_to_major(event.amount_minor, card["currency"])
        if event.amount_minor is not None
        else None
    )
    return {
        "recommendation_id": card["recommendation_id"],
        "recommendation_set_id": None,
        "trace_id": None,
        "click_id": None,
        "agent_id": card["agent_id"],
        "merchant_domain": card["merchant_domain"],
        "product_key": None,
        "variant_id": None,
        "rail": "reap_card",
        "quoted_item_total": None,
        "quoted_grand_total": minor_to_major(card["quote_total_minor"], card["currency"]),
        "quoted_currency": card["currency"],
        "quoted_at": None,
        "spec_expires_at": card["expires_at"],
        "actual_item_total": None,
        "actual_grand_total": actual,
        "actual_currency": card["currency"] if actual is not None else None,
        "outcome": outcome,
        "failure_reason": failure_reason,
        "failure_reason_raw": None,
        "latency_ms": None,
        "auth_outcome": auth_outcome,
        "reported_by": "reap",
        "occurred_at": None,
    }


@router.post("/reap")
async def receive_reap_webhook(request: Request) -> Dict[str, Any]:
    secret = webhook_secret()
    if not secret:
        raise HTTPException(status_code=503, detail="reap webhook receiver is not configured")

    raw = await request.body()
    if not verify_signature(raw, request.headers.get(signature_header_name()), secret):
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="signed body was not valid JSON")

    event = parse_event(body)
    if event is None:
        logger.warning("reap webhook: signed event with no usable identity — ignored")
        return {"status": "ok", "handled": "ignored_unparseable"}

    if not await record_event_once(event.event_id, event.event_type, None):
        return {"status": "ok", "handled": "duplicate"}

    card = await find_by_issuer_ref(event.issuer_card_ref)
    if card is None:
        # Not an error to Reap (200), but worth a line: either an event for a card minted
        # outside this system, or our issuance write was lost.
        logger.warning(f"reap webhook: unknown issuer_card_ref for event_id={event.event_id}")
        return {"status": "ok", "handled": "ignored_unknown_card"}

    inconsistency = check_card_consistency(event, card)
    if inconsistency is not None:
        alarm(inconsistency, card["card_id"], event)
        if inconsistency != "CARD_CAP_BREACH":
            return {"status": "ok", "handled": f"alarmed_{inconsistency.lower()}"}
        # cap breach falls through: record what actually happened, loudly.

    applied = False
    if event.event_type == "auth_approved":
        applied = await apply_auth_approved(card["card_id"], bool(card["single_use"]))
        if not applied:
            # An approval that found the card outside 'issued' is itself an alarm: the issuer
            # authorized an instrument we consider spent, revoked, or expired.
            alarm("AUTH_ON_NON_ISSUED_CARD", card["card_id"], event)
        if card["recommendation_id"]:
            await record_outcome(_outcome_values(card, event, "completed", None, "approved"))
    elif event.event_type == "auth_declined":
        applied = await apply_auth_declined(card["card_id"])
        if card["recommendation_id"]:
            await record_outcome(
                _outcome_values(
                    card, event, "failed", "payment_declined", event.decline_reason or "declined"
                )
            )
    elif event.event_type == "settlement":
        if event.amount_minor is not None:
            applied = await apply_settlement(card["card_id"], event.amount_minor)
        if card["recommendation_id"]:
            await record_outcome(_outcome_values(card, event, "completed", None, None))
    else:
        return {"status": "ok", "handled": "ignored_event_type"}

    return {"status": "ok", "handled": event.event_type, "applied": applied}
