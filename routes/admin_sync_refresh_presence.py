"""Admin route for the lightweight presence-refresh path (Stage 2a).

Decouples sync-hygiene from the full Path A ingest pipeline. The full
sync (POST /agent/internal/shopify/products/sync/{merchant_id}) is
necessary when content has changed but is too slow for the bootstrap
loop (codex hit proxy timeouts + SSH lifetime + nohup unreliability
trying to use it just to set last_full_sync_at).

This route fetches Shopify product IDs only, bumps last_seen_in_sync_at
+ sync_status='live' on matching catalog_products rows, and bumps
catalog_merchants.last_full_sync_at. Completes in seconds because no
taxonomy/SKU/offer writes happen.

Workflow:
  1. POST /admin/sync/refresh-presence/{merchant_id}
  2. python3 scripts/sweep_stale_catalog_products.py --merchant-id M
  3. (after eyeballing) --apply
"""

from __future__ import annotations

import os
from typing import Any, Dict

from fastapi import APIRouter, Depends, Header, HTTPException, Path, status

from services.shopify_products_sync import (
    ShopifyProductsSyncAuthError,
    ShopifyProductsSyncConfigError,
    ShopifyProductsSyncError,
    ShopifyProductsSyncRateLimitError,
    refresh_merchant_presence,
)


router = APIRouter(prefix="/admin/sync", tags=["Admin Sync Hygiene"])


async def require_sync_admin(
    x_admin_key: str = Header(..., alias="X-ADMIN-KEY"),
) -> None:
    """Same auth pattern as routes/shopify_products_sync_api.py — uses
    the existing admin key env var so operators don't need a new
    credential just for this endpoint."""
    expected = os.getenv("PROMOTIONS_ADMIN_KEY") or os.getenv("ADMIN_API_KEY")
    if not expected or x_admin_key != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="UNAUTHORIZED")


@router.post("/refresh-presence/{merchant_id}", response_model=Dict[str, Any])
async def refresh_presence_endpoint(
    merchant_id: str = Path(..., description="Internal merchant ID"),
    _: None = Depends(require_sync_admin),
) -> Dict[str, Any]:
    """Light-touch refresh of last_seen_in_sync_at + last_full_sync_at.

    Returns the summary dict from refresh_merchant_presence so operators
    can confirm the right number of products was fetched + the right
    number of rows touched."""
    try:
        summary = await refresh_merchant_presence(merchant_id=merchant_id)
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
