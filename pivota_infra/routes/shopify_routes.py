"""
Shopify Integration Routes (Phase 2 MCP)
 - Connect merchant to Shopify (mark MCP connected)
 - Sync products (first N) from Shopify Admin API

Note: This is a minimal viable integration using PAT (Admin API access token).
In production prefer OAuth per merchant and encrypt credentials.
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional, Dict, Any
import httpx
import os

from db.merchant_onboarding import merchant_onboarding
from db.database import database
from config.settings import settings

router = APIRouter(prefix="/integrations/shopify", tags=["integrations-shopify"])


class ShopifyConnectRequest(BaseModel):
    merchant_id: str
    shop_domain: Optional[str] = None  # e.g. mystore.myshopify.com
    access_token: Optional[str] = None  # optional, fallback to env


@router.post("/connect")
async def connect_shopify(req: ShopifyConnectRequest) -> Dict[str, Any]:
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
    url = f"https://{shop_domain}/admin/api/2024-07/shop.json"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, headers={"X-Shopify-Access-Token": access_token})
        if r.status_code not in (200, 401, 403):
            raise HTTPException(status_code=400, detail=f"Shopify validation failed (status={r.status_code})")
        if r.status_code in (401, 403):
            # Treat as recognized but insufficient permission
            pass
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Shopify validation error: {e}")

    # Mark MCP connected
    upd = (
        merchant_onboarding.update()
        .where(merchant_onboarding.c.merchant_id == req.merchant_id)
        .values(mcp_connected=True, mcp_platform="shopify")
    )
    await database.execute(upd)

    return {
        "status": "success",
        "message": "Shopify connected",
        "merchant_id": req.merchant_id,
        "mcp_connected": True,
        "platform": "shopify",
    }


class ShopifySyncRequest(BaseModel):
    merchant_id: str
    shop_domain: Optional[str] = None
    access_token: Optional[str] = None
    limit: int = 20


@router.post("/products/sync")
async def sync_products(req: ShopifySyncRequest) -> Dict[str, Any]:
    # Resolve credentials
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

    # Fetch products (no persistence yet, return to client for now)
    url = f"https://{shop_domain}/admin/api/2024-07/products.json?limit={max(1, min(req.limit, 50))}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, headers={"X-Shopify-Access-Token": access_token})
        if r.status_code == 200:
            data = r.json()
        elif r.status_code in (401, 403):
            # Return structure with hint
            data = {"hint": "insufficient permissions", "status": r.status_code, "body": r.text[:300]}
        else:
            raise HTTPException(status_code=400, detail=f"Shopify products failed (status={r.status_code})")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Shopify products error: {e}")

    return {
        "status": "success",
        "merchant_id": req.merchant_id,
        "platform": "shopify",
        "result": data,
    }


# -------- OAuth (minimal) --------
from urllib.parse import urlencode

@router.get("/oauth/start")
async def oauth_start(merchant_id: str, shop: str) -> Dict[str, Any]:
    client_id = os.getenv("SHOPIFY_CLIENT_ID") or getattr(settings, "shopify_client_id", None)
    redirect_uri = os.getenv("SHOPIFY_REDIRECT_URI") or getattr(settings, "shopify_redirect_uri", None)
    if not client_id or not redirect_uri:
        raise HTTPException(status_code=400, detail="Missing SHOPIFY_CLIENT_ID/SHOPIFY_REDIRECT_URI")
    scopes = os.getenv("SHOPIFY_SCOPES", "read_products,write_products,read_orders,write_orders,write_webhooks")
    params = {
        "client_id": client_id,
        "scope": scopes,
        "redirect_uri": redirect_uri,
        "state": merchant_id,
    }
    auth_url = f"https://{shop}/admin/oauth/authorize?{urlencode(params)}"
    return {"authorize": auth_url}


@router.get("/oauth/callback")
async def oauth_callback(request: Request) -> Dict[str, Any]:
    # Read query params
    shop = request.query_params.get("shop")
    code = request.query_params.get("code")
    state = request.query_params.get("state")  # merchant_id
    hmac_val = request.query_params.get("hmac")

    if not (shop and code and state and hmac_val):
        raise HTTPException(status_code=400, detail="Missing shop/code/state/hmac")

    # TODO: verify HMAC (requires app secret)
    client_id = os.getenv("SHOPIFY_CLIENT_ID") or getattr(settings, "shopify_client_id", None)
    client_secret = os.getenv("SHOPIFY_CLIENT_SECRET") or getattr(settings, "shopify_client_secret", None)
    redirect_uri = os.getenv("SHOPIFY_REDIRECT_URI") or getattr(settings, "shopify_redirect_uri", None)
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

    # Persist to merchant
    upd = (
        merchant_onboarding.update()
        .where(merchant_onboarding.c.merchant_id == state)
        .values(mcp_connected=True, mcp_platform="shopify", mcp_shop_domain=shop, mcp_access_token=access_token)
    )
    await database.execute(upd)

    return {"status": "success", "merchant_id": state, "shop": shop}


