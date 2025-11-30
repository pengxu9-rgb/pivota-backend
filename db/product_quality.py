from sqlalchemy import Table, Column, Integer, String, DateTime, Float, JSON, Index
from sqlalchemy.sql import func

from db.database import metadata, database  # database kept for potential future helpers

# Snapshot table for product quality scores.
# V1: used by quality preview/eval; later can be extended with more dimensions.

product_quality_snapshot = Table(
    "product_quality_snapshot",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    # Logical identifiers for merchant / platform / external product
    Column("merchant_id", String(100), nullable=False),
    Column("platform", String(50), nullable=False),
    Column("platform_product_id", String(200), nullable=False),
    Column("geo_code", String(16), nullable=True),
    # Optional canonical product_id for convenience (e.g. hash or composite)
    Column("product_id", String(200), nullable=True),
    # Snapshot metadata
    Column("snapshot_date", DateTime, server_default=func.now()),
    Column("content_quality_score", Float, nullable=True),
    Column("model_readiness_score", Float, nullable=True),
    Column("conversion_potential_score", Float, nullable=True),
    Column("rules_version", String(32), nullable=True),
    Column("model_version", String(32), nullable=True),
    Column("details", JSON, nullable=True),
    Column("created_at", DateTime, server_default=func.now()),
)

Index(
    "idx_quality_product_geo_date",
    product_quality_snapshot.c.merchant_id,
    product_quality_snapshot.c.platform,
    product_quality_snapshot.c.platform_product_id,
    product_quality_snapshot.c.geo_code,
    product_quality_snapshot.c.snapshot_date,
)

