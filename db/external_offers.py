from sqlalchemy import (
    Table,
    Column,
    Integer,
    String,
    DateTime,
    Index,
    Text,
    Numeric,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from db.database import metadata


# Cached snapshots for external product pages (OG/JSON-LD scraped).
# This enables showing external-only items (brand/title/price/image) without requiring them in the internal product DB.
external_offer_snapshots = Table(
    "external_offer_snapshots",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("market", String(8), nullable=False),  # US | JP | ...
    Column("canonical_url", Text, nullable=False),
    Column("url_hash", String(64), nullable=False),
    Column("domain", String(256), nullable=False),
    Column("title", Text, nullable=True),
    Column("brand", Text, nullable=True),
    Column("image_url", Text, nullable=True),
    Column("price_amount", Numeric, nullable=True),
    Column("price_currency", String(8), nullable=True),
    Column("availability", String(16), nullable=False, server_default="unknown"),  # in_stock|out_of_stock|unknown
    Column("evidence", JSONB, nullable=True),  # { provider, fetchedAt, snapshotId? }
    Column("last_checked_at", DateTime, nullable=True),
    # Optional manual overrides (ops/portal in phase 2)
    Column("override_title", Text, nullable=True),
    Column("override_brand", Text, nullable=True),
    Column("override_image_url", Text, nullable=True),
    Column("override_price_amount", Numeric, nullable=True),
    Column("override_price_currency", String(8), nullable=True),
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime, server_default=func.now()),
)

Index("idx_external_offer_snapshots_market_hash", external_offer_snapshots.c.market, external_offer_snapshots.c.url_hash, unique=True)
