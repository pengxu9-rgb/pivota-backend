"""Agent-decision-grade serving gates (K-beauty common-core hardening, PR4).

The existing index_pipeline_state gate admits a record once it "renders a PDP"
(live, titled, imaged, priced, identity-resolved, ...). The data contract wants
the agent surface to additionally require that a record is *decision-grade* for
the US K-beauty wedge: a US-buyable offer, a provenance-backed claim, any
required disclaimers, reviewed evidence, and category-module attributes.

These gates are ADDITIVE and FLAG-GATED so existing PDP serving never regresses:

  * ENABLE_KBEAUTY_AGENT_DECISION_GATES -- activates the US-buyable-offer gate,
    which is powered by data shipped today (catalog_offers.market, mig 149).
  * ENABLE_KBEAUTY_EVIDENCE_GATES -- activates the claim / disclaimer / reviewed
    -evidence gates. Left OFF until the product_intel.v1 authoring writer (in
    PIVOTA-Agent) populates evidence_profile / required_disclaimers; turning it
    on before then would (correctly) block records that have no authored
    evidence yet. The category-attributes gate is a stub here -- it stays
    non-blocking until the per-category attribute modules land.

Pure function (no DB / no I/O): it reads already-fetched eligibility-row keys,
so it runs inside _classify_product and unit-tests with synthetic dicts.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

from services.claim_safety import required_disclaimers_for_category

BLOCKER_NO_US_OFFER = "no_us_offer"
BLOCKER_NO_PROVENANCE_CLAIM = "no_provenance_claim"
BLOCKER_MISSING_DISCLAIMERS = "missing_disclaimers"
BLOCKER_UNREVIEWED_EVIDENCE = "unreviewed_evidence"
BLOCKER_MISSING_CATEGORY_ATTRS = "missing_category_attributes"


def _env_flag(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() == "true"


def agent_decision_gates_enabled() -> bool:
    return _env_flag("ENABLE_KBEAUTY_AGENT_DECISION_GATES")


def evidence_gates_enabled() -> bool:
    return _env_flag("ENABLE_KBEAUTY_EVIDENCE_GATES")


def evaluate_agent_decision_gates(
    row: Dict[str, Any],
    *,
    gates_enabled: Optional[bool] = None,
    evidence_gates: Optional[bool] = None,
) -> Optional[Tuple[str, str]]:
    """Return (blocker_code, blocker_detail) for the first failing
    agent-decision-grade gate, or None if all active gates pass.

    Only evaluated when the main flag is on; the evidence gates are further
    gated by the evidence sub-flag. The explicit kwargs make the gating testable
    without env mutation.
    """
    enabled = agent_decision_gates_enabled() if gates_enabled is None else gates_enabled
    if not enabled:
        return None

    # US-buyable offer -- powered by catalog_offers.market (mig 149) today.
    if not row.get("has_us_offer"):
        return (
            BLOCKER_NO_US_OFFER,
            "no catalog_offers row with market='US' and list_price > 0",
        )

    use_evidence = evidence_gates_enabled() if evidence_gates is None else evidence_gates
    if use_evidence:
        if int(row.get("provenance_claim_count") or 0) < 1:
            return (
                BLOCKER_NO_PROVENANCE_CLAIM,
                "evidence_profile has no provenance-backed claim",
            )
        # Only categories that mandate a disclaimer (e.g. supplements -> the
        # FDA/DSHEA disclaimer) gate on it. Absence of the signal defaults to
        # "present" so it never blocks until the authoring writer sets it False.
        if required_disclaimers_for_category(row.get("category_kind")):
            if row.get("required_disclaimers_present", True) is False:
                return (
                    BLOCKER_MISSING_DISCLAIMERS,
                    "a required disclaimer for this category is absent",
                )
        if str(row.get("evidence_review_state") or "") != "reviewed":
            return (
                BLOCKER_UNREVIEWED_EVIDENCE,
                "evidence_profile.review_state is not 'reviewed'",
            )
        # Category-module attributes: stub -- non-blocking until the per-category
        # modules ship and populate this signal.
        if row.get("category_attributes_present", True) is False:
            return (
                BLOCKER_MISSING_CATEGORY_ATTRS,
                "category-module attributes are not present",
            )

    return None
