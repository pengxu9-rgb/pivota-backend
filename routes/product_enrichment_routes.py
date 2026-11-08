from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from services.product_enrichment_pipeline import run_enrichment_for_product
from utils.auth import assert_merchant_owner_or_staff, get_current_user


router = APIRouter(
    prefix="/internal/product/enrichment",
    tags=["product_enrichment"],
)


class EnrichmentRunRequest(BaseModel):
    merchant_id: str
    platform: str
    platform_product_id: str
    geo_code: Optional[str] = "default"


@router.post("/run")
async def run_enrichment(
    body: EnrichmentRunRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Run the enrichment pipeline for a single product.

    This triggers:
    - Loading StandardProduct from products_cache
    - Auto-generating summary/bullets/tags/disclaimer (heuristic v1)
    - Upserting into product_enrichment
    - Running full quality eval and writing product_quality_snapshot
    - Emitting a PRODUCT_ENRICHED event (best-effort)

    It writes enrichment for body.merchant_id, so the caller must be that merchant
    or staff.
    """
    assert_merchant_owner_or_staff(current_user, body.merchant_id)
    try:
        result = await run_enrichment_for_product(
            merchant_id=body.merchant_id,
            platform=body.platform,
            platform_product_id=body.platform_product_id,
            geo_code=body.geo_code or "default",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to run enrichment: {e}")

    # If the pipeline decided to skip (e.g. missing cache / blocked by compliance)
    if result is None:
        return {
            "status": "skipped",
            "reason": "no_cache_or_blocked_by_compliance",
        }

    return {
        "status": "success",
        "data": result,
    }

