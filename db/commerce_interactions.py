from __future__ import annotations

from sqlalchemy import Column, DateTime, Index, String, Table, Text
from sqlalchemy.sql import func

from db.database import JSONB_TYPE, metadata


commerce_interactions = Table(
    "commerce_interactions",
    metadata,
    Column("interaction_id", String(64), primary_key=True),
    Column("merchant_id", String(50), nullable=False, index=True),
    Column("platform", String(32), nullable=True, index=True),
    Column("store_id", String(128), nullable=True, index=True),
    Column("surface", String(64), nullable=True, index=True),
    Column("commerce_surface", String(64), nullable=True, index=True),
    Column("prompt_id", String(64), nullable=True, index=True),
    Column("result_id", String(64), nullable=True, index=True),
    Column("click_id", String(64), nullable=True, index=True),
    # The recovery action this interaction can be attributed to, carried in on
    # the attributed link the agent followed. Indexed because the question it
    # answers — "did the fix we recommended produce an order?" — is a lookup
    # by key, and it is the only reason this column exists.
    Column("recovery_key", String(40), nullable=True, index=True),
    Column("cart_id", String(128), nullable=True, index=True),
    Column("quote_id", String(64), nullable=True, index=True),
    Column("checkout_id", String(128), nullable=True, index=True),
    Column("payment_id", String(128), nullable=True, index=True),
    Column("order_id", String(128), nullable=True, index=True),
    Column("refund_id", String(128), nullable=True, index=True),
    Column("return_id", String(128), nullable=True, index=True),
    Column("canonical_product_id", String(64), nullable=True, index=True),
    Column("canonical_variant_id", String(64), nullable=True, index=True),
    Column("trace_id", String(128), nullable=True, index=True),
    Column("brief_id", String(128), nullable=True, index=True),
    Column("session_id", String(128), nullable=True, index=True),
    Column("visitor_id", String(128), nullable=True, index=True),
    Column("buyer_id", String(128), nullable=True),
    Column("source_channel", String(128), nullable=True, index=True),
    Column("source_family", String(64), nullable=True, index=True),
    Column("query_source", String(128), nullable=True, index=True),
    Column("agent_id", String(64), nullable=True, index=True),
    Column("protocol_name", String(64), nullable=True, index=True),
    Column("llm_provider", String(64), nullable=True, index=True),
    Column("llm_model", String(128), nullable=True, index=True),
    Column("caller_id", String(128), nullable=True, index=True),
    Column("latest_event_type", String(64), nullable=True),
    Column("status", String(32), nullable=True),
    Column("metadata", JSONB_TYPE, nullable=True),
    Column("first_occurred_at", DateTime(timezone=True), nullable=True),
    Column("last_occurred_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False),
)

Index(
    "idx_commerce_interactions_click_id_unique",
    commerce_interactions.c.merchant_id,
    func.coalesce(commerce_interactions.c.store_id, ""),
    commerce_interactions.c.click_id,
    unique=True,
)
Index(
    "idx_commerce_interactions_quote_id_unique",
    commerce_interactions.c.merchant_id,
    func.coalesce(commerce_interactions.c.store_id, ""),
    commerce_interactions.c.quote_id,
    unique=True,
)
Index(
    "idx_commerce_interactions_checkout_id_unique",
    commerce_interactions.c.merchant_id,
    func.coalesce(commerce_interactions.c.store_id, ""),
    commerce_interactions.c.checkout_id,
    unique=True,
)
Index(
    "idx_commerce_interactions_order_id_unique",
    commerce_interactions.c.merchant_id,
    func.coalesce(commerce_interactions.c.store_id, ""),
    commerce_interactions.c.order_id,
    unique=True,
)
Index(
    "idx_commerce_interactions_refund_id_unique",
    commerce_interactions.c.merchant_id,
    func.coalesce(commerce_interactions.c.store_id, ""),
    commerce_interactions.c.refund_id,
    unique=True,
)
Index(
    "idx_commerce_interactions_return_id_unique",
    commerce_interactions.c.merchant_id,
    func.coalesce(commerce_interactions.c.store_id, ""),
    commerce_interactions.c.return_id,
    unique=True,
)


commerce_interaction_events = Table(
    "commerce_interaction_events",
    metadata,
    Column("event_id", String(64), primary_key=True),
    Column("interaction_id", String(64), nullable=False, index=True),
    Column("merchant_id", String(50), nullable=False, index=True),
    Column("platform", String(32), nullable=True, index=True),
    Column("store_id", String(128), nullable=True, index=True),
    Column("surface", String(64), nullable=True, index=True),
    Column("event_type", String(64), nullable=False, index=True),
    Column("occurred_at", DateTime(timezone=True), nullable=False, index=True),
    Column("canonical_product_id", String(64), nullable=True, index=True),
    Column("canonical_variant_id", String(64), nullable=True, index=True),
    Column("trace_id", String(128), nullable=True, index=True),
    Column("brief_id", String(128), nullable=True, index=True),
    Column("session_id", String(128), nullable=True, index=True),
    Column("visitor_id", String(128), nullable=True, index=True),
    Column("cart_id", String(128), nullable=True, index=True),
    Column("payment_id", String(128), nullable=True, index=True),
    Column("source", String(128), nullable=True),
    Column("upstream_idempotency_key", Text, nullable=True),
    Column("actor_type", String(32), nullable=True),
    Column("actor_id", String(128), nullable=True),
    Column("payload", JSONB_TYPE, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

Index(
    "idx_commerce_interaction_events_idempotency",
    commerce_interaction_events.c.merchant_id,
    commerce_interaction_events.c.event_type,
    commerce_interaction_events.c.upstream_idempotency_key,
    unique=True,
)
Index(
    "idx_commerce_interaction_events_merchant_occurred",
    commerce_interaction_events.c.merchant_id,
    commerce_interaction_events.c.occurred_at.desc(),
)
Index(
    "idx_commerce_interaction_events_merchant_platform_occurred",
    commerce_interaction_events.c.merchant_id,
    commerce_interaction_events.c.platform,
    commerce_interaction_events.c.occurred_at.desc(),
)
Index(
    "idx_commerce_interaction_events_merchant_store_occurred",
    commerce_interaction_events.c.merchant_id,
    commerce_interaction_events.c.store_id,
    commerce_interaction_events.c.occurred_at.desc(),
)
