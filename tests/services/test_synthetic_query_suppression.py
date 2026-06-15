"""Synthetic-placeholder probe queries ("<title> shopper question N") must never
reach a real open lane or merchant-facing action copy (the "Own the answer to
'… shopper question 15'" leak)."""

from services.sku_lane_priority import is_synthetic_probe_query
from services.sku_opportunity import _top_open_lanes
from services.next_best_action import _sku_query_phrase


def test_is_synthetic_probe_query():
    assert is_synthetic_probe_query("Aruen Tofu Collagen shopper question 15")
    assert is_synthetic_probe_query("best collagen shopper question 1")
    assert is_synthetic_probe_query("  X shopper question 99  ")
    assert is_synthetic_probe_query("SHOPPER QUESTION 3")
    assert not is_synthetic_probe_query("best collagen cream for dry skin")
    assert not is_synthetic_probe_query("where to buy Aruen")
    assert not is_synthetic_probe_query("does it answer the shopper question well")  # no trailing N
    assert not is_synthetic_probe_query("")
    assert not is_synthetic_probe_query(None)


def test_top_open_lanes_excludes_synthetic_queries():
    per_prompt = [
        {
            "open_lane": True, "query_class": "category",
            "query": "best collagen jelly cream", "opportunity_score": 0.9,
            "attribute_basis": ["collagen"],
        },
        {
            "open_lane": True, "query_class": "category",
            "query": "Aruen shopper question 7", "opportunity_score": 0.95,
            "attribute_basis": ["collagen"],
        },
    ]
    lanes = _top_open_lanes(per_prompt)
    queries = [lane["query"] for lane in lanes]
    assert "best collagen jelly cream" in queries
    assert all("shopper question" not in (q or "") for q in queries)


def test_sku_query_phrase_suppresses_placeholder():
    # A real query is quoted verbatim; a placeholder degrades to a generic phrase
    # so it never leaks into an action headline.
    assert _sku_query_phrase("best collagen cream") == '"best collagen cream"'
    assert _sku_query_phrase("Aruen shopper question 12") == "this product's category question"
    assert _sku_query_phrase("") == "this product's category question"
