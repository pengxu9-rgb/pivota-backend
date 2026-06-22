from sqlalchemy import Table, Column, Integer, String, DateTime, Float, JSON, Index
from sqlalchemy.sql import func
from typing import Any, Dict, List, Optional

from db.database import metadata, database


# Lightweight log table for agent-side ranking features. Dual purpose:
# (1) analytics / ML (LTR) training data, and (2) the NEUTRALITY PROOF TRAIL
# (P0.3/P0.4) — the merit feature vector + score behind each ranking decision,
# so "was this ranking merit-based, not pay-to-win?" is auditable. Written
# post-response (not in the online decision path).

agent_ranking_log = Table(
    "agent_ranking_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", DateTime, server_default=func.now()),
    # Request / query context
    Column("agent_id", String(64), nullable=True),
    Column("endpoint", String(64), nullable=True),
    Column("query", String(500), nullable=True),
    # Product identifiers
    Column("merchant_id", String(100), nullable=True),
    Column("platform", String(50), nullable=True),
    Column("platform_product_id", String(200), nullable=True),
    # Core scores for quick analysis
    Column("ranking_score", Float, nullable=True),
    Column("relevance_score", Float, nullable=True),
    Column("quality_content_score", Float, nullable=True),
    Column("quality_model_readiness", Float, nullable=True),
    Column("quality_conversion_potential", Float, nullable=True),
    # Full feature vector snapshot (JSON)
    Column("features", JSON, nullable=True),
)

Index(
    "idx_agent_ranking_log_product",
    agent_ranking_log.c.merchant_id,
    agent_ranking_log.c.platform,
    agent_ranking_log.c.platform_product_id,
    agent_ranking_log.c.created_at,
)


async def log_ranking_batch(
    *,
    agent_id: Optional[str],
    endpoint: str,
    query: Optional[str],
    products: List[Dict[str, Any]],
    max_rows: int = 50,
) -> None:
    """
    Persist a batch of ranking feature snapshots for later LTR / reranker
    training AND as the NEUTRALITY PROOF TRAIL (P0.3/P0.4): the merit feature
    vector + score behind each ranking decision, so "was this ranking
    merit-based?" is auditable. Do NOT remove — only the legacy *billing
    attribution* role moved to services.agent_decision_event_store; the
    ranking-feature snapshot persisted here is current and load-bearing.

    The persisted `features` payload comes from serialize_features_for_log and
    must contain only merit fields (locked by
    tests/services/test_ranking_neutrality.py) — never a commercial/ownership
    signal.

    - `products` are the already-ranked products returned to the caller.
    - Each product is expected to optionally contain:
      - `ranking_score`
      - `relevance_score`
      - `ranking_features` (as produced by serialize_features_for_log)
    """
    if not products:
        return

    rows = []

    for product in products[:max_rows]:
        features = (product.get("ranking_features") or {}).copy()
        if not isinstance(features, dict):
            features = {}

        platform_product_id = str(
            product.get("platform_product_id")
            or product.get("product_id")
            or product.get("id")
            or ""
        )

        row = {
            "agent_id": agent_id,
            "endpoint": endpoint,
            "query": query,
            "merchant_id": product.get("merchant_id"),
            "platform": product.get("platform"),
            "platform_product_id": platform_product_id or None,
            "ranking_score": product.get("ranking_score"),
            "relevance_score": product.get("relevance_score"),
            "quality_content_score": features.get("quality_content_score"),
            "quality_model_readiness": features.get("quality_model_readiness"),
            "quality_conversion_potential": features.get(
                "quality_conversion_potential"
            ),
            "features": features or None,
        }
        rows.append(row)

    if not rows:
        return

    query_obj = agent_ranking_log.insert()
    await database.execute_many(query_obj, rows)
