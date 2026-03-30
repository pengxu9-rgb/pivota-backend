from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Table
from sqlalchemy.sql import func

from db.database import JSONB_TYPE, metadata


merchant_commerce_readiness_state = Table(
    "merchant_commerce_readiness_state",
    metadata,
    Column("merchant_id", String(50), primary_key=True),
    Column("primary_platform", String(32), nullable=True, index=True),
    Column("active_psp", String(64), nullable=True),
    Column("foundation_status", String(32), nullable=False, index=True),
    Column("discover_status", String(32), nullable=False, index=True),
    Column("signals_status", String(32), nullable=False, index=True),
    Column("execute_status", String(32), nullable=False, index=True),
    Column("foundation_blockers", JSONB_TYPE, nullable=True),
    Column("discover_blockers", JSONB_TYPE, nullable=True),
    Column("signals_blockers", JSONB_TYPE, nullable=True),
    Column("execute_blockers", JSONB_TYPE, nullable=True),
    Column("surfaced_exposure_supported", Boolean, nullable=False, default=False),
    Column("first_store_connected_at", DateTime(timezone=True), nullable=True),
    Column("first_catalog_synced_at", DateTime(timezone=True), nullable=True),
    Column("first_discover_ready_at", DateTime(timezone=True), nullable=True),
    Column("days_to_discover_ready", Integer, nullable=True),
    Column("observed_at", DateTime(timezone=True), nullable=False, server_default=func.now(), index=True),
    Column("metadata", JSONB_TYPE, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False),
)
