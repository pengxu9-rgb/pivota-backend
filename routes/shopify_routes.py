"""
Shopify Integration Routes (Phase 2 MCP)
 - Connect merchant to Shopify (mark MCP connected)
 - Sync products (first N) from Shopify Admin API

Note: This is a minimal viable integration using PAT (Admin API access token).
In production prefer OAuth per merchant and encrypt credentials.
"""

from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel
from typing import Optional, Dict, Any
import httpx
import os
import logging

from db.merchant_onboarding import merchant_onboarding
from db.database import database
from config.settings import settings
from utils.auth import get_current_user

logger = logging.getLogger(__name__)

# 注意：产品同步已迁移到 /products/{merchant_id} API（实时代理）
# OAuth 已统一到 routes/merchant_store_connections.py（App A，只读）；
# 此路由仅保留 custom-token 连接。

router = APIRouter(prefix="/integrations/shopify", tags=["integrations-shopify"])


class ShopifyConnectRequest(BaseModel):
    merchant_id: str
    shop_domain: Optional[str] = None  # e.g. mystore.myshopify.com
    access_token: Optional[str] = None  # optional, fallback to env


@router.post("/connect-legacy")
async def connect_shopify_legacy(
    req: ShopifyConnectRequest,
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    DEPRECATED: Use /integrations/shopify/connect from merchant_store_connections.py instead.
    This endpoint is kept for backwards compatibility but should not be used.
    """
    # This legacy endpoint writes to merchant_onboarding.mcp_* fields and has historically been a source
    # of accidental credential overwrites. Restrict to employee/admin usage only.
    if current_user.get("role") not in ["employee", "admin", "super_admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    # Resolve credentials
    # Accept multiple env var names for flexibility
    shop_domain = (
        req.shop_domain
        or getattr(settings, "shopify_shop_domain", None)
        or os.getenv("SHOPIFY_STORE_URL")
        or os.getenv("SHOPIFY_SHOP_DOMAIN")
        or ""
    ).strip()
    access_token = (
        req.access_token
        or getattr(settings, "shopify_access_token", None)
        or os.getenv("SHOPIFY_ACCESS_TOKEN")
        or ""
    ).strip()
    if not shop_domain or not access_token:
        raise HTTPException(status_code=400, detail="Shopify credentials missing (shop_domain/access_token)")

    # Verify merchant exists
    m = await database.fetch_one(
        merchant_onboarding.select().where(merchant_onboarding.c.merchant_id == req.merchant_id)
    )
    if not m:
        raise HTTPException(status_code=404, detail="Merchant not found")

    # Validate credentials by calling /shop.json
    canon_domain = shop_domain.replace("https://", "").replace("http://", "").strip().strip("/").lower()
    url = f"https://{canon_domain}/admin/api/2025-10/shop.json"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, headers={"X-Shopify-Access-Token": access_token})
        if r.status_code != 200:
            # Never persist credentials if Shopify rejects them.
            # A common source of "token keeps becoming invalid" is accidentally overwriting the stored
            # Admin token with an invalid/expired token via legacy endpoints.
            raise HTTPException(status_code=400, detail=f"Invalid Shopify credentials (status={r.status_code})")
        shop = (r.json() or {}).get("shop") or {}
        canonical = str(shop.get("myshopify_domain") or "").strip().lower()
        if canonical and canonical != canon_domain:
            raise HTTPException(
                status_code=400,
                detail=f"Shopify token does not match shop domain (expected {canon_domain}, got {canonical})",
            )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Shopify validation error: {e}")

    # Mark MCP connected AND store credentials
    upd = (
        merchant_onboarding.update()
        .where(merchant_onboarding.c.merchant_id == req.merchant_id)
        .values(
            mcp_connected=True,
            mcp_platform="shopify",
            mcp_shop_domain=canon_domain,  # Store shop domain
            mcp_access_token=access_token  # Store access token
        )
    )
    await database.execute(upd)

    return {
        "status": "success",
        "message": "Shopify connected and credentials stored",
        "merchant_id": req.merchant_id,
        "mcp_connected": True,
        "platform": "shopify",
        "shop_domain": shop_domain
    }


# 注意：产品同步功能已迁移到 /products/{merchant_id} API
# 请使用 GET /products/{merchant_id} 实时获取产品（带缓存）

