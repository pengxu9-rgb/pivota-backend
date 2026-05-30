from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    Table,
    Text,
    func,
)

from db.database import JSONB_TYPE, metadata

# SKU Optimization OS -- hybrid publish path.
# Materialization target of PDP governance publish: approved module payloads are flattened
# into per-field rows here, which the public PDP merge hook reads at request time.
# See db/migrations/143_merchant_product_overlay.sql for the authoritative schema.
merchant_product_overlay = Table(
    "merchant_product_overlay",
    metadata,
    Column("overlay_id", Text, primary_key=True),
    Column("product_key", Text, nullable=False),
    Column("content_key", Text, nullable=True),
    Column("module_key", Text, nullable=False),
    Column("field_key", Text, nullable=False),
    Column("value_jsonb", JSONB_TYPE, nullable=False),
    Column("provenance", Text, nullable=False),
    Column("source_version_id", Text, nullable=True),
    Column("approval_status", Text, nullable=False, server_default="active"),
    Column("approved_by", Text, nullable=True),
    Column("approved_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # Partial unique + lookup indexes are created by migration 143 (authoritative schema).
    extend_existing=True,
)
