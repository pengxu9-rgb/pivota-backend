from typing import Any, Dict, List

from sqlalchemy import (
    Table,
    Column,
    Integer,
    String,
    Date,
    DateTime,
    Float,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from db.database import metadata, database


# Daily aggregation of Agent product events (for LTR / reranker labels).
# One row per (date, merchant, platform, product).

agent_product_metrics_daily = Table(
    "agent_product_metrics_daily",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("date", Date, nullable=False),
    Column("merchant_id", String(100), nullable=False),
    Column("platform", String(50), nullable=False),
    Column("platform_product_id", String(200), nullable=False),
    Column("impressions", Integer, nullable=False, server_default="0"),
    Column("clicks", Integer, nullable=False, server_default="0"),
    Column("purchases", Integer, nullable=False, server_default="0"),
    Column("ctr", Float, nullable=True),
    Column("cvr", Float, nullable=True),
    Column("last_event_at", DateTime, nullable=True),
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime, server_default=func.now(), onupdate=func.now()),
    UniqueConstraint(
        "date",
        "merchant_id",
        "platform",
        "platform_product_id",
        name="uq_agent_product_metrics_daily_key",
    ),
)


async def upsert_daily_metrics(rows: List[Dict[str, Any]]) -> None:
    """
    Upsert a batch of daily metrics rows.

    Each row should contain:
      - date (date)
      - merchant_id, platform, platform_product_id
      - impressions, clicks, purchases
      - ctr, cvr
      - last_event_at
    """
    if not rows:
        return

    # Basic ON CONFLICT upsert on the logical key (date + merchant + platform + product).
    query = """
    INSERT INTO agent_product_metrics_daily (
        date,
        merchant_id,
        platform,
        platform_product_id,
        impressions,
        clicks,
        purchases,
        ctr,
        cvr,
        last_event_at
    ) VALUES (
        :date,
        :merchant_id,
        :platform,
        :platform_product_id,
        :impressions,
        :clicks,
        :purchases,
        :ctr,
        :cvr,
        :last_event_at
    )
    ON CONFLICT (date, merchant_id, platform, platform_product_id)
    DO UPDATE SET
        impressions = EXCLUDED.impressions,
        clicks = EXCLUDED.clicks,
        purchases = EXCLUDED.purchases,
        ctr = EXCLUDED.ctr,
        cvr = EXCLUDED.cvr,
        last_event_at = EXCLUDED.last_event_at,
        updated_at = NOW()
    """

    await database.execute_many(query, rows)

