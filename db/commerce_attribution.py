from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    Text,
)
from sqlalchemy.sql import func

from db.database import JSONB_TYPE, metadata


surface_click_events = Table(
    "surface_click_events",
    metadata,
    Column("click_id", String(64), primary_key=True),
    Column("merchant_id", String(50), nullable=True, index=True),
    Column("interaction_id", String(64), nullable=True, index=True),
    Column("surface", String(64), nullable=False, index=True),
    Column("canonical_product_id", String(64), nullable=True, index=True),
    Column("canonical_variant_id", String(64), nullable=True, index=True),
    Column("prompt_cluster", String(128), nullable=True, index=True),
    Column("rule_id", String(64), nullable=True),
    Column("job_id", String(128), nullable=True),
    Column("session_id", String(128), nullable=True),
    Column("destination_url", Text, nullable=True),
    Column("dest_domain", String(256), nullable=True),
    Column("impression_count", Integer, nullable=False, default=0),
    Column("click_count", Integer, nullable=False, default=0),
    Column("first_impression_at", DateTime(timezone=True), nullable=True),
    Column("last_impression_at", DateTime(timezone=True), nullable=True),
    Column("first_click_at", DateTime(timezone=True), nullable=True),
    Column("last_click_at", DateTime(timezone=True), nullable=True),
    Column("user_agent", Text, nullable=True),
    Column("ip", String(64), nullable=True),
    Column("context", JSONB_TYPE, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False),
)

Index(
    "idx_surface_click_events_surface_created",
    surface_click_events.c.surface,
    surface_click_events.c.created_at,
)


commerce_attribution_edges = Table(
    "commerce_attribution_edges",
    metadata,
    Column("edge_id", String(64), primary_key=True),
    Column("merchant_id", String(50), nullable=False, index=True),
    Column("interaction_id", String(64), nullable=True, index=True),
    Column("click_id", String(64), nullable=True, index=True),
    Column("order_id", String(50), nullable=False, index=True),
    Column("surface", String(64), nullable=True, index=True),
    Column("canonical_product_id", String(64), nullable=True, index=True),
    Column("canonical_variant_id", String(64), nullable=True, index=True),
    Column("prompt_cluster", String(128), nullable=True, index=True),
    Column("latest_refund_id", String(64), nullable=True),
    Column("refund_ids", JSONB_TYPE, nullable=True),
    Column("refund_count", Integer, nullable=False, default=0),
    Column("refunded_amount", Numeric(10, 2), nullable=False, default=0),
    Column("checkout_started_at", DateTime(timezone=True), nullable=True),
    Column("latest_refund_at", DateTime(timezone=True), nullable=True),
    Column("metadata", JSONB_TYPE, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False),
)

Index(
    "idx_commerce_attribution_edges_order",
    commerce_attribution_edges.c.order_id,
    unique=True,
)
