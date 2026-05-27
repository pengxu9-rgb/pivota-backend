"""
Canonical identity + scoped membership helpers.

This layer is intentionally additive. Legacy tables (`users`, `employees`, and
`shop_users`) continue to exist while login decisions move toward one identity
with explicit portal memberships.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import Column, DateTime, Index, String, Table, Text, UniqueConstraint
from sqlalchemy.sql import func

from db.database import JSONB_TYPE, database, metadata
from utils.auth import verify_password


AUTH_MEMBERSHIP_TYPES = {"employee", "merchant", "agent", "customer"}
AUTH_MEMBERSHIP_ACTIVE = "active"
PORTAL_TO_MEMBERSHIP_TYPE = {
    "employee": "employee",
    "merchant": "merchant",
    "agent": "agent",
}
PORTAL_TO_AUDIENCE = {
    "employee": "employee-portal",
    "merchant": "merchant-portal",
    "agent": "agent-portal",
    "customer": "accounts",
}


auth_identities = Table(
    "auth_identities",
    metadata,
    Column("identity_id", String(64), primary_key=True),
    Column("email", String(255), nullable=False),
    Column("email_normalized", String(255), nullable=False, unique=True, index=True),
    Column("full_name", String(255), nullable=True),
    Column("status", String(32), nullable=False, default=AUTH_MEMBERSHIP_ACTIVE),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), onupdate=func.now()),
)


auth_credentials = Table(
    "auth_credentials",
    metadata,
    Column("identity_id", String(64), primary_key=True),
    Column("password_hash", String(255), nullable=False),
    Column("source", String(64), nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), onupdate=func.now()),
)


auth_memberships = Table(
    "auth_memberships",
    metadata,
    Column("membership_id", String(64), primary_key=True),
    Column("identity_id", String(64), nullable=False, index=True),
    Column("membership_type", String(32), nullable=False, index=True),
    Column("role", String(64), nullable=False),
    Column("status", String(32), nullable=False, default=AUTH_MEMBERSHIP_ACTIVE, index=True),
    Column("entity_id", String(128), nullable=False),
    Column("permissions", JSONB_TYPE, nullable=True),
    Column("source", String(64), nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), onupdate=func.now()),
    UniqueConstraint(
        "identity_id",
        "membership_type",
        "entity_id",
        name="uq_auth_memberships_identity_type_entity",
    ),
)

Index(
    "idx_auth_memberships_identity_type_status",
    auth_memberships.c.identity_id,
    auth_memberships.c.membership_type,
    auth_memberships.c.status,
)


auth_identity_events = Table(
    "auth_identity_events",
    metadata,
    Column("event_id", String(64), primary_key=True),
    Column("identity_id", String(64), nullable=True, index=True),
    Column("email_normalized", String(255), nullable=True, index=True),
    Column("event_type", String(64), nullable=False),
    Column("details", JSONB_TYPE, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def record_to_dict(record: Any) -> Optional[Dict[str, Any]]:
    if not record:
        return None
    if isinstance(record, dict):
        return dict(record)
    try:
        return dict(record)
    except Exception:
        keys = getattr(record, "keys", lambda: [])()
        return {key: record[key] for key in keys}


def normalize_permissions(raw: Any) -> list:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        import json

        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return [p.strip() for p in raw.split(",") if p.strip()]
    try:
        return list(raw)
    except Exception:
        return []


async def get_identity_by_email(email: str) -> Optional[Dict[str, Any]]:
    normalized = normalize_email(email)
    row = await database.fetch_one(
        auth_identities.select().where(auth_identities.c.email_normalized == normalized)
    )
    return record_to_dict(row)


async def ensure_identity(
    *,
    email: str,
    full_name: Optional[str] = None,
    status: str = AUTH_MEMBERSHIP_ACTIVE,
) -> Dict[str, Any]:
    normalized = normalize_email(email)
    existing = await get_identity_by_email(normalized)
    if existing:
        updates: Dict[str, Any] = {"status": status, "updated_at": datetime.utcnow()}
        if full_name:
            updates["full_name"] = full_name
        await database.execute(
            auth_identities.update()
            .where(auth_identities.c.identity_id == existing["identity_id"])
            .values(**updates)
        )
        refreshed = await get_identity_by_email(normalized)
        return refreshed or existing

    identity_id = f"identity_{secrets.token_hex(12)}"
    now = datetime.utcnow()
    try:
        await database.execute(
            auth_identities.insert().values(
                identity_id=identity_id,
                email=normalized,
                email_normalized=normalized,
                full_name=full_name,
                status=status,
                created_at=now,
                updated_at=now,
            )
        )
    except Exception:
        raced = await get_identity_by_email(normalized)
        if raced:
            return raced
        raise

    created = await get_identity_by_email(normalized)
    return created or {
        "identity_id": identity_id,
        "email": normalized,
        "email_normalized": normalized,
        "full_name": full_name,
        "status": status,
    }


async def upsert_credential_hash(
    *,
    identity_id: str,
    password_hash: str,
    source: str,
) -> None:
    existing = await database.fetch_one(
        auth_credentials.select().where(auth_credentials.c.identity_id == identity_id)
    )
    now = datetime.utcnow()
    if existing:
        await database.execute(
            auth_credentials.update()
            .where(auth_credentials.c.identity_id == identity_id)
            .values(password_hash=password_hash, source=source, updated_at=now)
        )
        return

    await database.execute(
        auth_credentials.insert().values(
            identity_id=identity_id,
            password_hash=password_hash,
            source=source,
            created_at=now,
            updated_at=now,
        )
    )


async def verify_identity_password(email: str, plain_password: str) -> Optional[Dict[str, Any]]:
    identity = await get_identity_by_email(email)
    if not identity or identity.get("status") != AUTH_MEMBERSHIP_ACTIVE:
        return None

    credential = await database.fetch_one(
        auth_credentials.select().where(auth_credentials.c.identity_id == identity["identity_id"])
    )
    credential_dict = record_to_dict(credential)
    if not credential_dict:
        return None
    if not verify_password(plain_password, str(credential_dict.get("password_hash") or "")):
        return None
    return identity


async def upsert_membership(
    *,
    email: str,
    membership_type: str,
    role: str,
    entity_id: str,
    status: str = AUTH_MEMBERSHIP_ACTIVE,
    permissions: Optional[Any] = None,
    full_name: Optional[str] = None,
    password_hash: Optional[str] = None,
    credential_source: Optional[str] = None,
    source: str = "legacy_sync",
) -> Dict[str, Any]:
    membership_type = (membership_type or "").strip().lower()
    if membership_type not in AUTH_MEMBERSHIP_TYPES:
        raise ValueError(f"unsupported membership_type={membership_type}")
    if not entity_id:
        raise ValueError("entity_id is required")

    identity = await ensure_identity(email=email, full_name=full_name)
    if password_hash:
        await upsert_credential_hash(
            identity_id=identity["identity_id"],
            password_hash=password_hash,
            source=credential_source or source,
        )

    normalized_permissions = normalize_permissions(permissions)
    existing = await database.fetch_one(
        auth_memberships.select().where(
            (auth_memberships.c.identity_id == identity["identity_id"])
            & (auth_memberships.c.membership_type == membership_type)
            & (auth_memberships.c.entity_id == str(entity_id))
        )
    )
    now = datetime.utcnow()
    values = {
        "role": role,
        "status": status,
        "permissions": normalized_permissions,
        "source": source,
        "updated_at": now,
    }

    if existing:
        await database.execute(
            auth_memberships.update()
            .where(auth_memberships.c.membership_id == existing["membership_id"])
            .values(**values)
        )
        refreshed = await database.fetch_one(
            auth_memberships.select().where(
                auth_memberships.c.membership_id == existing["membership_id"]
            )
        )
        membership = record_to_dict(refreshed) or record_to_dict(existing) or {}
    else:
        membership_id = f"membership_{secrets.token_hex(12)}"
        await database.execute(
            auth_memberships.insert().values(
                membership_id=membership_id,
                identity_id=identity["identity_id"],
                membership_type=membership_type,
                role=role,
                status=status,
                entity_id=str(entity_id),
                permissions=normalized_permissions,
                source=source,
                created_at=now,
                updated_at=now,
            )
        )
        membership = {
            "membership_id": membership_id,
            "identity_id": identity["identity_id"],
            "membership_type": membership_type,
            "role": role,
            "status": status,
            "entity_id": str(entity_id),
            "permissions": normalized_permissions,
            "source": source,
        }

    membership["identity"] = identity
    return membership


async def get_active_membership_by_email(
    *,
    email: str,
    membership_type: str,
) -> Optional[Dict[str, Any]]:
    identity = await get_identity_by_email(email)
    if not identity or identity.get("status") != AUTH_MEMBERSHIP_ACTIVE:
        return None

    rows = await database.fetch_all(
        auth_memberships.select()
        .where(
            (auth_memberships.c.identity_id == identity["identity_id"])
            & (auth_memberships.c.membership_type == membership_type)
            & (auth_memberships.c.status == AUTH_MEMBERSHIP_ACTIVE)
        )
        .order_by(auth_memberships.c.updated_at.desc())
    )
    for row in rows:
        membership = record_to_dict(row)
        if membership:
            membership["identity"] = identity
            return membership
    return None


async def record_identity_event(
    *,
    event_type: str,
    email: Optional[str] = None,
    identity_id: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    await database.execute(
        auth_identity_events.insert().values(
            event_id=f"auth_event_{secrets.token_hex(12)}",
            identity_id=identity_id,
            email_normalized=normalize_email(email or ""),
            event_type=event_type,
            details=details or {},
            created_at=datetime.utcnow(),
        )
    )
