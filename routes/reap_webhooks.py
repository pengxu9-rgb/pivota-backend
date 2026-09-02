"""The two Reap receivers: the record, and the live decision.

  POST /webhooks/reap            NOTIFICATION mode — the issuer's report of what happened to a
                                 minted card. Async, retried, at-least-once. Documented below.
  POST /webhooks/reap/authorize  REQUEST mode — Reap holding a card authorization open for
                                 1.6s waiting for APPROVE/DECLINE. Synchronous, never retried,
                                 fail-closed. Documented on receive_reap_authorization, whose
                                 response-code posture is the exact INVERSE of this one's.

They share the signature verifier and nothing else — separate endpoints, separate registrations
at Reap, separate signing secrets, separate env dials.

AUTH IS THE SIGNATURE: `X-Reap-Webhook-Signature: t=<unix seconds>,v1=<hex hmac>`, HMAC-SHA256
over `"{t}.{raw bytes}"` within a 300 s window (services/reap_webhooks.verify_signature, and
rule 1 there — the exact raw bytes, never a re-serialized body). No REAP_WEBHOOK_SECRET
configured means 503 for everything — an unset secret is a receiver that does not exist yet,
never one that accepts unsigned reports.

RESPONSE-CODE POSTURE, because webhook providers retry on non-2xx:
  503  secret unconfigured (we are not ready; retrying is correct)
  401  bad/missing signature, INCLUDING a valid MAC outside the replay window (retrying the
       same forgery is useless but harmless; a genuine delivery delayed past 300 s comes back
       re-signed on the next retry)
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

import decimal
import json
import time
from datetime import datetime, timezone
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
from db.database import database
from services.reap_external_auth import (
    REQUEST_TYPE,
    auth_signature_header_name,
    auth_webhook_secret,
    decide,
    external_auth_enabled,
    parse_authorization_request,
)
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

# A CARD_AUTHORIZATION_REQUEST is a few hundred bytes. 64 KiB is ~100x headroom and still small
# enough that hashing it cannot eat a meaningful share of Reap's 1.6-second budget. Scoped to
# the authorization route deliberately: the notification receiver is async and retried, so a
# large body there costs latency nobody is waiting on.
MAX_AUTHORIZATION_BODY_BYTES = 64 * 1024


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
        # Both columns are NOT NULL in migration 199, and an explicit NULL bind DEFEATS a
        # column default — review proved every real write died here while 22 faked-DB tests
        # passed. Same values the agent route sends when the caller omits them.
        "latency_ms": "{}",
        "auth_outcome": auth_outcome,
        "reported_by": "reap",
        "occurred_at": datetime.now(timezone.utc),
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

    # ONE transaction around dedup + transitions + outcome, and the reason is the delivery
    # contract. Dedup-first-and-autocommitted made this receiver at-most-once: a failure after
    # the dedup claim answered Reap's retry "duplicate" and the event's effects were lost with
    # only a log line. Inside a transaction, a mid-flight failure rolls the dedup claim back,
    # the 500 makes Reap redeliver, and the redelivery reprocesses — at-least-once, made safe
    # by the status-guarded transitions and the keyed outcome upsert. A concurrent duplicate's
    # ON CONFLICT insert blocks on ours and resolves "duplicate" only after we commit. The
    # span is DB-and-logging only (the no-network-IO-in-transaction gate holds), and the flow
    # is sequential in one request task, which is the safe shape for databases==0.7.0.
    async with database.transaction():
        card = await find_by_issuer_ref(event.issuer_card_ref)
        if card is None:
            # 200, and deliberately NO dedup row: there is nothing to protect from replay, and
            # if our issuance write shows up late, a redelivery can still land the event.
            logger.warning(f"reap webhook: unknown issuer_card_ref for event_id={event.event_id}")
            return {"status": "ok", "handled": "ignored_unknown_card"}

        if not await record_event_once(event.event_id, event.event_type, card["card_id"]):
            return {"status": "ok", "handled": "duplicate"}

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
                # An approval that found the card outside 'issued' is itself an alarm: the
                # issuer authorized an instrument we consider spent, revoked, or expired.
                alarm("AUTH_ON_NON_ISSUED_CARD", card["card_id"], event)
            if card["recommendation_id"]:
                await record_outcome(_outcome_values(card, event, "completed", None, "approved"))
        elif event.event_type == "auth_declined":
            applied = await apply_auth_declined(card["card_id"])
            if card["recommendation_id"]:
                await record_outcome(
                    _outcome_values(
                        card, event, "failed", "payment_declined",
                        event.decline_reason or "declined",
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


async def _read_bounded_body(request: Request) -> bytes:
    """Read the request body, aborting the moment it crosses MAX_AUTHORIZATION_BODY_BYTES.

    `await request.body()` buffers the WHOLE body first and only then lets the caller measure
    it, so a lying-small content-length with a 200 KB body was fully read into memory before
    anything refused it — the ceiling was a report, not a limit. Streaming with a running total
    stops at the first chunk that crosses the line: at most the ceiling plus one chunk is ever
    held.

    Two gates, because neither covers the other. The declared content-length (checked by the
    caller) refuses before a single byte is read, but it is attacker-supplied and a chunked
    request has none. This one measures what actually arrived and cannot be lied to.

    DELIBERATELY FAIL-CLOSED ON THE HEADER: a content-length declaring MORE than the ceiling is
    refused even if the body turns out to be small. Reap's real requests are a few hundred bytes
    and declare it honestly, so the only traffic this rejects is malformed or hostile — and on
    a decision endpoint a wrong refusal costs one declined authorization, while a wrong
    acceptance spends the budget of the next one.
    """
    total = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_AUTHORIZATION_BODY_BYTES:
            raise HTTPException(
                status_code=413, detail="authorization request body too large"
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/reap/authorize")
async def receive_reap_authorization(request: Request) -> Dict[str, str]:
    """POST /webhooks/reap/authorize — Reap's EXTERNAL AUTHORIZATION request (mode: REQUEST).

    The sibling of the receiver above, and its opposite in every posture that matters. That one
    records what already happened and answers 200 for anything it cannot act on, because a 4xx
    invites infinite redelivery. This one is the LIVE DECISION on a card authorization: Reap is
    holding the transaction open for 1.6 seconds waiting for our answer, and treats a timeout, a
    non-2xx, an unreachable host or an unparseable body as a DECLINE.

    That inverts the response-code posture completely — every error code here is a decline, so
    every ambiguity resolves toward one:

      503  the feature switch is off, or the endpoint's secret is unset. Both are fail-closed
           dials: an unconfigured decision endpoint must decline, never approve.
      413  body over MAX_AUTHORIZATION_BODY_BYTES, refused BEFORE the HMAC.
      401  bad or missing signature.
      400  signed body that is not JSON, is not a CARD_AUTHORIZATION_REQUEST, or carries no
           eventId/cardId — we cannot record a decision without an event id, and an unrecorded
           approval is the one outcome worse than a decline.
      200  a real decision: {"decision":"APPROVE"} or {"decision":"DECLINE","reason":...},
           and NOTHING else in the body.

    A 500 (database down mid-decision) also declines, and that is the correct failure: the
    alternative — approving, or declining without a ledger row — would spend or refuse money
    with no record of why. This handler therefore does not catch.

    REAP_EXTERNAL_AUTH_ENABLED is checked BEFORE the secret so the switch alone can take the
    endpoint out of service without touching secret storage.
    """
    started_at = time.monotonic()

    if not external_auth_enabled():
        raise HTTPException(status_code=503, detail="reap external authorization is disabled")
    secret = auth_webhook_secret()
    if not secret:
        # NOT falling back to REAP_WEBHOOK_SECRET: the REQUEST-mode endpoint is registered
        # separately and gets its own signingSecret. A fallback would authenticate live
        # spending decisions with the notification receiver's key.
        raise HTTPException(status_code=503, detail="reap authorization receiver is not configured")

    # SIZE CEILING BEFORE THE HMAC, and before the body is even buffered. See
    # _read_bounded_body: the declared content-length is the cheap first gate, and the streaming
    # read is the one that holds when that header lies or is absent.
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_AUTHORIZATION_BODY_BYTES:
        raise HTTPException(status_code=413, detail="authorization request body too large")
    raw = await _read_bounded_body(request)

    if not verify_signature(raw, request.headers.get(auth_signature_header_name()), secret):
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        # parse_float=Decimal, NOT the default float: Reap sends decimal MAJOR-unit amounts and
        # 42.50 as a binary float is 42.4999999999999964..., which would be compared against a
        # spending cap. Money never becomes a binary float on this path.
        body = json.loads(raw.decode("utf-8"), parse_float=decimal.Decimal)
    except Exception:
        raise HTTPException(status_code=400, detail="signed body was not valid JSON")

    if not isinstance(body, dict) or body.get("type") != REQUEST_TYPE:
        raise HTTPException(status_code=400, detail="unsupported authorization request type")

    authorization = parse_authorization_request(body)
    if authorization is None:
        # No detail from the body — the 400 says the shape was unusable, never what was in it.
        raise HTTPException(status_code=400, detail="authorization request had no usable identity")

    outcome = await decide(authorization, started_at)
    return outcome.body()
