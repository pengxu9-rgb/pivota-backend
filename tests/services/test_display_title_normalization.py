from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.title_normalization import normalize_display_title


@pytest.mark.parametrize(
    ("raw_title", "expected"),
    [
        # The audit's canonical leak: legal-entity prefix + bundle tag + "'s page".
        (
            "NUTRIONE CO., LTD [Bundle] Good Night Collagen 30 sticks's page",
            "Good Night Collagen 30 sticks",
        ),
        # Bundle tag stripped, size KEPT (unlike the search normalizer).
        (
            "[Bundle] Good Night Collagen, 30 sticks x 2box",
            "Good Night Collagen, 30 sticks x 2box",
        ),
        # Multiple leading tags.
        ("[SALE] [New] White Up Plus 30 tablets", "White Up Plus 30 tablets"),
        # Legal-entity prefix without a bracket tag.
        ("Acme Inc. - Vitamin C Serum 30ml", "Vitamin C Serum 30ml"),
        ("Foo Co.,Ltd : Hydrating Toner 150ml", "Hydrating Toner 150ml"),
    ],
)
def test_normalize_display_title_strips_noise_keeps_size(raw_title, expected):
    assert normalize_display_title(raw_title) == expected


def test_normalize_display_title_is_idempotent_for_clean_title():
    clean = "Good Night Collagen 30 sticks"
    assert normalize_display_title(clean) == clean
    assert normalize_display_title(normalize_display_title(clean)) == clean


def test_normalize_display_title_keeps_size_only_titles():
    # A short, size-bearing product name must survive intact.
    assert normalize_display_title("Magnesium 500mg") == "Magnesium 500mg"


def test_normalize_display_title_never_returns_empty_for_degenerate_input():
    # No real product name present -> return the original, never empty.
    assert normalize_display_title("[Bundle] Ltd") != ""


def test_normalize_display_title_backs_off_to_preserve_two_word_product():
    # Entity regex would match "Corp" and over-strip to a single token; the
    # backoff keeps the real two-word product name intact.
    assert normalize_display_title("Corp Wellness") == "Corp Wellness"


def test_normalize_display_title_does_not_strip_product_named_like_entity():
    # "Limited Edition" is part of the product, not a seller prefix; the
    # backoff (>=2 tokens must remain) keeps it from being over-stripped.
    out = normalize_display_title("Limited Edition Rose Serum")
    assert "Rose Serum" in out


def test_normalize_display_title_handles_empty_and_none():
    assert normalize_display_title("") == ""
    assert normalize_display_title(None) == ""
