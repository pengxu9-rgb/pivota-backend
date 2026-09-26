from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.agent_decision_gates import (
    BLOCKER_MISSING_CATEGORY_ATTRS,
    BLOCKER_MISSING_DISCLAIMERS,
    BLOCKER_NO_PROVENANCE_CLAIM,
    BLOCKER_NO_US_OFFER,
    BLOCKER_UNREVIEWED_EVIDENCE,
    evaluate_agent_decision_gates,
)

# A row that passes every active gate (US offer + reviewed, substantiated evidence).
# Supplement -> mandates a disclaimer, has no skincare attribute gate.
_FULL_ROW = {
    "has_us_offer": True,
    "category_kind": "supplement",
    "provenance_claim_count": 2,
    "required_disclaimers_present": True,
    "evidence_review_state": "reviewed",
}

# A skincare row that passes the category-attributes gate.
_FULL_SKINCARE_ROW = {
    "has_us_offer": True,
    "category_kind": "skincare",
    "provenance_claim_count": 1,
    "evidence_review_state": "reviewed",
    "has_category_concern": True,
    "has_key_actives": True,
}

# A haircare row that passes the category-attributes gate.
_FULL_HAIRCARE_ROW = {
    "has_us_offer": True,
    "category_kind": "haircare",
    "provenance_claim_count": 1,
    "evidence_review_state": "reviewed",
    "has_category_concern": True,
    "has_key_actives": True,
}


def test_disabled_flag_is_a_noop():
    # Even a totally empty row passes when the gates are off.
    assert evaluate_agent_decision_gates({}, gates_enabled=False) is None


def test_us_offer_gate_blocks_when_no_us_offer():
    result = evaluate_agent_decision_gates({"has_us_offer": False}, gates_enabled=True)
    assert result is not None
    assert result[0] == BLOCKER_NO_US_OFFER


def test_us_offer_gate_passes_with_us_offer_and_evidence_gates_off():
    # With evidence sub-flag off, a US offer alone passes -- the unauthored
    # evidence signals must NOT block.
    assert (
        evaluate_agent_decision_gates(
            {"has_us_offer": True}, gates_enabled=True, evidence_gates=False
        )
        is None
    )


def test_evidence_gates_block_without_provenance_claim():
    row = {**_FULL_ROW, "provenance_claim_count": 0}
    result = evaluate_agent_decision_gates(row, gates_enabled=True, evidence_gates=True)
    assert result is not None and result[0] == BLOCKER_NO_PROVENANCE_CLAIM


def test_evidence_gates_block_when_disclaimer_explicitly_absent():
    # Supplement mandates the FDA disclaimer; explicitly-absent -> block.
    row = {**_FULL_ROW, "required_disclaimers_present": False}
    result = evaluate_agent_decision_gates(row, gates_enabled=True, evidence_gates=True)
    assert result is not None and result[0] == BLOCKER_MISSING_DISCLAIMERS


def test_disclaimer_gate_skipped_for_category_without_a_mandate():
    # Skincare mandates no disclaimer, so an absent disclaimer must NOT block
    # (the skincare attribute signals are present, isolating disclaimer behavior).
    row = {**_FULL_SKINCARE_ROW, "required_disclaimers_present": False}
    assert (
        evaluate_agent_decision_gates(row, gates_enabled=True, evidence_gates=True) is None
    )


def test_evidence_gates_block_when_evidence_unreviewed():
    row = {**_FULL_ROW, "evidence_review_state": "observed"}
    result = evaluate_agent_decision_gates(row, gates_enabled=True, evidence_gates=True)
    assert result is not None and result[0] == BLOCKER_UNREVIEWED_EVIDENCE


def test_evidence_gates_pass_for_full_row():
    assert (
        evaluate_agent_decision_gates(_FULL_ROW, gates_enabled=True, evidence_gates=True)
        is None
    )


def test_missing_signals_default_present_so_only_real_gaps_block():
    # provenance_claim_count missing -> 0 -> blocks (a genuine gap), but missing
    # disclaimer/category signals default to "present" and must not block.
    row = {"has_us_offer": True, "evidence_review_state": "reviewed", "provenance_claim_count": 1}
    assert (
        evaluate_agent_decision_gates(row, gates_enabled=True, evidence_gates=True)
        is None
    )


def test_skincare_passes_with_concern_and_actives():
    assert (
        evaluate_agent_decision_gates(
            _FULL_SKINCARE_ROW, gates_enabled=True, evidence_gates=True
        )
        is None
    )


def test_skincare_blocks_without_skin_concern():
    row = {**_FULL_SKINCARE_ROW, "has_category_concern": False}
    result = evaluate_agent_decision_gates(row, gates_enabled=True, evidence_gates=True)
    assert result is not None and result[0] == BLOCKER_MISSING_CATEGORY_ATTRS
    assert "skin concern" in result[1]


def test_skincare_blocks_without_key_actives():
    row = {**_FULL_SKINCARE_ROW, "has_key_actives": False}
    result = evaluate_agent_decision_gates(row, gates_enabled=True, evidence_gates=True)
    assert result is not None and result[0] == BLOCKER_MISSING_CATEGORY_ATTRS
    assert "key actives" in result[1]


def test_haircare_passes_with_concern_and_actives():
    assert (
        evaluate_agent_decision_gates(
            _FULL_HAIRCARE_ROW, gates_enabled=True, evidence_gates=True
        )
        is None
    )


def test_haircare_blocks_without_hair_concern():
    row = {**_FULL_HAIRCARE_ROW, "has_category_concern": False}
    result = evaluate_agent_decision_gates(row, gates_enabled=True, evidence_gates=True)
    assert result is not None and result[0] == BLOCKER_MISSING_CATEGORY_ATTRS
    assert "hair concern" in result[1]


def test_haircare_blocks_without_key_ingredients():
    row = {**_FULL_HAIRCARE_ROW, "has_key_actives": False}
    result = evaluate_agent_decision_gates(row, gates_enabled=True, evidence_gates=True)
    assert result is not None and result[0] == BLOCKER_MISSING_CATEGORY_ATTRS
    assert "key ingredients" in result[1]


def test_non_gated_category_has_no_attribute_gate():
    # A supplement with no skincare/haircare signals must NOT block on category
    # attributes (its module hasn't shipped an attribute gate yet).
    assert (
        evaluate_agent_decision_gates(_FULL_ROW, gates_enabled=True, evidence_gates=True)
        is None
    )


# --- the priced-offer gate reads the SERVED regions, not the US alone --------
#
# Measured on prod 2026-09-07: this gate is ENABLED and `no_us_offer` blocks 398 of
# cocomo.sg's 399 rows and the residual 12 of jsmbeauty.sg's 170 -- correctly-ingested
# SGD rows from Singapore storefronts whose UCP checkout reaches ready_for_complete.


def test_the_gate_reads_the_served_region_column_when_it_is_present():
    """An SGD row: no USD offer, but a real offer in a currency a served region
    expects. It must stop being blocked -- WITHOUT anything converting the amount."""
    row = {"has_us_offer": False, "has_serving_region_offer": True}
    assert evaluate_agent_decision_gates(row, gates_enabled=True, evidence_gates=False) is None


def test_the_served_region_column_still_blocks_a_row_priced_in_nothing_we_serve():
    row = {"has_us_offer": False, "has_serving_region_offer": False}
    result = evaluate_agent_decision_gates(row, gates_enabled=True, evidence_gates=False)
    assert result is not None and result[0] == BLOCKER_NO_US_OFFER


def test_the_served_region_column_wins_over_a_stale_us_answer():
    """Both keys present and disagreeing: the configured one decides. Otherwise
    widening the region list would be silently undone by the older column."""
    row = {"has_us_offer": True, "has_serving_region_offer": False}
    result = evaluate_agent_decision_gates(row, gates_enabled=True, evidence_gates=False)
    assert result is not None and result[0] == BLOCKER_NO_US_OFFER


def test_a_row_without_the_new_column_falls_back_to_has_us_offer():
    """A caller that computed only the older column keeps its old verdict. Reading an
    ABSENT key as False would block every row such a caller ever fetched -- the failure
    mode `priced_offer_sql` uses EXISTS (never NULL) specifically to keep distinguishable."""
    assert (
        evaluate_agent_decision_gates(
            {"has_us_offer": True}, gates_enabled=True, evidence_gates=False
        )
        is None
    )
    blocked = evaluate_agent_decision_gates(
        {"has_us_offer": False}, gates_enabled=True, evidence_gates=False
    )
    assert blocked is not None and blocked[0] == BLOCKER_NO_US_OFFER


def test_the_blocker_detail_names_currency_and_not_the_market_column():
    """The old text said "market='US'". The SQL has never asked that -- it asks
    `currency = 'USD'`, and was moved off `market` because `market` is a NOT NULL
    DEFAULT 'US' no writer sets. On this cohort every blocked row IS market='US',
    so the old wording sent a reader to the one column that carries no signal."""
    _, detail = evaluate_agent_decision_gates(
        {"has_serving_region_offer": False}, gates_enabled=True, evidence_gates=False
    )
    assert "currency" in detail
    assert "market='US'" not in detail
