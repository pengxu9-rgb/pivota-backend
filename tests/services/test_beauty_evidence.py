from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.beauty_enrichment import _KEY_ACTIVES
from services.beauty_evidence import (
    EVIDENCE_GRADE_INGREDIENT,
    SOURCE_TYPE_INGREDIENT,
    _ACTIVE_BENEFITS,
    build_evidence_profile,
    derive_substantiated_claims,
)


def _actives(*labels):
    return [{"label": l} for l in labels]


def test_claim_requires_both_assertion_and_active():
    # Niacinamide present AND text claims brightening -> substantiated.
    claims = derive_substantiated_claims(
        "skincare", _actives("Niacinamide"), "A brightening serum for dull skin"
    )
    assert len(claims) == 1
    c = claims[0]
    assert c.substantiation_status == "substantiated"
    assert c.source_type == SOURCE_TYPE_INGREDIENT
    assert c.source_ref == "Niacinamide"
    assert c.evidence_grade == EVIDENCE_GRADE_INGREDIENT
    assert "brighten" in c.claim_text.lower()


def test_claimed_but_no_supporting_active_is_not_surfaced():
    # Claims brightening, but the only active (Hyaluronic Acid) is a hydrator.
    claims = derive_substantiated_claims(
        "skincare", _actives("Hyaluronic Acid"), "A brightening serum"
    )
    assert claims == []


def test_active_present_but_benefit_not_claimed_is_not_surfaced():
    # Niacinamide present but the text claims nothing brightening-related ->
    # Pivota does not invent a benefit the product doesn't assert.
    claims = derive_substantiated_claims(
        "skincare", _actives("Niacinamide"), "A simple daily moisturizer"
    )
    # "moisturizer" triggers hydrating, but Niacinamide doesn't substantiate it.
    assert all("hydrat" not in c.claim_text.lower() for c in claims)
    # and no brightening claim either (not asserted)
    assert all("brighten" not in c.claim_text.lower() for c in claims)


def test_multiple_actives_credit_the_supporting_ones():
    claims = derive_substantiated_claims(
        "skincare",
        _actives("Niacinamide", "Vitamin C", "Hyaluronic Acid"),
        "Brightening, hydrating serum",
    )
    by_text = {c.claim_text: c for c in claims}
    bright = next(c for t, c in by_text.items() if "brighten" in t.lower())
    assert "Niacinamide" in bright.source_ref and "Vitamin C" in bright.source_ref
    hydra = next(c for t, c in by_text.items() if "hydrat" in t.lower())
    assert hydra.source_ref == "Hyaluronic Acid"


def test_no_actives_or_no_text_yields_nothing():
    assert derive_substantiated_claims("skincare", [], "Brightening serum") == []
    assert derive_substantiated_claims("skincare", _actives("Niacinamide"), "") == []


def test_haircare_strengthening_substantiation():
    claims = derive_substantiated_claims(
        "haircare",
        _actives("Hydrolyzed Keratin"),
        "Repairs damaged hair and reduces breakage",
    )
    assert any("strengthen" in c.claim_text.lower() for c in claims)


def test_build_evidence_profile_shape():
    prof = build_evidence_profile(
        "skincare", _actives("Centella Asiatica"), "Soothing cream for redness"
    )
    assert prof is not None
    assert prof.review_state == "observed"
    assert prof.claims and prof.claims[0].substantiation_status == "substantiated"
    # Nothing substantiable -> None (no claim to make).
    assert build_evidence_profile("skincare", _actives("Centella Asiatica"), "x") is None


def test_active_benefits_labels_stay_in_sync_with_enrichment():
    # Guard against drift: every active in the benefit map must be a real curated
    # active label from beauty_enrichment.
    known = {a["label"] for a in _KEY_ACTIVES}
    assert set(_ACTIVE_BENEFITS).issubset(known), set(_ACTIVE_BENEFITS) - known
