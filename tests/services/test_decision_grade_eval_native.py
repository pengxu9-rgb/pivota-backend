from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.decision_grade_eval import (
    compare_decision_grade,
    compare_records,
    eval_record_from_payload,
    native_baseline_record,
    score_decision_grade,
)

_FULL_SKINCARE = {
    "category_kind": "skincare",
    "concerns": ["dryness"],
    "active_ingredients": [{"label": "Niacinamide", "concentration": "5%"}],
    "skincare_format": "serum",
    "evidence_claims": [
        {"claim_text": "hydrates", "source_ref": "lab_1", "substantiation_status": "substantiated"}
    ],
    "required_disclaimers": [],
    "alternatives": [{"product_key": "other"}],
    "best_us_offer": {"is_first_party": True, "price": "29.00"},
}


def test_native_baseline_strips_structured_signals_but_keeps_catalog_card():
    native = native_baseline_record(_FULL_SKINCARE)
    assert native["concerns"] == []
    assert native["active_ingredients"] == []
    assert native["evidence_claims"] == []
    assert native["alternatives"] == []
    # Format is title-derivable -> native keeps it.
    assert native["skincare_format"] == "serum"
    # The offer survives but loses verified first-party trust.
    assert native["best_us_offer"]["price"] == "29.00"
    assert "is_first_party" not in native["best_us_offer"]


def test_native_baseline_is_not_decision_grade_while_pivota_is():
    assert score_decision_grade(_FULL_SKINCARE).is_decision_grade is True
    assert score_decision_grade(native_baseline_record(_FULL_SKINCARE)).is_decision_grade is False


def test_compare_reports_per_dimension_advantage():
    cmp = compare_decision_grade(_FULL_SKINCARE)
    assert cmp["pivota"].is_decision_grade is True
    assert cmp["native"].is_decision_grade is False
    # Pivota leads on the proprietary dimensions, ties on buy (both have an offer).
    assert cmp["deltas"]["find"] > 0
    assert cmp["deltas"]["justify"] > 0
    assert cmp["deltas"]["compare"] > 0
    assert cmp["deltas"]["trust"] > 0
    assert cmp["deltas"]["buy"] == 0
    assert cmp["overall_advantage"] > 0


def test_compare_records_summary():
    summary = compare_records([_FULL_SKINCARE, _FULL_SKINCARE])
    assert summary["n"] == 2
    assert summary["pivota_decision_grade_rate"] == 1.0
    assert summary["native_decision_grade_rate"] == 0.0
    assert summary["pivota_overall_avg"] > summary["native_overall_avg"]
    assert summary["advantage_by_dimension"]["trust"] > 0


def test_eval_record_from_payload_maps_assembled_signals():
    payload = {
        "category_kind": "skincare",
        "concerns": ["acne-prone"],
        "active_ingredients": [{"label": "Salicylic Acid"}],
        "skincare_format": "toner",
        "evidence_profile": {
            "claims": [{"claim_text": "soothes", "source_ref": "s1", "substantiation_status": "substantiated"}]
        },
        "required_disclaimers": [],
    }
    rec = eval_record_from_payload(
        payload,
        best_us_offer={"is_first_party": True},
        alternatives=[{"product_key": "alt"}],
    )
    assert rec["category_kind"] == "skincare"
    assert rec["concerns"] == ["acne-prone"]
    assert rec["evidence_claims"][0]["source_ref"] == "s1"
    assert rec["alternatives"] == [{"product_key": "alt"}]
    # And it scores decision-grade end-to-end.
    assert score_decision_grade(rec).is_decision_grade is True


def test_eval_record_from_payload_handles_missing_payload():
    rec = eval_record_from_payload(None)
    assert rec["concerns"] == []
    assert rec["best_us_offer"] is None
    assert score_decision_grade(rec).is_decision_grade is False
