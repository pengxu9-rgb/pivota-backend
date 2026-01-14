from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from services.buyer_reviews_service import create_buyer_review, get_buyer_review_status, issue_submission_token


router = APIRouter(prefix="/buyer/reviews/v1", tags=["Buyer Reviews"])


def _bearer_token(authorization: Optional[str]) -> str:
    h = (authorization or "").strip()
    if not h:
        raise HTTPException(status_code=401, detail="UNAUTHORIZED")
    if not h.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="UNAUTHORIZED")
    return h[7:].strip()


class SubjectRef(BaseModel):
    merchant_id: str = Field(..., min_length=1)
    platform: str = Field(..., min_length=1)
    platform_product_id: str = Field(..., min_length=1)
    variant_id: Optional[str] = None


class IssueTokenRequest(BaseModel):
    merchant_id: str = Field(..., min_length=1)
    subjects: List[SubjectRef] = Field(..., min_length=1)
    verification: str = Field("unverified", min_length=1)
    ttl_seconds: int = Field(900, ge=60, le=3600)


@router.post("/verification/issue-token")
async def buyer_issue_token(
    request: Request,
    body: IssueTokenRequest,
) -> Dict[str, Any]:
    return issue_submission_token(
        request=request,
        merchant_id=body.merchant_id,
        subjects=[s.model_dump() for s in body.subjects],
        verification=body.verification,
        ttl_seconds=int(body.ttl_seconds),
    )


class CreateReviewRequest(BaseModel):
    merchant_id: str = Field(..., min_length=1)
    platform: str = Field(..., min_length=1)
    platform_product_id: str = Field(..., min_length=1)
    variant_id: Optional[str] = None
    rating: int = Field(..., ge=1, le=5)
    title: Optional[str] = Field(None, max_length=200)
    body: Optional[str] = Field(None, max_length=5000)


@router.post("/reviews")
async def buyer_create_review(
    request: Request,
    body: CreateReviewRequest,
    authorization: Optional[str] = Header(None),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
) -> Dict[str, Any]:
    token = _bearer_token(authorization)
    return await create_buyer_review(
        request=request,
        token=token,
        idempotency_key=(idempotency_key or "").strip(),
        merchant_id=body.merchant_id,
        platform=body.platform,
        platform_product_id=body.platform_product_id,
        variant_id=body.variant_id,
        rating=int(body.rating),
        title=body.title,
        body=body.body,
    )


@router.get("/reviews/{review_id}")
async def buyer_get_review(
    review_id: int,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    token = _bearer_token(authorization)
    return await get_buyer_review_status(token=token, review_id=int(review_id))

