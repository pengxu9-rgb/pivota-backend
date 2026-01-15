from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from observability.reviews_metrics import record_buyer_create, record_buyer_exchange, record_buyer_media_upload
from services.buyer_reviews_service import (
    attach_buyer_review_media,
    create_buyer_review,
    exchange_proof_for_submission_token,
    get_buyer_review_status,
    issue_submission_token,
)


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


class ExchangeProofRequest(BaseModel):
    ttl_seconds: int = Field(900, ge=60, le=3600)


@router.post("/verification/exchange")
async def buyer_exchange_proof(
    request: Request,
    body: ExchangeProofRequest,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    start = time.perf_counter()
    try:
        proof_token = _bearer_token(authorization)
        result = await exchange_proof_for_submission_token(
            request=request,
            proof_token=proof_token,
            ttl_seconds=int(body.ttl_seconds),
        )
        record_buyer_exchange(result="success", reason="ok", duration_seconds=(time.perf_counter() - start))
        return result
    except HTTPException as e:
        reason = str(e.detail or "error")
        record_buyer_exchange(result="error", reason=reason[:64], duration_seconds=(time.perf_counter() - start))
        raise
    except Exception:
        record_buyer_exchange(result="error", reason="exception", duration_seconds=(time.perf_counter() - start))
        raise


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
    start = time.perf_counter()
    try:
        token = _bearer_token(authorization)
        result = await create_buyer_review(
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
        record_buyer_create(result="success", reason="ok", duration_seconds=(time.perf_counter() - start))
        return result
    except HTTPException as e:
        reason = str(e.detail or "error")
        record_buyer_create(result="error", reason=reason[:64], duration_seconds=(time.perf_counter() - start))
        raise
    except Exception:
        record_buyer_create(result="error", reason="exception", duration_seconds=(time.perf_counter() - start))
        raise


@router.get("/reviews/{review_id}")
async def buyer_get_review(
    review_id: int,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    token = _bearer_token(authorization)
    return await get_buyer_review_status(token=token, review_id=int(review_id))


@router.post("/reviews/{review_id}/media")
async def buyer_attach_review_media(
    review_id: int,
    request: Request,
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    start = time.perf_counter()
    try:
        token = _bearer_token(authorization)
        blob = await file.read()
        result = await attach_buyer_review_media(
            request=request,
            token=token,
            review_id=int(review_id),
            filename=(file.filename or ""),
            content_type=(file.content_type or ""),
            blob=blob,
        )
        record_buyer_media_upload(result="success", reason="ok", duration_seconds=(time.perf_counter() - start))
        return result
    except HTTPException as e:
        reason = str(e.detail or "error")
        record_buyer_media_upload(result="error", reason=reason[:64], duration_seconds=(time.perf_counter() - start))
        raise
    except Exception:
        record_buyer_media_upload(result="error", reason="exception", duration_seconds=(time.perf_counter() - start))
        raise
