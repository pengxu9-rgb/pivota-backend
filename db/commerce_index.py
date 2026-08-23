"""Metadata for Commerce Index v2's source and delta-publication layers."""

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, String, Table, Text
from sqlalchemy.sql import func

from db.database import JSONB_TYPE, Float, metadata


commerce_index_sources = Table(
    "commerce_index_sources",
    metadata,
    Column("source_id", String(255), primary_key=True),
    Column("merchant_id", String(64), nullable=False, index=True),
    Column("provider", String(64), nullable=False),
    Column("integration_layer", String(32), nullable=False),
    Column("source_kind", String(64), nullable=False),
    Column("status", String(32), nullable=False, server_default="pending"),
    Column("consent_ref", String(255), nullable=True),
    Column("capabilities_json", JSONB_TYPE, nullable=True),
    Column("refresh_policy_json", JSONB_TYPE, nullable=True),
    Column("source_config_json", JSONB_TYPE, nullable=True),
    Column("last_success_at", DateTime, nullable=True),
    Column("created_at", DateTime, server_default=func.now(), nullable=False),
    Column("updated_at", DateTime, server_default=func.now(), nullable=False),
    Index("idx_commerce_index_sources_provider", "merchant_id", "provider", "integration_layer"),
)


commerce_index_field_changes = Table(
    "commerce_index_field_changes",
    metadata,
    Column("change_id", String(255), primary_key=True),
    Column("source_id", String(255), nullable=True, index=True),
    Column("merchant_id", String(64), nullable=False, index=True),
    Column("entity_type", String(32), nullable=False),
    Column("entity_id", String(255), nullable=False),
    Column("field_path", String(192), nullable=False),
    Column("source_system", String(64), nullable=False),
    Column("source_ref", String(255), nullable=True),
    Column("previous_fingerprint", String(64), nullable=True),
    Column("value_fingerprint", String(64), nullable=False),
    Column("confidence", Float, nullable=True),
    Column("observed_at", DateTime, nullable=False),
    Column("fresh_until", DateTime, nullable=True),
    Column("review_required", Boolean, nullable=False, server_default="false"),
    Column("reason", Text, nullable=False),
    Column("created_at", DateTime, server_default=func.now(), nullable=False),
    Index("idx_commerce_index_field_changes_entity", "merchant_id", "entity_type", "entity_id", "created_at"),
)


commerce_index_publication_jobs = Table(
    "commerce_index_publication_jobs",
    metadata,
    Column("job_id", String(255), primary_key=True),
    Column("change_id", String(255), nullable=False, index=True),
    Column("merchant_id", String(64), nullable=False, index=True),
    Column("target", String(64), nullable=False),
    Column("status", String(32), nullable=False, server_default="pending"),
    Column("scope_json", JSONB_TYPE, nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("claimed_by", String(128), nullable=True),
    Column("claimed_at", DateTime, nullable=True),
    Column("lease_until", DateTime, nullable=True),
    Column("error_message", Text, nullable=True),
    Column("published_at", DateTime, nullable=True),
    Column("created_at", DateTime, server_default=func.now(), nullable=False),
    Column("updated_at", DateTime, server_default=func.now(), nullable=False),
    Index("idx_commerce_index_publication_jobs_pending", "target", "status", "created_at"),
)


commerce_index_insight_refresh_requests = Table(
    "commerce_index_insight_refresh_requests",
    metadata,
    Column("request_id", String(255), primary_key=True),
    Column("change_id", String(255), nullable=False, unique=True),
    Column("merchant_id", String(64), nullable=False, index=True),
    Column("entity_type", String(32), nullable=False),
    Column("entity_id", String(255), nullable=False),
    Column("field_path", String(192), nullable=False),
    Column("status", String(32), nullable=False, server_default="pending_review"),
    Column("review_policy", String(64), nullable=False),
    Column("source_evidence_json", JSONB_TYPE, nullable=False),
    Column("created_at", DateTime, server_default=func.now(), nullable=False),
    Column("updated_at", DateTime, server_default=func.now(), nullable=False),
    Index("idx_commerce_index_insight_refresh_review", "status", "created_at"),
)


commerce_index_checkout_validation_requests = Table(
    "commerce_index_checkout_validation_requests",
    metadata,
    Column("request_id", String(255), primary_key=True),
    Column("change_id", String(255), nullable=False, unique=True),
    Column("merchant_id", String(64), nullable=False, index=True),
    Column("entity_type", String(32), nullable=False),
    Column("entity_id", String(255), nullable=False),
    Column("field_path", String(192), nullable=False),
    Column("status", String(32), nullable=False, server_default="requires_live_quote"),
    Column("validation_policy", String(96), nullable=False),
    Column("source_evidence_json", JSONB_TYPE, nullable=False),
    Column("created_at", DateTime, server_default=func.now(), nullable=False),
    Column("updated_at", DateTime, server_default=func.now(), nullable=False),
    Index("idx_commerce_index_checkout_validation", "merchant_id", "status", "created_at"),
)
