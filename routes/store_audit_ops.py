"""Admin-only diagnostics for the Store Audit UCP probe lane.

WHY THIS EXISTS. When the probe reports `blocked`, the reason is written to
verification_runs.error_message / evidence_jsonb and NOWHERE else: the Cloud Run
worker deliberately logs no endpoints or receipt bodies, and Cloud SQL is
private-IP only, so nothing outside the VPC can read it. On 2026-09-04 four of
six probed storefronts came back `inconclusive` while serving clean UCP
profiles, and four separate hypotheses (user agent, Accept header, profile
schema shape, cross-origin endpoint) were eliminated one round-trip at a time
from the outside without ever reaching the recorded answer.

WHAT IT IS NOT. Not a query endpoint. Two fixed questions, no caller-supplied
SQL, no table names on the wire, admin-gated, and every payload passes through
the SAME sensitive-key redaction the receipt path refuses writes on. A domain is
the only free input and it is normalized before use.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from db.audit_evidence import (
    ROUTE_KIND_UCP,
    ROUTE_KIND_UCP_DISCOVERY,
    VERIFIER_UCP_PROBE,
    fetch_verification_history_for_domain,
    summarize_ucp_route_merchant_coverage,
)
from routes.store_audit_probe_internal import SENSITIVE_RESULT_KEYS
from utils.auth import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ops/store-audit", tags=["store-audit-ops"])

_REDACTED = "[redacted]"
_MAX_STRING = 500


def _normalize_domain(value: str) -> str:
    """Same shape the lane keys on: host only, lowercase, no scheme, no www."""
    text = str(value or "").strip().lower()
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    text = text.split("@")[-1].split(":", 1)[0]
    text = text.strip(".")
    if text.startswith("www."):
        text = text[4:]
    return text


def _redact(value: Any, depth: int = 0) -> Any:
    """Drop anything the receipt path would have refused to accept.

    Deliberately keyed on the SAME set that guards writes: a key this system
    distrusts on the way in must not become readable on the way out just
    because a different door asked for it.
    """
    if depth > 8:
        return _REDACTED
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, nested in value.items():
            flat = re.sub(r"[^a-z0-9]", "", str(key).strip().lower())
            out[str(key)] = (
                _REDACTED if flat in SENSITIVE_RESULT_KEYS
                else _redact(nested, depth + 1)
            )
        return out
    if isinstance(value, list):
        return [_redact(item, depth + 1) for item in value[:25]]
    if isinstance(value, str):
        return value[:_MAX_STRING]
    return value


class VerificationAttempt(BaseModel):
    verify_id: str
    audit_run_id: Optional[str] = None
    status: str
    error_message: Optional[str] = None
    evidence: Optional[Dict[str, Any]] = None
    retry_count: int = 0
    max_retries: int = 0
    carried_variant: bool = False
    route_kind: Optional[str] = None
    route_is_active: Optional[bool] = None
    route_merchant_id: Optional[str] = None
    claimed_by_worker: Optional[str] = None
    created_at: Optional[str] = None
    completed_at: Optional[str] = None


class DomainDiagnosticsResponse(BaseModel):
    domain: str
    attempts: List[VerificationAttempt]
    # Says what an empty list MEANS, because the reader cannot tell otherwise:
    # the underlying lookup returns [] both for "never probed" and for "the
    # lookup itself failed", and a diagnostic that quietly conflates those is
    # the thing it was built to stop.
    note: str


class CoverageResponse(BaseModel):
    active_ucp_routes: int
    routes_with_proven_merchant: int
    note: str


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else None


@router.get("/domain-diagnostics", response_model=DomainDiagnosticsResponse)
async def domain_diagnostics(
    domain: str = Query(..., min_length=3, max_length=253),
    limit: int = Query(10, ge=1, le=50),
    _admin: Dict[str, Any] = Depends(require_admin),
) -> DomainDiagnosticsResponse:
    """Why this storefront's UCP probe reported what it reported."""
    normalized = _normalize_domain(domain)
    if not normalized:
        return DomainDiagnosticsResponse(
            domain="", attempts=[], note="domain did not normalize to a host",
        )
    rows = await fetch_verification_history_for_domain(
        normalized_domain=normalized,
        verifier_id=VERIFIER_UCP_PROBE,
        route_kinds=(ROUTE_KIND_UCP, ROUTE_KIND_UCP_DISCOVERY),
        limit=limit,
    )
    attempts = [
        VerificationAttempt(
            verify_id=str(r.get("verify_id") or ""),
            audit_run_id=str(r["audit_run_id"]) if r.get("audit_run_id") else None,
            status=str(r.get("status") or ""),
            error_message=(str(r["error_message"])[:_MAX_STRING]
                           if r.get("error_message") else None),
            evidence=_redact(r.get("evidence_jsonb")) if r.get("evidence_jsonb") else None,
            retry_count=int(r.get("retry_count") or 0),
            max_retries=int(r.get("max_retries") or 0),
            # The value itself is a merchant's variant id; whether one was
            # carried is the diagnostic fact, and it is the whole question for
            # the checkout-tested tier.
            carried_variant=bool(r.get("product_key")),
            route_kind=str(r["route_kind"]) if r.get("route_kind") else None,
            route_is_active=(bool(r["is_active"]) if r.get("is_active") is not None
                             else None),
            route_merchant_id=(str(r["route_merchant_id"])
                               if r.get("route_merchant_id") else None),
            claimed_by_worker=(str(r["claimed_by_worker"])[:_MAX_STRING]
                               if r.get("claimed_by_worker") else None),
            created_at=_iso(r.get("created_at")),
            completed_at=_iso(r.get("completed_at")),
        )
        for r in rows
    ]
    return DomainDiagnosticsResponse(
        domain=normalized,
        attempts=attempts,
        note=(
            "no rows: this domain has never been probed, OR the lookup failed "
            "(they are indistinguishable here — check service logs)"
            if not attempts else f"{len(attempts)} attempt(s), newest first"
        ),
    )


@router.get("/checkout-tier-coverage", response_model=CoverageResponse)
async def checkout_tier_coverage(
    _admin: Dict[str, Any] = Depends(require_admin),
) -> CoverageResponse:
    """How many active UCP routes could EVER reach the checkout-tested tier.

    Answer this before flipping STORE_AUDIT_UCP_PROBE_CHECKOUT_TIER_ENABLED:
    if routes_with_proven_merchant is 0, the flag changes nothing and the
    reprobe summary's `variant_carried: 0` would be indistinguishable from a
    bug.
    """
    summary = await summarize_ucp_route_merchant_coverage()
    failed = summary.get("active_ucp_routes", -1) < 0
    return CoverageResponse(
        active_ucp_routes=summary.get("active_ucp_routes", -1),
        routes_with_proven_merchant=summary.get("routes_with_proven_merchant", -1),
        note=(
            "lookup FAILED — these numbers are not measurements" if failed
            else "flipping the checkout tier changes nothing while "
                 "routes_with_proven_merchant is 0"
        ),
    )
