from __future__ import annotations

from sqlalchemy import (
    BigInteger,
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
    Column("commerce_surface", String(64), nullable=True, index=True),
    Column("canonical_product_id", String(64), nullable=True, index=True),
    Column("canonical_variant_id", String(64), nullable=True, index=True),
    Column("prompt_cluster", String(128), nullable=True, index=True),
    Column("rule_id", String(64), nullable=True),
    Column("job_id", String(128), nullable=True),
    Column("session_id", String(128), nullable=True),
    Column("source_channel", String(128), nullable=True, index=True),
    Column("source_family", String(64), nullable=True, index=True),
    Column("query_source", String(128), nullable=True, index=True),
    Column("agent_id", String(64), nullable=True, index=True),
    Column("protocol_name", String(64), nullable=True, index=True),
    Column("llm_provider", String(64), nullable=True, index=True),
    Column("llm_model", String(128), nullable=True, index=True),
    Column("caller_id", String(128), nullable=True, index=True),
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
    Column("commerce_surface", String(64), nullable=True, index=True),
    Column("canonical_product_id", String(64), nullable=True, index=True),
    Column("canonical_variant_id", String(64), nullable=True, index=True),
    Column("prompt_cluster", String(128), nullable=True, index=True),
    Column("source_channel", String(128), nullable=True, index=True),
    Column("source_family", String(64), nullable=True, index=True),
    Column("query_source", String(128), nullable=True, index=True),
    Column("agent_id", String(64), nullable=True, index=True),
    Column("protocol_name", String(64), nullable=True, index=True),
    Column("llm_provider", String(64), nullable=True, index=True),
    Column("llm_model", String(128), nullable=True, index=True),
    Column("caller_id", String(128), nullable=True, index=True),
    Column("latest_refund_id", String(64), nullable=True),
    Column("refund_ids", JSONB_TYPE, nullable=True),
    Column("refund_count", Integer, nullable=False, default=0),
    Column("refunded_amount", Numeric(10, 2), nullable=False, default=0),
    Column("checkout_started_at", DateTime(timezone=True), nullable=True),
    Column("latest_refund_at", DateTime(timezone=True), nullable=True),
    Column("metadata", JSONB_TYPE, nullable=True),
    # T2-2 external-conversion representation (migration 167). Additive + nullable:
    # the internal/PSP path leaves these NULL/default → behavior byte-identical.
    # `gross_attributed_gmv_cents` predates this (migration 109); declared here so
    # the external closure path can read/write it through the ORM.
    Column("state", Text, nullable=True),
    Column("converted_at", DateTime(timezone=True), nullable=True),
    Column("gross_attributed_gmv_cents", BigInteger, nullable=True),
    Column("currency", Text, nullable=True),
    Column("external_order_id", Text, nullable=True),
    Column("source", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False),
)

Index(
    "idx_commerce_attribution_edges_order",
    commerce_attribution_edges.c.order_id,
    unique=True,
)

# T2-2 idempotency guard: one edge per (merchant, external Shopify order).
# Internal edges keep external_order_id = NULL (distinct under Postgres
# multi-column NULL semantics) so this never constrains the internal path.
Index(
    "uq_commerce_attribution_edges_external_order",
    commerce_attribution_edges.c.merchant_id,
    commerce_attribution_edges.c.external_order_id,
    unique=True,
)
