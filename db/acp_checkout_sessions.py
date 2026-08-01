"""
ACP checkout sessions: backend-owned persistence for the in-process ACP
checkout-session layer (services/acp_checkout_session_service), replacing the
retired external pivota-acp service's `checkout_sessions` table.

Note: Migration 191 creates this table in production; the service also
self-heals the same DDL at runtime (raw DDL first, then this metadata for
SQLite dev/tests — same pattern as db/checkout_intents.py). Columns are Text on
purpose: the raw DDL is TEXT, and a VARCHAR(n) here could create a NARROWER
type than the migration if the SQLAlchemy path ever won a self-heal race.
"""

from __future__ import annotations

from sqlalchemy import Column, DateTime, Index, Integer, Table, Text
from sqlalchemy.sql import func

from db.database import JSONB_TYPE, metadata


acp_checkout_sessions = Table(
    "acp_checkout_sessions",
    metadata,
    Column("id", Text, primary_key=True),
    Column("merchant_id", Text, nullable=False),
    # The agent identity that minted the session; completion requires the same
    # agent (service layer enforces it) in addition to the merchant check.
    Column("agent_id", Text, nullable=True),
    Column("platform", Text, nullable=True),
    # ready_for_payment -> completing (claimed by exactly one completion
    # attempt) -> completed. A capture failure reverts to ready_for_payment.
    Column("status", Text, nullable=False, server_default="ready_for_payment"),
    Column("buyer", JSONB_TYPE, nullable=True),
    Column("items", JSONB_TYPE, nullable=False),
    Column("fulfillment_address", JSONB_TYPE, nullable=True),
    Column("quote", JSONB_TYPE, nullable=True),
    Column("metadata", JSONB_TYPE, nullable=True),
    Column("currency", Text, nullable=True),
    Column("total_cents", Integer, nullable=True),
    # Persisted IMMEDIATELY after order creation (before capture): a retry must
    # reuse this order, never mint another.
    Column("order_id", Text, nullable=True),
    # Stored completion result for idempotent replay of /complete on an
    # already-completed session (the money path must never re-charge).
    Column("completion", JSONB_TYPE, nullable=True),
    # Caller-supplied completion idempotency key — recorded/validated only.
    # The PSP idempotency key is DERIVED from the session id, deliberately NOT
    # from this value, so a retry with a fresh key cannot double-charge.
    Column("idempotency_key", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("expires_at", DateTime(timezone=True), nullable=True),
)


Index(
    "idx_acp_checkout_sessions_merchant_created",
    acp_checkout_sessions.c.merchant_id,
    acp_checkout_sessions.c.created_at,
)
# Plain lookup index (NOT unique): the service answers cross-session key reuse
# with a SELECT + 409 idempotency_conflict BEFORE any charge. The earlier
# partial UNIQUE index was wrong for this money path — a cross-session key
# collision would have aborted the completion UPDATE only AFTER the charge,
# wedging the session in a retry-recharge loop.
Index(
    "idx_acp_checkout_sessions_merchant_idem",
    acp_checkout_sessions.c.merchant_id,
    acp_checkout_sessions.c.idempotency_key,
)
