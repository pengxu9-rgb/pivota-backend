from fastapi import APIRouter, HTTPException
from typing import Any, Dict, Optional

from pydantic import BaseModel

from services.product_quality_service import preview_quality, full_quality_eval


router = APIRouter(
    prefix="/internal/product/quality",
    tags=["product_quality"],
)


@router.post("/preview")
async def product_quality_preview(payload: Dict[str, Any]):
    """
    Lightweight product quality preview for the Merchant Portal.

    This endpoint is designed to be called frequently while the user
    edits a product. It accepts a partial product payload (L0/L2/L3)
    and returns:
    - content_quality_score (0–100)
    - model_readiness_score (0–100)
    - conversion_potential_score (stubbed, may be null)
    - problems: list of field-level suggestions
    - components: internal component scores for debugging / UI display
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be JSON object")

    try:
        result = preview_quality(payload)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to compute preview: {e}")

    return {
        "status": "success",
        "data": result,
    }


class QualityEvalRequest(BaseModel):
    merchant_id: str
    platform: str
    platform_product_id: str
    geo_code: Optional[str] = None
    payload: Dict[str, Any]


@router.post("/eval")
async def product_quality_eval(body: QualityEvalRequest):
    """
    Full product quality eval + snapshot persistence.

    V1 implementation:
    - Reuses the same rule-based scoring as preview
    - Persists a snapshot row in product_quality_snapshot
    - Returns the same structure as /preview under `data`

    This endpoint is intended for:
    - On-submit checks when merchant clicks "提交上架"
    - Batch jobs that want to store quality history
    """
    try:
        result = await full_quality_eval(
            merchant_id=body.merchant_id,
            platform=body.platform,
            platform_product_id=body.platform_product_id,
            geo_code=body.geo_code,
            payload=body.payload,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to run full eval: {e}")

    return {
        "status": "success",
        "data": result,
    }
