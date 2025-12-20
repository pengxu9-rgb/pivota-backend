from utils.agent_search_intent import infer_query_overrides


def test_infer_query_overrides_detects_brush_intent_japanese_and_normalizes():
    query = "来週、aespaのNingningのメイクを一通り描くけど、そのメイクに合うブラシのおすすめはある？"
    overrides = infer_query_overrides(query=query, category=None)

    assert overrides["brush_intent"] is True
    assert overrides["category"] == "brush"
    # Long natural-language queries normalize to a stable token for matching.
    assert overrides["query"] == "brush"
    assert "brush" in overrides["terms"]


def test_infer_query_overrides_keeps_non_brush_query():
    overrides = infer_query_overrides(query="red dress", category=None)
    assert overrides["brush_intent"] is False
    assert overrides["category"] is None
    assert overrides["query"] == "red dress"
    assert "red" in overrides["terms"]
