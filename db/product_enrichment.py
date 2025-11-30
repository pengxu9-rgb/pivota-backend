from sqlalchemy import Table, Column, String, DateTime, JSON, Float, Index
from sqlalchemy.sql import func
from typing import Optional, Dict, Any
import json

from db.database import metadata, database

# Pivota-specific enrichment layer for products.
# Acts as an overlay on top of products_cache / StandardProduct.

product_enrichment = Table(
    "product_enrichment",
    metadata,
    Column("merchant_id", String(100), primary_key=True),
    Column("platform", String(50), primary_key=True),
    Column("platform_product_id", String(200), primary_key=True),
    Column("geo_code", String(16), primary_key=True, default="default"),
    # Overlay fields (all optional, only present when merchant / Pivota added them)
    Column("title_override", String(500), nullable=True),
    Column("summary_short", String(1000), nullable=True),
    Column("bullet_points", JSON, nullable=True),
    Column("usage_scenarios", JSON, nullable=True),
    Column("audience_tags", JSON, nullable=True),
    Column("topic_tags", JSON, nullable=True),
    Column("regulatory_disclaimer_local", String(2000), nullable=True),
    Column("extra_images", JSON, nullable=True),
    # LLM-related metrics (for internal use)
    Column("llm_readability_score", Float, nullable=True),
    Column("llm_safety_flags", JSON, nullable=True),
    # Audit
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime, server_default=func.now(), onupdate=func.now()),
)

Index(
    "idx_enrichment_merchant_platform",
    product_enrichment.c.merchant_id,
    product_enrichment.c.platform,
)


async def get_enrichment(
    merchant_id: str,
    platform: str,
    platform_product_id: str,
    geo_code: str = "default",
) -> Optional[Dict[str, Any]]:
    """Fetch enrichment row if it exists."""
    query = product_enrichment.select().where(
        (product_enrichment.c.merchant_id == merchant_id)
        & (product_enrichment.c.platform == platform)
        & (product_enrichment.c.platform_product_id == platform_product_id)
        & (product_enrichment.c.geo_code == geo_code)
    )
    row = await database.fetch_one(query)
    return dict(row) if row else None


async def upsert_enrichment(
    merchant_id: str,
    platform: str,
    platform_product_id: str,
    geo_code: str,
    data: Dict[str, Any],
) -> None:
    """
    Upsert enrichment row for a given merchant/platform/product/geo.

    Uses PostgreSQL's ON CONFLICT to avoid race conditions.
    """
    # Keep only known keys to avoid arbitrary data
    allowed_keys = {
        "title_override",
        "summary_short",
        "bullet_points",
        "usage_scenarios",
        "audience_tags",
        "topic_tags",
        "regulatory_disclaimer_local",
        "extra_images",
        "llm_readability_score",
        "llm_safety_flags",
    }
    clean_data = {k: v for k, v in data.items() if k in allowed_keys}

    # JSON-like fields must be encoded as strings when using raw SQL with asyncpg.
    # Postgres will parse the JSON text into the JSON column type.
    def _encode_json(value: Any) -> Optional[str]:
        if value is None:
            return None
        return json.dumps(value)

    # Basic ON CONFLICT upsert
    query = f"""
    INSERT INTO product_enrichment (
        merchant_id, platform, platform_product_id, geo_code,
        title_override, summary_short, bullet_points, usage_scenarios,
        audience_tags, topic_tags, regulatory_disclaimer_local,
        extra_images, llm_readability_score, llm_safety_flags
    ) VALUES (
        :merchant_id, :platform, :platform_product_id, :geo_code,
        :title_override, :summary_short, :bullet_points, :usage_scenarios,
        :audience_tags, :topic_tags, :regulatory_disclaimer_local,
        :extra_images, :llm_readability_score, :llm_safety_flags
    )
    ON CONFLICT (merchant_id, platform, platform_product_id, geo_code)
    DO UPDATE SET
        title_override = EXCLUDED.title_override,
        summary_short = EXCLUDED.summary_short,
        bullet_points = EXCLUDED.bullet_points,
        usage_scenarios = EXCLUDED.usage_scenarios,
        audience_tags = EXCLUDED.audience_tags,
        topic_tags = EXCLUDED.topic_tags,
        regulatory_disclaimer_local = EXCLUDED.regulatory_disclaimer_local,
        extra_images = EXCLUDED.extra_images,
        llm_readability_score = EXCLUDED.llm_readability_score,
        llm_safety_flags = EXCLUDED.llm_safety_flags,
        updated_at = NOW()
    """

    params = {
        "merchant_id": merchant_id,
        "platform": platform,
        "platform_product_id": platform_product_id,
        "geo_code": geo_code or "default",
        # Enrichment fields (may be missing)
        "title_override": clean_data.get("title_override"),
        "summary_short": clean_data.get("summary_short"),
        "bullet_points": _encode_json(clean_data.get("bullet_points")),
        "usage_scenarios": _encode_json(clean_data.get("usage_scenarios")),
        "audience_tags": _encode_json(clean_data.get("audience_tags")),
        "topic_tags": _encode_json(clean_data.get("topic_tags")),
        "regulatory_disclaimer_local": clean_data.get("regulatory_disclaimer_local"),
        "extra_images": _encode_json(clean_data.get("extra_images")),
        "llm_readability_score": clean_data.get("llm_readability_score"),
        "llm_safety_flags": _encode_json(clean_data.get("llm_safety_flags")),
    }

    await database.execute(query, params)
