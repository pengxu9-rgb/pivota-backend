from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.agent_decision_gates import (
    BLOCKER_MISSING_DISCLAIMERS,
    BLOCKER_NO_PROVENANCE_CLAIM,
    BLOCKER_NO_US_OFFER,
    BLOCKER_UNREVIEWED_EVIDENCE,
    evaluate_agent_decision_gates,
)

# A row that passes every active gate (US offer + reviewed, substantiated evidence).
_FULL_ROW = {
    "has_us_offer": True,
    "provenance_claim_count": 2,
    "required_disclaimers_present": True,
    "evidence_review_state": "reviewed",
    "category_attributes_present": True,
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
    row = {**_FULL_ROW, "required_disclaimers_present": False}
    result = evaluate_agent_decision_gates(row, gates_enabled=True, evidence_gates=True)
    assert result is not None and result[0] == BLOCKER_MISSING_DISCLAIMERS


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
