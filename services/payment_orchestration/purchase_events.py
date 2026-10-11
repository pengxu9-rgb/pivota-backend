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

import logging
import os
from collections import Counter
from typing import Any, Dict

import db.agent_purchase_events as events_db
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
        rail_state = parent["rail_state"]
        state = unified_state(parent["rail"], rail_state)
        event_type = EVENT_FOR_STATE.get(state or "")
        if event_type is None:
            counts["no_event_for_state"] += 1
            continue
        body, problem = await purchase_view.unified_purchase(
            parent, agent_id=parent["agent_id"], agent_user_ref_hash=parent["agent_user_ref_hash"]
        )
        if problem:
            counts[problem] += 1
            continue
        if body.get("rail_state") != rail_state:
            # The purchase moved between the candidate read and the view read. The next run sees
            # the new state; an event must describe the state it is keyed on.
            counts["moved"] += 1
            continue
        if event_type == "purchase.action_required" and "next_action" not in body:
            # The rail has not attached (or has already withdrawn) the page the buyer must open.
            # Telling the agent "act" with nowhere to send the buyer helps nobody: try again on the
            # next run, while the state is still the same.
            counts["no_action_yet"] += 1
            continue
        await events_db.record_event(
            purchase_id=parent["id"], agent_id=parent["agent_id"], rail=parent["rail"],
            rail_state=rail_state, unified_state=state, event_type=event_type,
            payload=_json_ready(body),
        )
        counts["recorded"] += 1
    return dict(counts)


async def hand_over(*, worker: str, limit: int = 50, lease_seconds: int = 300, max_attempts: int = 10) -> Dict[str, int]:
    """Pass claimed pending events to the agent webhook sender. Returns counts by outcome."""
    from services import agent_webhook_service

    counts: Counter = Counter()
    for event in await events_db.claim_pending(worker=worker, limit=limit, lease_seconds=lease_seconds):
        try:
            result = await agent_webhook_service.emit_agent_webhook_event(
                event["agent_id"], event_type=event["event_type"], payload=event["payload"],
                event_id=event["event_id"],
            )
        except Exception as exc:  # noqa: BLE001 - release and retry later, never drop
            status = await events_db.release(event["event_id"], worker=worker, max_attempts=max_attempts)
            logger.warning(
                "agent_purchase_events: hand-over failed event=%s error_type=%s now=%s",
                event["event_id"], type(exc).__name__, status,
            )
            counts["abandoned" if status == "abandoned" else "released"] += 1
            continue
        if not isinstance(result, dict):
            await events_db.release(event["event_id"], worker=worker, max_attempts=max_attempts)
            counts["released"] += 1
            continue
        if result.get("status") == "skipped":
            await events_db.finish(event["event_id"], worker=worker, status="skipped",
                                   skip_reason=str(result.get("reason") or "skipped")[:64])
            counts["skipped"] += 1
            continue
        await events_db.finish(event["event_id"], worker=worker, status="handed_over",
                               delivery_id=result.get("delivery_id"),
                               delivery_status=str(result.get("status") or "")[:32] or None)
        counts["handed_over"] += 1
    return dict(counts)
