"""Internal, merchant-scoped Store Audit receipt and capability endpoints.

The worker may report only an enumerated, redacted checkout observation.  The
backend owns merchant identity, evidence persistence, capability resolution,
and verification-run terminal transitions.
"""

from __future__ import annotations

import hmac
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Header, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator

from db.audit_evidence import (
    EVIDENCE_LEVEL_TESTED,
    EVIDENCE_TYPE_COMMERCE_CARTABILITY,
    EVIDENCE_TYPE_COMMERCE_CHECKOUT_ROUTE,
    EVIDENCE_TYPE_COMMERCE_PLATFORM,
    VERIFICATION_STATUS_BLOCKED,
    VERIFICATION_STATUS_SUCCEEDED,
    VERIFIER_COMMERCE_CHECKOUT_PROBE,
    claim_next_pending_verification,
    fetch_active_commerce_evidence,
    fetch_execution_route,
    get_claimed_verification_run,
    get_verification_run_for_worker,
    insert_evidence_item,
    mark_verification_blocked,
    mark_verification_failed_with_retry,
    mark_verification_succeeded,
)
from db.database import database
from services.commerce_capability_resolver import resolve_merchant_capability

router = APIRouter(
    prefix="/internal/store-audit/commerce-probes",
    tags=["store-audit-internal"],
)

_SENSITIVE_VALUE = re.compile(
    r"(?:https?://|\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b|"
    r"\b(?:token|secret|cookie|session(?:[_ -]?id)?|authorization|card|"
    r"payment|email|phone|address)\s*[=:]\s*\S+)",
    re.IGNORECASE,
)


def _enabled() -> bool:
    return os.getenv("STORE_AUDIT_COMMERCE_PROBE_RECEIPT_ENABLED", "false").strip().lower() == "true"


def _require_key(value: Optional[str]) -> None:
    if not _enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    expected = str(os.getenv("STORE_AUDIT_COMMERCE_PROBE_INTERNAL_KEY") or "").strip()
    if not expected:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail={"error": "CONFIG_MISSING"})
    if not value or not hmac.compare_digest(value.strip(), expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"error": "FORBIDDEN"})


def _safe_text(value: str) -> str:
    if _SENSITIVE_VALUE.search(value):
        raise ValueError("commerce probe fields must not contain sensitive data")
    return value


class CommercePlatform(BaseModel):
    platform: Literal["shopify", "cafe24", "woocommerce", "bigcommerce", "magento", "custom", "unknown"]
    checkout_provider: Literal["shopify", "cafe24", "stripe", "adyen", "antom", "custom", "unknown"]


class CartObservation(BaseModel):
    status: Literal["verified", "unavailable", "blocked", "selection_required", "unknown"]
    quantity: Optional[int] = Field(None, ge=1, le=99)
    cart_price: Optional[float] = Field(None, ge=0)
    currency: Optional[str] = Field(None, pattern=r"^[A-Z]{3}$")


class CheckoutRouteObservation(BaseModel):
    status: Literal[
        "guest_route_detected", "security_challenged_pre_address",
        "security_challenged", "blocked", "login_required", "unavailable", "unknown",
    ]
    challenge_stage: Optional[Literal["pre_address", "pre_checkout", "checkout"]] = None


class CommerceProbeReceipt(BaseModel):
    audit_run_id: str = Field(..., min_length=1, max_length=128)
    verification_run_id: str = Field(..., min_length=1, max_length=128)
    worker_id: str = Field(..., min_length=1, max_length=255)
    probe_id: str = Field(..., min_length=8, max_length=255)
    verifier_id: Literal["commerce_checkout_probe"]
    verification_status: Literal["succeeded", "failed", "blocked"]
    outcome_code: Optional[Literal["challenge", "network", "timeout", "not_checkout_reachable", "invalid_probe"]] = None
    observed_at: datetime
    platform: Optional[CommercePlatform] = None
    checkout: Optional[CheckoutRouteObservation] = None
    cart: Optional[CartObservation] = None

    @field_validator("audit_run_id", "verification_run_id", "worker_id", "probe_id")
    @classmethod
    def field_is_redacted(cls, value: str) -> str:
        return _safe_text(value)

    @model_validator(mode="after")
    def receipt_is_semantically_complete(self) -> "CommerceProbeReceipt":
        if self.verification_status == "succeeded" and not self.checkout:
            raise ValueError("successful checkout probe requires checkout evidence")
        return self


class CommerceProbeReceiptResponse(BaseModel):
    verification_status: str
    evidence_ids: list[str] = Field(default_factory=list)
    capability: Optional[dict] = None


class CommerceProbeClaimRequest(BaseModel):
    worker_id: str = Field(..., min_length=1, max_length=255)

    @field_validator("worker_id")
    @classmethod
    def worker_id_is_redacted(cls, value: str) -> str:
        return _safe_text(value)


class CommerceProbeClaimResponse(BaseModel):
    audit_run_id: str
    verification_run_id: str
    worker_id: str
    probe_id: str
    merchant_id: str
    target_url: str
    product_key: Optional[str] = None
    retry_count: int


def _expires_at(observed_at: datetime, hours: int) -> datetime:
    value = observed_at if observed_at.tzinfo else observed_at.replace(tzinfo=timezone.utc)
    return value + timedelta(hours=hours)


@asynccontextmanager
async def _transaction():
    async with database.transaction():
        yield


def _terminal_evidence(receipt: CommerceProbeReceipt) -> dict:
    return {
        "verifier_id": VERIFIER_COMMERCE_CHECKOUT_PROBE,
        "verification_status": receipt.verification_status,
        "outcome_code": receipt.outcome_code,
        "observed_at": receipt.observed_at.isoformat(),
        "probe_id": receipt.probe_id,
    }


@router.post("/claims", response_model=CommerceProbeClaimResponse)
async def claim_commerce_probe(
    payload: CommerceProbeClaimRequest,
    response: Response,
    x_internal_key: Optional[str] = Header(None, alias="X-Internal-Key"),
) -> Optional[CommerceProbeClaimResponse]:
    """Lease one pre-authorized merchant checkout probe for the worker."""
    _require_key(x_internal_key)
    claimed = await claim_next_pending_verification(
        worker_id=payload.worker_id,
        verifier_id=VERIFIER_COMMERCE_CHECKOUT_PROBE,
    )
    if not claimed:
        response.status_code = status.HTTP_204_NO_CONTENT
        return None
    route = await fetch_execution_route(
        execution_route_id=str(claimed.get("execution_route_id") or ""),
    )
    merchant_id = str(claimed.get("merchant_id") or "")
    if (
        not route
        or route.get("route_kind") != "storefront"
        or not merchant_id
        or merchant_id.startswith("prospect_")
    ):
        await mark_verification_blocked(
            verify_id=str(claimed["verify_id"]), worker_id=payload.worker_id,
            error_message="commerce_probe_claim_missing_merchant_storefront_route",
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"error": "TARGET_UNAVAILABLE"})
    retry_count = int(claimed.get("retry_count") or 0)
    verify_id = str(claimed["verify_id"])
    return CommerceProbeClaimResponse(
        audit_run_id=str(claimed["audit_run_id"]), verification_run_id=verify_id,
        worker_id=payload.worker_id, probe_id=f"{verify_id}:attempt:{retry_count + 1}",
        merchant_id=merchant_id, target_url=str(route["endpoint_normalized"]),
        product_key=str(claimed.get("product_key") or "") or None,
        retry_count=retry_count,
    )


@router.post("/receipts", response_model=CommerceProbeReceiptResponse)
async def receive_commerce_probe_receipt(
    receipt: CommerceProbeReceipt,
    x_internal_key: Optional[str] = Header(None, alias="X-Internal-Key"),
) -> CommerceProbeReceiptResponse:
    _require_key(x_internal_key)
    async with _transaction():
        return await _persist_receipt(receipt)


async def _persist_receipt(receipt: CommerceProbeReceipt) -> CommerceProbeReceiptResponse:
    claimed = await get_claimed_verification_run(
        verify_id=receipt.verification_run_id,
        worker_id=receipt.worker_id,
        verifier_id=VERIFIER_COMMERCE_CHECKOUT_PROBE,
    )
    if not claimed or str(claimed.get("audit_run_id") or "") != receipt.audit_run_id:
        previous = await get_verification_run_for_worker(
            verify_id=receipt.verification_run_id,
            worker_id=receipt.worker_id,
            verifier_id=VERIFIER_COMMERCE_CHECKOUT_PROBE,
        )
        previous_evidence = dict((previous or {}).get("evidence_jsonb") or {})
        if previous and previous_evidence.get("probe_id") == receipt.probe_id:
            return CommerceProbeReceiptResponse(verification_status=str(previous["status"]))
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"error": "VERIFICATION_NOT_CLAIMED"})
    merchant_id = str(claimed.get("merchant_id") or "")
    if not merchant_id or merchant_id.startswith("prospect_"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"error": "MERCHANT_NOT_RESOLVED"})
    product_key = str(claimed.get("product_key") or "") or None
    evidence_ids: list[str] = []
    if receipt.platform:
        evidence_id = await insert_evidence_item(
            audit_run_id=receipt.audit_run_id, merchant_id=merchant_id,
            evidence_type=EVIDENCE_TYPE_COMMERCE_PLATFORM, evidence_level=EVIDENCE_LEVEL_TESTED,
            expires_at=_expires_at(receipt.observed_at, 24 * 30),
            payload=receipt.platform.model_dump(),
            idempotency_key=f"commerce:{receipt.probe_id}:platform",
        )
        if not evidence_id:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail={"error": "EVIDENCE_PERSIST_FAILED"})
        evidence_ids.append(evidence_id)
    if receipt.checkout:
        payload = {"audit_scope": "merchant_checkout", **receipt.checkout.model_dump()}
        if product_key:
            payload["probe_product_key"] = product_key
        evidence_id = await insert_evidence_item(
            audit_run_id=receipt.audit_run_id, merchant_id=merchant_id,
            evidence_type=EVIDENCE_TYPE_COMMERCE_CHECKOUT_ROUTE, evidence_level=EVIDENCE_LEVEL_TESTED,
            expires_at=_expires_at(receipt.observed_at, 24), payload=payload,
            idempotency_key=f"commerce:{receipt.probe_id}:checkout_route",
        )
        if not evidence_id:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail={"error": "EVIDENCE_PERSIST_FAILED"})
        evidence_ids.append(evidence_id)
    if receipt.cart:
        if not product_key:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"error": "PRODUCT_NOT_RESOLVED"})
        evidence_id = await insert_evidence_item(
            audit_run_id=receipt.audit_run_id, merchant_id=merchant_id, product_key=product_key,
            evidence_type=EVIDENCE_TYPE_COMMERCE_CARTABILITY, evidence_level=EVIDENCE_LEVEL_TESTED,
            expires_at=_expires_at(receipt.observed_at, 6), payload=receipt.cart.model_dump(exclude_none=True),
            idempotency_key=f"commerce:{receipt.probe_id}:cartability",
        )
        if not evidence_id:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail={"error": "EVIDENCE_PERSIST_FAILED"})
        evidence_ids.append(evidence_id)
    evidence_jsonb = _terminal_evidence(receipt)
    if receipt.verification_status == "succeeded":
        completed = await mark_verification_succeeded(verify_id=receipt.verification_run_id, worker_id=receipt.worker_id, evidence_jsonb=evidence_jsonb)
        resulting_status = VERIFICATION_STATUS_SUCCEEDED
    elif receipt.verification_status == "blocked":
        completed = await mark_verification_blocked(verify_id=receipt.verification_run_id, worker_id=receipt.worker_id, error_message=receipt.outcome_code or "commerce_probe_blocked", evidence_jsonb=evidence_jsonb)
        resulting_status = VERIFICATION_STATUS_BLOCKED
    else:
        resulting_status = await mark_verification_failed_with_retry(verify_id=receipt.verification_run_id, worker_id=receipt.worker_id, error_message=receipt.outcome_code or "commerce_probe_failed", evidence_jsonb=evidence_jsonb)
        completed = resulting_status in {"pending", "exhausted_retries"}
    if not completed:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"error": "VERIFICATION_NOT_CLAIMED"})
    capability = resolve_merchant_capability(
        merchant_id=merchant_id,
        evidence=await fetch_active_commerce_evidence(merchant_id=merchant_id),
    )
    return CommerceProbeReceiptResponse(verification_status=resulting_status, evidence_ids=evidence_ids, capability=capability)


@router.get("/capabilities/{merchant_id}")
async def get_commerce_capability(
    merchant_id: str,
    x_internal_key: Optional[str] = Header(None, alias="X-Internal-Key"),
) -> dict:
    _require_key(x_internal_key)
    return resolve_merchant_capability(
        merchant_id=merchant_id,
        evidence=await fetch_active_commerce_evidence(merchant_id=merchant_id),
    )
