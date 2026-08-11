"""
Internal promotions API for Pivota Agent / Merchant Portal.
Backed by the PostgreSQL promotions table.
"""

import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from services.promotions_service import (
    PromotionCreate,
    PromotionOut,
    PromotionUpdate,
    PromotionStatus,
    create_promotion,
    get_promotion,
    list_promotions,
    soft_delete_promotion,
    update_promotion,
)

router = APIRouter(
    prefix="/agent/internal/promotions",
    tags=["promotions"],
)


async def require_promotions_admin(
    x_admin_key: str = Header(..., alias="X-ADMIN-KEY"),
) -> None:
    expected = os.getenv("PROMOTIONS_ADMIN_KEY") or os.getenv("ADMIN_API_KEY")
    if not expected or x_admin_key != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="UNAUTHORIZED")


@router.get("", response_model=Dict[str, Any])
async def list_promotions_endpoint(
    merchantId: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    channel: Optional[str] = Query(None),
    creatorId: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: None = Depends(require_promotions_admin),
) -> Dict[str, Any]:
    promos, total = await list_promotions(
        merchant_id=merchantId,
        status=status,
        channel=channel,
        creator_id=creatorId,
        search=search,
        limit=limit,
        offset=offset,
    )
    # Pydantic models are JSON serializable; we convert to dicts for consistency.
    return {
        "promotions": [p.dict() for p in promos],
        "total": total,
    }


@router.get("/{promotion_id}", response_model=Dict[str, Any])
async def get_promotion_endpoint(
    promotion_id: str,
    _: None = Depends(require_promotions_admin),
) -> Dict[str, Any]:
    promo = await get_promotion(promotion_id)
    if not promo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")
    return {"promotion": promo.dict()}


# Promo types the infra quote engine actually APPLIES. quote_service's
# _apply_infra_promotions_best_effort implements ONLY MULTI_BUY_DISCOUNT;
# a manually created FLASH_SALE or FREE_SHIPPING would validate, sync, and
# DISPLAY — and then silently never change a price at quote time (the
# 2026-08 audit's "promo trapdoor"). Shopify-synced promos of those types
# are different: their discount applies inside Shopify's own pricing engine,
# and the sync path calls services.promotions_service.create_promotion
# directly, not this route.
QUOTE_APPLIED_MANUAL_PROMO_TYPES = {"MULTI_BUY_DISCOUNT"}


@router.post("", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_promotion_endpoint(
    payload: PromotionCreate,
    _: None = Depends(require_promotions_admin),
) -> Dict[str, Any]:
    if payload.type not in QUOTE_APPLIED_MANUAL_PROMO_TYPES:
        # 400, not 422: the repo-wide error middleware rewrites every 422 into a
        # generic INVALID_REQUEST, which would erase this named refusal.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "PROMO_TYPE_NOT_APPLIED_AT_QUOTE",
                "message": (
                    f"Manual {payload.type} promotions are not applied by the quote engine — "
                    "they would display to shoppers but never change a price. Create the "
                    "discount in Shopify instead (it applies via Shopify pricing and syncs "
                    "back automatically), or use MULTI_BUY_DISCOUNT."
                ),
            },
        )
    try:
        promo = await create_promotion(payload)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_PROMOTION", "message": str(e)},
        )
    except Exception as e:
        # Surface internal error details for admin/debug callers so we can
        # see the real DB/validation issue instead of a generic 500.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "INTERNAL_ERROR", "message": str(e)},
        )
    return {"promotion": promo.dict()}


@router.patch("/{promotion_id}", response_model=Dict[str, Any])
async def update_promotion_endpoint(
    promotion_id: str,
    payload: PromotionUpdate,
    _: None = Depends(require_promotions_admin),
) -> Dict[str, Any]:
    try:
        promo = await update_promotion(promotion_id, payload)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_PROMOTION", "message": str(e)},
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "INTERNAL_ERROR", "message": str(e)},
        )
    if not promo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")
    return {"promotion": promo.dict()}


@router.delete("/{promotion_id}", response_model=Dict[str, Any])
async def delete_promotion_endpoint(
    promotion_id: str,
    _: None = Depends(require_promotions_admin),
) -> Dict[str, Any]:
    ok = await soft_delete_promotion(promotion_id)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="NOT_FOUND")
    return {"ok": True}
