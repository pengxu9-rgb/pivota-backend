from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.category_kind import (
    HAIRCARE,
    SKINCARE,
    SUPPLEMENT,
    is_makeup,
    resolve_category_kind,
)


@pytest.mark.parametrize(
    ("category_path", "expected"),
    [
        ("beauty/skincare/treat/serum", SKINCARE),
        ("beauty/skincare/sun/sunscreen", SKINCARE),  # sunscreen nested under skincare
        ("BEAUTY/SKINCARE/CLEANSE/CLEANSER", SKINCARE),  # case-insensitive
        ("beauty/haircare/shampoo", HAIRCARE),
        # Other topical beauty paths are not one of the three contract kinds.
        ("beauty/makeup/lips/lipstick", None),
        ("beauty/body/lotion", None),
        ("beauty/tools/brush", None),
    ],
)
def test_resolves_from_beauty_category_path(category_path, expected):
    assert resolve_category_kind(category_path=category_path) == expected


@pytest.mark.parametrize(
    ("category_path", "expected"),
    [
        # Exact subcategory-less paths must classify (trailing-slash bug fix).
        ("beauty/skincare", SKINCARE),
        ("beauty/skincare/", SKINCARE),
        ("beauty/haircare", HAIRCARE),
        ("beauty/haircare/", HAIRCARE),
        ("BEAUTY/HAIRCARE", HAIRCARE),
    ],
)
def test_resolves_exact_subcategoryless_path(category_path, expected):
    assert resolve_category_kind(category_path=category_path) == expected


def test_explicit_supplement_path_is_supplement():
    # Ownist "beauty/wellness/beauty-supplements/sets" must not drop to None.
    assert (
        resolve_category_kind(category_path="beauty/wellness/beauty-supplements/sets")
        == SUPPLEMENT
    )
    assert resolve_category_kind(category_path="beauty/supplements") == SUPPLEMENT


def test_bare_beauty_path_reaches_supplement_text_detection():
    # A bare "beauty" path no longer blocks supplement text detection (Ownist
    # "Triple Collagen" jelly sticks: ingestible active + dosage form).
    assert (
        resolve_category_kind(category_path="beauty", title="Triple Collagen jelly stick")
        == SUPPLEMENT
    )
    # But a bare path with no ingestible signal is still unknown, not guessed.
    assert resolve_category_kind(category_path="beauty", title="Velvet Lip Tint") is None


def test_supplement_detected_from_form_plus_active():
    # BB Lab "Good Night Collagen 30 sticks": collagen + ingestible form.
    assert (
        resolve_category_kind(title="Good Night Collagen 30 sticks") == SUPPLEMENT
    )


def test_supplement_detected_from_explicit_noun():
    assert resolve_category_kind(title="Skin Probiotics") == SUPPLEMENT
    assert (
        resolve_category_kind(product_type="Dietary Supplement", title="Vitamin C tablets")
        == SUPPLEMENT
    )


def test_supplement_detected_from_tags():
    assert (
        resolve_category_kind(title="Inner Glow Drink Mix", tags=["supplement", "kbeauty"])
        == SUPPLEMENT
    )


def test_topical_collagen_is_not_a_supplement():
    # Collagen in a topical cream (beauty path) must stay skincare, not supplement.
    assert (
        resolve_category_kind(
            category_path="beauty/skincare/moisturize/cream", title="Collagen Cream"
        )
        == SKINCARE
    )
    # A makeup stick must not be read as a supplement "stick".
    assert (
        resolve_category_kind(category_path="beauty/makeup/face/stick", title="Glow Stick Foundation")
        is None
    )


def test_collagen_without_form_is_not_guessed():
    # Collagen alone (no dosage form, no path) is ambiguous -> unknown, not guessed.
    assert resolve_category_kind(title="Collagen Essence") is None


def test_unknown_returns_none_never_defaulted():
    assert resolve_category_kind() is None
    assert resolve_category_kind(title="Hydrating Serum") is None  # no path, no supplement signal
    assert resolve_category_kind(category_path="fashion/tops/tee") is None


@pytest.mark.parametrize("title", [
    "Pro Filt'r Soft Matte Powder Foundation",
    "Killawatt Freestyle Highlighter",
    "Shadowstix Longwear Eyeshadow Stick",
    "Soft Radiance Setting Powder",
    "Instant Retouch Concealer",
    "Suede Powder Blush",
    "Lengthening Mascara",
])
def test_is_makeup_positive(title):
    assert is_makeup(title=title) is True


@pytest.mark.parametrize("title", [
    "Heartleaf 77% Soothing Toner",   # skincare
    "Snail Mucin Essence",            # skincare
    "BB Cream Natural Glow",          # hybrid — must NOT be flagged
    "Tinted Moisturizer SPF 30",      # hybrid
    "Hydrating Face Primer",          # primer — ambiguous, excluded
    "Gentle Makeup Remover",          # cleansing, not color cosmetic
    "Bond Repair Hair Oil",           # haircare
])
def test_is_makeup_negative(title):
    assert is_makeup(title=title) is False


def test_makeup_guard_demotes_mispathed_skincare():
    # a foundation mis-pathed under skincare must NOT resolve to skincare...
    assert resolve_category_kind(category_path="beauty/skincare/face", title="Matte Powder Foundation") is None
    # ...but a real serum under the same path still resolves skincare
    assert resolve_category_kind(category_path="beauty/skincare/face", title="Vitamin C Serum") == SKINCARE
