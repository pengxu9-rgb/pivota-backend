"""
Agent-side ranking helpers for Shopping Agent search.

V1 goals:
- Define a structured feature vector per product candidate
- Apply quality-based gating (CQ / MR thresholds)
- Compute a configurable ranking score that combines:
  - relevance
  - quality
  - enrichment completeness
  - (optional) business features
- Log features for later analysis / ML ranking
"""

from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional
import logging

from config.settings import settings
from db.database import database
from db.product_quality import product_quality_snapshot
from db.product_enrichment import product_enrichment

logger = logging.getLogger(__name__)


@dataclass
class AgentRankingConfig:
    """Configuration for agent ranking & gating."""

    cq_min_for_agent: float
    mr_min_for_agent: float
    w_rel: float
    w_quality: float
    w_enrichment: float
    w_business: float


def get_agent_ranking_config() -> AgentRankingConfig:
    """
    Build ranking config from global settings.

    All weights / thresholds are environment‑driven to avoid hard‑coding
    magic numbers. See config/settings.py for defaults.
    """
    return AgentRankingConfig(
        cq_min_for_agent=getattr(settings, "cq_min_for_agent", 0.0) or 0.0,
        mr_min_for_agent=getattr(settings, "mr_min_for_agent", 0.0) or 0.0,
        w_rel=getattr(settings, "ranking_w_rel", 0.6),
        w_quality=getattr(settings, "ranking_w_quality", 0.2),
        w_enrichment=getattr(settings, "ranking_w_enrichment", 0.2),
        w_business=getattr(settings, "ranking_w_business", 0.0),
    )


@dataclass
class AgentRankingFeatures:
    """
    Feature vector for a single product candidate.

    Numeric fields are expected to be in [0, 1] where possible; raw
    quality scores keep original 0–100 range for explainability.
    """

    merchant_id: str
    platform: str
    platform_product_id: str

    # Relevance features
    rel_semantic: float = 0.0
    rel_keyword: float = 0.0
    rel_category_match: float = 0.0

    # Quality features (raw scores 0–100)
    quality_content_score: Optional[float] = None
    quality_model_readiness: Optional[float] = None
    quality_conversion_potential: Optional[float] = None

    # Enrichment / content completeness
    has_enriched_summary: bool = False
    has_enriched_bullets: bool = False
    has_usage_scenarios: bool = False
    has_audience_tags: bool = False
    has_topic_tags: bool = False
    enrichment_auto_confidence: Optional[float] = None  # 0–1 when present

    # Compliance (placeholders for future integration)
    compliance_status: str = "unknown"
    has_compliance_issues: bool = False

    # Business features (optional, may remain None)
    merchant_reputation: Optional[float] = None  # 0–1
    is_new_product: bool = False


async def hydrate_quality_and_enrichment(
    features: AgentRankingFeatures,
) -> None:
    """
    Populate quality and enrichment-related fields in-place.

    This function is intentionally defensive and can be safely called
    even when no snapshot / enrichment exists for the product.
    """
    # Quality snapshot (latest row)
    q_row = await database.fetch_one(
        """
        SELECT content_quality_score,
               model_readiness_score,
               conversion_potential_score
        FROM product_quality_snapshot
        WHERE merchant_id = :merchant_id
          AND platform = :platform
          AND platform_product_id = :platform_product_id
        ORDER BY snapshot_date DESC
        LIMIT 1
        """,
        {
            "merchant_id": features.merchant_id,
            "platform": features.platform,
            "platform_product_id": features.platform_product_id,
        },
    )
    if q_row:
        features.quality_content_score = q_row["content_quality_score"]
        features.quality_model_readiness = q_row["model_readiness_score"]
        features.quality_conversion_potential = q_row["conversion_potential_score"]

    # Enrichment overlay
    e_row = await database.fetch_one(
        product_enrichment.select().where(
            (product_enrichment.c.merchant_id == features.merchant_id)
            & (product_enrichment.c.platform == features.platform)
            & (
                product_enrichment.c.platform_product_id
                == features.platform_product_id
            )
            & (product_enrichment.c.geo_code == "default")
        )
    )
    if e_row:
        enrichment = dict(e_row)
        features.has_enriched_summary = bool(enrichment.get("summary_short"))
        bullets = enrichment.get("bullet_points") or []
        features.has_enriched_bullets = bool(bullets)
        features.has_usage_scenarios = bool(enrichment.get("usage_scenarios"))
        features.has_audience_tags = bool(enrichment.get("audience_tags"))
        features.has_topic_tags = bool(enrichment.get("topic_tags"))

        # We don't have a dedicated auto_confidence field yet; reuse
        # llm_readability_score as a soft proxy in [0, 1] when present.
        llm_readability = enrichment.get("llm_readability_score")
        if isinstance(llm_readability, (int, float)):
            # Clamp to [0, 1]
            features.enrichment_auto_confidence = max(
                0.0, min(1.0, float(llm_readability))
            )


def passes_agent_gating(
    features: AgentRankingFeatures, config: AgentRankingConfig
) -> bool:
    """
    Hard filters for Agent ranking:
    - Compliance (placeholder for now)
    - Content quality / model readiness thresholds
    """
    if features.has_compliance_issues:
        return False

    cq = features.quality_content_score
    mr = features.quality_model_readiness

    if config.cq_min_for_agent > 0.0 and isinstance(cq, (int, float)):
        if cq < config.cq_min_for_agent:
            return False

    if config.mr_min_for_agent > 0.0 and isinstance(mr, (int, float)):
        if mr < config.mr_min_for_agent:
            return False

    # Products without scores are allowed through for now to avoid
    # cold‑start starvation. This can be tightened later via config.
    return True


def compute_agent_ranking_score(
    features: AgentRankingFeatures, config: AgentRankingConfig
) -> float:
    """
    Compute a single ranking score using a weighted combination of:
    - relevance
    - quality
    - enrichment completeness
    - business features (currently optional)
    """

    # --- Relevance ---------------------------------------------------------
    # For now we treat rel_semantic and rel_keyword the same (0–1 range).
    rel_semantic = max(0.0, min(1.0, float(features.rel_semantic or 0.0)))
    rel_keyword = max(0.0, min(1.0, float(features.rel_keyword or 0.0)))
    # Simple blend; can be replaced once true semantic score is available.
    rel_score = 0.7 * rel_semantic + 0.3 * rel_keyword

    # --- Quality -----------------------------------------------------------
    cq = float(features.quality_content_score or 0.0)
    mr = float(features.quality_model_readiness or 0.0)
    cp = float(features.quality_conversion_potential or 0.0)

    cq_norm = max(0.0, min(1.0, cq / 100.0)) if cq > 0 else 0.0
    mr_norm = max(0.0, min(1.0, mr / 100.0)) if mr > 0 else 0.0
    cp_norm = max(0.0, min(1.0, cp / 100.0)) if cp > 0 else 0.0

    # 基础质量仍以 CQ/MR 为主，CP 提供额外区分度
    quality_score = 0.5 * cq_norm + 0.3 * mr_norm + 0.2 * cp_norm

    # --- Enrichment --------------------------------------------------------
    enrichment_score = 0.0
    if features.has_enriched_summary:
        enrichment_score += 0.3
    if features.has_enriched_bullets:
        enrichment_score += 0.2
    if features.has_usage_scenarios:
        enrichment_score += 0.1
    if features.has_audience_tags:
        enrichment_score += 0.1
    if features.has_topic_tags:
        enrichment_score += 0.1

    if isinstance(features.enrichment_auto_confidence, (int, float)):
        enrichment_score += 0.2 * max(
            0.0, min(1.0, float(features.enrichment_auto_confidence))
        )

    # Clamp to [0, 1]
    enrichment_score = max(0.0, min(1.0, enrichment_score))

    # --- Business ----------------------------------------------------------
    if isinstance(features.merchant_reputation, (int, float)):
        business_score = max(
            0.0, min(1.0, float(features.merchant_reputation))
        )
    else:
        business_score = 0.0

    # Final weighted score
    score = (
        config.w_rel * rel_score
        + config.w_quality * quality_score
        + config.w_enrichment * enrichment_score
        + config.w_business * business_score
    )
    return float(score)


def serialize_features_for_log(
    features: AgentRankingFeatures, score: float
) -> Dict[str, Any]:
    """
    Helper to produce a compact JSON‑serializable view of features and score.
    Intended for logging / observability, not for API responses.
    """
    data = asdict(features)
    data["ranking_score"] = score
    return data
