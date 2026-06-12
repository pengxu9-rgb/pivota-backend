from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.haircare_attributes import (
    CERT_CLAIMED,
    CERT_VERIFIED,
    classify_cruelty_free,
    classify_vegan,
    detect_silicone_free,
    detect_sulfate_free,
    extract_format,
)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Rice Protein Strengthening Shampoo", "shampoo"),
        ("Daily Moisture Conditioner", "conditioner"),
        ("Argan Repair Hair Mask", "hair mask"),
        ("Scalp Soothing Treatment", "scalp treatment"),
        ("Leave-in Detangling Spray", "leave-in"),
        ("Smoothing Hair Serum", "hair serum"),
        ("Nourishing Hair Oil", "hair oil"),
        ("Volumizing Dry Shampoo", "dry shampoo"),
        ("Root Lift Styling Gel", "styling"),
        ("Just a Tote Bag", None),
    ],
)
def test_extract_format(title, expected):
    assert extract_format(title=title) == expected


def test_extract_format_specific_wins_and_uses_other_fields():
    # "dry shampoo" must win over the bare "shampoo" pattern.
    assert extract_format(title="Refreshing Dry Shampoo Powder") == "dry shampoo"
    assert extract_format(category_path="beauty/haircare/conditioner") == "conditioner"
    assert extract_format(product_type="Scalp Tonic") == "scalp treatment"


def test_detect_sulfate_free_only_on_explicit_claim():
    assert detect_sulfate_free("Gentle Shampoo, sulfate-free") is True
    assert detect_sulfate_free("No added sulfates, color-safe") is True
    assert detect_sulfate_free("SLS-free cleansing") is True
    assert detect_sulfate_free("SLS/SLES-free formula") is True
    # Listing a sulfate ingredient is NOT a sulfate-free claim.
    assert detect_sulfate_free("Contains sodium laureth sulfate") is False


def test_detect_silicone_free_only_on_explicit_claim():
    assert detect_silicone_free("Lightweight, silicone-free") is True
    assert detect_silicone_free("No silicones, no build-up") is True
    assert detect_silicone_free("Dimethicone-free conditioner") is True
    assert detect_silicone_free("Smoothing silicone serum") is False


# --- Vegan certification status -------------------------------------------------


def test_vegan_verified_from_authored_authority():
    certs = [{"type": "vegan", "authority": "The Vegan Society", "id": "VS-123"}]
    assert classify_vegan(certs) == CERT_VERIFIED


def test_vegan_verified_from_recognized_text_authority():
    assert classify_vegan(None, "Certified Vegan by vegan.org") == CERT_VERIFIED
    assert classify_vegan(None, "Registered with the Vegan Society") == CERT_VERIFIED
    assert classify_vegan(None, "PETA-approved vegan formula") == CERT_VERIFIED


def test_vegan_claimed_when_only_a_marketing_word():
    # A bare "vegan" tag is the lifestyle_tags case -> claimed, never verified.
    assert classify_vegan(None, "100% vegan haircare") == CERT_CLAIMED
    assert classify_vegan([{"type": "vegan", "authority": ""}]) == CERT_CLAIMED
    assert classify_vegan({"vegan": True}) == CERT_CLAIMED


def test_vegan_none_when_absent():
    assert classify_vegan(None, "Strengthening shampoo for damaged hair") is None
    assert classify_vegan(None) is None


def test_vegan_unrecognized_authority_is_only_claimed():
    certs = [{"type": "vegan", "authority": "Self-declared by brand"}]
    assert classify_vegan(certs) == CERT_CLAIMED


# --- Cruelty-free certification status ------------------------------------------


def test_cruelty_free_verified_from_authority():
    assert classify_cruelty_free([{"type": "cruelty_free", "authority": "Leaping Bunny"}]) == CERT_VERIFIED
    assert classify_cruelty_free(None, "Leaping Bunny certified") == CERT_VERIFIED
    assert classify_cruelty_free(None, "PETA Beauty Without Bunnies") == CERT_VERIFIED


def test_cruelty_free_claimed_vs_none():
    assert classify_cruelty_free(None, "cruelty-free and kind") == CERT_CLAIMED
    assert classify_cruelty_free(None, "not tested on animals") == CERT_CLAIMED
    assert classify_cruelty_free(None, "Smoothing hair oil") is None


def test_bare_string_cert_entries_resolve_by_kind():
    # A flat list of label strings (e.g. from a tags column).
    certs = ["Leaping Bunny", "vegan"]
    assert classify_cruelty_free(certs) == CERT_VERIFIED  # recognized authority
    assert classify_vegan(certs) == CERT_CLAIMED          # bare word only
