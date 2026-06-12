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

from services.claim_safety import (
    CATEGORY_HAIRCARE,
    CATEGORY_SKINCARE,
    required_disclaimers_for_category,
)

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
        # Category-module attributes: skincare requires the structured fit
        # signals an agent matches on (skin concern + key actives). Other
        # categories stay non-blocking until their modules ship.
        attrs_gap = _category_attributes_gap(row.get("category_kind"), row)
        if attrs_gap is not None:
            return (BLOCKER_MISSING_CATEGORY_ATTRS, attrs_gap)

    return None


def _category_attributes_gap(
    category_kind: Optional[str], row: Dict[str, Any]
) -> Optional[str]:
    """Return a reason string when a category's required attributes are absent,
    or None when present (or the category has no attribute gate yet).

    Skincare and haircare are both decision-grade only with the structured fit
    signals an agent matches on -- a category concern AND key ingredients (the
    same two underlying columns; only the wording differs). Other categories
    stay non-blocking until their modules ship."""
    if category_kind == CATEGORY_SKINCARE:
        return _concern_and_actives_gap("skincare", "skin concern", "key actives", row)
    if category_kind == CATEGORY_HAIRCARE:
        return _concern_and_actives_gap("haircare", "hair concern", "key ingredients", row)
    return None


def _concern_and_actives_gap(
    label: str, concern_name: str, actives_name: str, row: Dict[str, Any]
) -> Optional[str]:
    missing = []
    if not row.get("has_category_concern"):
        missing.append(concern_name)
    if not row.get("has_key_actives"):
        missing.append(actives_name)
    if missing:
        return f"{label} record is missing required attributes: {', '.join(missing)}"
    return None
