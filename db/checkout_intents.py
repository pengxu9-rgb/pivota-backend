"""
Checkout intents: short-lived checkout context used by shared checkout UI.

Design:
- Stores optional shipping/email prefill server-side (PII never goes into URLs).
- Binds prefill access to the originally minted checkout token hash.
- Supports limited read budget + used/expired enforcement.

Note: Migrations create this table in production. We also define it in SQLAlchemy
metadata so local SQLite dev can work without running raw Postgres migrations.
"""

from __future__ import annotations

from sqlalchemy import DateTime, Index, Integer, String, Table, Column
from sqlalchemy.sql import func

from db.database import JSONB_TYPE, metadata


checkout_intents = Table(
    "checkout_intents",
    metadata,
    Column("intent_id", String(80), primary_key=True),
    Column("agent_id", String(100), nullable=False),
    Column("buyer_ref", String(255), nullable=True),
    Column("agent_user_ref", String(255), nullable=True),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("prefill", JSONB_TYPE, nullable=True),
    Column("requested_scopes", JSONB_TYPE, nullable=True),
    Column("checkout_token_hash", String(64), nullable=True),
    Column("prefill_read_count", Integer, nullable=False, server_default="0"),
    Column("prefill_last_read_at", DateTime(timezone=True), nullable=True),
    Column("linked_buyer_id", String(50), nullable=True),
    Column("used_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now()),
)


Index("idx_checkout_intents_agent_buyer", checkout_intents.c.agent_id, checkout_intents.c.buyer_ref)
Index("idx_checkout_intents_expires_at", checkout_intents.c.expires_at)
Index("idx_checkout_intents_expires_used", checkout_intents.c.expires_at, checkout_intents.c.used_at)
Index("idx_checkout_intents_linked_buyer_created", checkout_intents.c.linked_buyer_id, checkout_intents.c.created_at)

