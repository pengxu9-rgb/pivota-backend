from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from datetime import datetime, timezone
from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, Field

from observability.reviews_metrics import record_buyer_create, record_buyer_exchange, record_buyer_media_upload
from db.database import database
from db.reviews_center import product_reviews, buyer_review_user_subject
from routes.accounts_orders_api import AccountsPrincipal, get_accounts_principal_ugc
from services.buyer_reviews_service import (
    buyer_submit_enabled,
    buyer_submit_merchant_allowed,
    attach_buyer_review_media,
    attach_buyer_review_media_from_user,
    create_buyer_review,
    exchange_proof_for_submission_token,
    get_buyer_review_status,
    issue_submission_token,
)
from services.reviews_service import VARIANT_ID_SENTINEL, build_product_key, build_sku_key
from services.ugc_capabilities_service import UgcSubject, bind_user_review_subject, get_review_slot_summary


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
    order_id: Optional[str] = None


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
            order_id=(body.order_id or "").strip() or None,
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

class CreateReviewFromUserRequest(BaseModel):
    # PDP v2 subject identifiers (for purchase eligibility + per-user dedupe)
    product_id: str = Field(..., min_length=1)
    product_group_id: Optional[str] = None

    # Canonical subject for storing the review in the reviews center
    subject: SubjectRef

    rating: Optional[int] = Field(None, ge=1, le=5)
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


@router.post("/reviews/from_user")
async def buyer_create_review_from_user(
    request: Request,
    response: Response,
    body: CreateReviewFromUserRequest,
    principal: AccountsPrincipal = Depends(get_accounts_principal_ugc),
) -> Dict[str, Any]:
    """
    In-app review creation for logged-in users (no invitation token required).
    """
    start = time.perf_counter()
    response.headers["Cache-Control"] = "private, no-store"

    try:
        if not buyer_submit_enabled():
            raise HTTPException(status_code=404, detail="BUYER_SUBMIT_DISABLED")

        subject_group_id = str(body.product_group_id or "").strip() or None
        product_id = str(body.product_id or "").strip()
        subject_type = "product_group" if subject_group_id else "product"
        subject_id = subject_group_id or product_id

        subject = UgcSubject(
            subject_type=subject_type,
            subject_id=subject_id,
            product_id=product_id,
            product_group_id=subject_group_id,
        )

        slot_summary = await get_review_slot_summary(
            email_normalized=principal.email_normalized,
            user_id=principal.user_id,
            subject=subject,
        )
        paid_order_ids = list(slot_summary.get("paid_order_ids") or [])
        is_purchaser = bool(paid_order_ids)
        available_order_ids = list(slot_summary.get("available_order_ids") or [])
        existing_bindings = list(slot_summary.get("bindings") or [])

        requested_rating = int(body.rating) if body.rating is not None else None
        title_text = (body.title or "").strip() or None
        body_text = (body.body or "").strip() or None

        if requested_rating is None and not title_text and not body_text:
            raise HTTPException(status_code=400, detail="EMPTY_REVIEW")

        if requested_rating is not None and not is_purchaser:
            raise HTTPException(status_code=403, detail="NOT_VERIFIED_FOR_RATING")
        if not is_purchaser:
            raise HTTPException(status_code=403, detail="NOT_PURCHASER")

        subject_ref = body.subject
        merchant_id = str(subject_ref.merchant_id or "").strip()
        if not merchant_id:
            raise HTTPException(status_code=400, detail="MISSING_SUBJECT")
        if not buyer_submit_merchant_allowed(merchant_id):
            raise HTTPException(status_code=403, detail="NOT_ALLOWED")

        platform = str(subject_ref.platform or "").strip().lower()
        platform_product_id = str(subject_ref.platform_product_id or "").strip()
        variant_id = (str(subject_ref.variant_id).strip() if subject_ref.variant_id is not None else "").strip() or None

        if not platform or not platform_product_id:
            raise HTTPException(status_code=400, detail="MISSING_SUBJECT")

        product_key = build_product_key(
            merchant_id=merchant_id,
            platform=platform,
            platform_product_id=platform_product_id,
        )
        sku_key = build_sku_key(
            merchant_id=merchant_id,
            platform=platform,
            platform_product_id=platform_product_id,
            variant_id=variant_id,
        )

        now_dt = datetime.now(timezone.utc)

        # Backward compatibility: if a historical binding exists without order_id,
        # preserve upgrade/add-rating behavior before allocating a new order slot.
        for existing_binding in existing_bindings:
            try:
                existing_review_id = int((existing_binding or {}).get("review_id") or 0)
            except Exception:
                existing_review_id = 0
            if existing_review_id <= 0:
                continue

            existing_review = await database.fetch_one(
                product_reviews.select().where(product_reviews.c.id == existing_review_id)
            )
            existing_verification = str(existing_review["verification"] if existing_review else "").strip().lower()
            existing_has_rating = False
            if existing_review:
                try:
                    existing_has_rating = existing_review["rating"] is not None  # type: ignore[index]
                except Exception:
                    existing_has_rating = getattr(existing_review, "rating", None) is not None

            if existing_verification in {"unverified", ""}:
                update_values: Dict[str, Any] = {
                    "verification": "verified_purchase",
                    "updated_at": now_dt,
                    "status": "under_review",
                }
                if requested_rating is not None:
                    update_values["rating"] = requested_rating
                if title_text is not None:
                    update_values["title"] = title_text
                if body_text is not None:
                    update_values["body"] = body_text

                await database.execute(
                    product_reviews.update()
                    .where(product_reviews.c.id == existing_review_id)
                    .values(**update_values)
                )
                record_buyer_create(result="success", reason="upgraded", duration_seconds=(time.perf_counter() - start))
                return {
                    "status": "success",
                    "review_id": int(existing_review_id),
                    "moderation_state": "under_review",
                    "upgraded": True,
                }

            if (
                existing_verification in {"verified_purchase", "verified_buyer"}
                and (not existing_has_rating)
                and requested_rating is not None
            ):
                update_values = {
                    "updated_at": now_dt,
                    "rating": requested_rating,
                    "status": "under_review",
                }
                if title_text is not None:
                    update_values["title"] = title_text
                if body_text is not None:
                    update_values["body"] = body_text

                await database.execute(
                    product_reviews.update()
                    .where(product_reviews.c.id == existing_review_id)
                    .values(**update_values)
                )
                record_buyer_create(result="success", reason="add_rating", duration_seconds=(time.perf_counter() - start))
                return {
                    "status": "success",
                    "review_id": int(existing_review_id),
                    "moderation_state": "under_review",
                    "updated": True,
                }

        if not available_order_ids:
            raise HTTPException(status_code=409, detail="ALREADY_REVIEWED")
        target_order_id = str(available_order_ids[0]).strip()

        review_id = await database.execute(
            product_reviews.insert().values(
                product_key=product_key,
                sku_key=sku_key,
                merchant_id=merchant_id,
                platform=platform,
                platform_product_id=platform_product_id,
                variant_id=(variant_id if variant_id and variant_id != VARIANT_ID_SENTINEL else None),
                group_id=None,
                author_user_id=None,
                source_type="native",
                source_system="accounts",
                external_review_id=None,
                dedupe_key=f"accounts|{principal.user_id}|{subject_type}|{subject_id}|{target_order_id}",
                verification="verified_purchase" if is_purchaser else "unverified",
                rating=requested_rating if is_purchaser else None,
                title=title_text,
                body=body_text,
                media_count=0,
                risk_flags={
                    "source": "accounts",
                    "accounts_user_id": principal.user_id,
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "order_id": target_order_id,
                },
                status="under_review",
                created_at=now_dt,
                updated_at=now_dt,
            )
        )

        try:
            await bind_user_review_subject(
                user_id=principal.user_id,
                subject_type=subject_type,
                subject_id=subject_id,
                review_id=int(review_id),
                order_id=target_order_id,
            )
        except HTTPException as e:
            if e.status_code == 409:
                # Idempotent replay: return existing review_id if we can find it.
                existing = await database.fetch_one(
                    buyer_review_user_subject.select().where(
                        (buyer_review_user_subject.c.user_id == principal.user_id)
                        & (buyer_review_user_subject.c.subject_type == subject_type)
                        & (buyer_review_user_subject.c.subject_id == subject_id)
                        & (buyer_review_user_subject.c.order_id == target_order_id)
                    )
                )
                if existing:
                    try:
                        existing_review_id = existing["review_id"]  # type: ignore[index]
                    except Exception:
                        existing_review_id = None
                    if existing_review_id:
                        return {
                            "status": "success",
                            "review_id": int(existing_review_id),
                            "moderation_state": "under_review",
                            "idempotent_replay": True,
                        }
            raise

        record_buyer_create(result="success", reason="ok", duration_seconds=(time.perf_counter() - start))
        return {"status": "success", "review_id": int(review_id), "moderation_state": "under_review"}
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


@router.post("/reviews/{review_id}/media/from_user")
async def buyer_attach_review_media_from_user(
    review_id: int,
    request: Request,
    file: UploadFile = File(...),
    principal: AccountsPrincipal = Depends(get_accounts_principal_ugc),
) -> Dict[str, Any]:
    start = time.perf_counter()
    try:
        blob = await file.read()
        result = await attach_buyer_review_media_from_user(
            request=request,
            user_id=principal.user_id,
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
