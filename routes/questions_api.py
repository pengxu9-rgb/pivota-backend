from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from routes.accounts_orders_api import AccountsPrincipal, get_accounts_or_guest_principal_ugc
from services.ugc_capabilities_service import (
    create_question,
    create_question_reply,
    get_question,
    list_question_replies,
    list_questions,
)


router = APIRouter(tags=["Questions"])


class CreateQuestionRequest(BaseModel):
    product_id: str = Field(..., min_length=1, alias="productId")
    product_group_id: Optional[str] = Field(None, alias="productGroupId")
    question: str = Field(..., min_length=1, max_length=2000)


class CreateQuestionReplyRequest(BaseModel):
    body: str = Field(..., min_length=1, max_length=4000)


@router.get("/questions")
async def get_questions(
    response: Response,
    product_id: str = Query(..., min_length=1, alias="productId"),
    product_group_id: Optional[str] = Query(None, alias="productGroupId"),
    limit: int = Query(10, ge=1, le=50),
):
    """
    List recent product Q&A questions.

    This is public (no auth required). It does not expose user identities.
    """
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"

    pid = str(product_id or "").strip()
    pgid = str(product_group_id or "").strip() or None
    subject_type = "product_group" if pgid else "product"
    subject_id = pgid or pid

    result = await list_questions(subject_type=subject_type, subject_id=subject_id, limit=int(limit))
    return {"status": "success", "subject_type": subject_type, "subject_id": subject_id, **result}


@router.get("/questions/{question_id}")
async def get_question_detail(
    response: Response,
    question_id: int,
):
    """
    Get a single question (public, no auth required).
    """
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    no_store_headers = {
        "Cache-Control": "private, no-store",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
    }

    try:
        q = await get_question(question_id=int(question_id))
    except HTTPException as e:
        code = (
            str(e.detail or "").strip()
            if isinstance(e.detail, str)
            else str((e.detail or {}).get("error", {}).get("code") or "ERROR").strip()
            if isinstance(e.detail, dict)
            else "ERROR"
        )
        message = "Unable to load question."
        if code == "QUESTION_NOT_FOUND":
            message = "Question not found."
        elif code == "INVALID_QUESTION":
            message = "Invalid question."
        raise HTTPException(
            status_code=int(getattr(e, "status_code", 500) or 500),
            detail={"error": {"code": code, "message": message}},
            headers=no_store_headers,
        )

    return {"status": "success", **q}


@router.get("/questions/{question_id}/replies")
async def get_question_replies(
    response: Response,
    question_id: int,
    limit: int = Query(20, ge=1, le=50),
):
    """
    List replies for a question (public, no auth required).
    """
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    no_store_headers = {
        "Cache-Control": "private, no-store",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
    }

    try:
        result = await list_question_replies(question_id=int(question_id), limit=int(limit))
    except HTTPException as e:
        code = (
            str(e.detail or "").strip()
            if isinstance(e.detail, str)
            else str((e.detail or {}).get("error", {}).get("code") or "ERROR").strip()
            if isinstance(e.detail, dict)
            else "ERROR"
        )
        message = "Unable to load replies."
        if code == "QUESTION_NOT_FOUND":
            message = "Question not found."
        elif code == "INVALID_QUESTION":
            message = "Invalid question."
        raise HTTPException(
            status_code=int(getattr(e, "status_code", 500) or 500),
            detail={"error": {"code": code, "message": message}},
            headers=no_store_headers,
        )

    return {"status": "success", "question_id": int(question_id), **result}


@router.post("/questions")
async def post_question(
    request: Request,
    response: Response,
    body: CreateQuestionRequest,
    principal: AccountsPrincipal = Depends(get_accounts_or_guest_principal_ugc),
):
    """
    Create a product Q&A question (UGC).

    Rules:
    - open to logged-in and guest users
    - basic anti-abuse: per actor+product rate limit (60s)
    """
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    no_store_headers = {
        "Cache-Control": "private, no-store",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
    }

    pid = str(body.product_id or "").strip()
    pgid = str(body.product_group_id or "").strip() or None
    subject_type = "product_group" if pgid else "product"
    subject_id = pgid or pid

    try:
        qid = await create_question(
            user_id=principal.user_id,
            subject_type=subject_type,
            subject_id=subject_id,
            question=str(body.question or "").strip(),
            window_seconds=60,
        )
    except HTTPException as e:
        code = (
            str(e.detail or "").strip()
            if isinstance(e.detail, str)
            else str((e.detail or {}).get("error", {}).get("code") or "ERROR").strip()
            if isinstance(e.detail, dict)
            else "ERROR"
        )
        message = "Unable to submit question."
        if code == "RATE_LIMITED":
            message = "Too many questions. Please try again in a minute."
        elif code == "QUESTION_TOO_SHORT":
            message = "Question is too short."
        elif code == "QUESTION_TOO_LONG":
            message = "Question is too long."
        elif code == "INVALID_SUBJECT":
            message = "Invalid product."
        raise HTTPException(
            status_code=int(getattr(e, "status_code", 500) or 500),
            detail={"error": {"code": code, "message": message}},
            headers=no_store_headers,
        )
    return {
        "status": "success",
        "question_id": int(qid),
        "subject_type": subject_type,
        "subject_id": subject_id,
        "moderation_status": "under_review",
    }


@router.post("/questions/{question_id}/replies")
async def post_question_reply(
    response: Response,
    question_id: int,
    body: CreateQuestionReplyRequest,
    principal: AccountsPrincipal = Depends(get_accounts_or_guest_principal_ugc),
):
    """
    Reply to a question (UGC).

    Rules:
    - open to logged-in and guest users
    - accepted replies are held for moderation before public display
    - basic anti-abuse: per actor+question rate limit (30s)
    """
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    no_store_headers = {
        "Cache-Control": "private, no-store",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
    }

    try:
        rid = await create_question_reply(
            user_id=principal.user_id,
            question_id=int(question_id),
            body=str(body.body or "").strip(),
            window_seconds=30,
        )
    except HTTPException as e:
        code = (
            str(e.detail or "").strip()
            if isinstance(e.detail, str)
            else str((e.detail or {}).get("error", {}).get("code") or "ERROR").strip()
            if isinstance(e.detail, dict)
            else "ERROR"
        )
        message = "Unable to submit reply."
        if code == "RATE_LIMITED":
            message = "Too many replies. Please try again in a minute."
        elif code == "REPLY_TOO_SHORT":
            message = "Reply is too short."
        elif code == "REPLY_TOO_LONG":
            message = "Reply is too long."
        elif code == "QUESTION_NOT_FOUND":
            message = "Question not found."
        elif code == "INVALID_QUESTION":
            message = "Invalid question."
        raise HTTPException(
            status_code=int(getattr(e, "status_code", 500) or 500),
            detail={"error": {"code": code, "message": message}},
            headers=no_store_headers,
        )

    return {
        "status": "success",
        "reply_id": int(rid),
        "question_id": int(question_id),
        "moderation_status": "under_review",
    }
