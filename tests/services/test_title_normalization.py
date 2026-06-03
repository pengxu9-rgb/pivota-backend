from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.bd_cold_start_service import normalize_product_title_for_search


@pytest.mark.parametrize(
    ("raw_title", "expected"),
    [
        (
            "[Bundle] Good Night Collagen (Low-Molecular Weight Collagen), "
            "30 sticks x 2box",
            "Good Night Collagen",
        ),
        ("[Bundle] White Up Plus, 30 tablets x 6box", "White Up Plus"),
        (
            "[Bundle] Slimming Collagen 84 tablets x 3box",
            "Slimming Collagen",
        ),
        (
            "[Bundle] Organic Olive Lemon Dual Shot 65:35, 10 Sticks x 3box",
            "Organic Olive Lemon Dual Shot 65:35",
        ),
    ],
)
def test_normalize_product_title_for_search_bblab_titles(raw_title, expected):
    assert normalize_product_title_for_search(raw_title) == expected


def test_normalize_product_title_for_search_is_idempotent_for_clean_title():
    assert (
        normalize_product_title_for_search("Good Night Collagen")
        == "Good Night Collagen"
    )


def test_normalize_product_title_for_search_keeps_specific_short_titles():
    assert normalize_product_title_for_search("Magnesium 500mg") == "Magnesium 500mg"


def test_normalize_product_title_for_search_never_returns_empty_for_noise():
    assert normalize_product_title_for_search("[Bundle], 2box") != ""
