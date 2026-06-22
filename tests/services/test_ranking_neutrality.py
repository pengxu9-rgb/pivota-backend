"""P0.3 — ranking neutrality lock.

Neutrality is the moat: the agentic decision layer must rank on MERIT, never on
ownership or commercial terms. A margin/ownership-tilted ranking is detectable
and erodes the frontier-model trust the whole index depends on. These tests
fail if a commercial signal ever leaks into the ranking feature vector, or if
the business weight stops defaulting OFF.
"""

from services.agent_ranking_service import (
    AgentRankingConfig,
    AgentRankingFeatures,
    compute_agent_ranking_score,
    get_agent_ranking_config,
)

# Signals that must NEVER influence ranking (ownership / commercial terms).
_FORBIDDEN_FEATURE_FIELDS = {
    "catalog_track", "offer_type", "take_rate", "takerate", "commission",
    "is_first_party", "first_party", "price", "list_price", "margin",
    "brand_relationship", "subscription_tier", "partner_tier", "billing",
    "internal_merchant",
}


def test_feature_vector_has_no_commercial_fields():
    fields = set(AgentRankingFeatures.__dataclass_fields__)
    leaked = fields & _FORBIDDEN_FEATURE_FIELDS
    assert not leaked, f"commercial signal leaked into ranking features: {leaked}"


def test_business_weight_defaults_off():
    # The business term must default OFF so merchant_reputation (or any future
    # business signal) can't tilt ranking unless deliberately enabled.
    cfg = get_agent_ranking_config()
    assert cfg.w_business == 0.0


def _features(**over):
    base = dict(
        merchant_id="m",
        platform="shopify",
        platform_product_id="p",
        rel_semantic=0.5,
        rel_keyword=0.4,
        quality_content_score=80.0,
        has_enriched_summary=True,
    )
    base.update(over)
    return AgentRankingFeatures(**base)


def test_score_invariant_to_business_signal_under_neutral_config():
    # Two candidates identical on merit but differing on the business signal
    # must score identically under the neutral (default) config.
    cfg = AgentRankingConfig(
        cq_min_for_agent=0.0,
        mr_min_for_agent=0.0,
        w_rel=0.6,
        w_quality=0.2,
        w_enrichment=0.2,
        w_business=0.0,
    )
    low = compute_agent_ranking_score(_features(merchant_reputation=0.0), cfg)
    high = compute_agent_ranking_score(_features(merchant_reputation=1.0), cfg)
    assert low == high
