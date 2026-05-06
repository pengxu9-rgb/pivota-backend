"""Phase 2b — verify the recall query path's category_path resolver.

`category_path_prefix_for_query` is the function that turns a free-form
user query into a category prefix that pivot_query_service.py uses to
bias recall toward catalog_products.category_path matches.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.pdp_category_classifier import (  # noqa: E402
    category_path_prefix_for_query,
    classify,
    resolve_path_from_row,
)


@pytest.mark.parametrize(
    "query,expected_prefix",
    [
        ("lipstick", "beauty/makeup/lip/"),
        ("matte lipstick under $30", "beauty/makeup/lip/"),
        ("nude lipstick everyday", "beauty/makeup/lip/"),
        # ZH after PR-04 expansion: "口红" → "口红 lipstick" reaches recall
        ("口红 lipstick", "beauty/makeup/lip/"),
        ("foundation", "beauty/makeup/face/"),
        ("cushion foundation", "beauty/makeup/face/"),
        ("mascara", "beauty/makeup/eye/"),
        ("waterproof volumizing mascara", "beauty/makeup/eye/"),
        ("vanilla perfume", "beauty/fragrance/"),
        ("eau de parfum", "beauty/fragrance/"),
        ("sunscreen", "beauty/skincare/sun/"),
        ("spf 50", "beauty/skincare/sun/"),
        ("hyaluronic acid serum", "beauty/skincare/treat/"),
        ("blemish treatment", "beauty/skincare/treat/"),
        ("gentle cleanser", "beauty/skincare/cleanse/"),
        ("face wash", "beauty/skincare/cleanse/"),
        ("barrier moisturizer", "beauty/skincare/moisturize/"),
        ("night cream", "beauty/skincare/moisturize/"),
    ],
)
def test_category_prefix_extracts_2_segments(query: str, expected_prefix: str) -> None:
    """Standard category queries resolve to the parent prefix (3 path
    segments, ending in /). The prefix is what
    pivot_query_service.py uses to bias the SQL toward category_path
    LIKE prefix||'%' matches."""
    assert category_path_prefix_for_query(query) == expected_prefix


@pytest.mark.parametrize(
    "query",
    [
        "",
        None,
        "buy now",
        "best deal under $50",
        "happy birthday",
        "random unrelated string",
    ],
)
def test_category_prefix_returns_none_for_unrelated_queries(query) -> None:
    """When the query doesn't match any category, recall should fall
    back to the existing trigram text scan — return None to signal that."""
    assert category_path_prefix_for_query(query) is None


def test_classify_priority_lipstick_over_lip_balm() -> None:
    """Lip Balm pattern is registered before Lipstick. Verify a string
    matching only lipstick-specific tokens still resolves to Lipstick
    (not silently falling into Lip Balm)."""
    hit = classify("liquid lip lacquer")
    assert hit is not None
    assert hit[0] == "Lipstick"


def test_classify_handles_none_and_empty() -> None:
    assert classify(None) is None
    assert classify("") is None
    assert resolve_path_from_row(category=None, product_type=None, title=None) is None
