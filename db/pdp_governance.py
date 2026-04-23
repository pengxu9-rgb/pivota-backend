from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, Float, Index, Integer, String, Table, Text
from sqlalchemy.sql import func

from db.database import JSONB_TYPE, metadata


pdp_subject_index = Table(
    "pdp_subject_index",
    metadata,
    Column("pdp_id", String(96), primary_key=True),
    Column("subject_type", String(32), nullable=False),
    Column("subject_ref", Text, nullable=False),
    Column("market", String(16), nullable=False, default="US"),
    Column("product_group_id", String(128), nullable=True),
    Column("external_product_id", String(128), nullable=True),
    Column("representative_product_key", Text, nullable=True),
    Column("title", Text, nullable=True),
    Column("image_url", Text, nullable=True),
    Column("seller_count", Integer, nullable=False, default=0),
    Column("external_only", Boolean, nullable=False, default=False),
    Column("status", String(24), nullable=False, default="active"),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

Index("idx_pdp_subject_index_subject", pdp_subject_index.c.subject_type, pdp_subject_index.c.subject_ref)
Index("idx_pdp_subject_index_updated", pdp_subject_index.c.updated_at.desc())
Index("idx_pdp_subject_index_market", pdp_subject_index.c.market)


pdp_module_versions = Table(
    "pdp_module_versions",
    metadata,
    Column("id", String(96), primary_key=True),
    Column("pdp_id", String(96), nullable=False, index=True),
    Column("module_key", String(40), nullable=False, index=True),
    Column("stage", String(20), nullable=False, index=True),
    Column("version", Integer, nullable=False, default=1),
    Column("status", String(32), nullable=False, default="draft"),
    Column("payload", JSONB_TYPE, nullable=False),
    Column("source_refs", JSONB_TYPE, nullable=True),
    Column("review_actor_type", String(32), nullable=True),
    Column("review_actor_id", String(128), nullable=True),
    Column("review_model", String(64), nullable=True),
    Column("review_decision", String(32), nullable=True),
    Column("review_confidence", Float, nullable=True),
    Column("review_rubric", JSONB_TYPE, nullable=True),
    Column("risk_level", String(32), nullable=False, default="low"),
    Column("requires_human", Boolean, nullable=False, default=False),
    Column("generated_by", String(64), nullable=True),
    Column("generation_ref", Text, nullable=True),
    Column("created_by_employee_id", String(128), nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("published_at", DateTime(timezone=True), nullable=True),
    Column("superseded_at", DateTime(timezone=True), nullable=True),
)

Index("idx_pdp_module_versions_lookup", pdp_module_versions.c.pdp_id, pdp_module_versions.c.module_key, pdp_module_versions.c.stage)
Index("idx_pdp_module_versions_created", pdp_module_versions.c.created_at.desc())


pdp_audit_log = Table(
    "pdp_audit_log",
    metadata,
    Column("id", String(96), primary_key=True),
    Column("pdp_id", String(96), nullable=False, index=True),
    Column("module_key", String(40), nullable=True),
    Column("action", String(64), nullable=False),
    Column("actor_type", String(32), nullable=False),
    Column("actor_id", String(128), nullable=True),
    Column("details", JSONB_TYPE, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

Index("idx_pdp_audit_log_created", pdp_audit_log.c.created_at.desc())


merchant_pdp_contributions = Table(
    "merchant_pdp_contributions",
    metadata,
    Column("id", String(96), primary_key=True),
    Column("pdp_id", String(96), nullable=False, index=True),
    Column("product_key", Text, nullable=False),
    Column("merchant_id", String(128), nullable=False, index=True),
    Column("module_key", String(40), nullable=False),
    Column("payload", JSONB_TYPE, nullable=False),
    Column("notes", Text, nullable=True),
    Column("status", String(32), nullable=False, default="submitted"),
    Column("reviewed_by_actor_type", String(32), nullable=True),
    Column("reviewed_by_actor_id", String(128), nullable=True),
    Column("review_decision", String(32), nullable=True),
    Column("review_notes", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

Index("idx_merchant_pdp_contributions_status", merchant_pdp_contributions.c.status, merchant_pdp_contributions.c.created_at.desc())
