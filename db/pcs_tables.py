"""
PCS v0.1 database tables (minimal set).

These tables are designed to be:
- append-only (events, audit, evidence versions)
- idempotent (unique idempotency keys)
- tamper-evident (hash chains)
"""

from sqlalchemy import CheckConstraint, Table, Column, BigInteger, String, DateTime, Boolean, Text, Integer, UniqueConstraint
from sqlalchemy.sql import func
from sqlalchemy.dialects.postgresql import JSONB

from db.database import metadata


pcs_merchant_capabilities = Table(
    "pcs_merchant_capabilities",
    metadata,
    Column("merchant_id", String(50), primary_key=True),
    Column("shopify_api_version", String(20), nullable=True),
    Column("scopes_json", JSONB, nullable=False, server_default="{}"),
    Column("has_shopify_payments", Boolean, nullable=False, server_default="false"),
    Column("has_returns_api", Boolean, nullable=False, server_default="false"),
    Column("last_checked_at", DateTime(timezone=True), nullable=True),
)


pcs_shop_policies = Table(
    "pcs_shop_policies",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("merchant_id", String(50), nullable=False, index=True),
    Column("policy_type", String(20), nullable=False, index=True),
    Column("url", Text, nullable=False),
    Column("title", Text, nullable=True),
    Column("body_html", Text, nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=True),
    Column("hash_sha256", String(64), nullable=False, index=True),
    Column("fetched_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    CheckConstraint("policy_type IN ('refund','shipping','privacy','terms')", name="ck_pcs_shop_policies_type"),
    UniqueConstraint("merchant_id", "policy_type", "hash_sha256", name="uq_pcs_shop_policies_merchant_type_hash"),
)


pcs_shopify_webhook_events = Table(
    "pcs_shopify_webhook_events",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("merchant_id", String(50), nullable=False, index=True),
    Column("shop_domain", String(255), nullable=True),
    Column("topic", String(100), nullable=False, index=True),
    Column("webhook_id", String(100), nullable=True),
    Column("idempotency_key", Text, nullable=False, index=True),
    Column("signature_verified", Boolean, nullable=False, server_default="false"),
    Column("received_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=True),
    Column("payload_json", JSONB, nullable=False),
    Column("payload_sha256", String(64), nullable=False),
    Column("prev_chain_hash", String(64), nullable=True),
    Column("chain_hash", String(64), nullable=False),
    UniqueConstraint("merchant_id", "idempotency_key", name="uq_pcs_shopify_webhook_events_merchant_idempotency"),
)


pcs_evidence_packs = Table(
    "pcs_evidence_packs",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("merchant_id", String(50), nullable=False, index=True),
    Column("order_id", String(50), nullable=True, index=True),
    Column("dispute_ref", Text, nullable=True, index=True),
    Column("pack_type", String(30), nullable=False, index=True),
    Column("pack_version", Integer, nullable=False),
    Column("status", String(20), nullable=False, index=True),
    Column("generated_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("frozen_at", DateTime(timezone=True), nullable=True),
    Column("manifest_json", JSONB, nullable=False),
    Column("manifest_sha256", String(64), nullable=False),
    Column("signature", Text, nullable=True),
    Column("assets_json", JSONB, nullable=False, server_default="[]"),
    CheckConstraint("pack_type IN ('order_snapshot','dispute_pack')", name="ck_pcs_evidence_packs_pack_type"),
    CheckConstraint("status IN ('draft','frozen')", name="ck_pcs_evidence_packs_status"),
    CheckConstraint("pack_version >= 1", name="ck_pcs_evidence_packs_pack_version"),
    UniqueConstraint("merchant_id", "pack_type", "order_id", "dispute_ref", "pack_version", name="uq_pcs_evidence_packs_key"),
)


pcs_audit_logs = Table(
    "pcs_audit_logs",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("merchant_id", String(50), nullable=False, index=True),
    Column("actor_type", String(20), nullable=False),
    Column("actor_ref", Text, nullable=True),
    Column("action", Text, nullable=False),
    Column("occurred_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("payload_json", JSONB, nullable=False, server_default="{}"),
    Column("payload_sha256", String(64), nullable=False),
    Column("prev_chain_hash", String(64), nullable=True),
    Column("chain_hash", String(64), nullable=False),
    CheckConstraint("actor_type IN ('system','staff','app','merchant','customer')", name="ck_pcs_audit_logs_actor_type"),
)
