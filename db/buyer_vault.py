"""
Buyer Vault (Unified Buyer Account) tables + lightweight helpers.

Design goals:
- Store buyer profile data (email is handled by shop_users) + shipping addresses.
- Provide agent-scoped, pairwise buyer_ref mapping (opaque, non-enumerable).
- Store mandate + authorization-token skeletons for future agentic payments.
- Never store any reusable payment method credentials (cards/wallets).
"""

from __future__ import annotations

import base64
import secrets
from typing import Any, Dict, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    Numeric,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from db.database import IS_SQLITE, JSONB_TYPE, database, metadata


_autoincrement_pk_type = Integer if IS_SQLITE else BigInteger


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

buyer_addresses = Table(
    "buyer_addresses",
    metadata,
    Column("id", String(80), primary_key=True),
    Column("buyer_id", String(50), nullable=False, index=True),
    Column("recipient_name", String(255), nullable=True),
    Column("line1", String(255), nullable=False),
    Column("line2", String(255), nullable=True),
    Column("city", String(120), nullable=False),
    Column("region", String(120), nullable=True),
    Column("postal_code", String(32), nullable=False),
    Column("country", String(2), nullable=False),
    Column("phone", String(32), nullable=True),
    Column("is_default", Boolean, nullable=False, server_default="false"),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), onupdate=func.now()),
)


buyer_agent_links = Table(
    "buyer_agent_links",
    metadata,
    # SQLite requires INTEGER PRIMARY KEY for autoincrement; BigInteger breaks local dev.
    Column("id", _autoincrement_pk_type, primary_key=True, autoincrement=True),
    Column("buyer_id", String(50), nullable=False, index=True),
    Column("agent_id", String(100), nullable=False, index=True),
    Column("agent_scoped_buyer_ref", String(128), nullable=False, unique=True, index=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("last_used_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint("buyer_id", "agent_id", name="uq_buyer_agent_links_buyer_agent"),
)


mandates = Table(
    "mandates",
    metadata,
    Column("id", String(80), primary_key=True),
    Column("buyer_id", String(50), nullable=False, index=True),
    Column("agent_id", String(100), nullable=False, index=True),
    Column("status", String(20), nullable=False, index=True),  # active | revoked | expired
    Column("constraints_json", JSONB_TYPE, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=True),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
)


authorization_tokens = Table(
    "authorization_tokens",
    metadata,
    Column("id", String(80), primary_key=True),
    Column("mandate_id", String(80), nullable=False, index=True),
    Column("intent_id", String(80), nullable=True, index=True),
    Column("token_hash", String(128), nullable=False, unique=True, index=True),
    Column("scope_json", JSONB_TYPE, nullable=True),
    Column("amount", Numeric(12, 2), nullable=True),
    Column("currency", String(8), nullable=True),
    Column("expires_at", DateTime(timezone=True), nullable=False, index=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("used_at", DateTime(timezone=True), nullable=True),
)


buyer_audit_logs = Table(
    "buyer_audit_logs",
    metadata,
    Column("id", _autoincrement_pk_type, primary_key=True, autoincrement=True),
    Column("buyer_id", String(50), nullable=True, index=True),
    Column("agent_id", String(100), nullable=True, index=True),
    Column("action", String(120), nullable=False, index=True),
    Column("details", JSONB_TYPE, nullable=True),
    Column("ip_address", String(64), nullable=True),
    Column("user_agent", String(512), nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)


buyer_save_challenges = Table(
    "buyer_save_challenges",
    metadata,
    Column("save_token_hash", String(64), primary_key=True),
    Column("intent_id", String(80), nullable=False, index=True),
    Column("order_id", String(80), nullable=True),
    Column("checkout_token_hash", String(64), nullable=True),
    Column("client_nonce_hash", String(64), nullable=False),
    Column("save_email", Boolean, nullable=False, server_default="true"),
    Column("save_address", Boolean, nullable=False, server_default="true"),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False, index=True),
    Column("redeemed_at", DateTime(timezone=True), nullable=True),
    Column("redeemed_buyer_id", String(50), nullable=True),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base64url_no_pad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def mint_pairwise_buyer_ref() -> str:
    """
    Generate an opaque, non-enumerable buyer_ref suitable for agent-scoped exposure.

    Uses 128 bits of randomness and base64url encoding.
    """
    return _base64url_no_pad(secrets.token_bytes(16))


async def audit_buyer_action(
    *,
    buyer_id: Optional[str],
    action: str,
    agent_id: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> None:
    try:
        await database.execute(
            buyer_audit_logs.insert().values(
                buyer_id=buyer_id,
                agent_id=agent_id,
                action=str(action or "").strip() or "unknown",
                details=details,
                ip_address=ip_address,
                user_agent=(str(user_agent)[:512] if user_agent else None),
            )
        )
    except Exception:
        # Best-effort: never break checkout/user flows on audit failures.
        return
