from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.decision_grade_eval import (
    compare_decision_grade,
    compare_records_by_category,
    eval_record_from_payload,
    native_baseline_record,
    score_decision_grade,
)

# A haircare record (Anuko-style: vegan-certified, structured fit) that should
# score decision-grade as Pivota serves it.
_FULL_HAIRCARE = {
    "category_kind": "haircare",
    "concerns": ["color-treated"],
    "active_ingredients": [{"label": "Hydrolyzed Rice Protein"}],
    "haircare_format": "shampoo",
    "sulfate_free": True,
    "vegan_status": "verified",
    "cruelty_free_status": "verified",
    "evidence_claims": [
        {"claim_text": "strengthens", "source_ref": "lab_1", "substantiation_status": "substantiated"}
    ],
    "required_disclaimers": [],
    "alternatives": [],
    "best_us_offer": {"is_first_party": True, "price": "24.00"},
}


def test_haircare_find_passes_with_concern_and_ingredients():
    find = score_decision_grade(_FULL_HAIRCARE).by_name("find")
    assert find.status == "pass"
    assert find.gaps == []


def test_haircare_find_blocks_without_hair_concern():
    rec = {**_FULL_HAIRCARE, "concerns": []}
    find = score_decision_grade(rec).by_name("find")
    assert find.status == "fail"
    assert "hair concern" in find.gaps


def test_haircare_verified_cert_is_a_pivota_only_find_signal():
    # A bare "claimed" cert is NOT credited; only "verified" raises the find score.
    verified = score_decision_grade(_FULL_HAIRCARE).by_name("find").score
    claimed = score_decision_grade(
        {**_FULL_HAIRCARE, "vegan_status": "claimed", "cruelty_free_status": "claimed"}
    ).by_name("find").score
    assert verified > claimed


def test_haircare_native_baseline_keeps_format_drops_verified_cert():
    native = native_baseline_record(_FULL_HAIRCARE)
    assert native["haircare_format"] == "shampoo"  # title-derivable
    assert native["vegan_status"] is None          # native can't verify a cert
    assert native["cruelty_free_status"] is None
    assert native["concerns"] == []


def test_haircare_pivota_beats_native_and_is_decision_grade():
    cmp = compare_decision_grade(_FULL_HAIRCARE)
    assert cmp["pivota"].is_decision_grade is True
    assert cmp["native"].is_decision_grade is False
    assert cmp["deltas"]["find"] > 0
    assert cmp["overall_advantage"] > 0


def test_eval_record_from_payload_maps_haircare_attributes():
    payload = {
        "category_kind": "haircare",
        "concerns": ["dry scalp"],
        "active_ingredients": [{"label": "Argan Oil"}],
        "haircare_format": "conditioner",
        "sulfate_free": True,
        "silicone_free": True,
        "vegan_status": "verified",
        "cruelty_free_status": "claimed",
        "evidence_profile": {
            "claims": [{"claim_text": "softens", "source_ref": "s1", "substantiation_status": "substantiated"}]
        },
        "required_disclaimers": [],
    }
    rec = eval_record_from_payload(payload, best_us_offer={"is_first_party": True})
    assert rec["haircare_format"] == "conditioner"
    assert rec["sulfate_free"] is True
    assert rec["vegan_status"] == "verified"
    assert score_decision_grade(rec).is_decision_grade is True


def test_compare_records_by_category_buckets_and_rolls_up():
    skincare = {
        "category_kind": "skincare",
        "concerns": ["dryness"],
        "active_ingredients": [{"label": "Niacinamide"}],
        "skincare_format": "serum",
        "evidence_claims": [
            {"claim_text": "hydrates", "source_ref": "s1", "substantiation_status": "substantiated"}
        ],
        "required_disclaimers": [],
        "best_us_offer": {"is_first_party": True},
    }
    report = compare_records_by_category([skincare, _FULL_HAIRCARE])
    assert set(report["by_category"]) == {"skincare", "haircare"}
    assert report["by_category"]["haircare"]["n"] == 1
    assert report["by_category"]["skincare"]["n"] == 1
    # Each category beats native; the overall roll-up covers both.
    assert report["by_category"]["haircare"]["pivota_decision_grade_rate"] == 1.0
    assert report["overall"]["n"] == 2
    assert report["overall"]["pivota_overall_avg"] > report["overall"]["native_overall_avg"]
