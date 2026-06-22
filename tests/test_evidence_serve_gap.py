"""Serve gate: only SUBSTANTIATED claims reach agent-facing surfaces.

Covers the shared chokepoint (claim_safety.substantiated_claims) and the two
P0 shapers that emit it — the agent PDP API (_row_as_product) and the public
canonical PDP (_shape_product_for_pdp). Pure functions; no DB.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.claim_safety import substantiated_claims
from routes.pivota_canonical_routes import _shape_product_for_pdp
from routes.agent_pdp_v1 import _row_as_product


def _profile(*statuses):
    return {
        "claims": [
            {
                "claim_text": f"claim-{i}",
                "source_ref": "niacinamide",
                "source_type": "ingredient_mechanism",
                "evidence_grade": "ingredient_inference",
                "substantiation_status": s,
            }
            for i, s in enumerate(statuses)
        ],
        "review_state": "observed",
    }


# --- the serve gate -----------------------------------------------------------

def test_substantiated_only_from_profile() -> None:
    out = substantiated_claims(_profile("substantiated", "unverified"))
    assert [c["claim_text"] for c in out] == ["claim-0"]
    assert out[0]["evidence_grade"] == "ingredient_inference"
    assert out[0]["source_ref"] == "niacinamide"


def test_substantiated_from_raw_list() -> None:
    out = substantiated_claims([{"claim_text": "X", "substantiation_status": "substantiated"}])
    assert len(out) == 1


def test_substantiated_from_json_string() -> None:
    s = json.dumps(_profile("substantiated"))
    assert len(substantiated_claims(s)) == 1


def test_substantiated_none_and_garbage_are_empty() -> None:
    assert substantiated_claims(None) == []
    assert substantiated_claims("not json") == []
    assert substantiated_claims({"claims": []}) == []
    assert substantiated_claims(123) == []


def test_substantiated_drops_unverified_flagged_rejected() -> None:
    # The whole point: a merchant can assert anything; only substantiated serves.
    assert substantiated_claims(_profile("unverified", "flagged", "rejected")) == []


# --- public canonical PDP shaper ---------------------------------------------

def test_canonical_shaper_emits_substantiated_only_plus_disclaimers() -> None:
    row = {
        "title": "Vitamin C serum",
        "evidence_profile": _profile("substantiated", "unverified"),
        "required_disclaimers": [{"code": "fda_dshea_supplement", "text": "…"}],
    }
    shaped = _shape_product_for_pdp(row)
    assert [c["claim_text"] for c in shaped["evidence_claims"]] == ["claim-0"]
    assert shaped["disclaimers"][0]["code"] == "fda_dshea_supplement"


def test_canonical_shaper_no_evidence_is_empty() -> None:
    shaped = _shape_product_for_pdp({"title": "X"})
    assert shaped["evidence_claims"] == []
    assert shaped["disclaimers"] == []


# --- agent PDP API shaper -----------------------------------------------------

def test_agent_pdp_row_emits_claims_and_never_leaks_raw() -> None:
    row = {
        "title": "Serum",
        "pivota_signature_id": "sig_abc",
        "evidence_profile": _profile("substantiated", "unverified"),
        "required_disclaimers": [],
    }
    product = _row_as_product(row)
    # raw profile (which carries the unverified claim) must never reach agents
    assert "evidence_profile" not in product
    assert "required_disclaimers" not in product
    assert [c["claim_text"] for c in product["evidence_claims"]] == ["claim-0"]
    assert product["disclaimers"] == []
