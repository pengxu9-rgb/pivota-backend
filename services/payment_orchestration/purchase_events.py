"""Detect rail-purchase events and hand them to the agent webhook sender (payment orchestration P1).

Two steps, run by jobs/agent_purchase_events.py on the worker:

  detect     For each rail purchase whose CURRENT state warrants an event and has none yet for that
             state, build the unified purchase view (the same body GET /agent/v2/commerce/purchases/
             {id} returns) and WRITE the event (db/agent_purchase_events.py). Nothing is sent here.
  hand_over  Claim pending events with a lease and pass each to
             services.agent_webhook_service.emit_agent_webhook_event with its stored event_id, which
             signs, POSTs and schedules retries. Every delivery of one event carries the same id.

Reads the rail ledgers; never writes them. Dark unless AGENT_PURCHASE_WEBHOOKS_ENABLED is on (and the
ledger dial, AGENT_PURCHASE_LEDGER_ENABLED, since the events hang off its rows).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import Counter
from typing import Any, Dict

import db.agent_purchase_events as events_db
from services import webhook_delivery_gate
from services.payment_orchestration import purchase_view
from services.payment_orchestration.rails import unified_state

logger = logging.getLogger(__name__)

AGENT_PURCHASE_WEBHOOKS_ENABLED_ENV = "AGENT_PURCHASE_WEBHOOKS_ENABLED"
_TRUTHY = frozenset({"1", "true", "on", "yes"})

#: Unified state -> the event an agent is told. States not listed (routing, locking, placing) are
#: progress the agent learns by polling; they are not worth a push.
EVENT_FOR_STATE: Dict[str, str] = {
    "needs_payment_method": "purchase.action_required",
    "awaiting_buyer_authorization": "purchase.action_required",
    "completed": "purchase.completed",
    "failed": "purchase.failed",
    "refused": "purchase.failed",
    "expired": "purchase.failed",
}


def is_enabled() -> bool:
    return (os.getenv(AGENT_PURCHASE_WEBHOOKS_ENABLED_ENV) or "").strip().lower() in _TRUTHY


def _json_ready(body: Dict[str, Any]) -> Dict[str, Any]:
    """The body as the route would serialise it (datetimes to ISO strings, and so on)."""
    from fastapi.encoders import jsonable_encoder

    return jsonable_encoder(body)


async def detect(*, lookback_hours: int = 24, limit: int = 200) -> Dict[str, int]:
    """Write one event per (purchase, rail state) that warrants one. Returns counts by outcome."""
    counts: Counter = Counter()
    for parent in await events_db.reap_candidates(lookback_hours=lookback_hours, limit=limit):
        try:
            outcome = await _detect_one(parent)
        except Exception as exc:  # noqa: BLE001 - one bad row must not stall every row after it
            logger.warning("agent_purchase_events: detect failed purchase=%s error_type=%s",
                           parent.get("id"), type(exc).__name__)
            outcome = "error"
        counts[outcome] += 1
    return dict(counts)


async def _detect_one(parent: Dict[str, Any]) -> str:
    """Judge one candidate and write what it yields. Returns the outcome to count."""
    rail_state = parent["rail_state"]
    state = unified_state(parent["rail"], rail_state)
    event_type = EVENT_FOR_STATE.get(state or "")
    if event_type is None:
        return "no_event_for_state"
    body, problem = await purchase_view.unified_purchase(
        parent, agent_id=parent["agent_id"], agent_user_ref_hash=parent["agent_user_ref_hash"]
    )
    if problem:
        # Permanent for this (purchase, state): the owner does not match, or the state has no
        # mapping. A skipped row records that, and keeps the purchase out of the next tick's
        # candidates instead of re-reading it (and crowding newer ones out) every 30 s.
        await _record_skip(parent, rail_state, state, event_type, problem)
        return problem
    if body.get("rail_state") != rail_state:
        # The purchase moved between the candidate read and the view read. The next run sees
        # the new state; an event must describe the state it is keyed on.
        return "moved"
    if event_type == "purchase.action_required" and "next_action" not in body:
        # The candidate read only offers action states whose page is attached and live, so the
        # view refusing it here means the page failed re-vetting (an enrollment-side check)
        # or died in between. Telling the agent "act" with nowhere to send the buyer helps
        # nobody. Recorded as skipped `no_action`, which is re-checked every few minutes: the
        # rail can re-publish the page in this same state, and then the event goes out.
        await _record_skip(parent, rail_state, state, event_type, "no_action")
        return "no_action"
    await events_db.record_event(
        purchase_id=parent["id"], agent_id=parent["agent_id"], rail=parent["rail"],
        rail_state=rail_state, unified_state=state, event_type=event_type,
        payload=_json_ready(body),
    )
    return "recorded"


async def _record_skip(parent: Dict[str, Any], rail_state: str, state: Any, event_type: str, reason: str) -> None:
    await events_db.record_event(
        purchase_id=parent["id"], agent_id=parent["agent_id"], rail=parent["rail"],
        rail_state=rail_state, unified_state=str(state or "unmapped"), event_type=event_type,
        payload={}, skip_reason=reason,
    )


#: Claimed per round. Small, so a tick cut short strands few claims (and those are released).
HAND_OVER_BATCH = 10
#: No new event is started after this long; a tick then ends within this + one send + detect.
HAND_OVER_BUDGET_SECONDS = 150.0
#: One hand-over is cut after this long (the sender's httpx timeout is per phase, not per call).
#: A cut send counts as a failed attempt; if it had reached the receiver, the re-send carries the
#: same event id, which receivers de-duplicate on.
HAND_OVER_SEND_TIMEOUT_SECONDS = 45.0


async def hand_over(
    *,
    worker: str,
    limit: int = 50,
    lease_seconds: int = 300,
    max_attempts: int = 10,
    budget_seconds: float = HAND_OVER_BUDGET_SECONDS,
    clock=time.monotonic,
) -> Dict[str, int]:
    """Pass claimed pending events to the agent webhook sender. Returns counts by outcome.

    While webhook delivery is switched off in this process (services/webhook_delivery_gate),
    nothing is claimed: events stay pending, untouched, until delivery is on again.
    """
    if not webhook_delivery_gate.delivery_enabled():
        return {"delivery_disabled": 1}
    counts: Counter = Counter()
    deadline = clock() + max(0.0, float(budget_seconds))
    done = 0
    while done < limit and clock() < deadline:
        unfinished: list = []
        stop = False
        try:
            # Claimed rows land in `unfinished` one by one, so a cancel even mid-claim releases them.
            await events_db.claim_pending(
                worker=worker, limit=min(HAND_OVER_BATCH, limit - done), lease_seconds=lease_seconds,
                sink=unfinished,
            )
            if not unfinished:
                break
            while unfinished:
                if clock() >= deadline:
                    stop = True  # budget spent: the rest of this round goes back untried
                    break
                outcome = await _hand_one(unfinished[0], worker=worker, max_attempts=max_attempts)
                unfinished.pop(0)
                counts[outcome] += 1
                done += 1
                if outcome == "delivery_disabled":
                    stop = True  # delivery went off mid-tick: give the rest back and stop
                    break
        finally:
            if unfinished:
                # Cut short (the scheduler deadline cancels the tick): give back what was never
                # tried, without counting an attempt, rather than leaving it claimed for a lease.
                await asyncio.shield(_release_untried(unfinished, worker=worker, max_attempts=max_attempts))
        if stop:
            break
    return dict(counts)


async def _release_untried(events: Any, *, worker: str, max_attempts: int) -> None:
    for event in events:
        try:
            await events_db.release(event["event_id"], worker=worker, max_attempts=max_attempts, burn=False)
        except Exception as exc:  # noqa: BLE001 - the lease returns it anyway
            logger.warning("agent_purchase_events: release failed event=%s error_type=%s",
                           event["event_id"], type(exc).__name__)


async def _hand_one(event: Dict[str, Any], *, worker: str, max_attempts: int) -> str:
    from services import agent_webhook_service

    try:
        result = await asyncio.wait_for(
            agent_webhook_service.emit_agent_webhook_event(
                event["agent_id"], event_type=event["event_type"], payload=event["payload"],
                event_id=event["event_id"],
            ),
            HAND_OVER_SEND_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - release and retry later, never drop (timeouts included)
        status = await events_db.release(event["event_id"], worker=worker, max_attempts=max_attempts)
        logger.warning(
            "agent_purchase_events: hand-over failed event=%s error_type=%s now=%s",
            event["event_id"], type(exc).__name__, status,
        )
        return "abandoned" if status == "abandoned" else "released"
    if not isinstance(result, dict):
        await events_db.release(event["event_id"], worker=worker, max_attempts=max_attempts)
        return "released"
    if webhook_delivery_gate.skipped_by_environment(result):
        # Delivery went off between the check above and this send: not an attempt, not an end.
        await events_db.release(event["event_id"], worker=worker, max_attempts=max_attempts, burn=False)
        return "delivery_disabled"
    if result.get("status") == "skipped":
        # The agent's own webhook config said no (not subscribed to purchase.*, or no destination).
        # Terminal by design: an event is about the moment it happened, and get_order answers for
        # any purchase the agent asks about later (documented in docs/agent_purchases_routes.md).
        await events_db.finish(event["event_id"], worker=worker, status="skipped",
                               skip_reason=str(result.get("reason") or "skipped")[:64])
        return "skipped"
    finished = await events_db.finish(event["event_id"], worker=worker, status="handed_over",
                                      delivery_id=result.get("delivery_id"),
                                      delivery_status=str(result.get("status") or "")[:32] or None)
    if not finished:
        # The lease ran out mid-send and another worker holds the event: it will send it again,
        # with the same event id (the receiver contract covers that). Worth seeing in the logs.
        logger.warning("agent_purchase_events: lease lost after send event=%s", event["event_id"])
    return "handed_over"
