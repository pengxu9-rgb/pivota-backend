"""
Accounts & Orders - customer-facing account tables.

These tables are intentionally separate from the legacy `users` table that
powers employee / merchant / agent logins, to avoid coupling and migration
complexity. They are used by the Accounts & Orders API (auth + orders).
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import (
    Table,
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    Float,
    Text,
    JSON,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from db.auth_identity import upsert_membership
from db.database import metadata, database


logger = logging.getLogger("accounts")


# ---------------------------------------------------------------------------
# Core account tables
# ---------------------------------------------------------------------------

shop_users = Table(
    "shop_users",
    metadata,
    Column("id", String(50), primary_key=True),
    Column("email", String(255), unique=True, nullable=False),
    Column("email_normalized", String(255), unique=True, nullable=False, index=True),
    Column("phone", String(32), nullable=True),
    Column("primary_role", String(50), nullable=False, server_default="customer"),
    Column("is_guest", Boolean, nullable=False, server_default="false"),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), onupdate=func.now()),
)


shop_user_memberships = Table(
    "shop_user_memberships",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", String(50), nullable=False),
    Column("merchant_id", String(50), nullable=False),
    Column("role", String(50), nullable=False),  # customer | merchant_staff | admin
    Column("permissions", JSON, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), onupdate=func.now()),
    UniqueConstraint("user_id", "merchant_id", name="uq_shop_user_memberships_user_merchant"),
)

shop_user_passwords = Table(
    "shop_user_passwords",
    metadata,
    Column("user_id", String(50), primary_key=True),
    # bcrypt hashes include salt and cost; keep as string.
    Column("password_hash", String(255), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), onupdate=func.now()),
)


shop_login_otps = Table(
    "shop_login_otps",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("channel", String(10), nullable=False),  # email | sms
    Column("email_normalized", String(255), nullable=True, index=True),
    Column("phone", String(32), nullable=True, index=True),
    Column("otp_code", String(12), nullable=False),
    Column("ip_address", String(45), nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("consumed_at", DateTime(timezone=True), nullable=True),
    Column("attempt_count", Integer, nullable=False, server_default="0"),
    Column("max_attempts", Integer, nullable=False, server_default="5"),
)


public_order_lookup_logs = Table(
    "public_order_lookup_logs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ip_address", String(45), nullable=True, index=True),
    Column("email_normalized", String(255), nullable=True, index=True),
    Column("order_id", String(50), nullable=True, index=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

shop_browse_history_events = Table(
    "shop_browse_history_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", String(50), nullable=False, index=True),
    Column("product_id", String(255), nullable=False, index=True),
    Column("merchant_id", String(255), nullable=True, index=True),
    Column("title", Text, nullable=True),
    Column("price", Float, nullable=True),
    Column("currency", String(16), nullable=True),
    Column("image_url", Text, nullable=True),
    Column("description", Text, nullable=True),
    Column("brand", Text, nullable=True),
    Column("category", Text, nullable=True),
    Column("product_type", Text, nullable=True),
    Column("viewed_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)


# ---------------------------------------------------------------------------
# Lightweight helpers (used by Accounts & Orders API)
# ---------------------------------------------------------------------------

def normalize_email(email: str) -> str:
    """Lowercase + trim email for consistent lookups."""
    return email.strip().lower()


async def sync_customer_auth_membership(
    user_row: dict,
    *,
    password_hash: Optional[str] = None,
) -> Optional[dict]:
    """
    Link a shop user to the canonical identity graph.

    This is best-effort during the transition so accounts auth does not go down
    if the additive auth_identity migration has not been applied yet.
    """
    try:
        user = dict(user_row or {})
        user_id = str(user.get("id") or "").strip()
        email = normalize_email(str(user.get("email") or user.get("email_normalized") or ""))
        if not user_id or not email:
            return None
        return await upsert_membership(
            email=email,
            membership_type="customer",
            role=str(user.get("primary_role") or "customer"),
            entity_id=user_id,
            status="inactive" if bool(user.get("is_guest")) else "active",
            full_name=None,
            password_hash=password_hash,
            credential_source="accounts_password" if password_hash else None,
            source="accounts_shop_user_sync",
        )
    except Exception as exc:
        logger.warning("[Accounts] Canonical customer membership sync skipped: %s", exc)
        return None


async def create_or_get_shop_user(email: str, phone: Optional[str] = None) -> dict:
    """
    Idempotently create a shop user for the given email.

    This does not touch the legacy `users` table.
    """
    norm = normalize_email(email)
    existing = await database.fetch_one(
        shop_users.select().where(shop_users.c.email_normalized == norm)
    )
    if existing:
        existing_dict = dict(existing)
        await sync_customer_auth_membership(existing_dict)
        return existing_dict

    import secrets

    user_id = f"u_{secrets.token_hex(8)}"
    now = datetime.utcnow()

    await database.execute(
        shop_users.insert().values(
            id=user_id,
            email=email,
            email_normalized=norm,
            phone=phone,
            primary_role="customer",
            is_guest=False,
            created_at=now,
            updated_at=now,
        )
    )

    created = await database.fetch_one(
        shop_users.select().where(shop_users.c.id == user_id)
    )
    user_dict = dict(created) if created else {
        "id": user_id,
        "email": email,
        "email_normalized": norm,
        "phone": phone,
        "primary_role": "customer",
        "is_guest": False,
    }
    await sync_customer_auth_membership(user_dict)
    return user_dict


async def record_public_lookup(ip: str, email_norm: str, order_id: str) -> None:
    """Insert a lightweight log row for public lookup rate limiting."""
    await database.execute(
        public_order_lookup_logs.insert().values(
            ip_address=ip,
            email_normalized=email_norm,
            order_id=order_id,
        )
    )


async def count_recent_public_lookup_by_ip(ip: str, window_seconds: int = 60) -> int:
    """Count recent lookups from a single IP."""
    since = datetime.utcnow() - timedelta(seconds=window_seconds)
    query = (
        public_order_lookup_logs.select()
        .with_only_columns(public_order_lookup_logs.c.id)
        .where(
            (public_order_lookup_logs.c.ip_address == ip)
            & (public_order_lookup_logs.c.created_at >= since)
        )
    )
    rows = await database.fetch_all(query)
    return len(rows)


async def count_recent_public_lookup_by_key(
    email_norm: str, order_id: str, window_seconds: int = 60
) -> int:
    """Count recent lookups for a specific (email, order_id) pair."""
    since = datetime.utcnow() - timedelta(seconds=window_seconds)
    query = (
        public_order_lookup_logs.select()
        .with_only_columns(public_order_lookup_logs.c.id)
        .where(
            (public_order_lookup_logs.c.email_normalized == email_norm)
            & (public_order_lookup_logs.c.order_id == order_id)
            & (public_order_lookup_logs.c.created_at >= since)
        )
    )
    rows = await database.fetch_all(query)
    return len(rows)
