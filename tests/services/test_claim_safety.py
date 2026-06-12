from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.claim_safety import (
    CATEGORY_HAIRCARE,
    CATEGORY_SKINCARE,
    CATEGORY_SUPPLEMENT,
    FDA_DSHEA_DISCLAIMER_CODE,
    FDA_DSHEA_DISCLAIMER_TEXT,
    SUBSTANTIATION_UNVERIFIED,
    normalize_claims,
    required_disclaimers_for_category,
)


def test_supplement_requires_fda_dshea_disclaimer():
    disclaimers = required_disclaimers_for_category(CATEGORY_SUPPLEMENT)
    assert len(disclaimers) == 1
    d = disclaimers[0]
    assert d.code == FDA_DSHEA_DISCLAIMER_CODE
    assert d.applies_to == CATEGORY_SUPPLEMENT
    assert d.text == FDA_DSHEA_DISCLAIMER_TEXT
    # Verbatim regulatory phrasing must be preserved.
    assert "has not been evaluated by the Food and Drug Administration" in d.text
    assert "diagnose, treat, cure, or prevent any disease" in d.text


def test_skincare_and_haircare_have_no_mandatory_disclaimer_by_default():
    assert required_disclaimers_for_category(CATEGORY_SKINCARE) == []
    assert required_disclaimers_for_category(CATEGORY_HAIRCARE) == []
    assert required_disclaimers_for_category(None) == []
    assert required_disclaimers_for_category("unknown") == []


def test_required_disclaimers_returns_fresh_mutable_list():
    a = required_disclaimers_for_category(CATEGORY_SUPPLEMENT)
    a.clear()
    b = required_disclaimers_for_category(CATEGORY_SUPPLEMENT)
    assert len(b) == 1  # not aliased to a shared instance


def test_normalize_claims_coerces_legacy_strings():
    claims = normalize_claims(["Hydrates skin", "  Brightens appearance  ", "", "   "])
    assert [c.claim_text for c in claims] == ["Hydrates skin", "Brightens appearance"]
    assert all(c.substantiation_status == SUBSTANTIATION_UNVERIFIED for c in claims)
    assert all(c.source_ref is None for c in claims)


def test_normalize_claims_coerces_structured_dicts():
    claims = normalize_claims(
        [
            {
                "claim_text": "Supports skin elasticity",
                "source_ref": "study_123",
                "source_type": "clinical_study",
                "evidence_grade": "clinical",
                "substantiation_status": "substantiated",
            },
            {"claim": "Vegan", "substantiation_status": "BOGUS"},  # bad status -> default
            {"claim_text": ""},  # dropped
        ]
    )
    assert len(claims) == 2
    first = claims[0]
    assert first.claim_text == "Supports skin elasticity"
    assert first.source_ref == "study_123"
    assert first.substantiation_status == "substantiated"
    # Unknown status falls back to the honest default.
    assert claims[1].claim_text == "Vegan"
    assert claims[1].substantiation_status == SUBSTANTIATION_UNVERIFIED


def test_normalize_claims_handles_non_list():
    assert normalize_claims(None) == []
    assert normalize_claims("not a list") == []
    assert normalize_claims({}) == []
