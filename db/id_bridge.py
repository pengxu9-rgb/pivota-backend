"""
ID bridge table for Aurora UUID <-> Pivota subject mapping.

This table enforces one-to-one key mapping to avoid dirty fan-out.
"""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from db.database import metadata


id_bridge = Table(
    "id_bridge",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("bridge_key_type", String(40), nullable=False),
    Column("bridge_key", String(128), nullable=False),
    Column("subject_kind", String(40), nullable=False),
    Column("product_group_id", String(128), nullable=True),
    Column("merchant_id", String(64), nullable=True),
    Column("product_id", String(128), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "bridge_key_type IN ('aurora_sku_uuid', 'aurora_product_uuid')",
        name="ck_id_bridge_key_type",
    ),
    CheckConstraint(
        "subject_kind IN ('product_group', 'canonical_product')",
        name="ck_id_bridge_subject_kind",
    ),
    CheckConstraint(
        "("
        "subject_kind = 'product_group' AND product_group_id IS NOT NULL"
        ") OR ("
        "subject_kind = 'canonical_product' AND merchant_id IS NOT NULL AND product_id IS NOT NULL"
        ")",
        name="ck_id_bridge_subject_shape",
    ),
    UniqueConstraint("bridge_key_type", "bridge_key", name="uq_id_bridge_key"),
)


Index("idx_id_bridge_product_group_id", id_bridge.c.product_group_id)
Index("idx_id_bridge_canonical_ref", id_bridge.c.merchant_id, id_bridge.c.product_id)
