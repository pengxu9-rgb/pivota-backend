"""
Shopify Order POC Log - EPIC-10

Stores Shopify POC orders for basic idempotency and auditing.
"""

from sqlalchemy import Table, Column, Integer, String, DateTime, JSON
from sqlalchemy.sql import func
from typing import Optional, Dict, Any

from db.database import metadata, database

shopify_order_poc_log = Table(
    "shopify_order_poc_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("onboarding_id", String(50), nullable=False, index=True),
    Column("request_hash", String(64), nullable=False, unique=True, index=True),
    Column("platform_product_id", String(100), nullable=False),
    Column("variant_id", String(100), nullable=True),
    Column("quantity", Integer, nullable=False),
    Column("buyer_email", String(255), nullable=True),
    Column("shopify_order_id", String(255), nullable=True),
    Column("shopify_order_name", String(255), nullable=True),
    Column("shopify_order_url", String(500), nullable=True),
    Column("raw_order", JSON, nullable=True),
    Column("created_at", DateTime, server_default=func.now(), nullable=False),
)


async def get_poc_log_by_hash(request_hash: str) -> Optional[Dict[str, Any]]:
    """Return a single POC log row by request_hash, if any."""
    query = shopify_order_poc_log.select().where(
        shopify_order_poc_log.c.request_hash == request_hash
    )
    row = await database.fetch_one(query)
    return dict(row) if row else None


async def insert_poc_log(
    onboarding_id: str,
    request_hash: str,
    platform_product_id: str,
    variant_id: Optional[str],
    quantity: int,
    buyer_email: Optional[str],
    shopify_order_id: Optional[str],
    shopify_order_name: Optional[str],
    shopify_order_url: Optional[str],
    raw_order: Optional[Dict[str, Any]],
) -> int:
    """Insert a new POC log row and return its ID."""
    values = {
        "onboarding_id": onboarding_id,
        "request_hash": request_hash,
        "platform_product_id": platform_product_id,
        "variant_id": variant_id,
        "quantity": quantity,
        "buyer_email": buyer_email,
        "shopify_order_id": shopify_order_id,
        "shopify_order_name": shopify_order_name,
        "shopify_order_url": shopify_order_url,
        "raw_order": raw_order or {},
    }
    query = shopify_order_poc_log.insert().values(**values)
    log_id = await database.execute(query)
    return int(log_id)

