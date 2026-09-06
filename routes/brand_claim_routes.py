"""P1 — brand claim endpoints (the front door for the claim WRITE path).

Scoped to the AUTHENTICATED merchant via get_current_merchant: a caller can only
claim/verify their OWN merchant — the claim's merchant_id is derived from auth,
never from the request body, so an attacker cannot flip an arbitrary merchant to
brand_direct. /claim/verify additionally cross-tenant-guards the claim row.

Storefront-agnostic: claiming + DNS/email verification is domain-based, so a
store-less brand can establish brand authority without owning a storefront.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from db import brand_claims as bc
from services import brand_attest_service as attest_svc
from services import brand_claim_service as svc
from utils.auth import get_current_employee, get_current_merchant

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/brands", tags=["brand-claims"])

_VALID_METHODS = {"dns", "email", "amazon", "shopify", "manual"}


class StartClaimBody(BaseModel):
    brand_domain: Optional[str] = Field(
        None, description="Domain used for DNS/email verification (required for dns + email)"
    )
    method: str = Field("dns", description="dns | email | amazon | shopify | manual")
    content_key: Optional[str] = Field(
        None, description="Optional: scope the claim to one canonical entity"
    )
    verification_email: Optional[str] = Field(
        None,
        description=(
            "For method=email: a mailbox AT brand_domain (e.g. admin@brand.com). "
            "We email a 6-digit code there; receiving it proves brand-domain control."
        ),
    )


class VerifyClaimBody(BaseModel):
    claim_id: str = Field(..., min_length=1)
    submitted_code: Optional[str] = Field(
        None, description="For method=email: the 6-digit code from the verification email."
    )


class AttestBody(BaseModel):
    product_key: str = Field(..., min_length=1)
    fields: dict = Field(
        ...,
        description=(
            "Brand-attested content: any of title, summary, description, "
            "bullet_points, usage_scenarios, audience_tags, topic_tags, disclaimer"
        ),
    )
    lab_evidence_ref: Optional[str] = Field(
        None, description="Reference to uploaded lab evidence (durable storage: next slice)"
    )


class DeclareDomainBody(BaseModel):
    domain: str = Field(
        ...,
        description=(
            "An additional domain this merchant operates. Stored as `declared` "
            "and NOT counted as official until control is proven via a claim."
        ),
    )


@router.post("/official-domains/declare")
async def declare_official_domain(
    body: DeclareDomainBody,
    merchant_id: str = Depends(get_current_merchant),
):
    """P0 item 5 — declare an additional official domain for the AUTHENTICATED
    merchant.

    Scoped to the caller's own merchant_id from the token, never a body field:
    the whole hazard here is attaching someone else's storefront to your audit.
    A domain another merchant has PROVEN is refused outright.

    The declaration does not count toward the official-domain set. It is stored
    so the portal can offer verification and so a claim can be started against
    it — POST /claim with this domain, publish the TXT record, POST
    /claim/verify — which is what promotes it to `verified`.
    """
    result = await svc.declare_official_domain(merchant_id, body.domain)
    status = result.get("status")
    if status == svc.DECLARE_INVALID_HOST:
        raise HTTPException(
            status_code=422,
            detail="domain must be a valid public hostname",
        )
    if status == svc.DECLARE_NOT_REGISTRABLE:
        raise HTTPException(
            status_code=422,
            detail=(
                "that is a public suffix or shared platform host, not a "
                "domain a single merchant owns"
            ),
        )
    if status == svc.DECLARE_TOO_MANY:
        raise HTTPException(
            status_code=429,
            detail=(
                "too many unverified declarations; verify the existing ones "
                "before adding more"
            ),
        )
    if status == svc.DECLARE_UNAVAILABLE:
        # 503: the owned-set read failed, so whether the merchant already has
        # this host could not be answered. Refusing is the only safe answer —
        # granting here is how a verified row could be downgraded — and it is
        # OUR outage, so neither 4xx nor "taken".
        raise HTTPException(
            status_code=503,
            detail="could not read your official-domain set; retry shortly",
        )
    if status == svc.DECLARE_WRITE_FAILED:
        # 500, and explicitly NOT 422: the hostname was fine, the write was
        # not. Answering 422 here is how a missing migration presented as
        # "domain must be a valid public hostname".
        raise HTTPException(
            status_code=500, detail="could not record the declaration",
        )
    if status == svc.DECLARE_TAKEN:
        # 409, not 403: the caller is authenticated and allowed here; the
        # DOMAIN is the thing in conflict. Deliberately does not say which
        # merchant proved it — that would leak the mapping to anyone who can
        # guess a hostname.
        raise HTTPException(
            status_code=409,
            detail="that domain is already proven by another merchant",
        )
    return result


@router.post("/claim")
async def start_claim(
    body: StartClaimBody,
    merchant_id: str = Depends(get_current_merchant),
):
    """Begin a brand claim for the AUTHENTICATED merchant. Returns the challenge
    token + human instructions. DNS-TXT is the default verification method."""
    method = (body.method or "dns").strip().lower()
    if method not in _VALID_METHODS:
        raise HTTPException(status_code=422, detail=f"unsupported claim method: {method}")
    # B4: dns + email verification require a well-formed public hostname (rejects
    # empty, internal names, IPs, malformed input before we resolve/send on it).
    if method in ("dns", "email") and not svc.is_valid_public_hostname(body.brand_domain):
        raise HTTPException(
            status_code=422,
            detail=f"brand_domain must be a valid public hostname for {method} verification",
        )
    if method == "email":
        if not svc.brand_claim_email_enabled():
            raise HTTPException(status_code=422, detail="email verification is not enabled")
        if not svc.email_target_valid(body.verification_email, body.brand_domain):
            raise HTTPException(
                status_code=422,
                detail="verification_email must be a mailbox at brand_domain",
            )
    result = await svc.start_brand_claim(
        merchant_id=merchant_id,
        brand_domain=(body.brand_domain or "").strip() or None,
        method=method,
        content_key=body.content_key,
        verification_email=(body.verification_email or "").strip() or None,
    )
    if not result.get("claim_id"):
        raise HTTPException(status_code=500, detail="failed to record brand claim")
    return result


@router.post("/claim/verify")
async def verify_claim(
    body: VerifyClaimBody,
    merchant_id: str = Depends(get_current_merchant),
):
    """Verify a pending claim. Cross-tenant guard: the claim must belong to the
    authenticated merchant (404 otherwise — don't leak existence across tenants).
    On success the merchant is marked brand_direct."""
    claim = await bc.get_brand_claim(body.claim_id)
    if not claim or claim.get("merchant_id") != merchant_id:
        raise HTTPException(status_code=404, detail="brand claim not found")
    return await svc.verify_brand_claim(body.claim_id, submitted_code=body.submitted_code)


@router.post("/attest")
async def attest(
    body: AttestBody,
    merchant_id: str = Depends(get_current_merchant),
):
    """A CLAIMED brand submits its own product content. It lands in the served
    overlay (product_enrichment) so AGENTS read the brand's copy, and advances
    claim_state -> attested. The SKU must be the caller's AND already claimed
    (the syndicate-after-claim gate)."""
    result = await attest_svc.attest_product(
        merchant_id=merchant_id,
        product_key=body.product_key,
        fields=body.fields,
        lab_evidence_ref=body.lab_evidence_ref,
    )
    status = result.get("status")
    if status == "not_found":
        raise HTTPException(status_code=404, detail="product not found")
    if status == "not_claimed":
        raise HTTPException(
            status_code=409, detail="product must be claimed before attestation"
        )
    if status in ("no_attestable_fields", "bad_request"):
        raise HTTPException(status_code=422, detail="no attestable fields provided")
    if status == "error":
        raise HTTPException(status_code=500, detail="failed to write attestation")
    return result


class ApproveClaimBody(BaseModel):
    evidence_ref: Optional[str] = Field(
        None,
        description=(
            "Reference to the ownership evidence reviewed (support ticket, brand "
            "registry, trademark, etc.). Recorded in the claim's proof trail."
        ),
    )


@router.post("/claim/{claim_id}/approve")
async def approve_claim(
    claim_id: str,
    body: ApproveClaimBody,
    employee: Dict[str, Any] = Depends(get_current_employee),
):
    """Support-assisted (MANUAL) verification — Pivota-employee only. A staffer who
    has reviewed a brand's ownership evidence offline approves a method='manual'
    claim; it flips to verified and the merchant is marked brand_direct. The
    approving employee is recorded in the proof trail."""
    approved_by = str(
        employee.get("employee_id")
        or employee.get("user_id")
        or employee.get("sub")
        or ""
    )
    if not approved_by:
        raise HTTPException(status_code=403, detail="employee identity required")
    result = await svc.approve_manual_claim(
        claim_id, approved_by=approved_by, evidence_ref=body.evidence_ref
    )
    status = result.get("status")
    if status == "not_found":
        raise HTTPException(status_code=404, detail="brand claim not found")
    if status == "not_manual":
        raise HTTPException(
            status_code=409, detail="only manual claims can be employee-approved"
        )
    if status == "forbidden":
        raise HTTPException(status_code=403, detail="employee identity required")
    return result
