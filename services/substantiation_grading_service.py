"""P1 — substantiation grading (employee-driven).

The deliberate review step that turns a brand's SUBMITTED lab evidence into a
GRADED outcome, and on 'substantiated' advances the product attested ->
substantiated (the lifecycle apex an agent can cite WITH evidence backing).

Honesty: grading is explicit + employee-gated; submitting evidence never
auto-substantiates (the verify-was-off lesson). Product-level advance for now
(>=1 substantiated evidence row); claim-specific SERVING is the honest surface
(a served badge names what's backed, never a blanket "verified product").
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import db.brand_attestation_evidence as evidence_db
from db.brand_attestation_evidence import (
    GRADING_FLAGGED,
    GRADING_REJECTED,
    GRADING_SUBSTANTIATED,
)

logger = logging.getLogger(__name__)

VALID_GRADES = frozenset({GRADING_SUBSTANTIATED, GRADING_FLAGGED, GRADING_REJECTED})


def grading_enabled() -> bool:
    """Flag: expose the substantiation-grading surface. Default OFF — ships dark,
    flipped per-env when graders are ready."""
    return os.getenv("ENABLE_SUBSTANTIATION_GRADING", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


async def grade_evidence(
    evidence_id: int,
    *,
    graded_by: str,
    grade: str,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """Grade ONE submitted evidence row. On 'substantiated' also advance the
    evidence's product attested -> substantiated and refresh its served PDP so the
    substantiated claim reaches agents. Employee-gated at the route (graded_by is
    recorded). Best-effort writes; never raises."""
    if not graded_by:
        return {"status": "forbidden"}
    if grade not in VALID_GRADES:
        return {"status": "invalid_grade", "valid": sorted(VALID_GRADES)}

    row = await evidence_db.get_evidence(evidence_id)
    if not row:
        return {"status": "not_found"}

    await evidence_db.update_evidence_grade(
        evidence_id, grading_status=grade, graded_by=graded_by, notes=notes
    )

    product_advanced = False
    if grade == GRADING_SUBSTANTIATED:
        merchant_id = row.get("merchant_id")
        product_key = row.get("product_key")
        content_key = row.get("content_key")
        if merchant_id and product_key:
            try:
                from services.claim_state import advance_product_to_substantiated

                product_advanced = await advance_product_to_substantiated(
                    merchant_id=merchant_id, product_key=product_key
                )
            except Exception as exc:  # noqa: BLE001 — best-effort
                logger.warning(
                    "grade_evidence: advance failed for %s/%s: %s",
                    merchant_id, product_key, str(exc)[:200],
                )
        if content_key:
            try:
                from services.agent_pdp_view_assembler import (
                    refresh_agent_pdp_view_for_content_key,
                )

                await refresh_agent_pdp_view_for_content_key(
                    content_key, refresh_source="substantiation_grading"
                )
            except Exception as exc:  # noqa: BLE001 — best-effort
                logger.warning(
                    "grade_evidence: pdp refresh failed for %s: %s",
                    content_key, str(exc)[:200],
                )

    return {
        "status": "graded",
        "evidence_id": evidence_id,
        "grade": grade,
        "graded_by": graded_by,
        "product_advanced": product_advanced,
    }
