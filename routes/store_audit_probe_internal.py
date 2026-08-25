"""Fail-closed internal receipt endpoint for Store Audit UCP probes.

The external UCP worker never writes Pivota's database. It submits a small,
redacted result only after claiming a ``ucp_probe`` verification job. This
router authenticates the worker, preserves the work-queue lease boundary, and
makes the backend the single owner of execution-route and evidence writes.
"""

from __future__ import annotations

import hmac
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Header, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator

from db.audit_evidence import (
    EVIDENCE_TYPE_ACCEPTANCE_SIGNAL,
    VERIFICATION_STATUS_BLOCKED,
    VERIFICATION_STATUS_SUCCEEDED,
    VERIFIER_UCP_PROBE,
    attach_execution_route_to_claimed_verification,
    claim_next_pending_verification,
    deactivate_execution_route,
    fetch_execution_route,
    get_claimed_verification_run,
    get_verification_run_for_worker,
    insert_evidence_item,
    mark_verification_blocked,
    mark_verification_failed_with_retry,
    mark_verification_succeeded,
    normalize_execution_route_identity,
    upsert_execution_route,
)
from db.database import database

router = APIRouter(
    prefix="/internal/store-audit/ucp-probes",
    tags=["store-audit-internal"],
)

_SENSITIVE_RESULT_KEYS = frozenset({
    "authorization", "checkouturl", "continueurl", "cookie", "cookies",
    "raw", "rawresponse", "response", "secret", "session", "token",
    "toolresult",
})
_MAX_SIGNAL_PAYLOAD_BYTES = 16_384
_MAX_SIGNAL_PAYLOAD_DEPTH = 8


def _receipt_enabled() -> bool:
    return (
        os.getenv("STORE_AUDIT_UCP_PROBE_RECEIPT_ENABLED", "false")
        .strip().lower() == "true"
    )


def _require_receipt_key(x_internal_key: Optional[str]) -> None:
    # A disabled receipt is deliberately indistinguishable from an absent
    # route. Setting a secret alone cannot expose a pre-rollout endpoint.
    if not _receipt_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    expected = str(os.getenv("STORE_AUDIT_UCP_PROBE_INTERNAL_KEY") or "").strip()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "CONFIG_MISSING", "message": "UCP receipt key is not configured"},
        )
    provided = str(x_internal_key or "").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "FORBIDDEN", "message": "Missing or invalid X-Internal-Key"},
        )


def _contains_sensitive_data(value: Any) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).strip().lower())
            if normalized_key in _SENSITIVE_RESULT_KEYS:
                return True
            if _contains_sensitive_data(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_sensitive_data(item) for item in value)
    elif isinstance(value, str):
        # There is no legitimate URL in the persisted acceptance-signal
        # payload. The route endpoint is a distinct, validated field.
        return "http://" in value.lower() or "https://" in value.lower()
    return False


def _payload_depth(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max((_payload_depth(item) for item in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_payload_depth(item) for item in value), default=0)
    return 0


class UcpRoute(BaseModel):
    normalized_domain: str = Field(..., min_length=1, max_length=255)
    route_kind: Literal["ucp"]
    endpoint: str = Field(..., min_length=8, max_length=2048)
    profile_fingerprint: Optional[str] = Field(None, max_length=128)
    expires_at: Optional[datetime] = None

    @model_validator(mode="after")
    def normalize_identity(self) -> "UcpRoute":
        try:
            domain, kind, endpoint = normalize_execution_route_identity(
                normalized_domain=self.normalized_domain,
                route_kind=self.route_kind,
                endpoint=self.endpoint,
            )
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        self.normalized_domain = domain
        self.route_kind = kind  # type: ignore[assignment]
        self.endpoint = endpoint
        return self


class AcceptanceSignal(BaseModel):
    evidence_type: Literal["acceptance_signal"]
    evidence_level: Literal["detected", "tested"]
    payload: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def payload_must_be_redacted(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        if _contains_sensitive_data(value):
            raise ValueError("acceptance signal must not contain raw, session, or URL data")
        try:
            encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("acceptance signal must be JSON serializable") from exc
        if len(encoded.encode("utf-8")) > _MAX_SIGNAL_PAYLOAD_BYTES:
            raise ValueError("acceptance signal exceeds payload size limit")
        if _payload_depth(value) > _MAX_SIGNAL_PAYLOAD_DEPTH:
            raise ValueError("acceptance signal exceeds payload depth limit")
        return value


class UcpProbeReceipt(BaseModel):
    audit_run_id: str = Field(..., min_length=1, max_length=128)
    verification_run_id: str = Field(..., min_length=1, max_length=128)
    worker_id: str = Field(..., min_length=1, max_length=255)
    probe_id: str = Field(..., min_length=8, max_length=255)
    verifier_id: Literal["ucp_probe"]
    verification_status: Literal["succeeded", "failed", "blocked"]
    reason: Optional[str] = Field(None, max_length=500)
    observed_at: datetime
    route: Optional[UcpRoute] = None
    acceptance_signal: Optional[AcceptanceSignal] = None

    @model_validator(mode="after")
    def route_signal_relationship_is_consistent(self) -> "UcpProbeReceipt":
        if self.acceptance_signal and not self.route:
            raise ValueError("acceptance signal requires a route")
        if self.acceptance_signal and self.verification_status != "succeeded":
            raise ValueError("acceptance signal requires a succeeded verification")
        return self


class UcpProbeReceiptResponse(BaseModel):
    verification_status: str
    execution_route_id: Optional[str] = None
    evidence_id: Optional[str] = None


class UcpProbeClaimRequest(BaseModel):
    worker_id: str = Field(..., min_length=1, max_length=255)


class UcpProbeClaimResponse(BaseModel):
    audit_run_id: str
    verification_run_id: str
    worker_id: str
    probe_id: str
    brand_domain: str
    retry_count: int
    variant_gid: Optional[str] = None


def _verification_evidence(receipt: UcpProbeReceipt) -> Dict[str, Any]:
    """Small terminal-run payload; deliberately excludes tool/raw responses."""
    return {
        "verifier_id": VERIFIER_UCP_PROBE,
        "verification_status": receipt.verification_status,
        "reason": receipt.reason,
        "observed_at": receipt.observed_at.isoformat(),
        "probe_id": receipt.probe_id,
    }


@asynccontextmanager
async def _receipt_transaction():
    """Keep route, evidence, and terminal status in one DB transaction."""
    async with database.transaction():
        yield


@router.post("/claims", response_model=UcpProbeClaimResponse)
async def claim_ucp_probe(
    payload: UcpProbeClaimRequest,
    response: Response,
    x_internal_key: Optional[str] = Header(None, alias="X-Internal-Key"),
) -> Optional[UcpProbeClaimResponse]:
    """Claim one UCP row for the isolated external crawl worker.

    The generic in-process verification worker explicitly excludes this
    verifier. Returning no route data other than the normalized commerce
    domain keeps the worker unable to discover unrelated merchant records.
    """
    _require_receipt_key(x_internal_key)
    claimed = await claim_next_pending_verification(
        worker_id=payload.worker_id,
        verifier_id=VERIFIER_UCP_PROBE,
    )
    if not claimed:
        # FastAPI validates a returned ``None`` against response_model before
        # honoring a mutated injected Response status. Return a concrete empty
        # response so an idle remote worker gets the intended 204, not a 500.
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    route_id = str(claimed.get("execution_route_id") or "")
    route = await fetch_execution_route(execution_route_id=route_id)
    if not route:
        await mark_verification_blocked(
            verify_id=str(claimed["verify_id"]),
            worker_id=payload.worker_id,
            error_message="ucp_probe_claim_missing_active_route",
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "ROUTE_UNAVAILABLE"},
        )
    retry_count = int(claimed.get("retry_count") or 0)
    verify_id = str(claimed["verify_id"])
    return UcpProbeClaimResponse(
        audit_run_id=str(claimed["audit_run_id"]),
        verification_run_id=verify_id,
        worker_id=payload.worker_id,
        probe_id=f"{verify_id}:attempt:{retry_count + 1}",
        brand_domain=str(route["normalized_domain"]),
        retry_count=retry_count,
        variant_gid=(
            str(claimed["product_key"])
            if str(claimed.get("product_key") or "").startswith(
                "gid://shopify/ProductVariant/"
            ) else None
        ),
    )


@router.post("/receipts", response_model=UcpProbeReceiptResponse)
async def receive_ucp_probe_receipt(
    receipt: UcpProbeReceipt,
    x_internal_key: Optional[str] = Header(None, alias="X-Internal-Key"),
) -> UcpProbeReceiptResponse:
    _require_receipt_key(x_internal_key)
    async with _receipt_transaction():
        return await _persist_ucp_probe_receipt(receipt)


async def _persist_ucp_probe_receipt(
    receipt: UcpProbeReceipt,
) -> UcpProbeReceiptResponse:

    claimed = await get_claimed_verification_run(
        verify_id=receipt.verification_run_id,
        worker_id=receipt.worker_id,
        verifier_id=VERIFIER_UCP_PROBE,
    )
    if not claimed:
        # A successful response can be lost after the backend committed the
        # terminal transition. A repeat of the exact same receipt is safe to
        # acknowledge, but a pending retry is not: it may already be owned by
        # another worker and must remain a conflict.
        previous = await get_verification_run_for_worker(
            verify_id=receipt.verification_run_id,
            worker_id=receipt.worker_id,
            verifier_id=VERIFIER_UCP_PROBE,
        )
        previous_evidence = (previous or {}).get("evidence_jsonb") or {}
        if not isinstance(previous_evidence, dict):
            previous_evidence = {}
        if (
            previous
            and str(previous.get("audit_run_id") or "") == receipt.audit_run_id
            and previous.get("status") in {
                VERIFICATION_STATUS_SUCCEEDED,
                VERIFICATION_STATUS_BLOCKED,
                "exhausted_retries",
            }
            and previous_evidence.get("probe_id") == receipt.probe_id
        ):
            return UcpProbeReceiptResponse(
                verification_status=str(previous["status"]),
                execution_route_id=(
                    str(previous["execution_route_id"])
                    if previous.get("execution_route_id") else None
                ),
            )
    if not claimed or str(claimed.get("audit_run_id") or "") != receipt.audit_run_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "VERIFICATION_NOT_CLAIMED", "message": "Verification is not claimed by this worker"},
        )

    execution_route_id = str(claimed.get("execution_route_id") or "") or None
    claimed_route = await fetch_execution_route(
        execution_route_id=execution_route_id or "",
    )
    if not claimed_route:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "CLAIMED_ROUTE_UNAVAILABLE"},
        )
    if receipt.route:
        # A receipt is bound to the route that the worker claimed. Endpoint
        # rotations must use an explicit route-transition workflow; allowing a
        # result to switch identity here would let a stale/compromised worker
        # rewrite another endpoint's evidence association.
        if (
            receipt.route.normalized_domain != claimed_route["normalized_domain"]
            or receipt.route.endpoint != claimed_route["endpoint_normalized"]
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "CLAIMED_ROUTE_IDENTITY_MISMATCH"},
            )
        # Discovery remains domain-keyed. A later verified merchant conversion
        # calls claim_execution_route; this receipt never claims a route.
        route = await upsert_execution_route(
            normalized_domain=receipt.route.normalized_domain,
            route_kind=receipt.route.route_kind,
            endpoint=receipt.route.endpoint,
            profile_fingerprint=receipt.route.profile_fingerprint,
            audit_run_id=receipt.audit_run_id,
            last_verified_at=receipt.observed_at,
            expires_at=receipt.route.expires_at,
        )
        if not route:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "ROUTE_PERSIST_FAILED"},
            )
        if str(route["execution_route_id"]) != execution_route_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "CLAIMED_ROUTE_IDENTITY_MISMATCH"},
            )
        attached = await attach_execution_route_to_claimed_verification(
            verify_id=receipt.verification_run_id,
            worker_id=receipt.worker_id,
            execution_route_id=execution_route_id,
        )
        if not attached:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "VERIFICATION_NOT_CLAIMED"},
            )
    elif (
        receipt.verification_status == VERIFICATION_STATUS_SUCCEEDED
        and receipt.reason == "not_ucp_reachable"
    ):
        # A clean profile fetch that no longer advertises UCP is different from
        # a WAF/timeout block. Stop re-probing the old positive route while
        # retaining its historical evidence.
        deactivated = await deactivate_execution_route(
            execution_route_id=execution_route_id or "",
            last_verified_at=receipt.observed_at,
        )
        if not deactivated:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "ROUTE_DEACTIVATE_FAILED"},
            )

    evidence_id: Optional[str] = None
    if receipt.acceptance_signal:
        merchant_id = claimed.get("merchant_id")
        # Cold-start jobs can contain legacy synthetic IDs. Route evidence is
        # intentionally domain-only until merchant conversion claims the route.
        if merchant_id and str(merchant_id).startswith("prospect_"):
            merchant_id = None
        evidence_id = await insert_evidence_item(
            audit_run_id=receipt.audit_run_id,
            merchant_id=merchant_id,
            evidence_type=EVIDENCE_TYPE_ACCEPTANCE_SIGNAL,
            execution_route_id=execution_route_id,
            evidence_level=receipt.acceptance_signal.evidence_level,
            expires_at=receipt.route.expires_at if receipt.route else None,
            payload={
                "verifier_id": VERIFIER_UCP_PROBE,
                "observed_at": receipt.observed_at.isoformat(),
                "probe_id": receipt.probe_id,
                "signal": receipt.acceptance_signal.payload,
            },
            idempotency_key=f"ucp_probe:{receipt.probe_id}:acceptance",
        )
        if not evidence_id:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "EVIDENCE_PERSIST_FAILED"},
            )

    evidence_jsonb = _verification_evidence(receipt)
    if receipt.verification_status == VERIFICATION_STATUS_SUCCEEDED:
        completed = await mark_verification_succeeded(
            verify_id=receipt.verification_run_id,
            worker_id=receipt.worker_id,
            evidence_jsonb=evidence_jsonb,
        )
        resulting_status = VERIFICATION_STATUS_SUCCEEDED
    elif receipt.verification_status == VERIFICATION_STATUS_BLOCKED:
        completed = await mark_verification_blocked(
            verify_id=receipt.verification_run_id,
            worker_id=receipt.worker_id,
            error_message=receipt.reason or "UCP upstream unavailable",
            evidence_jsonb=evidence_jsonb,
        )
        resulting_status = VERIFICATION_STATUS_BLOCKED
    else:
        resulting_status = await mark_verification_failed_with_retry(
            verify_id=receipt.verification_run_id,
            worker_id=receipt.worker_id,
            error_message=receipt.reason or "UCP probe failed",
            evidence_jsonb=evidence_jsonb,
        )
        completed = resulting_status in {"pending", "exhausted_retries"}

    if not completed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "VERIFICATION_NOT_CLAIMED"},
        )
    return UcpProbeReceiptResponse(
        verification_status=resulting_status,
        execution_route_id=execution_route_id,
        evidence_id=evidence_id,
    )
