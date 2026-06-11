"""The per-SKU next-best-action surfaces 'get indexed first' for un-indexed SKUs.

The v3 per-SKU report renders next_best_action (not the legacy select_playbooks
actions), so the get-indexed step has to live here to reach the merchant. An
un-indexed / not-serving SKU must get indexing as its #1 move, ahead of content
or lane work — you can't be cited if you're not live.
"""

from __future__ import annotations

from services.next_best_action import (
    PRIMARY_SKU_GET_INDEXED,
    build_sku_next_best_action,
)

_BANNED = ("_", "/", "serving_eligible", "pipeline", "catalog_", "content_key", "score")


def _scores(serving_points):
    return {
        "identity": {"score": 73, "breakdown": {}},
        "content_richness": {"score": 18, "breakdown": {}},
        "routability": {
            "score": 30,
            "breakdown": {"serving_eligibility": {"points": serving_points, "max": 40}},
        },
        "citation": {"score": 0, "breakdown": {}},
    }


def test_unindexed_sku_gets_indexing_as_primary_move():
    nba = build_sku_next_best_action(
        opportunity={},
        primary_gaps=[
            {"dimension": "routability", "bucket": "serving_eligibility",
             "gap": 40, "max": 40, "label": "Discoverable by AI", "why": "not live yet"},
        ],
        scores=_scores(0),
        identity={},
        sku_title="Good Night Collagen",
    )
    assert nba["primary_gap"] == PRIMARY_SKU_GET_INDEXED
    assert "index" in nba["headline"].lower()
    assert nba["first_move"] and nba["cta"]["label"]
    # merchant-safe copy in the rendered fields
    for field in ("headline", "why_this_first", "first_move"):
        text = nba[field].lower()
        for banned in ("serving_eligible", "pipeline", "catalog_", "content_key", "_score"):
            assert banned not in text, (field, banned)


def test_indexing_beats_content_and_lane_gaps():
    # Even with an open lane + content gap present, indexing must win.
    nba = build_sku_next_best_action(
        opportunity={"open_lanes": [{"query": "best collagen", "why_fit": ["x"]}]},
        primary_gaps=[
            {"dimension": "content_richness", "bucket": "product_quality_score",
             "gap": 25, "max": 25, "label": "Richer product detail", "why": "thin"},
            {"dimension": "routability", "bucket": "serving_eligibility",
             "gap": 40, "max": 40, "label": "Discoverable by AI", "why": "not live"},
        ],
        scores=_scores(0),
        identity={},
        sku_title="X",
    )
    assert nba["primary_gap"] == PRIMARY_SKU_GET_INDEXED


def test_served_sku_does_not_get_indexing_move():
    nba = build_sku_next_best_action(
        opportunity={},
        primary_gaps=[],
        scores=_scores(40),  # fully serving-eligible
        identity={},
        sku_title="X",
    )
    assert nba["primary_gap"] != PRIMARY_SKU_GET_INDEXED


def test_breakdown_absent_falls_back_to_gap_signal():
    # No routability breakdown, but a full serving_eligibility gap is present.
    scores = {
        "identity": {"score": 70},
        "content_richness": {"score": 20},
        "routability": {"score": 30},
        "citation": {"score": 0},
    }
    nba = build_sku_next_best_action(
        opportunity={},
        primary_gaps=[
            {"dimension": "routability", "bucket": "serving_eligibility",
             "gap": 40, "max": 40, "label": "Discoverable by AI", "why": "not live"},
        ],
        scores=scores,
        identity={},
        sku_title="X",
    )
    assert nba["primary_gap"] == PRIMARY_SKU_GET_INDEXED
