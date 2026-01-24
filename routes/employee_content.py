from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from utils.auth import get_current_employee


router = APIRouter(prefix="/employee/content", tags=["employee-content"])


class GenerateContentRequest(BaseModel):
    subject_type: str = Field(..., description="e.g. product_key")
    subject_id: str = Field(..., description="e.g. merch_x|shopify|123")
    content_type: str = Field(default="description", description="description|summary|highlights")
    tone: Optional[str] = Field(default=None, description="e.g. professional, friendly")
    length: Optional[str] = Field(default=None, description="short|medium|long")
    locale: Optional[str] = Field(default=None, description="e.g. en-US, zh-CN")
    current_text: Optional[str] = Field(default=None, description="Current content (markdown preferred)")
    context: Optional[Dict[str, Any]] = Field(default=None, description="Optional extra context for generation")


@router.post("/generate")
async def generate_content(
    body: GenerateContentRequest,
    _: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    """
    MVP stub endpoint for future LLM integration.

    Returns mock candidates so the Employee Portal can wire the UX end-to-end
    behind a feature flag without requiring a queue or external model calls.
    """
    base = (body.current_text or "").strip()
    if not base:
        base = f"Draft {body.content_type} for {body.subject_type}:{body.subject_id}"

    tone = (body.tone or "neutral").strip()
    length = (body.length or "medium").strip()
    locale = (body.locale or "en-US").strip()

    def _cand(suffix: str, text: str) -> Dict[str, Any]:
        return {
            "id": f"cand_{suffix}_{uuid4().hex[:8]}",
            "content_markdown": text,
            "meta": {"tone": tone, "length": length, "locale": locale, "mock": True},
        }

    candidates: List[Dict[str, Any]] = [
        _cand("v1", f"{base}\n\n---\n\n(Improved: tone={tone}, length={length}, locale={locale})"),
        _cand("v2", f"## Key benefits\n\n- {base[:80] or '...' }\n\n(Stub candidate; replace with LLM output)"),
    ]

    return {
        "status": "degraded",
        "note": "LLM integration not enabled; returning mock candidates",
        "candidates": candidates,
    }

