"""
ACP checkout sessions: backend-owned persistence for the in-process ACP
checkout-session layer (services/acp_checkout_session_service), replacing the
retired external pivota-acp service's `checkout_sessions` table.

Note: Migration 191 creates this table in production. We also define it in
SQLAlchemy metadata so local SQLite dev/tests work without running raw Postgres
migrations (same pattern as db/checkout_intents.py).
"""

from __future__ import annotations

from sqlalchemy import Column, DateTime, Index, Integer, String, Table
from sqlalchemy.sql import func

from db.database import JSONB_TYPE, metadata


acp_checkout_sessions = Table(
    "acp_checkout_sessions",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("merchant_id", String(100), nullable=False),
    Column("platform", String(40), nullable=True),
    Column("status", String(40), nullable=False, server_default="ready_for_payment"),
    Column("buyer", JSONB_TYPE, nullable=True),
    Column("items", JSONB_TYPE, nullable=False),
    Column("fulfillment_address", JSONB_TYPE, nullable=True),
    Column("quote", JSONB_TYPE, nullable=True),
    Column("metadata", JSONB_TYPE, nullable=True),
    Column("currency", String(10), nullable=True),
    Column("total_cents", Integer, nullable=True),
    Column("order_id", String(100), nullable=True),
    # Stored completion result for idempotent replay of /complete on an
    # already-completed session (the money path must never re-charge).
    Column("completion", JSONB_TYPE, nullable=True),
    Column("idempotency_key", String(200), nullable=True),
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
# One completion per (merchant, idempotency_key); partial so key-less sessions
# never collide. Works on both Postgres and SQLite (both support partial
# unique indexes via the dialect-specific *_where kwargs).
Index(
    "uq_acp_checkout_sessions_merchant_idem",
    acp_checkout_sessions.c.merchant_id,
    acp_checkout_sessions.c.idempotency_key,
    unique=True,
    postgresql_where=acp_checkout_sessions.c.idempotency_key.isnot(None),
    sqlite_where=acp_checkout_sessions.c.idempotency_key.isnot(None),
)
