"""
Admin API to sync Shopify price rules into the Pivota promotions table.

This is an internal helper for the agent/creator stack so that marketing
logic can reuse Shopify's discount system while keeping Pivota as the
source of truth for promotions.
"""

import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Path, Query, status
from pydantic import BaseModel, EmailStr

from services.shopify_discount_fixture_service import create_shopify_discount_validation_fixtures
from services.shopify_promotions_sync import (
    ShopifyPromotionsConfigError,
    ShopifyPromotionsAuthError,
    ShopifyPromotionsRateLimitError,
    ShopifyPromotionsError,
    _fetch_access_scopes_for_config,
    get_shopify_config_for_merchant,
    probe_shopify_discount_nodes_access_for_merchant,
    sync_shopify_promotions_for_merchant,
)

router = APIRouter(
    prefix="/agent/internal/shopify/promotions",
    tags=["shopify_promotions"],
)


class ShopifyDiscountFixtureCreateRequest(BaseModel):
    customer_email: EmailStr
    code_prefix: Optional[str] = None
    product_id: Optional[str] = None
    upcoming_starts_in_minutes: int = 2
    upcoming_duration_minutes: int = 20
    api_version: Optional[str] = "2026-04"


async def _sync_shopify_promotions_job(merchant_id: str) -> None:
    try:
        await sync_shopify_promotions_for_merchant(merchant_id=merchant_id)
    except Exception:  # pragma: no cover - background defensive logging
        # Don't surface to clients; just ensure it is logged for investigation.
        import logging

        logging.getLogger(__name__).exception(
            "Shopify promotions sync job failed",
            extra={"merchant_id": merchant_id},
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
    background_tasks: BackgroundTasks,
    merchant_id: str = Path(..., description="Internal merchant ID"),
    wait: bool = Query(
        False,
        description="If true, wait for sync completion (may time out on large stores).",
    ),
    _: None = Depends(require_shopify_promotions_admin),
) -> Dict[str, Any]:
    """
    Sync Shopify price rules for a merchant into the promotions table.

    This endpoint is admin-only and is intended to be called from internal
    tools or one-off scripts (not from public frontends).
    """
    try:
        if not wait:
            background_tasks.add_task(_sync_shopify_promotions_job, merchant_id)
            return {"ok": True, "scheduled": True}

        summary = await sync_shopify_promotions_for_merchant(merchant_id=merchant_id)
        return {"ok": True, "scheduled": False, "summary": summary}
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
    background_tasks: BackgroundTasks,
    merchant_id: str = Path(..., description="Internal merchant ID"),
    _: None = Depends(require_shopify_promotions_admin),
) -> Dict[str, Any]:
    """
    GET alias for sync endpoint to make it easy to trigger from a browser.
    """
    return await sync_shopify_promotions_endpoint(
        background_tasks=background_tasks,
        merchant_id=merchant_id,
    )


@router.get(
    "/preflight/{merchant_id}/discount-nodes",
    response_model=Dict[str, Any],
    status_code=status.HTTP_200_OK,
)
async def preflight_shopify_discount_nodes_access(
    merchant_id: str = Path(..., description="Internal merchant ID"),
    api_version: str = Query("2025-10", description="Shopify Admin API version to probe."),
    _: None = Depends(require_shopify_promotions_admin),
) -> Dict[str, Any]:
    """
    Read-only Admin GraphQL preflight for discount-node access.

    This intentionally does not call the promotion sync/upsert path. It exists
    so live fixture validation can distinguish a custom-app token/scope blocker
    from quote-time discount execution regressions.
    """
    try:
        probe = await probe_shopify_discount_nodes_access_for_merchant(
            merchant_id=merchant_id,
            api_version=api_version,
        )
        ok = probe.get("discountNodesAccess") == "ok" and bool(probe.get("hasReadDiscountsScope"))
        return {"ok": ok, "probe": probe}
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
    except ShopifyPromotionsError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "SHOPIFY_PREFLIGHT_ERROR", "message": str(exc)},
        )


@router.get(
    "/preflight/{merchant_id}/access-scopes",
    response_model=Dict[str, Any],
    status_code=status.HTTP_200_OK,
)
async def preflight_shopify_access_scopes(
    merchant_id: str = Path(..., description="Internal merchant ID"),
    _: None = Depends(require_shopify_promotions_admin),
) -> Dict[str, Any]:
    """
    Read-only preflight for the current merchant token's Shopify Admin access scopes.

    This complements discount-nodes preflight when the stored capability report is
    stale and we need the token's real-time scope set.
    """
    try:
        cfg = await get_shopify_config_for_merchant(merchant_id)
        scopes = await _fetch_access_scopes_for_config(cfg)
        scope_set = {str(scope).strip().lower() for scope in scopes if str(scope or "").strip()}
        return {
            "ok": True,
            "merchant_id": merchant_id,
            "shop_domain": cfg.shop_domain,
            "access_scopes": scopes,
            "has_read_discounts": "read_discounts" in scope_set,
            "has_write_discounts": "write_discounts" in scope_set,
            "has_read_customers": "read_customers" in scope_set,
        }
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
    except ShopifyPromotionsError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "SHOPIFY_PREFLIGHT_ERROR", "message": str(exc)},
        )


@router.post(
    "/fixtures/{merchant_id}",
    response_model=Dict[str, Any],
    status_code=status.HTTP_200_OK,
)
async def create_shopify_discount_fixtures(
    body: ShopifyDiscountFixtureCreateRequest,
    merchant_id: str = Path(..., description="Internal merchant ID"),
    _: None = Depends(require_shopify_promotions_admin),
) -> Dict[str, Any]:
    """
    Create a bounded set of Shopify-native discount fixtures for live validation.

    This route is admin-only and intentionally narrow: it creates only the
    specific test fixtures needed for discount audit coverage.
    """
    try:
        summary = await create_shopify_discount_validation_fixtures(
            merchant_id=merchant_id,
            customer_email=str(body.customer_email),
            code_prefix=body.code_prefix,
            product_id=body.product_id,
            upcoming_starts_in_minutes=body.upcoming_starts_in_minutes,
            upcoming_duration_minutes=body.upcoming_duration_minutes,
            api_version=body.api_version,
        )
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
    except ShopifyPromotionsError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "SHOPIFY_FIXTURE_ERROR", "message": str(exc)},
        )
