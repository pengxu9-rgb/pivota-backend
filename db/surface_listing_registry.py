from __future__ import annotations

from sqlalchemy import Column, DateTime, Index, String, Table
from sqlalchemy.sql import func

from db.database import JSONB_TYPE, metadata


surface_listing_states = Table(
    "surface_listing_states",
    metadata,
    Column("listing_id", String(64), primary_key=True),
    Column("merchant_id", String(50), nullable=False, index=True),
    Column("interaction_id", String(64), nullable=True, index=True),
    Column("surface", String(32), nullable=False, index=True),
    Column("listing_key", String(255), nullable=False),
    Column("canonical_product_id", String(64), nullable=True, index=True),
    Column("canonical_variant_id", String(64), nullable=True, index=True),
    Column("status", String(32), nullable=False, index=True),
    Column("last_exported_at", DateTime(timezone=True), nullable=True),
    Column("last_indexed_at", DateTime(timezone=True), nullable=True),
    Column("last_tradeable_at", DateTime(timezone=True), nullable=True),
    Column("latest_receipt_id", String(64), nullable=True),
    Column("latest_error_id", String(64), nullable=True),
    Column("metadata", JSONB_TYPE, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False),
)

Index(
    "idx_surface_listing_states_lookup",
    surface_listing_states.c.merchant_id,
    surface_listing_states.c.surface,
    surface_listing_states.c.listing_key,
    unique=True,
)


surface_listing_receipts = Table(
    "surface_listing_receipts",
    metadata,
    Column("receipt_id", String(64), primary_key=True),
    Column("listing_id", String(64), nullable=False, index=True),
    Column("merchant_id", String(50), nullable=False, index=True),
    Column("surface", String(32), nullable=False, index=True),
    Column("listing_key", String(255), nullable=False),
    Column("status", String(32), nullable=False),
    Column("payload", JSONB_TYPE, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)


surface_listing_errors = Table(
    "surface_listing_errors",
    metadata,
    Column("error_id", String(64), primary_key=True),
    Column("listing_id", String(64), nullable=False, index=True),
    Column("merchant_id", String(50), nullable=False, index=True),
    Column("surface", String(32), nullable=False, index=True),
    Column("listing_key", String(255), nullable=False),
    Column("error_code", String(128), nullable=False),
    Column("error_message", String(1024), nullable=True),
    Column("payload", JSONB_TYPE, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)
