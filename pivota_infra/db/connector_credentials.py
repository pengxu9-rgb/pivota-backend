"""
Connector Credentials Store - EPIC‑3

Per-merchant, per-connector credential storage with encrypted payloads.
This module is additive and does not modify any existing v1 flows.
"""

from sqlalchemy import Table, Column, Integer, String, DateTime, Boolean, Text, JSON
from sqlalchemy.sql import func
from db.database import metadata, database
from typing import Dict, Any, Optional, List
from datetime import datetime

connector_credentials = Table(
    "connector_credentials",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("merchant_id", String(50), nullable=False, index=True),
    Column("connector", String(50), nullable=False, index=True),
    Column("credential_label", String(100), nullable=True),
    Column("credentials_encrypted", Text, nullable=False),
    Column("is_valid", Boolean, nullable=False, server_default="true"),
    Column("last_validation_result", JSON, nullable=True),
    Column("last_validated_at", DateTime, nullable=True),
    Column("last_used_at", DateTime, nullable=True),
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime, server_default=func.now(), onupdate=func.now()),
)


async def create_connector_credentials(
    merchant_id: str,
    connector: str,
    credentials_encrypted: str,
    credential_label: Optional[str] = None,
    last_validation_result: Optional[Dict[str, Any]] = None,
    last_validated_at: Optional[datetime] = None,
) -> int:
    """
    Create a new connector_credentials row and return its ID.

    The credentials_encrypted payload should already be encrypted by the caller.
    """
    values: Dict[str, Any] = {
        "merchant_id": merchant_id,
        "connector": connector,
        "credential_label": credential_label,
        "credentials_encrypted": credentials_encrypted,
        "is_valid": True,
        "last_validation_result": last_validation_result,
        "last_validated_at": last_validated_at,
    }
    query = connector_credentials.insert().values(**values)
    cred_id = await database.execute(query)
    return int(cred_id)


async def get_connector_credential(credential_id: int) -> Optional[Dict[str, Any]]:
    """Fetch a single connector credential by ID."""
    query = connector_credentials.select().where(connector_credentials.c.id == credential_id)
    row = await database.fetch_one(query)
    return dict(row) if row else None


async def get_latest_connector_credential_for_merchant(
    merchant_id: str,
    connector: str,
) -> Optional[Dict[str, Any]]:
    """
    Fetch the most recently created valid credential for a merchant/connector pair.
    """
    query = (
        connector_credentials.select()
        .where(
            (connector_credentials.c.merchant_id == merchant_id)
            & (connector_credentials.c.connector == connector)
            & (connector_credentials.c.is_valid.is_(True))
        )
        .order_by(connector_credentials.c.created_at.desc())
        .limit(1)
    )
    row = await database.fetch_one(query)
    return dict(row) if row else None


async def mark_credential_used(credential_id: int) -> bool:
    """Update last_used_at for a credential."""
    query = (
        connector_credentials.update()
        .where(connector_credentials.c.id == credential_id)
        .values(last_used_at=datetime.utcnow(), updated_at=datetime.utcnow())
    )
    await database.execute(query)
    return True


async def list_connector_credentials_for_merchant(
    merchant_id: str,
    connector: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    List connector credentials for a merchant, optionally filtered by connector.
    Newest first.
    """
    query = connector_credentials.select().where(connector_credentials.c.merchant_id == merchant_id)
    if connector:
        query = query.where(connector_credentials.c.connector == connector)
    query = query.order_by(connector_credentials.c.created_at.desc())
    rows = await database.fetch_all(query)
    return [dict(r) for r in rows]
