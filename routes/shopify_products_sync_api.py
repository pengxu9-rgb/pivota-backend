"""
Admin API to sync Shopify products into products_cache.

This is an internal helper for the creator/agent stack so that product
search and category pages can rely on products_cache being populated.
"""

import os
from typing import Any, Dict

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, status

from services.shopify_products_sync import (
    ShopifyProductsSyncAuthError,
    ShopifyProductsSyncConfigError,
    ShopifyProductsSyncError,
    ShopifyProductsSyncRateLimitError,
    sync_shopify_products_for_merchant,
)

router = APIRouter(
    prefix="/agent/internal/shopify/products",
    tags=["shopify_products"],
)


async def require_shopify_products_admin(
    x_admin_key: str = Header(..., alias="X-ADMIN-KEY"),
) -> None:
    expected = os.getenv("PROMOTIONS_ADMIN_KEY") or os.getenv("ADMIN_API_KEY")
    if not expected or x_admin_key != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="UNAUTHORIZED")


@router.post("/sync/{merchant_id}", response_model=Dict[str, Any], status_code=status.HTTP_200_OK)
async def sync_shopify_products_endpoint(
    merchant_id: str = Path(..., description="Internal merchant ID"),
    limit: int = Query(default=500, ge=1, le=5000),
    _: None = Depends(require_shopify_products_admin),
) -> Dict[str, Any]:
    try:
        summary = await sync_shopify_products_for_merchant(
            merchant_id=merchant_id,
            limit=limit,
        )
        return {"ok": True, "summary": summary}
    except ShopifyProductsSyncConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "SHOPIFY_CONFIG_ERROR", "message": str(exc)},
        )
    except ShopifyProductsSyncAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "SHOPIFY_AUTH_ERROR", "message": str(exc)},
        )
    except ShopifyProductsSyncRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "SHOPIFY_RATE_LIMIT", "message": str(exc)},
        )
    except ShopifyProductsSyncError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "SHOPIFY_PRODUCTS_ERROR", "message": str(exc)},
        )
    except Exception as exc:  # pragma: no cover
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "INTERNAL_ERROR", "message": str(exc)},
        )


@router.get("/sync/{merchant_id}", response_model=Dict[str, Any], status_code=status.HTTP_200_OK)
async def sync_shopify_products_get(
    merchant_id: str = Path(..., description="Internal merchant ID"),
    limit: int = Query(default=500, ge=1, le=5000),
    _: None = Depends(require_shopify_products_admin),
) -> Dict[str, Any]:
    return await sync_shopify_products_endpoint(merchant_id=merchant_id, limit=limit)  # type: ignore[arg-type]

