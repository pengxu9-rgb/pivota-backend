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
    assert agent_api._has_fragrance_signal("Le Labo Santal 33") is True


def test_beauty_soft_penalty_prefers_sunscreen_over_serum_for_sunscreen_query():
    sunscreen_like = {
        "title": "Lightweight Face Sunscreen SPF 50",
        "category": "face sunscreen",
        "product_type": "sunscreen",
        "tags": ["spf", "sunscreen", "face"],
    }
    serum_like = {
        "title": "Niacinamide 10% + Zinc 1% Serum",
        "category": "serum",
        "product_type": "serum",
        "tags": ["niacinamide", "oil control"],
    }

    sunscreen_score = agent_api._apply_semantic_soft_penalty(
        product=sunscreen_like,
        score=0.6,
        query_semantic_class="beauty",
        normalized_query="best sunscreen for oily skin",
    )
    serum_score = agent_api._apply_semantic_soft_penalty(
        product=serum_like,
        score=0.6,
        query_semantic_class="beauty",
        normalized_query="best sunscreen for oily skin",
    )

    assert sunscreen_score > serum_score
    assert sunscreen_score > 0.6
    assert serum_score < 0.2


def test_beauty_soft_penalty_demotes_lip_and_body_candidates_for_treatment_query():
    treatment_like = {
        "title": "Oil Control Treatment Serum",
        "category": "serum treatment",
        "product_type": "serum",
        "tags": ["oil control", "treatment"],
    }
    lip_like = {
        "title": "Overnight Lip Treatment",
        "category": "lip care",
        "product_type": "lip balm",
        "tags": ["lip treatment"],
    }
    body_like = {
        "title": "Body Oil for Dry Skin",
        "category": "body oil",
        "product_type": "body oil",
        "tags": ["body", "oil"],
    }

    treatment_score = agent_api._apply_semantic_soft_penalty(
        product=treatment_like,
        score=0.55,
        query_semantic_class="beauty",
        normalized_query="oil control treatment",
    )
    lip_score = agent_api._apply_semantic_soft_penalty(
        product=lip_like,
        score=0.55,
        query_semantic_class="beauty",
        normalized_query="oil control treatment",
    )
    body_score = agent_api._apply_semantic_soft_penalty(
        product=body_like,
        score=0.55,
        query_semantic_class="beauty",
        normalized_query="oil control treatment",
    )

    assert treatment_score > lip_score
    assert treatment_score > body_score
    assert lip_score < 0.15
    assert body_score < 0.15
