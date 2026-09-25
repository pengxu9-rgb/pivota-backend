"""Merchant-facing Store Readiness journey orchestration.

This route closes the gap between a visibility audit and the existing internal
commerce browser worker. A merchant supplies one representative product URL;
the backend proves the host belongs to that merchant, creates a bounded
storefront route, and enqueues one browser journey. The worker may search,
inspect the PDP, add one item, and fill synthetic shipping data, but it stops
before payment and never places an order.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from db.audit_evidence import (
    VERIFIER_COMMERCE_CHECKOUT_PROBE,
    claim_execution_route,
    enqueue_verification_run,
    fetch_latest_commerce_verification_for_merchant,
    has_in_flight_verification_for_route,
    upsert_execution_route,
)
from db.database import database
from db.merchant_audit_runs import (
    record_audit_run_completed,
    record_audit_run_started,
)
from routes.store_audit_public_intake import normalize_store_domain
from services.brand_claim_service import merchant_owns_domain
from utils.auth import get_current_merchant

router = APIRouter(
    prefix="/api/merchant-center/audit/store-readiness",
    tags=["store-readiness"],
)

SUBJECT_TYPE_STORE_READINESS = "store_readiness"
STEP_NAMES = (
    "storefront_access", "product_search", "product_detail",
    "add_to_cart", "shipping_address", "checkout",
)
StepStatus = Literal[
    "pending", "passed", "failed", "blocked", "not_supported", "not_run",
]


class StoreReadinessRunRequest(BaseModel):
    product_url: str = Field(..., min_length=12, max_length=2048)


class StoreReadinessStep(BaseModel):
    step: str
    status: StepStatus
    reason: Optional[str] = None


class StoreReadinessResponse(BaseModel):
    state: Literal["not_run", "pending", "complete", "failed", "blocked"]
    overall: Literal["not_run", "running", "ready", "attention", "blocked"]
    audit_run_id: Optional[str] = None
    verification_run_id: Optional[str] = None
    checked_at: Optional[datetime] = None
    steps: List[StoreReadinessStep]
    safety_stop: str = "Synthetic shipping data only; payment and order submission are never attempted."


def _require_enabled() -> None:
    enabled = os.getenv(
        "STORE_AUDIT_COMMERCE_PROBE_RECEIPT_ENABLED", "false",
    ).strip().lower() == "true"
    if not enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "STORE_READINESS_UNAVAILABLE"},
        )


def _target(value: str) -> tuple[str, str]:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        if (
            parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.port
        ):
            raise ValueError
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_PRODUCT_URL"},
        )
    domain = normalize_store_domain(raw)
    if not domain:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_PRODUCT_URL"},
        )
    # execution_routes deliberately removes query/fragment. This also keeps
    # variant tokens and analytics parameters out of worker claims.
    clean = f"https://{parsed.hostname.lower()}{parsed.path or '/'}"
    return domain, clean


async def _product_identity(*, merchant_id: str, url: str) -> str:
    try:
        row = await database.fetch_one(
            """
            SELECT product_key
              FROM catalog_products
             WHERE merchant_id = :merchant_id
               AND canonical_url IS NOT NULL
               AND split_part(canonical_url, '?', 1) = :url
               AND suppression_reason IS NULL
             ORDER BY updated_at DESC NULLS LAST
             LIMIT 1
            """,
            {"merchant_id": merchant_id, "url": url},
        )
    except Exception:  # noqa: BLE001 - URL audits do not require catalog sync
        row = None
    if row and row.get("product_key"):
        return str(row["product_key"])
    digest = hashlib.sha256(f"{merchant_id}|{url}".encode()).hexdigest()[:24]
    return f"url_probe:{digest}"


def _empty_steps(status_value: StepStatus = "not_run") -> List[StoreReadinessStep]:
    return [StoreReadinessStep(step=name, status=status_value) for name in STEP_NAMES]


def _project(row: Optional[Dict[str, Any]]) -> StoreReadinessResponse:
    if not row:
        return StoreReadinessResponse(
            state="not_run", overall="not_run", steps=_empty_steps(),
        )
    queue_status = str(row.get("status") or "")
    payload = row.get("evidence_jsonb") if isinstance(row.get("evidence_jsonb"), dict) else {}
    raw_steps = payload.get("steps") if isinstance(payload, dict) else None
    indexed: Dict[str, Dict[str, Any]] = {
        str(item.get("step")): item
        for item in (raw_steps or []) if isinstance(item, dict)
    }
    if queue_status in {"pending", "claimed"}:
        step_default: StepStatus = "pending"
        state = "pending"
        overall = "running"
    else:
        step_default = "not_run"
        state = "complete" if queue_status == "succeeded" else (
            "blocked" if queue_status == "blocked" else "failed"
        )
        overall = "blocked" if state == "blocked" else "attention"
    steps: List[StoreReadinessStep] = []
    for name in STEP_NAMES:
        item = indexed.get(name, {})
        candidate = str(item.get("status") or step_default)
        if candidate not in {
            "pending", "passed", "failed", "blocked", "not_supported", "not_run",
        }:
            candidate = step_default
        steps.append(StoreReadinessStep(
            step=name, status=candidate, reason=str(item.get("reason") or "") or None,
        ))
    if state == "complete":
        critical = {step.step: step.status for step in steps}
        if (
            critical.get("storefront_access") == "passed"
            and critical.get("product_search") == "passed"
            and critical.get("product_detail") == "passed"
            and critical.get("add_to_cart") == "passed"
            and critical.get("checkout") == "passed"
            and critical.get("shipping_address") == "passed"
        ):
            overall = "ready"
    checked_at = row.get("completed_at") or row.get("last_checked_at")
    return StoreReadinessResponse(
        state=state, overall=overall,
        audit_run_id=str(row.get("audit_run_id") or "") or None,
        verification_run_id=str(row.get("verify_id") or "") or None,
        checked_at=checked_at,
        steps=steps,
    )


@router.get("", response_model=StoreReadinessResponse)
async def get_store_readiness(
    merchant_id: str = Depends(get_current_merchant),
) -> StoreReadinessResponse:
    _require_enabled()
    return _project(await fetch_latest_commerce_verification_for_merchant(
        merchant_id=merchant_id,
    ))


@router.post("", response_model=StoreReadinessResponse, status_code=202)
async def run_store_readiness(
    payload: StoreReadinessRunRequest,
    merchant_id: str = Depends(get_current_merchant),
) -> StoreReadinessResponse:
    _require_enabled()
    domain, target_url = _target(payload.product_url)
    try:
        owns = await merchant_owns_domain(merchant_id, domain)
    except Exception:  # noqa: BLE001
        owns = False
    if not owns:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "DOMAIN_NOT_BOUND"},
        )

    route = await upsert_execution_route(
        normalized_domain=domain, route_kind="storefront", endpoint=target_url,
        merchant_id=merchant_id,
    )
    if not route:
        raise HTTPException(status_code=503, detail={"error": "ROUTE_CREATE_FAILED"})
    route_id = str(route.get("execution_route_id") or "")
    route_owner = str(route.get("merchant_id") or "")
    if not route_owner:
        claimed = await claim_execution_route(
            execution_route_id=route_id, merchant_id=merchant_id,
        )
        route_owner = str((claimed or {}).get("merchant_id") or "")
    if route_owner != merchant_id:
        raise HTTPException(status_code=409, detail={"error": "ROUTE_OWNED_BY_OTHER"})

    if await has_in_flight_verification_for_route(
        execution_route_id=route_id,
        verifier_id=VERIFIER_COMMERCE_CHECKOUT_PROBE,
    ):
        return _project(await fetch_latest_commerce_verification_for_merchant(
            merchant_id=merchant_id,
        ))

    product_key = await _product_identity(merchant_id=merchant_id, url=target_url)
    run_id = await record_audit_run_started(
        merchant_id=merchant_id,
        product_keys=[product_key],
        subject_type=SUBJECT_TYPE_STORE_READINESS,
    )
    if not run_id:
        raise HTTPException(status_code=503, detail={"error": "RUN_CREATE_FAILED"})
    route = await upsert_execution_route(
        normalized_domain=domain, route_kind="storefront", endpoint=target_url,
        merchant_id=merchant_id, audit_run_id=run_id,
    )
    if not route:
        await record_audit_run_completed(
            run_id=run_id, status="failed", error_message="store_readiness_route_update_failed",
        )
        raise HTTPException(status_code=503, detail={"error": "ROUTE_CREATE_FAILED"})
    verify_id = await enqueue_verification_run(
        audit_run_id=run_id, merchant_id=merchant_id, product_key=product_key,
        execution_route_id=route_id,
        verifier_id=VERIFIER_COMMERCE_CHECKOUT_PROBE,
        max_retries=1, idempotency_key=f"store_readiness:{run_id}",
    )
    if not verify_id:
        await record_audit_run_completed(
            run_id=run_id, status="failed", error_message="store_readiness_enqueue_failed",
        )
        # The unique active-route and active-merchant indexes close the race
        # between the check above and INSERT. If another request won, return
        # its pending journey rather than surfacing a false outage.
        existing = await fetch_latest_commerce_verification_for_merchant(
            merchant_id=merchant_id,
        )
        if existing and str(existing.get("status") or "") in {"pending", "claimed"}:
            return _project(existing)
        raise HTTPException(status_code=503, detail={"error": "ENQUEUE_FAILED"})
    return StoreReadinessResponse(
        state="pending", overall="running", audit_run_id=run_id,
        verification_run_id=verify_id, steps=_empty_steps("pending"),
    )
