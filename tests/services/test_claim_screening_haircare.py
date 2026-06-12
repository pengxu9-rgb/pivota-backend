from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.claim_safety import CATEGORY_HAIRCARE, SUBSTANTIATION_FLAGGED, SUBSTANTIATION_REJECTED
from services.claim_screening import (
    STATUS_COSMETIC_OK,
    STATUS_DRUG_CLAIM,
    STATUS_OTC_DRUG,
    STATUS_REVIEW,
    screen_claim,
    substantiation_status_for_screening,
)


@pytest.mark.parametrize(
    ("claim", "expected_status"),
    [
        # Cosmetic-safe haircare claims pass.
        ("Gently cleanses and adds shine", STATUS_COSMETIC_OK),
        ("Strengthens the appearance of damaged hair", STATUS_COSMETIC_OK),
        ("Vegan, sulfate-free volumizing shampoo", STATUS_COSMETIC_OK),
        ("Reduces the appearance of split ends", STATUS_COSMETIC_OK),
        # Hair-growth / anti-hair-loss = drug claims.
        ("Regrows hair in 8 weeks", STATUS_DRUG_CLAIM),
        ("Clinically proven to treat hair loss", STATUS_DRUG_CLAIM),
        ("Stimulates hair growth", STATUS_DRUG_CLAIM),
        ("Prevents balding", STATUS_DRUG_CLAIM),
        ("Formulated with minoxidil", STATUS_DRUG_CLAIM),
        # Anti-dandruff = OTC drug.
        ("Anti-dandruff scalp shampoo", STATUS_OTC_DRUG),
        ("Fights dandruff with every wash", STATUS_OTC_DRUG),
        # Antifungal/antibacterial = drug action.
        ("Antifungal scalp treatment", STATUS_DRUG_CLAIM),
        # Therapeutic scalp condition = drug claim.
        ("Treats scalp psoriasis", STATUS_DRUG_CLAIM),
        # Condition wording without an appearance safe harbor -> review.
        ("Reduces scalp inflammation", STATUS_REVIEW),
    ],
)
def test_haircare_screening(claim, expected_status):
    assert screen_claim(CATEGORY_HAIRCARE, claim).status == expected_status


def test_appearance_safe_harbor_rescues_cosmetic_haircare_phrasing():
    # "reduces the appearance of thinning" is a cosmetic volumizing claim.
    assert (
        screen_claim(CATEGORY_HAIRCARE, "Reduces the appearance of thinning hair").status
        == STATUS_COSMETIC_OK
    )


def test_haircare_substantiation_mapping():
    drug = screen_claim(CATEGORY_HAIRCARE, "Regrows hair")
    otc = screen_claim(CATEGORY_HAIRCARE, "Anti-dandruff shampoo")
    ok = screen_claim(CATEGORY_HAIRCARE, "Adds shine")
    assert substantiation_status_for_screening(drug) == SUBSTANTIATION_REJECTED
    assert substantiation_status_for_screening(otc) == SUBSTANTIATION_FLAGGED
    assert substantiation_status_for_screening(ok) is None
