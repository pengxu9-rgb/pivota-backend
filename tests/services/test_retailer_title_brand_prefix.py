"""options.title_brands: a sub-brand the product's own title names, for an UMBRELLA vendor.

Measured 2026-09-26: wigtypes.com and beautyofnewyork.com list 38 "Red by Kiss ..." hair brushes and
7 "Kiss GoldFinger ..." nail items under vendor "Kiss New York", so every one of them was branded
"Kiss New York". brand_by_vendor cannot fix that -- it may only RESPELL a vendor (same letters), by
design -- and it should not: one vendor label covers several lines. The title does name the line.
"""
from __future__ import annotations

import pytest

from services.curated_brand_feed import shopify_product_to_record, title_brand_for
from services.retailer_ingest.pipeline import validate_options

PREFIXES = {"Red by Kiss": "Red by Kiss", "Kiss GoldFinger": "KISS", "Red": "Red"}


@pytest.mark.parametrize("title,want", [
    ("Red by Kiss HH65 Edge Boar Brush", "Red by Kiss"),
    ("RED BY KISS  HH43 Easy Grip Detangle Brush", "Red by Kiss"),    # case + stray spacing
    ("Kiss GoldFinger Snow Queen Limited Edition 24 Nails", "KISS"),
    ("Red-Hot Detangler", "Red"),                                    # hyphen after the prefix
])
def test_the_longest_named_prefix_wins(title, want):
    assert title_brand_for(title, PREFIXES) == want


@pytest.mark.parametrize("title", [
    "Kiss New York Professional - TOP BROW CREAM",   # no prefix applies: vendor-derived brand stays
    "Redken All Soft Shampoo",                        # "Red" must end at a word boundary
    "The Red by Kiss Brush",                          # a PREFIX, not a substring
    "",
    None,
])
def test_no_prefix_means_no_title_brand(title):
    assert title_brand_for(title, PREFIXES) is None


def test_no_prefixes_is_a_no_op():
    assert title_brand_for("Red by Kiss HH65 Edge Boar Brush", None) is None
    assert title_brand_for("Red by Kiss HH65 Edge Boar Brush", {}) is None


def _product(title, vendor="Kiss New York"):
    return {"title": title, "handle": "h-" + str(abs(hash(title))), "vendor": vendor, "product_type": "Brush",
            "variants": [{"id": 1, "price": "9.99", "available": True, "sku": "S1"}],
            "images": [{"src": "https://wigtypes.com/i.jpg"}]}


def test_the_record_takes_the_title_brand_over_the_umbrella_vendor():
    rec = shopify_product_to_record(_product("Red by Kiss HH65 Edge Boar Brush"), domain="wigtypes.com",
                                    category_path="beauty", brand_override="Kiss New York",
                                    source_role="retailer", retailer_name="WigTypes",
                                    title_brand="Red by Kiss")
    assert rec is not None
    assert rec["pdp"]["brand"] == "Red by Kiss"


def test_without_a_title_brand_the_record_is_unchanged():
    rec = shopify_product_to_record(_product("Kiss New York Professional - TOP BROW CREAM"),
                                    domain="wigtypes.com", category_path="beauty",
                                    brand_override="Kiss New York", source_role="retailer",
                                    retailer_name="WigTypes")
    assert rec is not None
    assert rec["pdp"]["brand"] == "Kiss New York"


def _options(**extra):
    return {"vendors": ["Kiss New York"], "multi_brand": True, "brands": {"Kiss New York": "Kiss New York"},
            **extra}


def test_validate_accepts_prefixes_that_name_their_brand():
    validate_options(_options(title_brands={"Red by Kiss": "Red by Kiss", "Kiss GoldFinger": "KISS"}))


@pytest.mark.parametrize("title_brands,needle", [
    ({"Red by Kiss": "Revlon"}, "named in its title prefix"),      # a relabel, not a sub-brand
    ({"Kiss GoldFinger": "KISS New York"}, "named in its title prefix"),
    ({"Red": "Red"}, r"4\+ letters"),                                  # too short to be evidence
    ({}, r"4\+ letters"),
    ({"Red by Kiss": ""}, r"4\+ letters"),
])
def test_validate_refuses_a_brand_the_prefix_does_not_name(title_brands, needle):
    with pytest.raises(ValueError, match=needle):
        validate_options(_options(title_brands=title_brands))


def test_validate_refuses_title_brands_outside_a_multi_brand_cohort():
    with pytest.raises(ValueError, match="only meaningful with options.multi_brand"):
        validate_options({"vendors": ["Kiss New York"], "title_brands": {"Red by Kiss": "Red by Kiss"}})
