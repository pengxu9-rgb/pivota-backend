from routes import agent_api


def test_resolve_retrieval_profile_for_fragrance_query():
    profile = agent_api._resolve_retrieval_profile(
        query_text="perfume for date night",
        category_text=None,
        profile_hint=None,
    )
    assert profile["id"] == "fragrance_strict"
    assert profile["confidence"] in {"high", "medium"}


def test_resolve_retrieval_profile_respects_hint():
    profile = agent_api._resolve_retrieval_profile(
        query_text="anything",
        category_text=None,
        profile_hint="lingerie",
    )
    assert profile["id"] == "lingerie_strict"
    assert profile["reason"] == "explicit_profile_hint"


def test_profile_filter_blocks_tools_for_fragrance():
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
    assert agent_api._passes_retrieval_profile_filter(brush_like, "fragrance_strict") is False
    assert agent_api._passes_retrieval_profile_filter(perfume_like, "fragrance_strict") is True
