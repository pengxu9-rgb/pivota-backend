from routes import agent_api


def test_resolve_retrieval_profile_for_fragrance_query():
    profile = agent_api._resolve_retrieval_profile(
        query_text="perfume for date night",
        category_text=None,
        profile_hint=None,
    )
    assert profile["id"] == "fragrance_strict"
    assert profile["confidence"] in {"high", "medium"}


def test_resolve_retrieval_profile_for_brand_query_without_category():
    profile = agent_api._resolve_retrieval_profile(
        query_text="tom ford",
        category_text=None,
        profile_hint=None,
    )
    assert profile["id"] == "brand_broad"
    assert profile["reason"] in {"brand_keyword_match", "explicit_profile_hint"}


def test_resolve_retrieval_profile_for_brand_query_with_category():
    profile = agent_api._resolve_retrieval_profile(
        query_text="dior perfume",
        category_text=None,
        profile_hint=None,
    )
    assert profile["id"] == "fragrance_strict"


def test_resolve_retrieval_profile_respects_hint():
    profile = agent_api._resolve_retrieval_profile(
        query_text="anything",
        category_text=None,
        profile_hint="lingerie",
    )
    assert profile["id"] == "lingerie_strict"
    assert profile["reason"] == "explicit_profile_hint"


def test_profile_filter_is_recall_first_for_fragrance():
    original = agent_api.SEARCH_EXTERNAL_HARD_RULE_PRUNE
    agent_api.SEARCH_EXTERNAL_HARD_RULE_PRUNE = False
    brush_like = {
        "title": "Makeup Brush Set",
        "category": "beauty tools",
        "product_type": "brush",
        "tags": ["tool", "brush"],
    }
    perfume_like = {
        "title": "Eau de Parfum Spray",
        "category": "fragrance",
        "product_type": "perfume",
        "tags": ["parfum"],
    }
    try:
        assert agent_api._passes_retrieval_profile_filter(brush_like, "fragrance_strict") is True
        assert agent_api._passes_retrieval_profile_filter(perfume_like, "fragrance_strict") is True
        # Contract-facing semantic class is normalized to "fragrance".
        assert agent_api._passes_retrieval_profile_filter(brush_like, "fragrance") is True
        assert agent_api._passes_retrieval_profile_filter(perfume_like, "fragrance") is True
    finally:
        agent_api.SEARCH_EXTERNAL_HARD_RULE_PRUNE = original


def test_profile_filter_keeps_tool_candidate_when_prune_enabled():
    original = agent_api.SEARCH_EXTERNAL_HARD_RULE_PRUNE
    agent_api.SEARCH_EXTERNAL_HARD_RULE_PRUNE = True
    mixed_like = {
        "title": "Fragrance Discovery Brush Set",
        "category": "fragrance beauty tools",
        "product_type": "perfume brush",
        "tags": ["tool", "brush", "fragrance"],
    }
    try:
        assert agent_api._passes_retrieval_profile_filter(mixed_like, "fragrance") is True
    finally:
        agent_api.SEARCH_EXTERNAL_HARD_RULE_PRUNE = original


def test_has_fragrance_signal_supports_compact_tokens():
    assert agent_api._has_fragrance_signal("EAUDEPARFUM collection") is True
    assert agent_api._has_fragrance_signal("best bodymist picks") is True
