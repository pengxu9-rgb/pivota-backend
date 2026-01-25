from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from routes.accounts_orders_api import AccountsPrincipal, get_accounts_principal_ugc
from services.ugc_capabilities_service import create_question


router = APIRouter(tags=["Questions"])


class CreateQuestionRequest(BaseModel):
    product_id: str = Field(..., min_length=1, alias="productId")
    product_group_id: str | None = Field(None, alias="productGroupId")
    question: str = Field(..., min_length=1, max_length=2000)


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

    pid = str(body.product_id or "").strip()
    pgid = str(body.product_group_id or "").strip() or None
    subject_type = "product_group" if pgid else "product"
    subject_id = pgid or pid

    qid = await create_question(
        user_id=principal.user_id,
        subject_type=subject_type,
        subject_id=subject_id,
        question=str(body.question or "").strip(),
        window_seconds=60,
    )
    return {"status": "success", "question_id": int(qid), "subject_type": subject_type, "subject_id": subject_id}
