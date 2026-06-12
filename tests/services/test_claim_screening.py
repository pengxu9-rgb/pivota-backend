from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.claim_safety import (
    CATEGORY_SKINCARE,
    CATEGORY_SUPPLEMENT,
    SUBSTANTIATION_FLAGGED,
    SUBSTANTIATION_REJECTED,
)
from services.claim_screening import (
    STATUS_COSMETIC_OK,
    STATUS_DRUG_CLAIM,
    STATUS_OTC_DRUG,
    STATUS_REVIEW,
    screen_claim,
    screen_claims,
    substantiation_status_for_screening,
)


@pytest.mark.parametrize(
    ("claim", "expected_status"),
    [
        # Cosmetic-safe (appearance) claims pass.
        ("Hydrates and brightens the appearance of dull skin", STATUS_COSMETIC_OK),
        ("Reduces the appearance of fine lines and wrinkles", STATUS_COSMETIC_OK),
        ("Smooths and softens skin for a radiant glow", STATUS_COSMETIC_OK),
        # The "appearance of" safe harbor rescues a condition word.
        ("Reduces the appearance of acne scars over time", STATUS_COSMETIC_OK),
        # Hard therapeutic claims -> drug claim.
        ("Treats acne and clears up breakouts fast", STATUS_DRUG_CLAIM),
        ("Cures eczema and prevents flare-ups", STATUS_DRUG_CLAIM),
        # Structure/function claims -> drug claim.
        ("Repairs the skin barrier", STATUS_DRUG_CLAIM),
        ("Boosts collagen production for firmer skin", STATUS_DRUG_CLAIM),
        ("An anti-inflammatory soothing essence", STATUS_DRUG_CLAIM),
        ("Reverses the signs of aging", STATUS_DRUG_CLAIM),
        # SPF / sunscreen -> OTC drug.
        ("SPF 50 broad spectrum sun protection", STATUS_OTC_DRUG),
        ("Daily sunscreen for UVA/UVB defense", STATUS_OTC_DRUG),
        # Condition wording without an appearance safe harbor -> review.
        ("Reduces acne and redness", STATUS_REVIEW),
    ],
)
def test_skincare_screening(claim, expected_status):
    assert screen_claim(CATEGORY_SKINCARE, claim).status == expected_status


def test_empty_claim_is_cosmetic_ok():
    assert screen_claim(CATEGORY_SKINCARE, "").status == STATUS_COSMETIC_OK
    assert screen_claim(CATEGORY_SKINCARE, None).status == STATUS_COSMETIC_OK


def test_non_skincare_categories_are_not_screened_here():
    # Supplement screening lands with the supplement module; until then it's a
    # no-op here (never a false drug_claim against the wrong ruleset).
    result = screen_claim(CATEGORY_SUPPLEMENT, "Supports skin elasticity")
    assert result.status == STATUS_COSMETIC_OK
    assert result.rule_id == "not_screened"


def test_screen_claims_pairs_inputs_with_results():
    pairs = screen_claims(CATEGORY_SKINCARE, ["Treats acne", "Brightens appearance"])
    assert [t for t, _ in pairs] == ["Treats acne", "Brightens appearance"]
    assert pairs[0][1].status == STATUS_DRUG_CLAIM
    assert pairs[1][1].status == STATUS_COSMETIC_OK


def test_substantiation_status_mapping():
    drug = screen_claim(CATEGORY_SKINCARE, "Treats acne")
    otc = screen_claim(CATEGORY_SKINCARE, "SPF 30 sunscreen")
    review = screen_claim(CATEGORY_SKINCARE, "Reduces acne")
    ok = screen_claim(CATEGORY_SKINCARE, "Brightens the appearance")
    assert substantiation_status_for_screening(drug) == SUBSTANTIATION_REJECTED
    assert substantiation_status_for_screening(otc) == SUBSTANTIATION_FLAGGED
    assert substantiation_status_for_screening(review) == SUBSTANTIATION_FLAGGED
    assert substantiation_status_for_screening(ok) is None
