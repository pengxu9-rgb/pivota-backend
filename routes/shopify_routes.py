"""
Shopify Integration Routes (Phase 2 MCP)
 - Connect merchant to Shopify (mark MCP connected)
 - Sync products (first N) from Shopify Admin API

Note: This is a minimal viable integration using PAT (Admin API access token).
In production prefer OAuth per merchant and encrypt credentials.
"""

from services.merchant_store_service import get_merchant_active_stores, get_primary_store
from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
import httpx
import os
from datetime import datetime
import logging
import hmac
import hashlib
from urllib.parse import urlencode, quote, urlparse, parse_qsl

from db.merchant_onboarding import merchant_onboarding
from db.database import database
from config.settings import settings
from utils.auth import get_current_user

logger = logging.getLogger(__name__)

# 注意：产品同步已迁移到 /products/{merchant_id} API（实时代理）
# 此路由仅保留 Shopify 连接和 OAuth

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
    url = f"https://{canon_domain}/admin/api/2024-07/shop.json"
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


def _generate_state(merchant_id: str) -> str:
    """Generate HMAC-signed state token merchant_id:ts:signature"""
    ts = str(int(datetime.utcnow().timestamp()))
    secret = (settings.shopify_client_secret or os.getenv("SHOPIFY_CLIENT_SECRET") or "").encode()
    base = f"{merchant_id}:{ts}".encode()
    sig = hmac.new(secret, base, hashlib.sha256).hexdigest()
    return f"{merchant_id}:{ts}:{sig}"


def _verify_state(state: str, max_age_seconds: int = 600) -> Optional[str]:
    try:
        merchant_id, ts_str, sig = state.split(":", 2)
        secret = (settings.shopify_client_secret or os.getenv("SHOPIFY_CLIENT_SECRET") or "").encode()
        base = f"{merchant_id}:{ts_str}".encode()
        expected = hmac.new(secret, base, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        ts = int(ts_str)
        now = int(datetime.utcnow().timestamp())
        if now - ts > max_age_seconds:
            return None
        return merchant_id
    except Exception:
        return None


def _verify_shopify_hmac(query_string: str) -> bool:
    """
    Verify Shopify callback HMAC per docs.
    Expects raw query string; removes hmac param; sorts key=value; join with '&'; HMAC-SHA256 with client secret.
    """
    try:
        params = dict(parse_qsl(query_string, keep_blank_values=True))
        hmac_param = params.pop("hmac", None)
        if not hmac_param:
            return False
        # Build message
        message = "&".join(
            f"{k}={v}" for k, v in sorted(params.items(), key=lambda i: i[0])
        )
        secret = (settings.shopify_client_secret or os.getenv("SHOPIFY_CLIENT_SECRET") or "").encode()
        digest = hmac.new(secret, message.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(digest, hmac_param)
    except Exception:
        return False


@router.get("/oauth/start")
async def oauth_start(merchant_id: str, shop: str, current_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    # RBAC: allow merchant/employee/admin; if merchant, must match merchant_id
    if current_user["role"] not in ["merchant", "employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    if current_user["role"] == "merchant" and current_user.get("merchant_id") != merchant_id:
        raise HTTPException(status_code=403, detail="Can only initiate OAuth for your own merchant_id")

    client_id = settings.shopify_client_id or os.getenv("SHOPIFY_CLIENT_ID")
    redirect_uri = settings.shopify_redirect_uri or os.getenv("SHOPIFY_REDIRECT_URI")
    if not client_id or not redirect_uri:
        raise HTTPException(status_code=400, detail="Missing SHOPIFY_CLIENT_ID/SHOPIFY_REDIRECT_URI")
    scopes = settings.shopify_scopes or os.getenv("SHOPIFY_SCOPES", "read_products")

    state = _generate_state(merchant_id)
    params = {
        "client_id": client_id,
        "scope": scopes,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    auth_url = f"https://{shop}/admin/oauth/authorize?{urlencode(params)}"
    logger.info(f"shopify_oauth_start merchant_id={merchant_id} shop={shop} scopes={scopes}")
    return {"authorize": auth_url, "state": state}


@router.get("/oauth/callback")
async def oauth_callback(request: Request) -> Dict[str, Any]:
    # Read query params
    shop = request.query_params.get("shop")
    code = request.query_params.get("code")
    state = request.query_params.get("state")  # merchant_id
    hmac_val = request.query_params.get("hmac")

    if not (shop and code and state and hmac_val):
        raise HTTPException(status_code=400, detail="Missing shop/code/state/hmac")

    # Verify HMAC and state
    raw_qs = urlparse(str(request.url)).query
    if not _verify_shopify_hmac(raw_qs):
        raise HTTPException(status_code=400, detail="Invalid HMAC")

    merchant_id_from_state = _verify_state(state)
    if not merchant_id_from_state:
        raise HTTPException(status_code=400, detail="Invalid or expired state")

    client_id = settings.shopify_client_id or os.getenv("SHOPIFY_CLIENT_ID")
    client_secret = settings.shopify_client_secret or os.getenv("SHOPIFY_CLIENT_SECRET")
    redirect_uri = settings.shopify_redirect_uri or os.getenv("SHOPIFY_REDIRECT_URI")
    if not (client_id and client_secret and redirect_uri):
        raise HTTPException(status_code=400, detail="Missing client credentials")

    token_url = f"https://{shop}/admin/oauth/access_token"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(token_url, json={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
            })
        if r.status_code != 200:
            raise HTTPException(status_code=400, detail=f"Token exchange failed: {r.text[:200]}")
        access_token = r.json().get("access_token")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"OAuth error: {e}")

    access_token = str(access_token or "").strip()
    if not access_token:
        raise HTTPException(status_code=400, detail="OAuth token exchange returned empty access_token")

    # Validate token against Shopify before persisting (prevents storing bad/partial credentials).
    canon_domain = str(shop).replace("https://", "").replace("http://", "").strip().strip("/").lower()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            check = await client.get(
                f"https://{canon_domain}/admin/api/2024-07/shop.json",
                headers={"X-Shopify-Access-Token": access_token},
            )
        if check.status_code != 200:
            raise HTTPException(status_code=400, detail=f"OAuth token invalid (status={check.status_code})")
        shop_data = (check.json() or {}).get("shop") or {}
        canonical = str(shop_data.get("myshopify_domain") or "").strip().lower()
        if canonical and canonical != canon_domain:
            raise HTTPException(
                status_code=400,
                detail=f"OAuth token does not match shop domain (expected {canon_domain}, got {canonical})",
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"OAuth validation error: {e}")

    # Persist to merchant
    upd = (
        merchant_onboarding.update()
        .where(merchant_onboarding.c.merchant_id == merchant_id_from_state)
        .values(mcp_connected=True, mcp_platform="shopify", mcp_shop_domain=canon_domain, mcp_access_token=access_token)
    )
    await database.execute(upd)

    logger.info(f"shopify_oauth_success merchant_id={merchant_id_from_state} shop={shop}")
    return {"status": "success", "merchant_id": merchant_id_from_state, "shop": canon_domain}
