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
from typing import Any, Dict, Literal, Optional

import db.brand_attestation_evidence as evidence_db
from db.brand_attestation_evidence import (
    GRADING_FLAGGED,
    GRADING_REJECTED,
    GRADING_SUBSTANTIATED,
)
from services.claim_safety import (
    MERCHANT_EVIDENCE_SOURCE_TYPES,
    REVIEWED_MERCHANT_EVIDENCE_SOURCE_TYPES,
    review_source_url,
)

logger = logging.getLogger(__name__)

VALID_GRADES = frozenset({GRADING_SUBSTANTIATED, GRADING_FLAGGED, GRADING_REJECTED})


def grading_enabled() -> bool:
    """Flag: expose the substantiation-grading surface. Default OFF — ships dark,
    flipped per-env when graders are ready."""
    return os.getenv("ENABLE_SUBSTANTIATION_GRADING", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


async def submitted_product_claims(*, merchant_id: str, product_key: str) -> Optional[list[Dict[str, Any]]]:
    """Exact-product queue for staff. Legacy claims may need a new public source."""
    from db.database import database
    from db.product_evidence import fetch_product_evidence_row, merchant_visible_claim

    product = await database.fetch_one(
        "SELECT 1 FROM catalog_products WHERE product_key = :pk AND merchant_id = :mid",
        {"pk": product_key, "mid": merchant_id},
    )
    if not product:
        return None
    row = await fetch_product_evidence_row(product_key, strict=True)
    if not row or row.get("merchant_id") != merchant_id:
        return []
    submitted = []
    for raw in row.get("claims") or []:
        if not isinstance(raw, dict):
            continue
        claim = merchant_visible_claim(raw)
        if claim.get("review_status") != "submitted":
            continue
        source_url = review_source_url(claim.get("public_source_url")) or review_source_url(claim.get("source_ref"))
        submitted.append({
            "claim_text": claim.get("claim_text"), "source_type": claim.get("source_type"),
            "source_ref": claim.get("source_ref"), "public_source_url": source_url,
        })
    return submitted


async def verify_product_claim(
    *,
    merchant_id: str,
    product_key: str,
    claim_text: str,
    source_type: str,
    source_ref: str,
    citation_url: str,
    citation_type: str,
    evidence_grade: Literal["a", "b", "c"],
    reviewer: str,
) -> Dict[str, Any]:
    """Record staff verification of one exact submitted claim and its public citation."""
    from db.product_evidence import (
        claim_identity,
        fetch_product_evidence_row,
        merchant_visible_claim,
        product_evidence_transaction,
        upsert_product_evidence,
    )

    cite_url = review_source_url(citation_url)
    identity = claim_identity({"claim_text": claim_text, "source_type": source_type, "source_ref": source_ref})
    if (not reviewer or not merchant_id or not product_key or not all(identity) or not cite_url
            or identity[1] not in MERCHANT_EVIDENCE_SOURCE_TYPES - {"merchant_positioning"}
            or citation_type not in REVIEWED_MERCHANT_EVIDENCE_SOURCE_TYPES
            or evidence_grade not in ("a", "b", "c")):
        return {"status": "invalid_claim_review"}

    async with product_evidence_transaction(product_key) as db:
        product = await db.fetch_one(
            """
            SELECT content_key
            FROM catalog_products
            WHERE product_key = :key AND merchant_id = :merchant_id
            """,
            {"key": product_key, "merchant_id": merchant_id},
        )
        row = await fetch_product_evidence_row(product_key, db=db, strict=True)
        if not product or not row or row.get("merchant_id") != merchant_id:
            return {"status": "not_found"}
        claims = row.get("claims") or []
        matches = [
            index for index, claim in enumerate(claims)
            if isinstance(claim, dict) and claim_identity(claim) == identity
        ]
        if len(matches) != 1:
            return {"status": "not_found" if not matches else "ambiguous_claim"}
        index = matches[0]
        claim = claims[index]
        if merchant_visible_claim(claim).get("review_status") not in ("submitted", "verified"):
            return {"status": "claim_not_submitted"}
        claims[index] = {
            **claim,
            "substantiation_status": "substantiated",
            "review_status": "verified",
            "evidence_grade": evidence_grade,
            "review_source_url": cite_url,
            "review_source_type": citation_type,
            "reviewed_by": reviewer,
        }
        if claims[index] != claim:
            await upsert_product_evidence(
                product_key,
                merchant_id=merchant_id,
                claims=claims,
                review_state=row.get("review_state") or "observed",
                required_disclaimers=row.get("required_disclaimers"),
                db=db,
            )

    refreshed = False
    content_key = dict(product).get("content_key")
    if content_key:
        try:
            from services.agent_pdp_view_assembler import refresh_agent_pdp_view_for_content_key

            refreshed = await refresh_agent_pdp_view_for_content_key(
                content_key, refresh_source="product_claim_review"
            )
        except Exception as exc:
            logger.warning("verify_product_claim: pdp refresh failed for %s: %s",
                           content_key, str(exc)[:200])
    return {"status": "verified", "product_key": product_key, "served_refresh": bool(refreshed)}


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
