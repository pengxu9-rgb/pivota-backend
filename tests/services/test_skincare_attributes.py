from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.skincare_attributes import (
    detect_fragrance_free,
    extract_format,
    extract_spf_value,
    merge_concentration_into_actives,
)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Centella Hydrating Serum", "serum"),
        ("Rice Water Cleansing Foam", "cleanser"),
        ("Snail Mucin Essence", "essence"),
        ("Daily Sun Cream SPF50+ PA++++", "sunscreen"),
        ("Aloe Soothing Sheet Mask", "sheet mask"),
        ("Ceramide Moisturizer", "cream"),
        ("Vitamin C Ampoule", "ampoule"),
        ("Brightening Eye Cream", "eye cream"),
        ("Just a Tote Bag", None),
    ],
)
def test_extract_format(title, expected):
    assert extract_format(title=title) == expected


def test_extract_format_uses_category_path_and_product_type():
    assert extract_format(category_path="beauty/skincare/treat/toner") == "toner"
    assert extract_format(product_type="Face Oil") == "oil"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Daily Sun Cream SPF 50+", 50),
        ("Mineral Sunscreen SPF30", 30),
        ("SPF15 tinted moisturizer", 15),
        ("Hydrating serum, no sun protection", None),
        ("SPF 999 typo", None),  # out of plausible range
    ],
)
def test_extract_spf_value(text, expected):
    assert extract_spf_value(text) == expected


def test_detect_fragrance_free_only_on_explicit_claim():
    assert detect_fragrance_free("Gentle Toner, fragrance-free") is True
    assert detect_fragrance_free("No added fragrance, for sensitive skin") is True
    assert detect_fragrance_free("Parfum-free formula") is True
    # "unscented" is NOT treated as fragrance-free (may contain masking fragrance).
    assert detect_fragrance_free("Unscented lotion") is False
    assert detect_fragrance_free("Rose-scented cream") is False


def test_merge_concentration_from_list_of_dicts():
    actives = [{"label": "Niacinamide"}, {"label": "Centella Asiatica"}]
    notes = [{"ingredient": "Niacinamide", "concentration": "5%"}]
    merged = merge_concentration_into_actives(actives, notes)
    assert merged[0] == {"label": "Niacinamide", "concentration": "5%"}
    assert merged[1] == {"label": "Centella Asiatica"}  # untouched, no note


def test_merge_concentration_from_mapping_and_preserves_existing():
    actives = [{"label": "Retinol", "concentration": "0.3%"}, {"label": "Vitamin C"}]
    notes = {"Retinol": "1%", "Vitamin C": "10%"}
    merged = merge_concentration_into_actives(actives, notes)
    # Existing concentration is preserved, not overwritten.
    assert merged[0]["concentration"] == "0.3%"
    assert merged[1]["concentration"] == "10%"


def test_merge_concentration_noop_without_notes():
    actives = [{"label": "Niacinamide"}]
    assert merge_concentration_into_actives(actives, None) == actives
    assert merge_concentration_into_actives(actives, []) == actives
