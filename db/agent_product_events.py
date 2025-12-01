from sqlalchemy import Table, Column, Integer, String, DateTime, Float, Index
from sqlalchemy.sql import func
from typing import Any, Dict, List, Optional

from db.database import metadata, database


# Event log for Agent product interactions (for LTR / reranker labels).

agent_product_events = Table(
    "agent_product_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", DateTime, server_default=func.now()),
    # Who + where
    Column("agent_id", String(64), nullable=True),
    Column("session_id", String(128), nullable=True),
    Column("event_type", String(32), nullable=False),  # impression / click / purchase / add_to_cart
    Column("endpoint", String(64), nullable=True),
    Column("query", String(500), nullable=True),
    # Product identifiers
    Column("merchant_id", String(100), nullable=True),
    Column("platform", String(50), nullable=True),
    Column("platform_product_id", String(200), nullable=True),
    # Ranking context
    Column("ranking_score", Float, nullable=True),
    Column("position", Integer, nullable=True),
    Column("quality_content_score", Float, nullable=True),
    Column("quality_model_readiness", Float, nullable=True),
)

Index(
    "idx_agent_product_events_product",
    agent_product_events.c.merchant_id,
    agent_product_events.c.platform,
    agent_product_events.c.platform_product_id,
    agent_product_events.c.created_at,
)


async def log_product_events(rows: List[Dict[str, Any]]) -> None:
    """
    Best-effort batch insert for product events.

    Each row may contain:
      - agent_id, session_id, event_type, endpoint, query
      - merchant_id, platform, platform_product_id
      - ranking_score, position, quality_content_score, quality_model_readiness
    """
    if not rows:
        return

    # Filter out completely empty rows defensively
    clean_rows: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if not row.get("event_type"):
            continue
        clean_rows.append(row)

    if not clean_rows:
        return

    query = agent_product_events.insert()
    await database.execute_many(query, clean_rows)

