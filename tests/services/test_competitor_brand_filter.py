from __future__ import annotations

import pytest

from services.competitor_brand_filter import (
    filter_competitor_brands,
    is_ingredient_or_category_type,
)

# Generic ingredient / supplement / actives TYPES — must be classified as
# non-brand (the bug: these were surfacing as "competitors AI named").
INGREDIENT_TYPES = [
    "Magnesium",
    "Ashwagandha",
    "Probiotics",
    "Creatine",
    "Vitamin D",
    "Vitamin B12",
    "Calcium",
    "Iron",
    "Zinc",
    "Collagen",
    "Niacinamide",
    "Retinol",
    "Hyaluronic Acid",
    "Omega-3",
    "Fiber",
    "Turmeric",
    "Magnesium Glycinate",
    "Fish Oil",
    "Protein Powder",
    "Collagen Supplement",
    "Magnesium and Zinc",
]

# Real competitor brands / products — must be kept (each carries an identity
# token even when it also contains a category word like "Proteins"/"Nutrition").
REAL_BRANDS = [
    "Thorne",
    "Vital Proteins",
    "Ancient Nutrition",
    "The Ordinary",
    "Paula's Choice",
    "COSRX",
    "Pure Encapsulations",
    "Olaplex",
    "Ritual",
    "Optimum Nutrition",
    "Skinceuticals",
]


@pytest.mark.parametrize("name", INGREDIENT_TYPES)
def test_ingredient_types_are_classified_as_type(name):
    assert is_ingredient_or_category_type(name) is True


@pytest.mark.parametrize("name", REAL_BRANDS)
def test_real_brands_are_not_classified_as_type(name):
    assert is_ingredient_or_category_type(name) is False


def test_filter_drops_types_keeps_brands_and_preserves_order():
    names = ["Magnesium", "Thorne", "Ashwagandha", "Vital Proteins", "Vitamin D"]
    assert filter_competitor_brands(names) == ["Thorne", "Vital Proteins"]


def test_filter_returns_empty_when_only_types():
    assert filter_competitor_brands(["Magnesium", "Probiotics", "Iron"]) == []


def test_blank_and_connective_only_names_are_not_dropped_as_types():
    # No ingredient/category word present → not a type; the caller's own
    # emptiness/strip checks handle blanks. We must not over-claim "type".
    assert is_ingredient_or_category_type("") is False
    assert is_ingredient_or_category_type("   ") is False
    assert is_ingredient_or_category_type("and the") is False
