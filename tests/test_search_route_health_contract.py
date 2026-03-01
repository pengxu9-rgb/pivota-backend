from routes import agent_api
from routes import agent_shop_gateway


def test_finalize_search_metadata_syncs_fallback_reason_to_route_health():
    metadata = {
        "orchestrator_path": "search_route_adapter",
        "decision_node": "agent_products_search",
        "domain_filter_dropped_external": 3,
        "external_fill_gate_reason": "cache_hit",
        "semantic_retry_applied": True,
        "semantic_retry_query": "perfume fragrance",
        "semantic_retry_hits": 2,
        "external_seed_brand_strict_rows": 12,
        "external_seed_brand_relevant_rows": 10,
        "external_seed_broad_fallback_used": True,
        "external_seed_broad_scope_rows": 24,
        "fallback_reason": "semantic_retry_exhausted",
        "route_health": {},
    }
    normalized = agent_api._finalize_search_metadata(metadata)
    assert normalized["route_health"]["orchestrator_path"] == "search_route_adapter"
    assert normalized["route_health"]["decision_node"] == "agent_products_search"
    assert normalized["route_health"]["domain_filter_dropped_external"] == 3
    assert normalized["route_health"]["external_fill_gate_reason"] == "cache_hit"
    assert normalized["route_health"]["semantic_retry_applied"] is True
    assert normalized["route_health"]["semantic_retry_query"] == "perfume fragrance"
    assert normalized["route_health"]["semantic_retry_hits"] == 2
    assert normalized["route_health"]["external_seed_brand_strict_rows"] == 12
    assert normalized["route_health"]["external_seed_brand_relevant_rows"] == 10
    assert normalized["route_health"]["external_seed_broad_fallback_used"] is True
    assert normalized["route_health"]["external_seed_broad_scope_rows"] == 24
    assert normalized["fallback_reason"] == "semantic_retry_exhausted"
    assert normalized["route_health"]["fallback_reason"] == "semantic_retry_exhausted"


def test_gateway_route_health_syncs_fallback_reason_bidirectionally():
    metadata = {
        "route_health": {
            "fallback_reason": "upstream_timeout_fallback",
        }
    }
    normalized = agent_shop_gateway._normalize_gateway_route_health(
        metadata,
        default_decision_node="test_node",
    )
    assert normalized["fallback_reason"] == "upstream_timeout_fallback"
    assert normalized["route_health"]["fallback_reason"] == "upstream_timeout_fallback"


def test_gateway_external_seed_dedupe_softens_in_prune_mode():
    wrapper = {
        "product": {
            "product_id": "ext-1",
            "title": "Fenty Beauty Gloss",
            "price": 29.0,
            "currency": "USD",
            "vendor": "Fenty",
        }
    }
    full_offer_keys = agent_shop_gateway._build_offer_keys(
        "Fenty Beauty Gloss",
        29.0,
        "USD",
        "Fenty",
    )
    offer_keys = {next(iter(full_offer_keys))}

    original = agent_shop_gateway.SEARCH_EXTERNAL_HARD_RULE_PRUNE
    try:
        agent_shop_gateway.SEARCH_EXTERNAL_HARD_RULE_PRUNE = True
        kept_pruned = agent_shop_gateway._filter_external_seed_wrappers([wrapper], offer_keys, set())
        assert len(kept_pruned) == 1

        agent_shop_gateway.SEARCH_EXTERNAL_HARD_RULE_PRUNE = False
        kept_relaxed = agent_shop_gateway._filter_external_seed_wrappers([wrapper], offer_keys, set())
        assert len(kept_relaxed) == 1
    finally:
        agent_shop_gateway.SEARCH_EXTERNAL_HARD_RULE_PRUNE = original


def test_gateway_fragrance_retry_query_never_noop_for_perfume():
    retry_query = agent_shop_gateway._build_fragrance_semantic_retry_query("perfume")
    assert isinstance(retry_query, str)
    assert retry_query
    assert retry_query != "perfume"
    assert "fragrance" in retry_query or "parfum" in retry_query or "cologne" in retry_query
