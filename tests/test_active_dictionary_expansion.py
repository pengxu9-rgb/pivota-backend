"""Expanded active dictionary: the new INCI actives are recognized and, when the
product text claims the matching benefit, produce substantiated claims. Pure, no DB.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.beauty_enrichment import extract_key_actives  # noqa: E402
from services.beauty_evidence import derive_substantiated_claims  # noqa: E402
from services.claim_safety import SUBSTANTIATION_SUBSTANTIATED  # noqa: E402


def _labels(inci):
    return {a["label"] for a in extract_key_actives(inci)}


def test_new_inci_actives_recognized():
    labels = _labels(
        "Water, Tocopherol, Squalane, Houttuynia Cordata Extract, "
        "Butyrospermum Parkii Butter, Simmondsia Chinensis Seed Oil, Trehalose, "
        "Glycyrrhiza Glabra Root Extract, Galactomyces Ferment Filtrate, Alpha-Arbutin, Bakuchiol"
    )
    assert {
        "Vitamin E", "Squalane", "Heartleaf", "Shea Butter", "Jojoba Oil",
        "Trehalose", "Licorice", "Galactomyces", "Arbutin", "Bakuchiol",
    } <= labels


def test_vitamin_e_earns_antioxidant_claim_when_text_claims_it():
    actives = extract_key_actives("Water, Tocopherol")
    claims = derive_substantiated_claims(
        "skincare", actives, "An antioxidant serum that defends against pollution"
    )
    assert any("environmental stressors" in c.claim_text for c in claims)
    assert all(c.substantiation_status == SUBSTANTIATION_SUBSTANTIATED for c in claims)


def test_heartleaf_earns_soothing_claim():
    actives = extract_key_actives("Water, Houttuynia Cordata Extract")
    claims = derive_substantiated_claims("skincare", actives, "A calming toner that soothes redness")
    assert any(c.substantiation_status == SUBSTANTIATION_SUBSTANTIATED for c in claims)
    assert any("redness" in c.claim_text for c in claims)


def test_active_present_but_benefit_not_claimed_yields_nothing():
    # 3-part gate: active present but text doesn't claim the benefit -> no claim.
    actives = extract_key_actives("Water, Squalane")
    assert derive_substantiated_claims("skincare", actives, "A lightweight everyday formula") == []


def test_arbutin_brightening_claim():
    actives = extract_key_actives("Water, Alpha-Arbutin, Niacinamide")
    claims = derive_substantiated_claims("skincare", actives, "Brightens dark spots for an even glow")
    assert any(c.substantiation_status == SUBSTANTIATION_SUBSTANTIATED for c in claims)
