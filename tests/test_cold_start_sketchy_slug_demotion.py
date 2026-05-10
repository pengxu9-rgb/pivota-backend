"""
Cold-start audit SKU picker demotes draft / duplicate / test PDP slugs.

Reproduced the Grüns failure: catalog-intelligence returned
`gruns.co/products/gruns-copy` ("Mother's Day Bundle - Adults + Kids")
in the first slot of the candidate list. The audit ran against that
sketchy SKU instead of the flagship Greens Gummies (which Forbes etc.
explicitly cite by name) and scored 0/0/0.

Fix: `_demote_sketchy_pdp_slugs` moves products with `-copy`,
`-draft`, `-test`, `-clone`, `-archive`, `-old`, `-backup`,
`-duplicate`, `-temp` slug suffixes (and `_` variants) to the END
of the candidate list. Doesn't drop them — if every product is
sketchy, the audit still proceeds against them.
"""

from __future__ import annotations


def test_copy_suffix_demoted():
    from services.bd_cold_start_service import _demote_sketchy_pdp_slugs
    products = [
        {"title": "Mother's Day Bundle", "pdp_url": "https://gruns.co/products/gruns-copy"},
        {"title": "Greens Gummies", "pdp_url": "https://gruns.co/products/greens-gummies"},
    ]
    out = _demote_sketchy_pdp_slugs(products)
    assert out[0]["title"] == "Greens Gummies"
    assert out[1]["title"] == "Mother's Day Bundle"


def test_draft_suffix_demoted():
    from services.bd_cold_start_service import _demote_sketchy_pdp_slugs
    products = [
        {"title": "Test product", "pdp_url": "https://example.com/products/winter-collection-draft"},
        {"title": "Real product", "pdp_url": "https://example.com/products/winter-collection"},
    ]
    out = _demote_sketchy_pdp_slugs(products)
    assert out[0]["title"] == "Real product"
    assert out[1]["title"] == "Test product"


def test_underscore_suffix_demoted():
    """Some platforms use underscore suffixes (Wix, custom Shopify
    namespacing). Same rule applies."""
    from services.bd_cold_start_service import _demote_sketchy_pdp_slugs
    products = [
        {"title": "Sketchy", "pdp_url": "https://example.com/products/serum_test"},
        {"title": "Clean", "pdp_url": "https://example.com/products/serum"},
    ]
    out = _demote_sketchy_pdp_slugs(products)
    assert out[0]["title"] == "Clean"


def test_query_string_does_not_break_slug_extraction():
    """PDP URLs in the wild often have UTM / tracking params. Extract
    the slug from the path, ignore the query string."""
    from services.bd_cold_start_service import _has_sketchy_pdp_slug
    assert _has_sketchy_pdp_slug({
        "pdp_url": "https://gruns.co/products/gruns-copy?utm_source=instagram",
    }) is True
    assert _has_sketchy_pdp_slug({
        "pdp_url": "https://gruns.co/products/greens-gummies?utm_source=instagram",
    }) is False


def test_trailing_slash_handled():
    from services.bd_cold_start_service import _has_sketchy_pdp_slug
    assert _has_sketchy_pdp_slug({
        "pdp_url": "https://gruns.co/products/gruns-copy/",
    }) is True


def test_substring_in_middle_not_flagged():
    """Conservative match: only suffixes count. A product slug like
    `vintage-test-tube-display` (the literal product) shouldn't be
    flagged as sketchy."""
    from services.bd_cold_start_service import _has_sketchy_pdp_slug
    assert _has_sketchy_pdp_slug({
        "pdp_url": "https://example.com/products/vintage-test-tube-display",
    }) is False
    assert _has_sketchy_pdp_slug({
        "pdp_url": "https://example.com/products/copy-paste-toolkit",
    }) is False


def test_all_sketchy_returns_unchanged_order():
    """When every product is sketchy, demotion is a no-op (the audit
    still picks them — better to audit a draft SKU than fail with
    no products at all)."""
    from services.bd_cold_start_service import _demote_sketchy_pdp_slugs
    products = [
        {"title": "A", "pdp_url": "https://x.com/products/a-copy"},
        {"title": "B", "pdp_url": "https://x.com/products/b-draft"},
    ]
    out = _demote_sketchy_pdp_slugs(products)
    assert [p["title"] for p in out] == ["A", "B"]


def test_empty_or_missing_pdp_url_does_not_flag():
    from services.bd_cold_start_service import _has_sketchy_pdp_slug
    assert _has_sketchy_pdp_slug({"pdp_url": ""}) is False
    assert _has_sketchy_pdp_slug({}) is False
    assert _has_sketchy_pdp_slug({"pdp_url": None}) is False
