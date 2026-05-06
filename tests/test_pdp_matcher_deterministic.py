"""Pure-function tests for services/pdp_matcher/deterministic.py.

No DB required — exercises the three deterministic matchers and their
priority ordering with hand-crafted seed + candidate dicts.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.pdp_matcher.deterministic import (  # noqa: E402
    canonical_url_match,
    matches_for_seed,
    normalize_canonical_url,
    source_product_id_match,
    title_brand_match,
    trigram_similarity,
)


def _seed(**kwargs):
    base = {
        "id": "ext_1",
        "external_product_id": None,
        "title": None,
        "canonical_url": None,
        "destination_url": None,
        "domain": None,
        "seed_data": None,
    }
    base.update(kwargs)
    return base


def _pdp(**kwargs):
    base = {
        "product_key": "pk_1",
        "source_product_id": None,
        "canonical_url": None,
        "title": None,
        "brand": None,
        "merchant_id": "merch_a",
    }
    base.update(kwargs)
    return base


# ---------- normalize_canonical_url ----------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://www.maccosmetics.com/products/ruby-woo", "https://maccosmetics.com/products/ruby-woo"),
        ("HTTPS://MACCOSMETICS.COM/products/ruby-woo/", "https://maccosmetics.com/products/ruby-woo"),
        ("https://maccosmetics.com/products/ruby-woo?ref=instagram&utm=foo", "https://maccosmetics.com/products/ruby-woo"),
        ("https://maccosmetics.com/products/ruby-woo#reviews", "https://maccosmetics.com/products/ruby-woo"),
        ("", ""),
        (None, ""),
        ("not a url", ""),
        ("https://maccosmetics.com/", "https://maccosmetics.com/"),
    ],
)
def test_normalize_canonical_url(raw, expected):
    assert normalize_canonical_url(raw) == expected


# ---------- trigram_similarity ----------

def test_trigram_similarity_identical():
    assert trigram_similarity("MAC Ruby Woo", "MAC Ruby Woo") == 1.0


def test_trigram_similarity_high_for_close_titles():
    sim = trigram_similarity("MAC Ruby Woo Lipstick", "MAC Ruby Woo Matte Lipstick")
    assert sim >= 0.6


def test_trigram_similarity_low_for_unrelated_titles():
    sim = trigram_similarity("MAC Ruby Woo Lipstick", "Sephora Glossy Eyeshadow Palette")
    assert sim < 0.3


def test_trigram_similarity_handles_empty():
    assert trigram_similarity("", "anything") == 0.0
    assert trigram_similarity(None, None) == 0.0


# ---------- source_product_id_match ----------

def test_source_id_match_unique_hit():
    seed = _seed(external_product_id="SKU-12345")
    candidates = [
        _pdp(product_key="pk_a", source_product_id="SKU-12345"),
        _pdp(product_key="pk_b", source_product_id="SKU-99999"),
    ]
    result = source_product_id_match(seed=seed, candidates=candidates)
    assert result is not None
    assert result["product_key"] == "pk_a"
    assert result["matcher"] == "source_product_id_match"
    assert result["confidence"] >= 0.9


def test_source_id_match_returns_none_when_ambiguous():
    seed = _seed(external_product_id="SKU-12345")
    candidates = [
        _pdp(product_key="pk_a", source_product_id="SKU-12345"),
        _pdp(product_key="pk_b", source_product_id="sku-12345"),  # case-insensitive collision
    ]
    assert source_product_id_match(seed=seed, candidates=candidates) is None


def test_source_id_match_returns_none_when_seed_empty():
    seed = _seed(external_product_id=None)
    candidates = [_pdp(product_key="pk_a", source_product_id="SKU-12345")]
    assert source_product_id_match(seed=seed, candidates=candidates) is None


def test_source_id_match_returns_none_when_no_candidates_match():
    seed = _seed(external_product_id="SKU-NOMATCH")
    candidates = [_pdp(product_key="pk_a", source_product_id="SKU-OTHER")]
    assert source_product_id_match(seed=seed, candidates=candidates) is None


def test_source_id_match_normalizes_punctuation():
    """A seed external_product_id with hyphens should still match a
    candidate source_product_id that has different punctuation."""
    seed = _seed(external_product_id="SKU-12345")
    candidates = [_pdp(product_key="pk_a", source_product_id="sku 12345")]
    result = source_product_id_match(seed=seed, candidates=candidates)
    assert result is not None
    assert result["product_key"] == "pk_a"


# ---------- canonical_url_match ----------

def test_canonical_url_match_exact():
    seed = _seed(canonical_url="https://www.maccosmetics.com/products/ruby-woo")
    candidates = [
        _pdp(product_key="pk_a", canonical_url="https://maccosmetics.com/products/ruby-woo"),
        _pdp(product_key="pk_b", canonical_url="https://sephora.com/products/something-else"),
    ]
    result = canonical_url_match(seed=seed, candidates=candidates)
    assert result is not None
    assert result["product_key"] == "pk_a"
    assert result["matcher"] == "canonical_url_match"


def test_canonical_url_match_fallback_to_destination_url():
    """When canonical_url is None, fall back to destination_url."""
    seed = _seed(canonical_url=None, destination_url="https://maccosmetics.com/products/ruby-woo?utm=ig")
    candidates = [_pdp(product_key="pk_a", canonical_url="https://maccosmetics.com/products/ruby-woo")]
    result = canonical_url_match(seed=seed, candidates=candidates)
    assert result is not None
    assert result["product_key"] == "pk_a"


def test_canonical_url_match_strips_query_string():
    seed = _seed(canonical_url="https://maccosmetics.com/products/ruby-woo?ref=foo")
    candidates = [_pdp(product_key="pk_a", canonical_url="https://maccosmetics.com/products/ruby-woo?ref=bar")]
    result = canonical_url_match(seed=seed, candidates=candidates)
    assert result is not None


def test_canonical_url_match_returns_none_when_seed_blank():
    seed = _seed(canonical_url=None, destination_url=None)
    candidates = [_pdp(product_key="pk_a", canonical_url="https://anything.com/foo")]
    assert canonical_url_match(seed=seed, candidates=candidates) is None


def test_canonical_url_match_returns_none_for_ambiguous():
    seed = _seed(canonical_url="https://maccosmetics.com/products/ruby-woo")
    candidates = [
        _pdp(product_key="pk_a", canonical_url="https://maccosmetics.com/products/ruby-woo"),
        _pdp(product_key="pk_b", canonical_url="https://www.maccosmetics.com/products/ruby-woo/"),  # post-normalize identical
    ]
    assert canonical_url_match(seed=seed, candidates=candidates) is None


# ---------- title_brand_match ----------

def test_title_brand_match_picks_best_above_threshold():
    seed = _seed(
        title="MAC Ruby Woo Matte Lipstick",
        domain="maccosmetics.com",
    )
    candidates = [
        _pdp(product_key="pk_a", title="MAC Ruby Woo Matte Lipstick", brand="MAC"),
        _pdp(product_key="pk_b", title="MAC Velvet Teddy Lipstick", brand="MAC"),
    ]
    result = title_brand_match(seed=seed, candidates=candidates)
    assert result is not None
    assert result["product_key"] == "pk_a"
    assert result["matcher"] == "title_brand_match"
    assert result["confidence"] >= 0.85


def test_title_brand_match_requires_brand_match():
    """Same title, different brand — should NOT match."""
    seed = _seed(
        title="MAC Ruby Woo Lipstick",
        domain="maccosmetics.com",
    )
    candidates = [_pdp(product_key="pk_a", title="MAC Ruby Woo Lipstick", brand="Charlotte Tilbury")]
    assert title_brand_match(seed=seed, candidates=candidates) is None


def test_title_brand_match_skips_below_threshold():
    seed = _seed(
        title="MAC Ruby Woo Matte Lipstick",
        domain="maccosmetics.com",
    )
    candidates = [_pdp(product_key="pk_a", title="MAC Lipglass Clear Gloss", brand="MAC")]
    assert title_brand_match(seed=seed, candidates=candidates) is None


def test_title_brand_match_uses_seed_data_brand():
    seed = _seed(
        title="Ruby Woo Matte Lipstick",
        seed_data={"brand": "MAC"},
        domain="sephora.com",  # multi-merchant — domain isn't a brand hint
    )
    candidates = [_pdp(product_key="pk_a", title="Ruby Woo Matte Lipstick", brand="MAC")]
    result = title_brand_match(seed=seed, candidates=candidates)
    assert result is not None


def test_title_brand_match_uses_first_word_as_brand_hint():
    seed = _seed(title="MAC Ruby Woo Matte Lipstick", domain=None)
    candidates = [
        _pdp(product_key="pk_a", title="MAC Ruby Woo Matte Lipstick", brand="MAC"),
    ]
    result = title_brand_match(seed=seed, candidates=candidates)
    assert result is not None


def test_title_brand_match_skips_multi_merchant_domain():
    """sephora.com hosts many brands — shouldn't be used as a brand hint."""
    seed = _seed(
        title="Charlotte Tilbury Pillow Talk Lipstick",
        domain="sephora.com",
    )
    candidates = [
        _pdp(product_key="pk_a", title="Charlotte Tilbury Pillow Talk Lipstick", brand="Charlotte Tilbury"),
    ]
    result = title_brand_match(seed=seed, candidates=candidates)
    # Title hint "charlotte" matches "charlotte tilbury" — should still match.
    assert result is not None


# ---------- matches_for_seed (priority) ----------

def test_priority_source_id_wins_over_url_match():
    seed = _seed(
        external_product_id="SKU-99",
        canonical_url="https://example.com/different-page",
    )
    candidates = [
        _pdp(product_key="pk_a", source_product_id="SKU-99"),
        _pdp(product_key="pk_b", canonical_url="https://example.com/different-page"),
    ]
    result = matches_for_seed(seed=seed, candidates=candidates)
    assert result is not None
    assert result["matcher"] == "source_product_id_match"
    assert result["product_key"] == "pk_a"


def test_priority_url_wins_over_title_match():
    seed = _seed(
        external_product_id=None,
        canonical_url="https://maccosmetics.com/p/ruby-woo",
        title="MAC Ruby Woo Lipstick",
        domain="maccosmetics.com",
    )
    candidates = [
        _pdp(product_key="pk_a", canonical_url="https://maccosmetics.com/p/ruby-woo"),
        _pdp(product_key="pk_b", title="MAC Ruby Woo Lipstick", brand="MAC"),
    ]
    result = matches_for_seed(seed=seed, candidates=candidates)
    assert result is not None
    assert result["matcher"] == "canonical_url_match"
    assert result["product_key"] == "pk_a"


def test_falls_through_to_title_when_source_id_and_url_miss():
    seed = _seed(
        external_product_id=None,
        canonical_url=None,
        title="MAC Ruby Woo Matte Lipstick",
        domain="maccosmetics.com",
    )
    candidates = [_pdp(product_key="pk_a", title="MAC Ruby Woo Matte Lipstick", brand="MAC")]
    result = matches_for_seed(seed=seed, candidates=candidates)
    assert result is not None
    assert result["matcher"] == "title_brand_match"


def test_no_match_returns_none():
    seed = _seed(
        external_product_id="SKU-LONELY",
        canonical_url="https://lonely.com/foo",
        title="Some Random Item",
    )
    candidates = [
        _pdp(product_key="pk_a", source_product_id="SKU-OTHER", canonical_url="https://other.com/x", title="Other"),
    ]
    result = matches_for_seed(seed=seed, candidates=candidates)
    assert result is None


def test_empty_candidates_returns_none():
    seed = _seed(external_product_id="SKU-1", title="MAC Lipstick")
    assert matches_for_seed(seed=seed, candidates=[]) is None


# ---------- output shape ----------

def test_match_result_shape():
    seed = _seed(external_product_id="SKU-1")
    candidates = [_pdp(product_key="pk_x", source_product_id="SKU-1")]
    result = matches_for_seed(seed=seed, candidates=candidates)
    assert result is not None
    assert set(result.keys()) >= {"product_key", "confidence", "matcher", "evidence"}
    assert isinstance(result["product_key"], str)
    assert 0.0 <= float(result["confidence"]) <= 1.0
    assert isinstance(result["matcher"], str)
