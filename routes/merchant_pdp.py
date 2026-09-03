from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field


SKU_OPT_OVERLAY_V1_ENABLED = os.getenv("SKU_OPT_OVERLAY_V1", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# Modules a merchant may self-approve via the LLM-reviewed auto-publish path.
# v1: 'copy' only (low-risk, machine-publishable). Widen deliberately.
MERCHANT_SELF_APPROVE_MODULES = {"copy"}

from db.database import database
from services.pdp_governance_service import (
    DEFAULT_MARKET,
    REVIEW_ACTOR_GPT55,
    create_merchant_contribution,
    ensure_pdp_governance_tables,
    get_pdp_projection,
    parse_product_key,
    review_module_version,
)
from services.pdp_copy_review import generate_copy_review_rubric
from utils.auth import get_current_user
from services.merchant_write_guardrails import (
    GuardrailViolation,
    guardrail_block_message,
)


router = APIRouter(prefix="/merchant/pdps", tags=["merchant-pdp-governance"])

logger = logging.getLogger(__name__)


class MerchantContributionRequest(BaseModel):
    module_key: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    notes: Optional[str] = None
    market: str = DEFAULT_MARKET


def _merchant_id(current_user: Dict[str, Any]) -> str:
    merchant_id = current_user.get("merchant_id")
    if not merchant_id:
        raise HTTPException(status_code=403, detail="MERCHANT_REQUIRED")
    return str(merchant_id)


def _map_error(exc: Exception) -> HTTPException:
    # A guardrail refusal is an operator-readable 422, never a 500 traceback:
    # the write was refused on purpose and the caller needs the reasons.
    if isinstance(exc, GuardrailViolation):
        return HTTPException(
            status_code=422,
            detail={
                "code": "MERCHANT_WRITE_GUARDRAIL",
                "message": guardrail_block_message(exc.violations),
                "violations": exc.violations,
            },
        )
    message = str(exc)
    if message in {"PDP_NOT_FOUND", "PDP_MODULE_VERSION_NOT_FOUND"}:
        return HTTPException(status_code=404, detail=message)
    if message in {"INVALID_PRODUCT_KEY", "INVALID_PDP_MODULE", "PDP_RESOLUTION_REQUIRES_PRODUCT_KEY_OR_SEED"}:
        return HTTPException(status_code=400, detail=message)
    if message == "MERCHANT_PRODUCT_FORBIDDEN":
        return HTTPException(status_code=403, detail=message)
    return HTTPException(status_code=500, detail=message[:300])


@router.get("/product/{platform}/{platform_product_id}")
async def get_product_pdp_status(
    platform: str,
    platform_product_id: str,
    market: str = Query(default=DEFAULT_MARKET),
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    merchant_id = _merchant_id(current_user)
    product_key = f"{merchant_id}|{platform}|{platform_product_id}"
    try:
        projection = await get_pdp_projection(product_key=product_key, market=market)
        await ensure_pdp_governance_tables()
        rows = await database.fetch_all(
            """
            SELECT id, pdp_id, product_key, merchant_id, module_key, status,
                   reviewed_by_actor_type, reviewed_by_actor_id, review_decision,
                   review_notes, notes, created_at, updated_at
            FROM merchant_pdp_contributions
            WHERE merchant_id = :merchant_id
              AND product_key = :product_key
            ORDER BY created_at DESC
            LIMIT 50
            """,
            {"merchant_id": merchant_id, "product_key": product_key},
        )
        return {
            "status": "success",
            "product_key": product_key,
            "pdp": projection["pdp"],
            "modules": projection["modules"],
            "published_payload": projection["published_payload"],
            "contributions": [
                {
                    "id": row["id"],
                    "pdp_id": row["pdp_id"],
                    "product_key": row["product_key"],
                    "module_key": row["module_key"],
                    "status": row["status"],
                    "reviewed_by_actor_type": row["reviewed_by_actor_type"],
                    "reviewed_by_actor_id": row["reviewed_by_actor_id"],
                    "review_decision": row["review_decision"],
                    "review_notes": row["review_notes"],
                    "notes": row["notes"],
                    "created_at": str(row["created_at"]) if row["created_at"] else None,
                    "updated_at": str(row["updated_at"]) if row["updated_at"] else None,
                }
                for row in rows
            ],
        }
    except Exception as exc:
        raise _map_error(exc)


@router.post("/product/{platform}/{platform_product_id}/contributions")
async def submit_product_pdp_contribution(
    platform: str,
    platform_product_id: str,
    body: MerchantContributionRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    merchant_id = _merchant_id(current_user)
    product_key = f"{merchant_id}|{platform}|{platform_product_id}"
    try:
        parse_product_key(product_key)
        return await create_merchant_contribution(
            product_key=product_key,
            merchant_id=merchant_id,
            module_key=body.module_key,
            payload=body.payload,
            notes=body.notes,
            market=body.market,
        )
    except Exception as exc:
        raise _map_error(exc)


class SupplierEvidenceRequest(BaseModel):
    # The merchant supplies EVIDENCE, not copy: an INCI list (pasted) OR a
    # product-page URL we crawl for the INCI (the easier front door). Pivota
    # verifies → substantiates → screens → grades it into provenance-backed,
    # claim-safe claims on the canonical record. Lab/cert/community land later.
    raw_inci: Optional[str] = None
    brand_url: Optional[str] = None


@router.post("/product/{platform}/{platform_product_id}/evidence")
async def submit_product_evidence(
    platform: str,
    platform_product_id: str,
    body: SupplierEvidenceRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """Supplier evidence intake — verify + grade the merchant's evidence into the
    canonical record and serve it (docs/SUPPLIER_EVIDENCE_INTAKE.md). Keyed to the
    authed merchant's own product."""
    merchant_id = _merchant_id(current_user)
    product_key = f"{merchant_id}|{platform}|{platform_product_id}"
    try:
        parse_product_key(product_key)
        from services.supplier_evidence_intake import ingest_supplier_evidence

        out = await ingest_supplier_evidence(
            product_key, raw_inci=body.raw_inci, brand_url=body.brand_url
        )
        # Record a successful Pivota-page evidence submission as a completed task,
        # so the merchant's action lands in the one unified Action plan (the task
        # queue is the single source of truth for "what to do" / "what's done").
        # Best-effort: the evidence is already saved regardless of the task.
        await _record_evidence_task(merchant_id, platform_product_id, out)
        return out
    except Exception as exc:
        raise _map_error(exc)


async def _record_evidence_task(merchant_id: str, label: str, out: Any) -> None:
    """Record a successful supplier-evidence submission as a COMPLETED
    `merchant_tasks` row so it shows in the merchant's single Action plan / task
    queue. Only on a real grade (status 'ok' + >=1 substantiated claim) — attempts
    that produced nothing don't pollute the queue. Never raises (the evidence is
    already persisted). Reuses the same task path as the niche-content flow; no
    new store. Note: one done row per successful submit (re-submits are real
    events) — add evidence_jsonb.product_key dedup later if the queue gets noisy."""
    try:
        if not isinstance(out, dict) or str(out.get("status")) != "ok":
            return
        claims = out.get("substantiated_claims")
        if not isinstance(claims, list) or not claims:
            return
        from db.merchant_tasks import record_task_created, update_task_status

        n = len(claims)
        task_id = await record_task_created(
            merchant_id=merchant_id,
            title=f"Add Pivota-page evidence for: {label}",
            body=(
                f"Submitted ingredient evidence → {n} cited "
                f"claim{'' if n == 1 else 's'} now on your Pivota page."
            ),
            severity="medium",
            lever="sku_evidence",
            assigned_to_agent="supplier_evidence",
            evidence={
                "kind": "sku_evidence",
                "product_key": out.get("product_key"),
                "content_key": out.get("content_key"),
                "served": out.get("served"),
                "substantiated_claims": claims[:10],
            },
        )
        if task_id:
            await update_task_status(task_id=task_id, status="done")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "evidence task record failed for merchant=%s: %s",
            merchant_id,
            str(exc)[:200],
        )


class MerchantApproveRequest(BaseModel):
    module_key: str = "copy"
    market: str = DEFAULT_MARKET
    # NOTE: no caller-supplied version_id -- we always review exactly the staged
    # version the projection resolves for this merchant's product_key, so a caller
    # cannot point the approve at an arbitrary (or another merchant's) version.


@router.post("/product/{platform}/{platform_product_id}/approve")
async def approve_product_pdp_module(
    platform: str,
    platform_product_id: str,
    body: MerchantApproveRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """Merchant approves a staged module; route through the LLM-reviewed GPT55
    gate. On pass for a low-risk module the gate auto-publishes, which (when
    SKU_OPT_OVERLAY_V1 is on) materializes a merchant_product_overlay row that the
    public PDP merge hook serves. Merchants are NOT direct publish authorities;
    the gate is. Failure or budget cap -> needs_human_review, nothing publishes.
    """
    if not SKU_OPT_OVERLAY_V1_ENABLED:
        raise HTTPException(status_code=404, detail="SKU_OPT_OVERLAY_V1_DISABLED")
    if body.module_key not in MERCHANT_SELF_APPROVE_MODULES:
        raise HTTPException(status_code=400, detail="MODULE_NOT_MERCHANT_APPROVABLE")

    merchant_id = _merchant_id(current_user)
    product_key = f"{merchant_id}|{platform}|{platform_product_id}"
    try:
        parse_product_key(product_key)
        projection = await get_pdp_projection(product_key=product_key, market=body.market)
        pdp_id = projection["pdp"]["pdp_id"]

        # Find the staged module the merchant is approving. get_pdp_projection
        # returns one summary per module_key with the staged version nested under
        # the "staged" key (NOT a top-level "stage" field).
        module_summary = next(
            (
                m
                for m in projection.get("modules", [])
                if m.get("module_key") == body.module_key
            ),
            None,
        )
        staged = (module_summary or {}).get("staged")
        if not staged:
            raise HTTPException(status_code=404, detail="NO_STAGED_MODULE")
        # Always review exactly this staged version (no caller-supplied id).
        version_id = staged.get("id")
        if not version_id:
            raise HTTPException(status_code=404, detail="NO_STAGED_MODULE")
        payload = staged.get("payload") if isinstance(staged.get("payload"), dict) else {}

        rubric = await generate_copy_review_rubric(
            merchant_id=merchant_id,
            payload=payload,
            source_refs=staged.get("source_refs"),
        )
        if rubric is None:
            return {
                "status": "success",
                "product_key": product_key,
                "module_key": body.module_key,
                "decision": "needs_human_review",
                "published": False,
                "reason": "copy_review_unavailable",
            }

        result = await review_module_version(
            pdp_id=pdp_id,
            module_key=body.module_key,
            version_id=version_id,
            actor_type=REVIEW_ACTOR_GPT55,
            actor_id=f"merchant:{merchant_id}",
            external_rubric=rubric,
        )
        return {
            "status": "success",
            "product_key": product_key,
            "module_key": body.module_key,
            "decision": result.get("decision"),
            "published": bool(result.get("published")),
            "rubric_confidence": rubric.get("confidence"),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise _map_error(exc)
