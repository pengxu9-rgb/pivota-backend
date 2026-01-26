from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from routes.accounts_orders_api import AccountsPrincipal, get_accounts_principal_ugc
from services.ugc_capabilities_service import create_question, list_questions


router = APIRouter(tags=["Questions"])


class CreateQuestionRequest(BaseModel):
    product_id: str = Field(..., min_length=1, alias="productId")
    product_group_id: str | None = Field(None, alias="productGroupId")
    question: str = Field(..., min_length=1, max_length=2000)


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


@router.post("/questions")
async def post_question(
    request: Request,
    response: Response,
    body: CreateQuestionRequest,
    principal: AccountsPrincipal = Depends(get_accounts_principal_ugc),
):
    """
    Create a product Q&A question (UGC).

    Rules:
    - must be logged in
    - basic anti-abuse: per user+product rate limit (60s)
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
        elif code == "NOT_AUTHENTICATED":
            message = "Please log in to ask a question."

        raise HTTPException(
            status_code=int(getattr(e, "status_code", 500) or 500),
            detail={"error": {"code": code, "message": message}},
            headers=no_store_headers,
        )
    return {"status": "success", "question_id": int(qid), "subject_type": subject_type, "subject_id": subject_id}
