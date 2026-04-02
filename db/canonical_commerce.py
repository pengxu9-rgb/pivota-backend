from __future__ import annotations

from sqlalchemy import (
    Boolean,
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


canonical_products = Table(
    "canonical_products",
    metadata,
    Column("canonical_product_id", String(64), primary_key=True),
    Column("merchant_id", String(50), nullable=False, index=True),
    Column("platform", String(32), nullable=False),
    Column("platform_product_id", String(255), nullable=False),
    Column("title", String(512), nullable=False),
    Column("description", Text, nullable=True),
    Column("brand", String(255), nullable=True),
    Column("category", String(255), nullable=True),
    Column("default_image_url", Text, nullable=True),
    Column("status", String(32), nullable=True),
    Column("orderable", Boolean, nullable=True),
    Column("currency", String(8), nullable=True),
    Column("visible_attributes", JSONB_TYPE, nullable=True),
    Column("ingredient_ids", JSONB_TYPE, nullable=True),
    Column("standard_product_data", JSONB_TYPE, nullable=False),
    Column("source_payload_hash", String(64), nullable=False),
    Column("source_recorded_at", DateTime(timezone=True), nullable=True),
    Column("expires_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False),
)

Index(
    "idx_canonical_products_merchant_platform_product",
    canonical_products.c.merchant_id,
    canonical_products.c.platform,
    canonical_products.c.platform_product_id,
    unique=True,
)


canonical_variants = Table(
    "canonical_variants",
    metadata,
    Column("canonical_variant_id", String(64), primary_key=True),
    Column("canonical_product_id", String(64), nullable=False, index=True),
    Column("merchant_id", String(50), nullable=False, index=True),
    Column("platform", String(32), nullable=False),
    Column("platform_product_id", String(255), nullable=False),
    Column("platform_variant_id", String(255), nullable=False),
    Column("title", String(512), nullable=False),
    Column("sku", String(255), nullable=True),
    Column("barcode", String(255), nullable=True),
    Column("currency", String(8), nullable=True),
    Column("option_values", JSONB_TYPE, nullable=True),
    Column("visible_option_labels", JSONB_TYPE, nullable=True),
    Column("image_url", Text, nullable=True),
    Column("standard_variant_data", JSONB_TYPE, nullable=False),
    Column("source_payload_hash", String(64), nullable=False),
    Column("source_recorded_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False),
)

Index(
    "idx_canonical_variants_merchant_platform_variant",
    canonical_variants.c.merchant_id,
    canonical_variants.c.platform,
    canonical_variants.c.platform_product_id,
    canonical_variants.c.platform_variant_id,
    unique=True,
)


canonical_offers = Table(
    "canonical_offers",
    metadata,
    Column("canonical_offer_id", String(64), primary_key=True),
    Column("canonical_product_id", String(64), nullable=False, index=True),
    Column("canonical_variant_id", String(64), nullable=False, index=True),
    Column("merchant_id", String(50), nullable=False, index=True),
    Column("currency", String(8), nullable=False),
    Column("amount", Numeric(10, 2), nullable=False),
    Column("compare_at_amount", Numeric(10, 2), nullable=True),
    Column("availability", String(32), nullable=True),
    Column("orderable", Boolean, nullable=True),
    Column("checkout_url", Text, nullable=True),
    Column("source_payload_hash", String(64), nullable=False),
    Column("source_recorded_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False),
)

Index(
    "idx_canonical_offers_variant",
    canonical_offers.c.canonical_variant_id,
    unique=True,
)


canonical_inventory_snapshots = Table(
    "canonical_inventory_snapshots",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("canonical_product_id", String(64), nullable=False, index=True),
    Column("canonical_variant_id", String(64), nullable=False, index=True),
    Column("merchant_id", String(50), nullable=False, index=True),
    Column("quantity", Integer, nullable=True),
    Column("availability", String(32), nullable=True),
    Column("observed_at", DateTime(timezone=True), nullable=True),
    Column("source", String(128), nullable=False),
    Column("stale", Boolean, nullable=False, default=False),
    Column("source_payload_hash", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

Index(
    "idx_canonical_inventory_variant_observed",
    canonical_inventory_snapshots.c.canonical_variant_id,
    canonical_inventory_snapshots.c.observed_at,
)


canonical_product_sources = Table(
    "canonical_product_sources",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("canonical_product_id", String(64), nullable=False, index=True),
    Column("canonical_variant_id", String(64), nullable=True, index=True),
    Column("merchant_id", String(50), nullable=False, index=True),
    Column("platform", String(32), nullable=False),
    Column("platform_product_id", String(255), nullable=False),
    Column("platform_variant_id", String(255), nullable=True),
    Column("source_type", String(64), nullable=False),
    Column("source_name", String(128), nullable=False),
    Column("source_recorded_at", DateTime(timezone=True), nullable=True),
    Column("payload_hash", String(64), nullable=False),
    Column("raw_payload", JSONB_TYPE, nullable=True),
    Column("is_primary", Boolean, nullable=False, default=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

Index(
    "idx_canonical_product_sources_lookup",
    canonical_product_sources.c.merchant_id,
    canonical_product_sources.c.platform,
    canonical_product_sources.c.platform_product_id,
    canonical_product_sources.c.platform_variant_id,
)
