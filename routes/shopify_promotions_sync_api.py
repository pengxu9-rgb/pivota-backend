"""
Admin API to sync Shopify price rules into the Pivota promotions table.

This is an internal helper for the agent/creator stack so that marketing
logic can reuse Shopify's discount system while keeping Pivota as the
source of truth for promotions.
"""

import os
from typing import Any, Dict

from fastapi import APIRouter, Depends, Header, HTTPException, Path, status

from services.shopify_promotions_sync import (
    ShopifyPromotionsConfigError,
    ShopifyPromotionsAuthError,
    ShopifyPromotionsRateLimitError,
    ShopifyPromotionsError,
    sync_shopify_promotions_for_merchant,
)

router = APIRouter(
    prefix="/agent/internal/shopify/promotions",
    tags=["shopify_promotions"],
)


async def require_shopify_promotions_admin(
    x_admin_key: str = Header(..., alias="X-ADMIN-KEY"),
) -> None:
    expected = os.getenv("PROMOTIONS_ADMIN_KEY") or os.getenv("ADMIN_API_KEY")
    if not expected or x_admin_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="UNAUTHORIZED",
        )


@router.post(
    "/sync/{merchant_id}",
    response_model=Dict[str, Any],
    status_code=status.HTTP_200_OK,
)
async def sync_shopify_promotions_endpoint(
    merchant_id: str = Path(..., description="Internal merchant ID"),
    _: None = Depends(require_shopify_promotions_admin),
) -> Dict[str, Any]:
    """
    Sync Shopify price rules for a merchant into the promotions table.

    This endpoint is admin-only and is intended to be called from internal
    tools or one-off scripts (not from public frontends).
    """
    try:
        summary = await sync_shopify_promotions_for_merchant(merchant_id=merchant_id)
        return {"ok": True, "summary": summary}
    except ShopifyPromotionsConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "SHOPIFY_CONFIG_ERROR", "message": str(exc)},
        )
    except ShopifyPromotionsAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "SHOPIFY_AUTH_ERROR", "message": str(exc)},
        )
    except ShopifyPromotionsRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "SHOPIFY_RATE_LIMIT", "message": str(exc)},
        )
    except ShopifyPromotionsError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "SHOPIFY_PROMOTIONS_ERROR", "message": str(exc)},
        )
    except Exception as exc:  # pragma: no cover - defensive
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "INTERNAL_ERROR", "message": str(exc)},
        )


@router.get(
    "/sync/{merchant_id}",
    response_model=Dict[str, Any],
    status_code=status.HTTP_200_OK,
)
async def sync_shopify_promotions_get(
    merchant_id: str = Path(..., description="Internal merchant ID"),
    _: None = Depends(require_shopify_promotions_admin),
) -> Dict[str, Any]:
    """
    GET alias for sync endpoint to make it easy to trigger from a browser.
    """
    return await sync_shopify_promotions_endpoint(merchant_id=merchant_id)  # type: ignore[arg-type]
