from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.decision_grade_eval import (
    FAIL,
    PASS,
    score_decision_grade,
    score_records,
)

# A fully decision-grade skincare record.
_FULL_SKINCARE = {
    "category_kind": "skincare",
    "concerns": ["dryness", "dullness"],
    "active_ingredients": [{"label": "Niacinamide", "concentration": "5%"}],
    "skincare_format": "serum",
    "evidence_claims": [
        {"claim_text": "hydrates", "source_ref": "lab_1", "substantiation_status": "substantiated"}
    ],
    "required_disclaimers": [],  # skincare mandates none
    "alternatives": [{"product_key": "other_serum"}],
    "best_us_offer": {"is_first_party": True},
}


def test_full_skincare_record_is_decision_grade():
    score = score_decision_grade(_FULL_SKINCARE)
    assert score.is_decision_grade is True
    assert score.overall == 1.0
    for name in ("find", "justify", "compare", "trust", "buy"):
        assert score.by_name(name).status == PASS


def test_find_fails_without_key_actives():
    rec = {**_FULL_SKINCARE, "active_ingredients": []}
    score = score_decision_grade(rec)
    assert score.by_name("find").status == FAIL
    assert "key actives" in score.by_name("find").gaps
    assert score.is_decision_grade is False


def test_justify_fails_with_only_unsubstantiated_claims():
    rec = {
        **_FULL_SKINCARE,
        "evidence_claims": [
            {"claim_text": "brightens", "source_ref": "blog", "substantiation_status": "unverified"}
        ],
    }
    score = score_decision_grade(rec)
    assert score.by_name("justify").status == FAIL
    assert score.is_decision_grade is False


def test_supplement_justify_requires_disclaimer():
    supplement = {
        "category_kind": "supplement",
        "active_ingredients": [{"label": "Collagen"}],
        "evidence_claims": [
            {"claim_text": "supports elasticity", "source_ref": "study_9", "substantiation_status": "substantiated"}
        ],
        "required_disclaimers": [],  # missing the mandatory FDA disclaimer
        "best_us_offer": {"is_first_party": True},
    }
    score = score_decision_grade(supplement)
    assert score.by_name("justify").status == FAIL
    assert "required disclaimers" in score.by_name("justify").gaps
    # With the disclaimer present, justify passes.
    ok = score_decision_grade(
        {**supplement, "required_disclaimers": [{"code": "fda_dshea_supplement", "text": "..."}]}
    )
    assert ok.by_name("justify").status == PASS


def test_buy_fails_without_us_offer():
    rec = {**_FULL_SKINCARE, "best_us_offer": None}
    score = score_decision_grade(rec)
    assert score.by_name("buy").status == FAIL
    assert score.is_decision_grade is False


def test_compare_fails_but_does_not_block_decision_grade():
    # No alternatives (supply-gated): compare fails, but the record is still
    # decision-grade because the four core dimensions pass.
    rec = {**_FULL_SKINCARE, "alternatives": []}
    score = score_decision_grade(rec)
    assert score.by_name("compare").status == FAIL
    assert score.is_decision_grade is True


def test_score_records_summary():
    incomplete = {**_FULL_SKINCARE, "best_us_offer": None}
    summary = score_records([_FULL_SKINCARE, incomplete])
    assert summary["n"] == 2
    assert summary["decision_grade_count"] == 1
    assert summary["decision_grade_rate"] == 0.5
    assert summary["by_dimension_avg"]["buy"] == 0.5  # one has an offer, one doesn't
    assert 0.0 < summary["overall_avg"] <= 1.0
