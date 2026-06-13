"""The per-SKU CTA carries an executable action descriptor.

The frontend wires a real "have Pivota do it" button to the per-SKU CTA. For
that it needs {action, target_sku_key} on the cta, not just a label — otherwise
a wired button has nowhere to POST. This guards the descriptor contract:
  - get-indexed -> request_indexing
  - content/page fixes -> request_enrichment
  - monitor-only -> none
  - target_sku_key is threaded through from the report builder
  - every per-SKU primary gap maps to a known action value
"""

from __future__ import annotations

from services.next_best_action import (
    PRIMARY_SKU_CONTENT_REVISION_GAP,
    PRIMARY_SKU_GET_INDEXED,
    SKU_CTA_ACTIONS,
    _SKU_CTA_ACTION,
    build_sku_next_best_action,
)


def _scores(serving_points, content_score=18):
    return {
        "identity": {"score": 73, "breakdown": {}},
        "content_richness": {"score": content_score, "breakdown": {}},
        "routability": {
            "score": 30,
            "breakdown": {"serving_eligibility": {"points": serving_points, "max": 40}},
        },
        "citation": {"score": 0, "breakdown": {}},
    }


def test_get_indexed_cta_requests_indexing_with_target():
    nba = build_sku_next_best_action(
        opportunity={},
        primary_gaps=[
            {"dimension": "routability", "bucket": "serving_eligibility",
             "gap": 40, "max": 40, "label": "Discoverable by AI", "why": "not live"},
        ],
        scores=_scores(0),
        identity={},
        sku_title="Good Night Collagen",
        sku_key="shopify:gid://shopify/ProductVariant/42",
    )
    assert nba["primary_gap"] == PRIMARY_SKU_GET_INDEXED
    cta = nba["cta"]
    assert cta["action"] == "request_indexing"
    assert cta["target_sku_key"] == "shopify:gid://shopify/ProductVariant/42"
    assert cta["label"]


def test_content_gap_cta_requests_enrichment():
    nba = build_sku_next_best_action(
        opportunity={},
        primary_gaps=[
            {"dimension": "content_richness", "bucket": "product_quality_score",
             "gap": 25, "max": 25, "label": "Richer product detail", "why": "thin"},
        ],
        scores=_scores(40, content_score=20),  # serving-eligible, so content wins
        identity={},
        sku_title="X",
        sku_key="sku_123",
    )
    assert nba["primary_gap"] == PRIMARY_SKU_CONTENT_REVISION_GAP
    assert nba["cta"]["action"] == "request_enrichment"
    assert nba["cta"]["target_sku_key"] == "sku_123"


def test_no_sku_key_omits_target():
    nba = build_sku_next_best_action(
        opportunity={},
        primary_gaps=[
            {"dimension": "routability", "bucket": "serving_eligibility",
             "gap": 40, "max": 40, "label": "Discoverable by AI", "why": "not live"},
        ],
        scores=_scores(0),
        identity={},
        sku_title="X",
    )
    # action is still present; target is simply not stamped
    assert nba["cta"]["action"] == "request_indexing"
    assert "target_sku_key" not in nba["cta"]


def test_every_sku_gap_maps_to_a_known_action():
    from services import next_best_action as nba_mod

    sku_gaps = {
        getattr(nba_mod, name)
        for name in dir(nba_mod)
        if name.startswith("PRIMARY_SKU_")
    }
    assert set(_SKU_CTA_ACTION) == sku_gaps, (
        "every PRIMARY_SKU_* gap needs a CTA action mapping"
    )
    for action in _SKU_CTA_ACTION.values():
        assert action in SKU_CTA_ACTIONS, action
