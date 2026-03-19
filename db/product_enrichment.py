from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Iterable, Optional, Tuple

from sqlalchemy import Column, DateTime, Float, Index, JSON, String, Table, Text
from sqlalchemy.sql import func

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
    Column("description_markdown", Text, nullable=True),
    Column("bullet_points", JSON, nullable=True),
    Column("usage_scenarios", JSON, nullable=True),
    Column("audience_tags", JSON, nullable=True),
    Column("topic_tags", JSON, nullable=True),
    Column("regulatory_disclaimer_local", String(2000), nullable=True),
    Column("extra_images", JSON, nullable=True),
    # LLM-related metrics (for internal use)
    Column("llm_readability_score", Float, nullable=True),
    Column("llm_safety_flags", JSON, nullable=True),
    # Employee edit metadata (optional)
    Column("updated_by_employee_id", String(64), nullable=True),
    Column("updated_by_email", String(255), nullable=True),
    # Audit
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime, server_default=func.now(), onupdate=func.now()),
)

Index(
    "idx_enrichment_merchant_platform",
    product_enrichment.c.merchant_id,
    product_enrichment.c.platform,
)

logger = logging.getLogger(__name__)

_PRODUCT_ENRICHMENT_DDL_READY = False
_PRODUCT_ENRICHMENT_DDL_LOCK = asyncio.Lock()


async def ensure_product_enrichment_table() -> None:
    """
    Best-effort defensive DDL.

    Production normally relies on metadata.create_all(engine) in startup, but this:
    - avoids hard failures if import ordering changes
    - adds new columns incrementally without requiring a migration for MVP
    """
    global _PRODUCT_ENRICHMENT_DDL_READY
    if _PRODUCT_ENRICHMENT_DDL_READY:
        return
    async with _PRODUCT_ENRICHMENT_DDL_LOCK:
        if _PRODUCT_ENRICHMENT_DDL_READY:
            return
        try:
            statements = [
                """
                CREATE TABLE IF NOT EXISTS product_enrichment (
                  merchant_id VARCHAR(100) NOT NULL,
                  platform VARCHAR(50) NOT NULL,
                  platform_product_id VARCHAR(200) NOT NULL,
                  geo_code VARCHAR(16) NOT NULL DEFAULT 'default',

                  title_override VARCHAR(500),
                  summary_short VARCHAR(1000),
                  description_markdown TEXT,
                  bullet_points JSONB,
                  usage_scenarios JSONB,
                  audience_tags JSONB,
                  topic_tags JSONB,
                  regulatory_disclaimer_local VARCHAR(2000),
                  extra_images JSONB,
                  llm_readability_score DOUBLE PRECISION,
                  llm_safety_flags JSONB,

                  updated_by_employee_id VARCHAR(64),
                  updated_by_email VARCHAR(255),

                  created_at TIMESTAMPTZ DEFAULT NOW(),
                  updated_at TIMESTAMPTZ DEFAULT NOW(),
                  PRIMARY KEY (merchant_id, platform, platform_product_id, geo_code)
                );
                """,
                "ALTER TABLE product_enrichment ADD COLUMN IF NOT EXISTS description_markdown TEXT;",
                "ALTER TABLE product_enrichment ADD COLUMN IF NOT EXISTS updated_by_employee_id VARCHAR(64);",
                "ALTER TABLE product_enrichment ADD COLUMN IF NOT EXISTS updated_by_email VARCHAR(255);",
                "CREATE INDEX IF NOT EXISTS idx_enrichment_merchant_platform ON product_enrichment(merchant_id, platform);",
            ]
            for stmt in statements:
                await database.execute(stmt)
        except Exception as exc:
            # Best-effort; callers will degrade when table/columns are missing.
            logger.warning("ensure_product_enrichment_table failed (best-effort): %s", str(exc)[:200])
            return
        _PRODUCT_ENRICHMENT_DDL_READY = True


async def get_enrichment(
    merchant_id: str,
    platform: str,
    platform_product_id: str,
    geo_code: str = "default",
) -> Optional[Dict[str, Any]]:
    """Fetch enrichment row if it exists."""
    await ensure_product_enrichment_table()
    query = product_enrichment.select().where(
        (product_enrichment.c.merchant_id == merchant_id)
        & (product_enrichment.c.platform == platform)
        & (product_enrichment.c.platform_product_id == platform_product_id)
        & (product_enrichment.c.geo_code == geo_code)
    )
    row = await database.fetch_one(query)
    return dict(row) if row else None


async def get_enrichments_for_products(
    merchant_id: str,
    *,
    product_keys: Optional[Iterable[Tuple[str, str]]] = None,
    geo_code: str = "default",
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """
    Bulk fetch enrichment rows for a merchant's products.

    The query stays merchant-scoped and filters by geo_code in SQL, then applies
    any product key filter in memory to avoid per-product lookups.
    """
    await ensure_product_enrichment_table()

    keys = {
        (str(platform or "").strip(), str(platform_product_id or "").strip())
        for platform, platform_product_id in (product_keys or [])
        if str(platform or "").strip() and str(platform_product_id or "").strip()
    }
    platforms = sorted({platform for platform, _ in keys})

    query = product_enrichment.select().where(
        (product_enrichment.c.merchant_id == merchant_id)
        & (product_enrichment.c.geo_code == (geo_code or "default"))
    )
    if platforms:
        query = query.where(product_enrichment.c.platform.in_(platforms))

    rows = await database.fetch_all(query)
    enrichments: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        payload = dict(row)
        key = (
            str(payload.get("platform") or "").strip(),
            str(payload.get("platform_product_id") or "").strip(),
        )
        if not key[0] or not key[1]:
            continue
        if keys and key not in keys:
            continue
        enrichments[key] = payload
    return enrichments


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
    await ensure_product_enrichment_table()
    # Keep only known keys to avoid arbitrary data
    allowed_keys = {
        "title_override",
        "summary_short",
        "description_markdown",
        "bullet_points",
        "usage_scenarios",
        "audience_tags",
        "topic_tags",
        "regulatory_disclaimer_local",
        "extra_images",
        "llm_readability_score",
        "llm_safety_flags",
        "updated_by_employee_id",
        "updated_by_email",
    }
    clean_data = {k: v for k, v in data.items() if k in allowed_keys}

    if not clean_data:
        return

    # JSON-like fields must be encoded as strings when using raw SQL with asyncpg.
    # Postgres will parse the JSON text into the JSON column type.
    def _encode_json(value: Any) -> Optional[str]:
        if value is None:
            return None
        return json.dumps(value)

    json_keys = {
        "bullet_points",
        "usage_scenarios",
        "audience_tags",
        "topic_tags",
        "extra_images",
        "llm_safety_flags",
    }

    base_cols = ["merchant_id", "platform", "platform_product_id", "geo_code"]
    patch_cols = sorted(clean_data.keys())
    cols = base_cols + patch_cols

    insert_cols = ", ".join(cols)
    insert_vals = ", ".join([f":{c}" for c in cols])

    update_sets = ", ".join([f"{c} = EXCLUDED.{c}" for c in patch_cols] + ["updated_at = NOW()"])

    query = f"""
    INSERT INTO product_enrichment ({insert_cols})
    VALUES ({insert_vals})
    ON CONFLICT (merchant_id, platform, platform_product_id, geo_code)
    DO UPDATE SET {update_sets}
    """

    params: Dict[str, Any] = {
        "merchant_id": merchant_id,
        "platform": platform,
        "platform_product_id": platform_product_id,
        "geo_code": geo_code or "default",
    }

    for k in patch_cols:
        val = clean_data.get(k)
        if k in json_keys:
            params[k] = _encode_json(val)
        else:
            params[k] = val

    await database.execute(query, params)
