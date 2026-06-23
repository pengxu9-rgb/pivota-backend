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
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from db import brand_claims as bc
from services import brand_attest_service as attest_svc
from services import brand_claim_service as svc
from utils.auth import get_current_merchant

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/brands", tags=["brand-claims"])

_VALID_METHODS = {"dns", "email", "amazon", "shopify", "manual"}


class StartClaimBody(BaseModel):
    brand_domain: Optional[str] = Field(
        None, description="Domain used for DNS/email verification (required for dns)"
    )
    method: str = Field("dns", description="dns | email | amazon | shopify | manual")
    content_key: Optional[str] = Field(
        None, description="Optional: scope the claim to one canonical entity"
    )


class VerifyClaimBody(BaseModel):
    claim_id: str = Field(..., min_length=1)


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
    # B4: dns verification requires a well-formed public hostname (rejects empty,
    # internal names, IPs, malformed input before we ever resolve TXT on it).
    if method == "dns" and not svc.is_valid_public_hostname(body.brand_domain):
        raise HTTPException(
            status_code=422,
            detail="brand_domain must be a valid public hostname for dns verification",
        )
    result = await svc.start_brand_claim(
        merchant_id=merchant_id,
        brand_domain=(body.brand_domain or "").strip() or None,
        method=method,
        content_key=body.content_key,
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
    return await svc.verify_brand_claim(body.claim_id)


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
