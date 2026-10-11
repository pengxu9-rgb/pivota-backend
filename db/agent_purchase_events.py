"""Durable purchase events for agent platforms (migration 265, payment orchestration P1).

One row per (purchase, rail state) that warrants telling the agent something:
`purchase.action_required` (the buyer must open a rail-hosted page), `purchase.completed`,
`purchase.failed`. services/payment_orchestration/purchase_events.py DETECTS them from the rail
ledgers and writes them here; it then hands each one to the existing agent webhook sender
(services/agent_webhook_service.py), which signs, delivers and retries.

WHY A ROW BEFORE ANY DELIVERY. The shared webhook sender POSTs first and records afterwards, and
its retry loop holds no claim, so on its own it can lose an event (crash before the record) or
send one twice (two retry loops). Here:
  - the event is WRITTEN before anything is sent, and the UNIQUE (purchase_id, rail_state) index
    makes detection idempotent: any number of detector runs insert it once;
  - `event_id` is minted once, stored, and passed to the sender, so every delivery and every retry
    of one event carries the SAME id. Receivers de-duplicate on it (documented contract), which
    turns the sender's at-least-once delivery into exactly-once handling.
  - handing an event to the sender is CLAIMED with a lease (conditional UPDATE ... RETURNING, both
    dialects, as in db/reap_agentic_ledger), so two worker ticks never hand the same event over at
    once; a crashed hand-over is retried after the lease, with the same event_id.

ORDER IS NOT PROMISED. Hand-over is oldest-first, but a retried hand-over (or the sender's own retry)
can deliver an older event after a newer one. Each event carries the purchase view AS OF its state
(`data.state`), so a receiver can tell which is newer, and get_order is always the authoritative read.

No PII: the payload is the owner-scoped public purchase view (the same body
GET /agent/v2/commerce/purchases/{id} returns). Opens no database transactions. Every statement is a
module-level string literal; dialect splits are if/else statements (the PREPARE gate's rule).
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from db.database import IS_POSTGRES, database
from db.reap_agentic_ledger import _decode_dt, _decode_json

#: The event types. The CHECK below holds the same strings; a test parses them back out.
EVENT_TYPES = ("purchase.action_required", "purchase.completed", "purchase.failed")
#: Where an event is in its hand-over to the webhook sender.
STATUSES = ("pending", "handed_over", "skipped", "abandoned")

_CREATE_TABLE_PG = """
CREATE TABLE IF NOT EXISTS agent_purchase_events (
    event_id VARCHAR(64) PRIMARY KEY,
    purchase_id VARCHAR(64) NOT NULL,
    agent_id VARCHAR(128) NOT NULL,
    rail VARCHAR(16) NOT NULL,
    rail_state VARCHAR(32) NOT NULL,
    unified_state VARCHAR(40) NOT NULL,
    event_type VARCHAR(40) NOT NULL CONSTRAINT ck_agent_purchase_events_type
        CHECK (event_type IN ('purchase.action_required', 'purchase.completed', 'purchase.failed')),
    payload JSONB NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending' CONSTRAINT ck_agent_purchase_events_status
        CHECK (status IN ('pending', 'handed_over', 'skipped', 'abandoned')),
    attempts INTEGER NOT NULL DEFAULT 0,
    claimed_by VARCHAR(64),
    claimed_at TIMESTAMPTZ,
    delivery_id VARCHAR(64),
    delivery_status VARCHAR(32),
    skip_reason VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    handed_over_at TIMESTAMPTZ
)
"""
_CREATE_TABLE_SQLITE = (
    _CREATE_TABLE_PG.replace("TIMESTAMPTZ", "TIMESTAMP")
    .replace("JSONB", "TEXT")
    .replace("now()", "CURRENT_TIMESTAMP")
)

#: Detection is idempotent on this: one event per purchase per rail state.
_CREATE_UNIQUE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_purchase_events_purchase_state
    ON agent_purchase_events (purchase_id, rail_state)
"""

#: The hand-over loop's read: pending rows, oldest first.
_CREATE_PENDING_INDEX = """
CREATE INDEX IF NOT EXISTS idx_agent_purchase_events_status_created
    ON agent_purchase_events (status, created_at)
"""


async def ensure_agent_purchase_events_schema() -> None:
    """Self-heal parity with migration 265. Idempotent; called at every startup."""
    if IS_POSTGRES:
        await database.execute(_CREATE_TABLE_PG)
    else:
        await database.execute(_CREATE_TABLE_SQLITE)
    await database.execute(_CREATE_UNIQUE_INDEX)
    await database.execute(_CREATE_PENDING_INDEX)


def new_event_id() -> str:
    return f"evt_{uuid.uuid4().hex[:24]}"


_INSERT_SQL = """
    INSERT INTO agent_purchase_events (
        event_id, purchase_id, agent_id, rail, rail_state, unified_state, event_type, payload,
        status, skip_reason
    ) VALUES (
        :event_id, :purchase_id, :agent_id, :rail, :rail_state, :unified_state, :event_type,
        CAST(:payload AS JSONB), :status, :skip_reason
    )
    ON CONFLICT (purchase_id, rail_state) DO NOTHING
"""
_INSERT_SQL_SQLITE = _INSERT_SQL.replace("CAST(:payload AS JSONB)", ":payload")

_SELECT_BY_PURCHASE_STATE_SQL = """
    SELECT event_id FROM agent_purchase_events WHERE purchase_id = :purchase_id AND rail_state = :rail_state
"""


async def record_event(
    *,
    purchase_id: str,
    agent_id: str,
    rail: str,
    rail_state: str,
    unified_state: str,
    event_type: str,
    payload: Dict[str, Any],
    skip_reason: Optional[str] = None,
) -> Optional[str]:
    """Write the event if this (purchase, rail state) has none yet. Returns the event_id that
    exists afterwards (this call's or a concurrent writer's).

    With `skip_reason` the row is written already `skipped`: detection found this (purchase,
    state) can never yield an event (owner mismatch, no page to send the buyer to). The row is
    the record of that decision AND what stops the detector re-reading the purchase every tick.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown purchase event type {event_type!r}")
    values = {
        "event_id": new_event_id(),
        "purchase_id": purchase_id,
        "agent_id": agent_id,
        "rail": rail,
        "rail_state": rail_state,
        "unified_state": unified_state,
        "event_type": event_type,
        "payload": json.dumps(payload, separators=(",", ":"), sort_keys=True),
        "status": "skipped" if skip_reason else "pending",
        "skip_reason": str(skip_reason)[:64] if skip_reason else None,
    }
    if IS_POSTGRES:
        await database.execute(_INSERT_SQL, values)
    else:
        await database.execute(_INSERT_SQL_SQLITE, values)
    row = await database.fetch_one(
        _SELECT_BY_PURCHASE_STATE_SQL, {"purchase_id": purchase_id, "rail_state": rail_state}
    )
    return str(row["event_id"]) if row else None


# ── hand-over claims ─────────────────────────────────────────────────────────────────────────

_SELECT_CLAIMABLE_SQL = """
    SELECT event_id FROM agent_purchase_events
     WHERE status = 'pending'
       AND (claimed_at IS NULL OR claimed_at < clock_timestamp() - (:lease_seconds * INTERVAL '1 second'))
     ORDER BY created_at, event_id
     LIMIT :limit
"""
_SELECT_CLAIMABLE_SQL_SQLITE = """
    SELECT event_id FROM agent_purchase_events
     WHERE status = 'pending'
       AND (claimed_at IS NULL OR claimed_at < datetime('now', '-' || :lease_seconds || ' seconds'))
     ORDER BY created_at, event_id
     LIMIT :limit
"""

#: THE claim. Conditional on the row still being pending and unclaimed (or its lease expired), so
#: of two workers racing for one row exactly one gets it back. A claim is NOT an attempt: only a
#: hand-over that actually reached the sender and failed counts (`release(..., burn=True)`), so an
#: event claimed by a tick that was cut short does not drift toward `abandoned`.
_CLAIM_SQL = """
    UPDATE agent_purchase_events
       SET claimed_by = :worker, claimed_at = clock_timestamp()
     WHERE event_id = :event_id
       AND status = 'pending'
       AND (claimed_at IS NULL OR claimed_at < clock_timestamp() - (:lease_seconds * INTERVAL '1 second'))
    RETURNING event_id, purchase_id, agent_id, rail, rail_state, unified_state, event_type, payload,
              attempts, created_at
"""
_CLAIM_SQL_SQLITE = """
    UPDATE agent_purchase_events
       SET claimed_by = :worker, claimed_at = CURRENT_TIMESTAMP
     WHERE event_id = :event_id
       AND status = 'pending'
       AND (claimed_at IS NULL OR claimed_at < datetime('now', '-' || :lease_seconds || ' seconds'))
    RETURNING event_id, purchase_id, agent_id, rail, rail_state, unified_state, event_type, payload,
              attempts, created_at
"""

#: Fenced on the holder: a worker whose lease was taken over cannot overwrite the new holder.
_FINISH_SQL = """
    UPDATE agent_purchase_events
       SET status = :status, delivery_id = :delivery_id, delivery_status = :delivery_status,
           skip_reason = :skip_reason, handed_over_at = clock_timestamp(),
           claimed_by = NULL, claimed_at = NULL
     WHERE event_id = :event_id AND claimed_by = :worker AND status = 'pending'
    RETURNING event_id
"""
_FINISH_SQL_SQLITE = _FINISH_SQL.replace("clock_timestamp()", "CURRENT_TIMESTAMP")

_RELEASE_SQL = """
    UPDATE agent_purchase_events
       SET claimed_by = NULL, attempts = attempts + :burn,
           claimed_at = CASE WHEN :burn = 1 THEN claimed_at ELSE NULL END,
           status = CASE WHEN attempts + :burn >= :max_attempts THEN 'abandoned' ELSE status END
     WHERE event_id = :event_id AND claimed_by = :worker AND status = 'pending'
    RETURNING event_id, status
"""


def _event(row: Any) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    out = dict(row)
    out["payload"] = _decode_json(out.get("payload"))
    if "created_at" in out:
        out["created_at"] = _decode_dt(out["created_at"])
    return out


async def claim_pending(*, worker: str, limit: int = 50, lease_seconds: int = 300) -> List[Dict[str, Any]]:
    """Claim up to `limit` pending events for hand-over. Rows another worker holds are skipped."""
    params = {"limit": max(1, min(500, int(limit))), "lease_seconds": int(lease_seconds)}
    if IS_POSTGRES:
        ids = await database.fetch_all(_SELECT_CLAIMABLE_SQL, params)
    else:
        ids = await database.fetch_all(_SELECT_CLAIMABLE_SQL_SQLITE, params)
    claimed: List[Dict[str, Any]] = []
    for row in ids:
        values = {"event_id": row["event_id"], "worker": worker, "lease_seconds": int(lease_seconds)}
        if IS_POSTGRES:
            got = await database.fetch_one(_CLAIM_SQL, values)
        else:
            got = await database.fetch_one(_CLAIM_SQL_SQLITE, values)
        if got is not None:
            claimed.append(_event(got))
    return claimed


async def finish(
    event_id: str,
    *,
    worker: str,
    status: str,
    delivery_id: Optional[str] = None,
    delivery_status: Optional[str] = None,
    skip_reason: Optional[str] = None,
) -> bool:
    """Record the hand-over outcome. False when this worker no longer holds the claim."""
    if status not in ("handed_over", "skipped"):
        raise ValueError(f"finish status must be handed_over or skipped, got {status!r}")
    values = {
        "event_id": event_id, "worker": worker, "status": status, "delivery_id": delivery_id,
        "delivery_status": delivery_status, "skip_reason": skip_reason,
    }
    if IS_POSTGRES:
        row = await database.fetch_one(_FINISH_SQL, values)
    else:
        row = await database.fetch_one(_FINISH_SQL_SQLITE, values)
    return row is not None


async def release(event_id: str, *, worker: str, max_attempts: int = 10, burn: bool = True) -> Optional[str]:
    """Give the claim back. `burn` counts a failed hand-over as an attempt and KEEPS `claimed_at`,
    so the event is next claimable one lease later (a back-off, not a hot loop on a failing
    receiver); past `max_attempts` it is abandoned (visible, never silently dropped). `burn=False`
    returns an event that was never tried (delivery switched off, tick cut short), claimable at
    once. Returns the resulting status, or None if this worker does not hold it."""
    row = await database.fetch_one(
        _RELEASE_SQL,
        {"event_id": event_id, "worker": worker, "max_attempts": int(max_attempts), "burn": 1 if burn else 0},
    )
    return str(row["status"]) if row else None


# ── detection reads (per rail) ───────────────────────────────────────────────────────────────

#: Reap purchases whose CURRENT state warrants an event and that have none for that state yet.
#:
#: Bounded by a lookback on `state_entered_at` (when the row entered its current state; claims,
#: releases and requeues move `updated_at` but never this), so turning the dial on does not replay
#: events for purchases that settled long ago. A pause LONGER than the lookback drops the events
#: of purchases that changed state during it (documented; get_order stays authoritative).
#:
#: The two action states are candidates only while the page the buyer must open is attached and
#: not past its deadline (the same conditions the view applies, so a row the view would answer
#: "no page" for is not re-read every tick and cannot crowd newer purchases out of the LIMIT).
#: The state list must equal the rail states `EVENT_FOR_STATE` maps; a test pins it.
_REAP_CANDIDATES_SQL = """
    SELECT p.id, p.rail, p.executor, p.rail_purchase_id, p.agent_id, p.agent_user_ref_hash,
           p.routing_plan, p.created_at, r.state AS rail_state
      FROM agent_purchases p
      JOIN reap_agentic_purchases r ON r.id = p.rail_purchase_id
     WHERE p.rail = 'reap'
       AND p.agent_id IS NOT NULL
       AND p.agent_user_ref_hash IS NOT NULL
       AND r.state IN ('needs_enrollment', 'awaiting_approval', 'completed', 'failed', 'refused', 'expired')
       AND r.state_entered_at > clock_timestamp() - (:lookback_hours * INTERVAL '1 hour')
       AND (r.state NOT IN ('needs_enrollment', 'awaiting_approval') OR (
            r.hosted_url IS NOT NULL
            AND (r.hosted_url_expires_at IS NULL OR r.hosted_url_expires_at > clock_timestamp())
            AND (r.state <> 'needs_enrollment' OR r.hosted_url_expires_at IS NOT NULL)
            AND (r.state <> 'awaiting_approval' OR r.reap_quote_expires_at IS NULL
                 OR r.reap_quote_expires_at > clock_timestamp())
       ))
       AND NOT EXISTS (
            SELECT 1 FROM agent_purchase_events e
             WHERE e.purchase_id = p.id AND e.rail_state = r.state
           )
     ORDER BY r.state_entered_at, p.id
     LIMIT :limit
"""
_REAP_CANDIDATES_SQL_SQLITE = """
    SELECT p.id, p.rail, p.executor, p.rail_purchase_id, p.agent_id, p.agent_user_ref_hash,
           p.routing_plan, p.created_at, r.state AS rail_state
      FROM agent_purchases p
      JOIN reap_agentic_purchases r ON r.id = p.rail_purchase_id
     WHERE p.rail = 'reap'
       AND p.agent_id IS NOT NULL
       AND p.agent_user_ref_hash IS NOT NULL
       AND r.state IN ('needs_enrollment', 'awaiting_approval', 'completed', 'failed', 'refused', 'expired')
       AND r.state_entered_at > datetime('now', '-' || :lookback_hours || ' hours')
       AND (r.state NOT IN ('needs_enrollment', 'awaiting_approval') OR (
            r.hosted_url IS NOT NULL
            AND (r.hosted_url_expires_at IS NULL OR r.hosted_url_expires_at > CURRENT_TIMESTAMP)
            AND (r.state <> 'needs_enrollment' OR r.hosted_url_expires_at IS NOT NULL)
            AND (r.state <> 'awaiting_approval' OR r.reap_quote_expires_at IS NULL
                 OR r.reap_quote_expires_at > CURRENT_TIMESTAMP)
       ))
       AND NOT EXISTS (
            SELECT 1 FROM agent_purchase_events e
             WHERE e.purchase_id = p.id AND e.rail_state = r.state
           )
     ORDER BY r.state_entered_at, p.id
     LIMIT :limit
"""


async def reap_candidates(*, lookback_hours: int = 24, limit: int = 200) -> List[Dict[str, Any]]:
    params = {"lookback_hours": max(1, int(lookback_hours)), "limit": max(1, min(1000, int(limit)))}
    if IS_POSTGRES:
        rows = await database.fetch_all(_REAP_CANDIDATES_SQL, params)
    else:
        rows = await database.fetch_all(_REAP_CANDIDATES_SQL_SQLITE, params)
    out = []
    for row in rows:
        item = dict(row)
        item["routing_plan"] = _decode_json(item.get("routing_plan"))
        item["created_at"] = _decode_dt(item.get("created_at"))
        out.append(item)
    return out
